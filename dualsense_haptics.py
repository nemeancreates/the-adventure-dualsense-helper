"""
DualSense haptics helper for The Adventure (WoW addon).

What it does
  The addon paints four small squares in the top-left corner of the game when
  "DualSense helper" is on in its settings: red = weak motor level, green = strong
  motor level, blue = a fixed 170 (so this program knows the squares are the addon's).
  This program looks at the screen, reads them ~120 times a second, and plays the same
  feel through the controller's two haptic actuators (the same ones games use for
  texture and weather), over the controller's USB sound device.

Needs: Windows, the DualSense plugged in by USB (audio haptics do not work over
Bluetooth), the game in "Windowed (Fullscreen)" / borderless mode (a screen read can't
see exclusive fullscreen), Python with numpy and Pillow (both already installed here).
No downloads, no drivers.

Run:   python dualsense_haptics.py            (or double-click run_helper.bat)
       python dualsense_haptics.py --list     show sound devices
       python dualsense_haptics.py --test     play a short buzz to check the device
       python dualsense_haptics.py --gain 1.5 louder (default 1.0; clips at full)
       python dualsense_haptics.py --channels 3,4   which output channels are the actuators
Stop with Ctrl+C.
"""
import argparse
import collections
import ctypes
import datetime
import math
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
import wave
from ctypes import wintypes

import numpy as np

# APP_DIR: where its own files go (the log, your settings, recordings if you keep them) - beside the program when it
# is the built TheAdventureHelper.exe, else beside this script. BUNDLE_DIR: files packed inside the built program.
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
    BUNDLE_DIR = getattr(sys, "_MEIPASS", APP_DIR)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    BUNDLE_DIR = APP_DIR
LOG_FILE = os.path.join(APP_DIR, "helper_log.txt")


def log_line(text):
    """A line in helper_log.txt only (not on screen): what it saw and what it sent, for finding problems."""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3] + "  " + text + "\n")
    except OSError:
        pass


class _Tee:
    """Everything printed also goes into helper_log.txt."""

    def __init__(self, out):
        self.out = out

    def write(self, text):
        self.out.write(text)
        if text.strip():
            for line in text.rstrip("\n").split("\n"):
                log_line(line)

    def flush(self):
        self.out.flush()


os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

try:  # physical pixels on every monitor, so 6 px really is 6 px (per-monitor aware; older Windows: system aware)
    ctypes.windll.user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
    if not ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
        ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ---- reading a small part of the screen quickly, on any monitor ----------------------------------------------
class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


_u32, _gdi = ctypes.windll.user32, ctypes.windll.gdi32
_u32.GetDC.restype = ctypes.c_void_p
_u32.GetDC.argtypes = [ctypes.c_void_p]
_u32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_gdi.CreateCompatibleDC.restype = ctypes.c_void_p
_gdi.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
_gdi.CreateCompatibleBitmap.restype = ctypes.c_void_p
_gdi.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
_gdi.SelectObject.restype = ctypes.c_void_p
_gdi.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_gdi.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                        ctypes.c_int, ctypes.c_int, ctypes.c_uint32]
_gdi.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
                           ctypes.c_void_p, ctypes.c_uint]
_gdi.DeleteObject.argtypes = [ctypes.c_void_p]
_gdi.DeleteDC.argtypes = [ctypes.c_void_p]


def virtual_origin():
    """Top-left of the whole desktop (all monitors) in screen coordinates: where an all-screens grab starts."""
    return _u32.GetSystemMetrics(76), _u32.GetSystemMetrics(77)  # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN


def grab_region(left, top, right, bottom):
    """RGB pixels of one small screen rectangle (screen coordinates, any monitor), straight from the screen: a few
    milliseconds, where an all-screens screenshot cropped afterwards took about 87 ms."""
    w, h = int(right - left), int(bottom - top)
    hdc = _u32.GetDC(None)
    mdc = _gdi.CreateCompatibleDC(hdc)
    bmp = _gdi.CreateCompatibleBitmap(hdc, w, h)
    old = _gdi.SelectObject(mdc, bmp)
    try:
        _gdi.BitBlt(mdc, 0, 0, w, h, hdc, int(left), int(top), 0x00CC0020)  # SRCCOPY
        bi = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        if not _gdi.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0):
            raise OSError("GetDIBits failed")
        return np.frombuffer(buf.raw, dtype=np.uint8).reshape(h, w, 4)[:, :, 2::-1].copy()  # BGRA -> RGB
    finally:
        _gdi.SelectObject(mdc, old)
        _gdi.DeleteObject(bmp)
        _gdi.DeleteDC(mdc)
        _u32.ReleaseDC(None, hdc)

RATE = 48000
CHUNK = 480          # 10 ms of audio per buffer
NBUF = 4             # ~40 ms of audio in flight: the delay you can feel
NCH = 4
STRONG_HZ = 55.0     # the "heavy motor" carrier: low rumble
WEAK_HZ = 170.0      # the "light motor" carrier: buzz

# ----------------------------------------------------------------------------------------------- screen cells


MAGENTA = (255, 0, 255)
GREEN = (0, 255, 0)
TOL = 30  # the game draws the strip a little dim (pure 255 comes out about 244)


def _near(arr, rgb):
    d = np.abs(arr.astype(np.int16) - np.array(rgb, dtype=np.int16))
    return (d.max(axis=2) <= TOL)


def _box6(mask):
    """True where a whole 6x6 window starting at that pixel is set."""
    m = mask.astype(np.int32)
    H, W = m.shape
    S = np.zeros((H + 1, W + 1), dtype=np.int32)
    S[1:, 1:] = m.cumsum(0).cumsum(1)
    return (S[6:, 6:] - S[:-6, 6:] - S[6:, :-6] + S[:-6, :-6]) == 36


def find_cell(arr):
    """arr: HxWx3 uint8 (RGB). The addon paints four 6x6 squares side by side: two level squares, then a pure
    magenta one, then a pure green one. Returns (x, y) of the first square, or None. Pure function."""
    H, W = arr.shape[:2]
    if H < 6 or W < 24:
        return None
    a = _box6(_near(arr, MAGENTA))
    b = _box6(_near(arr, GREEN))
    both = a[:, :-6] & b[:, 6:]
    ys, xs = np.nonzero(both)
    for y, x in zip(ys.tolist(), xs.tolist()):
        if x >= 12:
            left = arr[y + 3, x - 9].astype(int)
            right = arr[y + 3, x - 3].astype(int)
            if abs(left[0] - right[0]) <= 3 and abs(left[1] - right[1]) <= 3:
                return x - 12, y
    return None


STRIP = 14  # squares in the addon's strip


def measure_strip(arr, x, y):
    """The strip's real size on this screen. find_cell() only finds a 6x6 patch inside the magenta and green squares;
    the squares themselves can be bigger (the game draws in its own units: on a 1440p screen a "6" came out about
    8 pixels wide, so reading every 6 pixels drifted onto the wrong squares from the sixth one on). Measures the
    magenta and green squares and returns (centre x of square 3, square width, centre y), all in pixels."""
    H, W = arr.shape[:2]
    xm, row = x + 12, y + 2  # inside the magenta square

    def is_(px, rgb):
        return int(np.abs(px.astype(int) - np.array(rgb)).max()) <= TOL

    def run(cx, rgb):
        left = right = cx
        while left > 0 and is_(arr[row, left - 1], rgb):
            left -= 1
        while right < W - 1 and is_(arr[row, right + 1], rgb):
            right += 1
        return left, right

    mL, mR = run(xm + 1, MAGENTA)
    gx = mR + 1
    while gx < W - 1 and not is_(arr[row, gx], GREEN):  # past a blended edge pixel or two
        gx += 1
    gL, gR = run(min(W - 1, gx + 1), GREEN)
    cm, cg = (mL + mR) / 2.0, (gL + gR) / 2.0
    w = cg - cm
    col = int(round(cm))
    top = bottom = row
    while top > 0 and is_(arr[top - 1, col], MAGENTA):
        top -= 1
    while bottom < H - 1 and is_(arr[bottom + 1, col], MAGENTA):
        bottom += 1
    return cm, max(4.0, w), (top + bottom) / 2.0


def locate_strip(arr):
    """Finds the addon's strip anywhere in a screenshot, whatever size the game drew it and even with soft edges:
    a solid patch of magenta, the same-sized green square right after it, and two matching level squares before it.
    Returns (centre x of the magenta square, square width, centre y) or None."""
    H, W = arr.shape[:2]
    m = _near(arr, MAGENTA)
    mm = m.astype(np.int32)
    S = np.zeros((H + 1, W + 1), dtype=np.int32)
    S[1:, 1:] = mm.cumsum(0).cumsum(1)
    win = (S[4:, 4:] - S[:-4, 4:] - S[4:, :-4] + S[:-4, :-4]) == 16  # a solid 4x4 patch of magenta
    ys, xs = np.nonzero(win)
    tried = set()
    for y, x in zip(ys.tolist()[:400], xs.tolist()[:400]):
        key = (y // 3, x // 3)
        if key in tried:
            continue
        tried.add(key)
        if x < 2 or y < 0:
            continue
        cm, w, yc = measure_strip(arr, x - 12, y)
        if not (4.0 <= w <= 40.0):
            continue
        yy = int(round(yc))
        # green right after, and the two level squares before match each other
        xg = int(round(cm + w))
        x1, x2 = int(round(cm - 2 * w)), int(round(cm - w))
        if not (0 <= x1 and xg < W and 0 <= yy < H):
            continue
        if int(np.abs(arr[yy, xg].astype(int) - np.array(GREEN)).max()) > TOL:
            continue
        a, b = arr[yy, x1].astype(int), arr[yy, x2].astype(int)
        if abs(a[0] - b[0]) <= 3 and abs(a[1] - b[1]) <= 3:
            return cm, w, yc
    return None


def canon_strip(arr, cm, w, yc):
    """The strip's ten squares, each read at its true centre, laid out as clean 6x6 squares (60x6): the format every
    reader below expects, whatever size the game drew them."""
    out = np.zeros((6, 6 * STRIP, 3), dtype=np.uint8)
    H, W = arr.shape[:2]
    yy = int(round(yc))
    for k in range(1, STRIP + 1):
        xx = int(round(cm + (k - 3) * w))
        if 0 <= xx < W and 0 <= yy < H:
            out[:, (k - 1) * 6:k * 6] = arr[yy, xx]
    return out


def normalize_strip(arr):
    """Something see-through and dark over the corner (another addon's window, a game fade) dims every square by the
    same amount: black stays black, pure colours drop (244 came out as 98). The magenta and green marker squares show
    by how much, so the whole strip is brightened back before it is read. Returns (strip, trustworthy)."""
    m = arr[3, 15].astype(float)
    g = arr[3, 21].astype(float)
    ok = (m[0] > 40 and m[2] > 40 and m[1] < 0.35 * min(m[0], m[2])
          and g[1] > 40 and g[0] < 0.35 * g[1] and g[2] < 0.35 * g[1]
          and abs(m[0] - m[2]) < 0.25 * max(m[0], m[2]))
    if not ok:
        return arr, False
    ref = (m[0] + m[2] + g[1]) / 3.0
    if ref >= 225:
        return arr, True  # (full brightness already)
    scale = min(4.0, 250.0 / ref)
    return np.clip(arr.astype(float) * scale, 0, 255).astype(np.uint8), True


def decode(arr, x, y):
    """(weak, strong, rain, wind) from the strip at x, y, or None if the markers aren't there.
    weak/strong are 0..2 (effects); rain and wind are 0..1 (the weather feel, carried in the blue of the two
    level squares; a blue of exactly 170 is the older addon's constant and means none)."""
    a = arr[y + 3, x + 3].astype(int)
    c = arr[y + 3, x + 9].astype(int)
    m1 = arr[y + 3, x + 15].astype(int)
    m2 = arr[y + 3, x + 21].astype(int)
    if np.abs(m1 - np.array(MAGENTA)).max() > TOL or np.abs(m2 - np.array(GREEN)).max() > TOL:
        return None
    if abs(a[0] - c[0]) > 3 or abs(a[1] - c[1]) > 3:
        return None  # caught mid-change
    def weather(b):
        return 0.0 if abs(int(b) - 170) <= 2 else int(b) / 255.0
    return a[0] / 255.0 * 2.0, a[1] / 255.0 * 2.0, weather(a[2]), weather(c[2])


def mic_flag(arr, x, y):
    """True while the addon's fifth square (at x+24..x+29) is yellow: the Talk to type key is held."""
    if arr.shape[1] < x + 30:
        return False
    m = arr[y + 3, x + 27].astype(int)
    return bool(m[0] > 200 and m[1] > 200 and m[2] < 90)


def _bits(arr, x, y, k):
    """Square k's red, green and blue as on/off (the exact things are sent that way)."""
    if arr.shape[1] < x + 6 * k:
        return None
    m = arr[y + 3, x + 6 * k - 3].astype(int)
    return int(m[0] > 128), int(m[1] > 128), int(m[2] > 128)


def wheel_request(arr, x, y):
    """Squares 9 and 13: the channel picked on the addon's wheel (1-5) and how many picks so far (0-7)."""
    a, b = _bits(arr, x, y, 9), _bits(arr, x, y, 13)
    if a is None or b is None:
        return None
    return a[0] + 2 * a[1] + 4 * a[2], b[0] + 2 * b[1] + 4 * b[2]


def health_level(arr, x, y):
    """The tenth square (x+54..x+59): your health 0..1, or None."""
    if arr.shape[1] < x + 60:
        return None
    m = arr[y + 3, x + 57].astype(int)
    if m[2] < 200:
        return None
    return m[0] / 255.0


def chan_flag(arr, x, y):
    """While chat is open: (channels you can send to - bits 1 party, 2 raid, 4 instance, 8 guild -, the one the chat
    box is on - 0 say .. 4 guild, 5 another). Squares 8, 11 and 12, all on/off. None if chat is closed."""
    a, b, c = _bits(arr, x, y, 8), _bits(arr, x, y, 11), _bits(arr, x, y, 12)
    if a is None or b is None or c is None or not a[0]:
        return None
    return a[1] + 2 * a[2] + 4 * b[0] + 8 * b[1], min(5, c[0] + 2 * c[1] + 4 * c[2])


def fish_flag(arr, x, y):
    """The addon's seventh square (x+36..x+41): (hook mode 0-3, trigger feel, line in the water). Full red = your
    line is in the water; half red = a fishing pole in your hands, ready to cast. None otherwise."""
    a, b = _bits(arr, x, y, 7), _bits(arr, x, y, 14)
    if a is None or b is None or not (a[0] or a[1]):
        return None
    return b[0] + 2 * b[1], bool(a[2]), bool(a[0])


def menu_flag(arr, x, y):
    """Square 14's blue: a menu is open in the game (ours, the map, bags, a trade...) - the touchpad is a mouse."""
    b = _bits(arr, x, y, 14)
    return bool(b and b[2])


def map_flag(arr, x, y):
    """Square 11's blue: the world map is open (a touchpad click on its left / right side zooms out / in)."""
    b = _bits(arr, x, y, 11)
    return bool(b and b[2])


def chat_flag(arr, x, y):
    """True while the addon's sixth square (x+30..x+35) is cyan: the game's chat box is open for typing."""
    if arr.shape[1] < x + 36:
        return False
    m = arr[y + 3, x + 33].astype(int)
    return bool(m[0] < 90 and m[1] > 200 and m[2] > 200)


# ----------------------------------------------------------------------------------------------- winmm audio

winmm = ctypes.windll.winmm
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
WHDR_DONE = 0x1
WHDR_PREPARED = 0x2
CALLBACK_NULL = 0


class WAVEFORMATEXTENSIBLE(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD), ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD), ("nBlockAlign", wintypes.WORD),
                ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD),
                ("wValidBitsPerSample", wintypes.WORD), ("dwChannelMask", wintypes.DWORD),
                ("SubFormat", ctypes.c_ubyte * 16)]


class WAVEHDR(ctypes.Structure):
    _fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", wintypes.DWORD),
                ("dwBytesRecorded", wintypes.DWORD), ("dwUser", ctypes.c_size_t), ("dwFlags", wintypes.DWORD),
                ("dwLoops", wintypes.DWORD), ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_size_t)]


class WAVEOUTCAPSW(ctypes.Structure):
    _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD), ("vDriverVersion", wintypes.UINT),
                ("szPname", ctypes.c_wchar * 32), ("dwFormats", wintypes.DWORD), ("wChannels", wintypes.WORD),
                ("wReserved1", wintypes.WORD), ("dwSupport", wintypes.DWORD)]


winmm.waveOutOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), wintypes.UINT, ctypes.c_void_p, ctypes.c_size_t,
                              ctypes.c_size_t, wintypes.DWORD]
winmm.waveOutPrepareHeader.argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR), wintypes.UINT]
winmm.waveOutUnprepareHeader.argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR), wintypes.UINT]
winmm.waveOutWrite.argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR), wintypes.UINT]
winmm.waveOutReset.argtypes = [ctypes.c_void_p]
winmm.waveOutClose.argtypes = [ctypes.c_void_p]
winmm.waveOutGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.POINTER(WAVEOUTCAPSW), wintypes.UINT]


def devices():
    out = []
    for i in range(winmm.waveOutGetNumDevs()):
        caps = WAVEOUTCAPSW()
        if winmm.waveOutGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            out.append((i, caps.szPname, caps.wChannels))
    return out


def find_device(name_part):
    for i, name, ch in devices():
        if name_part.lower() in name.lower():
            return i, name
    return None


# ----------------------------------------------------------------------------------------------- listening (opt-in)
# --listen: the helper also listens to what your PC is playing (Windows' own "loopback" copy of the sound going to
# your default speakers or headset; nothing is recorded from a microphone) and judges whether it hears rain/ice
# (steady broadband hiss) or wind/dust (steady low rumble), and how hard. It is only used when the addon's weather
# is set to Nothing. Every few seconds it writes what it heard to sound_log/ (numbers only), and every so often it
# saves a short clip there too, named with the weather you had set, so a storm's real loudness can be learned.
# The clips contain whatever was playing at the time (including voice chat). They stay on this PC; delete them freely.


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


def make_guid(text):
    u = uuid.UUID(text)
    g = GUID()
    g.Data1, g.Data2, g.Data3 = u.time_low, u.time_mid, u.time_hi_version
    g.Data4[:] = bytes([u.clock_seq_hi_variant, u.clock_seq_low]) + u.node.to_bytes(6, "big")
    return g


CLSID_ENUM = make_guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_ENUM = make_guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_CLIENT = make_guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")
IID_CAPTURE = make_guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317")
ole32 = ctypes.windll.ole32
ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(GUID),
                                   ctypes.POINTER(ctypes.c_void_p)]


def com_call(obj, index, argtypes, *args):
    """Call method number `index` of a COM object (read straight from its function table)."""
    vtbl = ctypes.cast(ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])
    hr = fn(obj, *args)
    if hr < 0:
        raise OSError(f"audio call {index} failed ({hr & 0xFFFFFFFF:#010x})")
    return hr


class Analyzer:
    """Turns audio into a weather judgement. Pure (no devices), so it can be tested with made-up sound.
    Every 4096 samples it measures the energy in three bands (low 50-400 Hz, mid 400-2000, high 2000-8000), how
    noise-like the high band is, and over the last 3 seconds how steady each is. Rain/ice = steady, noise-like highs;
    wind/dust = steady, low-heavy rumble. The result glides up in about 3 s and down in about 10 s."""

    FRAME = 4096

    def __init__(self, sens=1.0):
        self.sens = sens
        self.recent = collections.deque(maxlen=36)
        self.rain = 0.0
        self.wind = 0.0
        self.raw_rain = 0.0
        self.raw_wind = 0.0
        self.stats = {}
        self.patter = 0.0       # 0..1: how much it sounds like rain pattering on a roof from inside
        self.indoors_heard = False
        self.buf = np.zeros(0, dtype=np.float32)
        self.win = np.hanning(self.FRAME).astype(np.float32)

    def push(self, mono, fs):
        self.buf = np.concatenate([self.buf, mono.astype(np.float32)])
        while len(self.buf) >= self.FRAME:
            frame, self.buf = self.buf[:self.FRAME], self.buf[self.FRAME:]
            self._frame(frame, fs)

    def _frame(self, x, fs):
        spec = np.abs(np.fft.rfft(x * self.win)) / self.FRAME
        p = spec ** 2
        f = np.fft.rfftfreq(self.FRAME, 1.0 / fs)
        hi_top = min(8000.0, fs / 2 - 100)

        def band(lo, hi):
            m = (f >= lo) & (f < hi)
            return float(np.sqrt(p[m].sum())) if m.any() else 0.0

        low, mid, high = band(50, 400), band(400, 2000), band(2000, hi_top)
        mh = (f >= 2000) & (f < hi_top)
        ph = p[mh] + 1e-12
        flat = float(np.exp(np.mean(np.log(ph))) / np.mean(ph)) if mh.any() else 0.0
        ml = (f >= 50) & (f < 400)
        pl = p[ml] + 1e-12
        flat_low = float(np.exp(np.mean(np.log(pl))) / np.mean(pl)) if ml.any() else 0.0
        total = float(np.sqrt(p.sum()))
        self.recent.append((low, mid, high, flat, total, flat_low))
        r = np.array(self.recent)
        if len(r) < 12:
            return
        lowv, midv, highv, flatv = r[:, 0], r[:, 1], r[:, 2], r[:, 3]
        cv_h = float(highv.std() / (highv.mean() + 1e-12))
        cv_l = float(lowv.std() / (lowv.mean() + 1e-12))
        high_db = 20 * math.log10(highv.mean() + 1e-9)
        low_db = 20 * math.log10(lowv.mean() + 1e-9)
        frac_low = float((lowv ** 2).mean() / ((lowv ** 2).mean() + (midv ** 2).mean() + (highv ** 2).mean() + 1e-12))
        flat_m = float(flatv.mean())
        flat_l = float(r[:, 5].mean())
        raw_rain = raw_wind = 0.0
        if flat_m > 0.35 and cv_h < 0.35 and high_db > -42:
            raw_rain = min(1.0, max(0.0, (high_db + 42) / 24.0)) * self.sens
        # wind is noise-like as well as low and steady: a steady bass note or hum (music, engines) is tonal and is ignored
        if frac_low > 0.55 and cv_l < 0.4 and low_db > -45 and flat_l > 0.25:
            raw_wind = min(1.0, max(0.0, (low_db + 45) / 25.0)) * self.sens
        self.raw_rain, self.raw_wind = min(1.0, raw_rain), min(1.0, raw_wind)
        # Inside, rain is a sparse pitter-patter on the roof: quiet, uneven, made of separate taps, not a steady hiss.
        # So: the high band is low-ish, patchy (varies a lot) and has clear peaks over the last second
        # (a first guess; the log's patter column is what to tune it from)
        recent_h = highv[-12:]
        peaky = float(recent_h.max() / (recent_h.mean() + 1e-12))
        raw_patter = 1.0 if (peaky > 2.0 and cv_h > 0.45 and -78 < high_db < -42) else 0.0
        self.patter += (raw_patter - self.patter) * (0.12 if raw_patter > self.patter else 0.04)
        if self.patter > 0.6:
            self.indoors_heard = True
        elif self.patter < 0.3:
            self.indoors_heard = False
        for name in ("rain", "wind"):
            raw, cur = getattr(self, "raw_" + name), getattr(self, name)
            setattr(self, name, cur + (raw - cur) * (0.03 if raw > cur else 0.008))
        self.stats = dict(total_db=20 * math.log10(total + 1e-9), low_db=low_db,
                          mid_db=20 * math.log10(midv.mean() + 1e-9), high_db=high_db, flat=flat_m, cv_high=cv_h,
                          cv_low=cv_l, frac_low=frac_low, flat_low=flat_l, peaky=peaky,
                          patter=self.patter)


class Loopback(threading.Thread):
    """Reads the sound going to the default output device (WASAPI loopback) and feeds an Analyzer."""

    def __init__(self, analyzer, on_audio=None):
        super().__init__(daemon=True)
        self.an = analyzer
        self.on_audio = on_audio
        self.running = True
        self.error = None
        self.ready = threading.Event()
        self.fs = 48000

    def run(self):
        try:
            ole32.CoInitializeEx(None, 0)
            enum = ctypes.c_void_p()
            hr = ole32.CoCreateInstance(ctypes.byref(CLSID_ENUM), None, 23, ctypes.byref(IID_ENUM), ctypes.byref(enum))
            if hr < 0:
                raise OSError("no audio device enumerator")
            dev = ctypes.c_void_p()
            com_call(enum, 4, [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)], 0, 0, ctypes.byref(dev))
            client = ctypes.c_void_p()
            com_call(dev, 3, [ctypes.POINTER(GUID), wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                     ctypes.byref(IID_CLIENT), 23, None, ctypes.byref(client))
            pfmt = ctypes.c_void_p()
            com_call(client, 8, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(pfmt))
            fmt = WAVEFORMATEXTENSIBLE.from_address(pfmt.value)
            ch, fs, bits, tag = int(fmt.nChannels), int(fmt.nSamplesPerSec), int(fmt.wBitsPerSample), int(fmt.wFormatTag)
            is_float = tag == 3 or (tag == WAVE_FORMAT_EXTENSIBLE and bytes(fmt.SubFormat)[:2] == b"\x03\x00")
            self.fs = fs
            com_call(client, 3, [ctypes.c_int, wintypes.DWORD, ctypes.c_int64, ctypes.c_int64, ctypes.c_void_p,
                                 ctypes.c_void_p], 0, 0x00020000, 10_000_000, 0, pfmt, None)
            cap = ctypes.c_void_p()
            com_call(client, 14, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(IID_CAPTURE),
                     ctypes.byref(cap))
            com_call(client, 10, [])
            self.ready.set()
            block = ch * (bits // 8)
            while self.running:
                n = ctypes.c_uint32()
                com_call(cap, 5, [ctypes.POINTER(ctypes.c_uint32)], ctypes.byref(n))
                if n.value == 0:
                    time.sleep(0.01)
                    continue
                while n.value > 0 and self.running:
                    data = ctypes.c_void_p()
                    frames = ctypes.c_uint32()
                    flags = wintypes.DWORD()
                    pos, qpc = ctypes.c_uint64(), ctypes.c_uint64()
                    com_call(cap, 3, [ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32),
                                      ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_uint64),
                                      ctypes.POINTER(ctypes.c_uint64)], ctypes.byref(data), ctypes.byref(frames),
                             ctypes.byref(flags), ctypes.byref(pos), ctypes.byref(qpc))
                    if flags.value & 0x2 or not data.value:
                        mono = np.zeros(frames.value, dtype=np.float32)
                    else:
                        raw = ctypes.string_at(data.value, frames.value * block)
                        if is_float and bits == 32:
                            a = np.frombuffer(raw, dtype=np.float32)
                        elif bits == 16:
                            a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                        else:
                            a = np.zeros(frames.value * ch, dtype=np.float32)
                        mono = a.reshape(-1, ch).mean(axis=1)
                    com_call(cap, 4, [ctypes.c_uint32], frames.value)
                    self.an.push(mono, fs)
                    if self.on_audio:
                        self.on_audio(mono, fs)
                    com_call(cap, 5, [ctypes.POINTER(ctypes.c_uint32)], ctypes.byref(n))
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            self.ready.set()


MODES = {"off": (0.0, 0.0), "drizzle": (0.3, 0.0), "rain": (0.6, 0.0), "downpour": (1.0, 0.25),
         "storm": (0.85, 0.5), "windy": (0.0, 0.7)}


def mode_name(rain, wind):
    best, best_d = "off", 9.0
    for name, (r, w) in MODES.items():
        d = abs(r - rain) + abs(w - wind)
        if d < best_d:
            best, best_d = name, d
    return best


class SoundLog:
    """Writes what the listener hears to sound_log/: a numbers file every few seconds, and short clips now and then
    (more often while it sounds stormy), keeping only the newest 80 clips."""

    def __init__(self, folder, clip_every=180.0, clips=True):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self.clips = clips
        self.clip_every = clip_every
        self.ring = collections.deque()
        self.ring_len = 0
        self.fs = 48000
        self.last_row = 0.0
        self.last_clip = time.time()
        self.csv = os.path.join(folder, "heard-" + datetime.date.today().isoformat() + ".csv")
        if not os.path.exists(self.csv):
            with open(self.csv, "w", encoding="utf-8") as f:
                f.write("time,addon_weather,total_db,low_db,mid_db,high_db,flatness,cv_high,cv_low,low_share,flat_low,"
                        "peaky,patter,indoors_heard,"
                        "heard_rain,heard_wind,used_rain,used_wind\n")

    def feed(self, mono, fs):
        self.fs = fs
        self.ring.append(mono)
        self.ring_len += len(mono)
        while self.ring_len - len(self.ring[0]) > fs * 3:
            self.ring_len -= len(self.ring.popleft())

    def tick(self, an, manual_mode, used_rain, used_wind):
        now = time.time()
        st = an.stats
        if st and now - self.last_row >= 5:
            self.last_row = now
            with open(self.csv, "a", encoding="utf-8") as f:
                f.write(("%s,%s," + ",".join(["%.1f"] * 4 + ["%.3f"] * 9 + ["%.3f", "%.3f", "%d"]) + chr(10)) % (
                    datetime.datetime.now().strftime("%H:%M:%S"), manual_mode, st["total_db"], st["low_db"],
                    st["mid_db"], st["high_db"], st["flat"], st["cv_high"], st["cv_low"], st["frac_low"],
                    st["flat_low"], an.rain, an.wind, used_rain, used_wind, st["peaky"], st["patter"],
                    1 if an.indoors_heard else 0))
        stormy = max(an.rain, an.wind, used_rain, used_wind) > 0.15
        every = min(30.0, self.clip_every) if stormy else self.clip_every
        if self.clips and self.ring_len >= self.fs * 2 and now - self.last_clip >= every:
            self.last_clip = now
            x = np.concatenate(list(self.ring))
            x = x[: int(self.fs * 3)]
            x = x[: len(x) // 2 * 2].reshape(-1, 2).mean(axis=1)  # half the rate: smaller files
            pcm = (np.clip(x, -1, 1) * 32000).astype(np.int16)
            name = "clip-%s-%s-heard-r%.2f-w%.2f.wav" % (datetime.datetime.now().strftime("%Y%m%d-%H%M%S"),
                                                         manual_mode, an.rain, an.wind)
            with wave.open(os.path.join(self.folder, name), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(self.fs // 2)
                w.writeframes(pcm.tobytes())
            clips = sorted(f for f in os.listdir(self.folder) if f.startswith("clip-"))
            for old in clips[:-80]:
                try:
                    os.remove(os.path.join(self.folder, old))
                except OSError:
                    pass


# ----------------------------------------------------------------------------------------------- talk to type (opt-in)
# --mic: while you hold the addon's "Talk to type" key (or the touchpad zone set to it), the helper listens through
# Windows' own offline speech recogniser (mic_listener.ps1) and, when you let go, puts what you said into the game's
# open chat box (copy to the clipboard, then Ctrl+V, only if the game is the window in front). You still press Send.
# Nothing leaves your PC. The microphone used is Windows' default recording device (Settings > Sound > Input).
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.OpenClipboard.argtypes = [ctypes.c_void_p]
user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]


def set_clipboard(text):
    data = (text + "\0").encode("utf-16-le")
    h = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.03)
    else:
        return False
    user32.EmptyClipboard()
    user32.SetClipboardData(13, h)  # CF_UNICODETEXT
    user32.CloseClipboard()
    return True


def steam_running():
    """Is Steam running? (its PlayStation controller support takes the DualSense over)"""
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe", "/NH"], capture_output=True, text=True,
                             timeout=5, creationflags=0x08000000).stdout
        return "steam.exe" in out.lower()
    except Exception:  # noqa: BLE001
        return False


def game_in_front():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return "warcraft" in buf.value.lower()


def press_ctrl_v():
    tap(0x56, (0x11,), "Ctrl+V (paste)")


def tidy(text):
    text = " ".join(text.split())
    return text[:1].upper() + text[1:] if text else text


class Mic:
    """Runs mic_listener.ps1 (Windows PowerShell + the offline recogniser) and collects what it hears."""

    early = False

    def cancel(self):
        pass

    def __init__(self, wave=None):
        script = os.path.join(BUNDLE_DIR, "mic_listener.ps1")
        ps = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                          "powershell.exe")
        args = [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script]
        if wave:
            args += ["-Wave", wave]
        self.p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, bufsize=1, creationflags=0x08000000)  # no console window
        self.q = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.ready = False
        self.error = None

    def _read(self):
        for line in self.p.stdout:
            self.q.put(line.rstrip("\r\n"))
        self.q.put(None)

    def wait_ready(self, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            try:
                line = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                self.error = "the speech listener stopped"
                return False
            if line == "READY":
                self.ready = True
                return True
            if line.startswith("ERROR"):
                self.error = line.split("\t", 1)[-1]
                return False
        self.error = "the speech listener did not start"
        return False

    def _send(self, word):
        try:
            self.p.stdin.write(word + "\n")
            self.p.stdin.flush()
        except OSError:
            pass

    def start(self):
        while not self.q.empty():
            self.q.get_nowait()
        self._send("START")

    def stop(self, timeout=4.0):
        """Finish listening and return everything heard since start()."""
        self._send("STOP")
        heard, end = [], time.time() + timeout
        while time.time() < end:
            try:
                line = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None or line == "DONE":
                break
            if line.startswith("TEXT\t"):
                heard.append(line.split("\t", 1)[1])
        return tidy(" ".join(heard))

    def close(self):
        self._send("QUIT")
        try:
            self.p.wait(timeout=2)
        except Exception:  # noqa: BLE001
            self.p.kill()


# ----------------------------------------------------------------------------------------------- Whisper (talk to type)
# The better speech engine: OpenAI's Whisper model run locally by faster-whisper (pip), using the small.en model kept
# in the Hugging Face cache on this PC. It is loaded from disk only (local_files_only), so nothing is sent anywhere.
# The microphone is recorded by the helper itself, only while you hold the button / the Talk to type key is on, and
# the recording is kept in memory just long enough to turn it into text.

PKEY_FRIENDLY_NAME_FMT = make_guid("A45C254E-DF1C-4EFD-8020-67D146A850E0")


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("p", ctypes.c_void_p), ("p2", ctypes.c_void_p)]


def _enumerator():
    enum = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(CLSID_ENUM), None, 23, ctypes.byref(IID_ENUM), ctypes.byref(enum))
    if hr < 0:
        raise OSError("no audio device enumerator")
    return enum


def capture_devices(enum):
    """[(name, IMMDevice)] for every active recording device."""
    coll = ctypes.c_void_p()
    com_call(enum, 3, [ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)], 1, 1, ctypes.byref(coll))
    count = ctypes.c_uint()
    com_call(coll, 3, [ctypes.POINTER(ctypes.c_uint)], ctypes.byref(count))
    key = PROPERTYKEY()
    key.fmtid, key.pid = PKEY_FRIENDLY_NAME_FMT, 14
    out = []
    for i in range(count.value):
        dev = ctypes.c_void_p()
        com_call(coll, 4, [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)], i, ctypes.byref(dev))
        name = "?"
        try:
            store = ctypes.c_void_p()
            com_call(dev, 4, [wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)], 0, ctypes.byref(store))
            pv = PROPVARIANT()
            com_call(store, 5, [ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT)], ctypes.byref(key),
                     ctypes.byref(pv))
            if pv.vt == 31 and pv.p:  # VT_LPWSTR
                name = ctypes.wstring_at(pv.p)
            ole32.PropVariantClear(ctypes.byref(pv))
        except OSError:
            pass
        out.append((name, dev))
    return out


class MicCapture(threading.Thread):
    """Records one microphone (Windows' default recording device, or one picked by name) until finish()."""

    def __init__(self, device_name=None, strict=False):
        super().__init__(daemon=True)
        self.device_name = device_name
        self.strict = strict  # only the named microphone - never Windows' default instead
        self.chunks = []
        self.fs = 16000
        self.error = None
        self.used = None
        self._halt = threading.Event()
        self.ready = threading.Event()

    def run(self):
        try:
            ole32.CoInitializeEx(None, 0)
            enum = _enumerator()
            dev = None
            if self.device_name:
                for name, d in capture_devices(enum):
                    if self.device_name.lower() in name.lower():
                        dev, self.used = d, name
                        break
            if dev is None and self.strict and self.device_name:
                raise RuntimeError(f"no microphone named like '{self.device_name}' (controller unplugged, or on Bluetooth?)")
            if dev is None:
                dev = ctypes.c_void_p()
                com_call(enum, 4, [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)], 1, 0,
                         ctypes.byref(dev))
                self.used = self.used or "Windows default recording device"
            client = ctypes.c_void_p()
            com_call(dev, 3, [ctypes.POINTER(GUID), wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                     ctypes.byref(IID_CLIENT), 23, None, ctypes.byref(client))
            pfmt = ctypes.c_void_p()
            com_call(client, 8, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(pfmt))
            fmt = WAVEFORMATEXTENSIBLE.from_address(pfmt.value)
            ch, fs, bits, tag = int(fmt.nChannels), int(fmt.nSamplesPerSec), int(fmt.wBitsPerSample), int(fmt.wFormatTag)
            is_float = tag == 3 or (tag == WAVE_FORMAT_EXTENSIBLE and bytes(fmt.SubFormat)[:2] == b"\x03\x00")
            self.fs = fs
            com_call(client, 3, [ctypes.c_int, wintypes.DWORD, ctypes.c_int64, ctypes.c_int64, ctypes.c_void_p,
                                 ctypes.c_void_p], 0, 0, 2_000_000, 0, pfmt, None)
            cap = ctypes.c_void_p()
            com_call(client, 14, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(IID_CAPTURE),
                     ctypes.byref(cap))
            com_call(client, 10, [])
            self.ready.set()
            block = ch * (bits // 8)
            while not self._halt.is_set():
                n = ctypes.c_uint32()
                com_call(cap, 5, [ctypes.POINTER(ctypes.c_uint32)], ctypes.byref(n))
                if n.value == 0:
                    time.sleep(0.01)
                    continue
                while n.value > 0:
                    data, frames, flags = ctypes.c_void_p(), ctypes.c_uint32(), wintypes.DWORD()
                    pos, qpc = ctypes.c_uint64(), ctypes.c_uint64()
                    com_call(cap, 3, [ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32),
                                      ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_uint64),
                                      ctypes.POINTER(ctypes.c_uint64)], ctypes.byref(data), ctypes.byref(frames),
                             ctypes.byref(flags), ctypes.byref(pos), ctypes.byref(qpc))
                    if flags.value & 0x2 or not data.value:
                        mono = np.zeros(frames.value, dtype=np.float32)
                    else:
                        raw = ctypes.string_at(data.value, frames.value * block)
                        if is_float and bits == 32:
                            x = np.frombuffer(raw, dtype=np.float32)
                        elif bits == 16:
                            x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                        else:
                            x = np.zeros(frames.value * ch, dtype=np.float32)
                        mono = x.reshape(-1, ch).mean(axis=1)
                    self.chunks.append(mono.copy())
                    com_call(cap, 4, [ctypes.c_uint32], frames.value)
                    com_call(cap, 5, [ctypes.POINTER(ctypes.c_uint32)], ctypes.byref(n))
            com_call(client, 11, [])
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            self.ready.set()

    def finish(self):
        self._halt.set()
        self.join(timeout=1.5)
        audio = np.concatenate(self.chunks) if self.chunks else np.zeros(0, dtype=np.float32)
        return audio, self.fs


def list_mics():
    ole32.CoInitializeEx(None, 0)
    return [name for name, _ in capture_devices(_enumerator())]


def to_16k(x, fs):
    if len(x) == 0 or fs == 16000:
        return x.astype(np.float32)
    if fs % 16000 == 0:
        k = fs // 16000
        x = x[: len(x) // k * k]
        return x.reshape(-1, k).mean(axis=1).astype(np.float32)
    t = np.arange(0, len(x) / fs, 1 / 16000)
    return np.interp(t, np.arange(len(x)) / fs, x).astype(np.float32)


# Words that help it spell game things right; it is only a hint
WOW_HINT = ("World of Warcraft chat. Stormwind, Orgrimmar, Ironforge, Darnassus, Undercity, Thunder Bluff, Elwynn, "
            "Westfall, Barrens, quest, dungeon, raid, guild, party, healer, tank, DPS, LFG, mount, inn.")
NOISE_WORDS = {"you", "thank you", "thanks for watching", "bye", "thank you for watching"}


class WhisperMic:
    """Talk to type with Whisper. start() begins recording at once (so the first words are kept even while the
    button is still deciding between a click and a hold), cancel() throws it away, stop() turns it into text."""

    early = True

    def __init__(self, model="small.en", device_name=None, threads=4, keep_dir=None, fallback=False):
        self.keep_dir = keep_dir
        self.fallback = fallback  # may another microphone stand in when the chosen one sends nothing?
        self.why = ""
        self.model = None
        self.error = None
        self.device_name = device_name
        self.cap = None
        self.name = model
        self.threads = threads
        self._loader = threading.Thread(target=self._load, daemon=True)
        self._loader.start()

    def _load(self):
        try:
            from faster_whisper import WhisperModel
            self.model = WhisperModel(self.name, device="cpu", compute_type="int8", cpu_threads=self.threads,
                                      local_files_only=True)
        except Exception as e:  # noqa: BLE001
            self.error = f"could not load the {self.name} speech model ({e})"
            return
        self.pick_working_mic()

    def pick_working_mic(self):
        """Listens to each microphone for half a second and keeps the first that actually sends sound (the one you
        chose or Windows' default first). A microphone can be "there" yet send nothing at all - the DualSense's did."""
        if not self.fallback:
            return self.check_mic()
        try:
            names = [self.device_name] if self.device_name else []
            names += [None] + [n for n in list_mics() if n not in names]
            for name in names:
                cap = MicCapture(name)
                cap.start()
                cap.ready.wait(3)
                time.sleep(0.5)
                audio, fs = cap.finish()
                if len(audio) > fs * 0.2:
                    if name != self.device_name:
                        print(f"Talk to type uses: {cap.used}" + ("" if name is None else
                              "  (the default microphone sends no sound)"))
                    self.device_name = name
                    return True
                log_line(f"microphone sends no sound: {cap.used}")
            print("No microphone is sending any sound - check it isn't muted (the PS5 mic button's light, or Windows).")
        except Exception as e:  # noqa: BLE001
            log_line(f"microphone check failed: {e!r}")
        return False

    def check_mic(self):
        """Listens to the chosen microphone (the controller's) for half a second and says whether it sends sound."""
        try:
            cap = MicCapture(self.device_name, strict=True)
            cap.start()
            cap.ready.wait(3)
            time.sleep(0.5)
            audio, fs = cap.finish()
            if cap.error:
                print("Talk to type: " + str(cap.error))
                return False
            if len(audio) > fs * 0.2:
                log_line(f"microphone OK: {cap.used}")
                return True
            print(f"Talk to type: {cap.used} sends no sound at all. Turn the controller fully off "
                  "(unplug it, hold the PS button about 10 s until every light is off), then plug it back in and press "
                  "PS - it is checked again each time you hold the mic button.")
        except Exception as e:  # noqa: BLE001
            log_line(f"microphone check failed: {e!r}")
        return False

    def wait_ready(self, timeout=120):
        self._loader.join(timeout)
        return self.model is not None

    def start(self):
        if self.cap is None:
            self.cap = MicCapture(self.device_name, strict=not self.fallback)
            self.cap.start()
            self.cap.ready.wait(2)
            if self.cap.error:
                print("Could not open the microphone:", self.cap.error)

    def cancel(self):
        if self.cap is not None:
            self.cap.finish()
            self.cap = None

    def stop(self):
        cap, self.cap = self.cap, None
        if cap is None:
            return ""
        audio, fs = cap.finish()
        return self.transcribe(audio, fs)

    @staticmethod
    def level_up(a):
        """Brings the voice (not a click or bump) to a normal loudness: the loud part of the speech, measured in
        30 ms pieces, is set to a fixed level, up to 30 times louder, with the rare peaks softly squashed."""
        n = 480
        if len(a) < n * 4:
            return a
        frames = a[: len(a) // n * n].reshape(-1, n)
        rms = np.sqrt((frames ** 2).mean(axis=1))
        loud = float(np.percentile(rms, 90))
        if loud <= 1e-6:
            return a
        g = min(30.0, 0.1 / loud)
        return (np.tanh(a * g * 1.4) / 1.4).astype(np.float32)

    def transcribe(self, audio, fs, keep_as=None):
        a = to_16k(audio, fs)
        self.why = ""
        if len(a) == 0:
            if self.fallback:
                self.why = "the microphone sent no sound at all - looking for one that works"
                threading.Thread(target=self.pick_working_mic, daemon=True).start()
            else:
                self.why = ("the controller's microphone sent no sound at all - turn the controller fully off "
                            "(unplugged, hold PS 10 s), then plug it back in")
            return ""
        if len(a) < 16000 * 0.4:
            self.why = f"too short ({len(a) / 16000:.1f} s of sound)"
            self.keep(a, "(" + self.why + ")", keep_as)
            return ""
        level = float(np.sqrt(np.mean(a ** 2)))
        if level < 0.0008:
            self.why = "too quiet - is the right microphone set as Windows' default?"
            self.keep(a, "(" + self.why + ")", keep_as)
            return ""  # silence: don't let it imagine words
        raw = a
        a = self.level_up(a)
        segs, _ = self.model.transcribe(
            a, language="en", beam_size=5, initial_prompt=WOW_HINT, condition_on_previous_text=False,
            vad_filter=True, vad_parameters={"threshold": 0.25, "min_silence_duration_ms": 700, "speech_pad_ms": 400})
        text = " ".join(seg.text.strip() for seg in segs).strip()
        if text.lower().strip(" .!?") in NOISE_WORDS and level < 0.01:
            text = ""
        self.keep(raw, text, keep_as)
        return text

    def keep(self, a16, heard, said=None):
        """Your recording (16 kHz) and what it heard, in mic_samples, so it can be tuned to your voice. Newest 60."""
        if not self.keep_dir:
            return
        try:
            os.makedirs(self.keep_dir, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            with wave.open(os.path.join(self.keep_dir, stamp + ".wav"), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes((np.clip(a16, -1, 1) * 32000).astype(np.int16).tobytes())
            with open(os.path.join(self.keep_dir, "heard.txt"), "a", encoding="utf-8") as f:
                f.write(f"{stamp}.wav\theard: {heard}" + (f"\tsaid: {said}" if said else "") + "\n")
            wavs = sorted(x for x in os.listdir(self.keep_dir) if x.endswith(".wav"))
            for old in wavs[:-60]:
                os.remove(os.path.join(self.keep_dir, old))
        except OSError:
            pass

    def close(self):
        self.cancel()


def whisper_available(model="small.en"):
    """faster-whisper is there AND its speech model is already on this PC (it is never fetched by itself)."""
    try:
        import importlib.util
        if importlib.util.find_spec("faster_whisper") is None:
            return False
        from faster_whisper.utils import download_model
        download_model(model, local_files_only=True)
        return True
    except Exception:  # noqa: BLE001
        print(f"Talk to type: the {model} speech model isn't on this PC yet, so the built-in Windows engine is used. "
              "To get the better one (about 480 MB, downloaded once): run the installer again and say yes to the "
              "speech model, or start the helper once with --get-model.")
        return False


def get_model(model="small.en"):
    """Downloads the speech model once into this PC's Hugging Face cache (after that it is only read from disk)."""
    from faster_whisper.utils import download_model
    print(f"Downloading the {model} speech model (about 480 MB, once)...")
    path = download_model(model)
    print("Done - the speech model is in", path)


class PadWatcher(threading.Thread):
    """Reads the DualSense's own USB report: its mic (mute) button, which the game cannot see, how hard R2 is pulled,
    and the motion sensor (a sharp flick of the controller). Over USB the report starts with id 1 (mic button = bit 2
    of byte 10, R2 = byte 6, pitch rate = bytes 16-17); over Bluetooth it is id 0x31 and everything is one byte later.
    Queues "down"/"up" (mic button), "r2pull" and "flick". The only thing it ever writes to the pad is the R2 trigger's
    resistance (set_trigger), and only over USB."""

    def __init__(self, flick_threshold=4500.0):
        super().__init__(daemon=True)
        self.events = queue.Queue()
        self.down_since = None
        self.error = None
        self.running = True
        self.handle = None
        self.whandle = None
        self.length = 64
        self.out_length = 48
        self.usb = True
        self.r2 = 0
        self.flick_threshold = flick_threshold
        self._r2_high = False
        self._bias = 0.0
        self._last_flick = 0.0
        self.last_peak = 0.0
        self._touch = None  # (start x, start time, latest x) while a finger is on the touchpad
        self.touch_x = 960  # where the finger last was (0-1919)
        self.click_since = None  # when the touchpad was pressed in, while it is held
        self._was = False   # the mic button's last state
        self._open()

    def _open(self):
        hid, setupapi, k32 = ctypes.windll.hid, ctypes.windll.setupapi, ctypes.windll.kernel32
        guid = GUID()
        hid.HidD_GetHidGuid(ctypes.byref(guid))
        setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
        setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD]
        setupapi.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID),
                                                         wintypes.DWORD, ctypes.c_void_p]
        setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                                              ctypes.c_void_p]
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                    wintypes.DWORD, ctypes.c_void_p]
        hid.HidD_GetPreparsedData.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]

        class Caps(ctypes.Structure):
            _fields_ = [("Usage", wintypes.USHORT), ("UsagePage", wintypes.USHORT),
                        ("InputReportByteLength", wintypes.USHORT), ("OutputReportByteLength", wintypes.USHORT),
                        ("FeatureReportByteLength", wintypes.USHORT), ("Reserved", wintypes.USHORT * 17),
                        ("More", wintypes.USHORT * 10)]
        hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(Caps)]

        class Iface(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("Guid", GUID), ("Flags", wintypes.DWORD),
                        ("Reserved", ctypes.c_size_t)]
        hdev = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x12)
        index = 0
        paths = []
        while True:
            d = Iface()
            d.cbSize = ctypes.sizeof(Iface)
            if not setupapi.SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(guid), index, ctypes.byref(d)):
                break
            need = wintypes.DWORD()
            setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(d), None, 0, ctypes.byref(need), None)
            buf = ctypes.create_string_buffer(need.value)
            ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(d), buf, need.value, None, None):
                path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
                low = path.lower()
                if "vid_054c" in low or "vid&0002054c" in low:  # USB, or Bluetooth
                    paths.append(path)
            index += 1
        for path in paths:
            for access in (0xC0000000, 0x80000000):
                h = k32.CreateFileW(path, access, 3, None, 3, 0, None)
                if h not in (None, ctypes.c_void_p(-1).value):
                    break
                h = None
            if not h:
                continue
            pp = ctypes.c_void_p()
            hid.HidD_GetPreparsedData(h, ctypes.byref(pp))
            caps = Caps()
            hid.HidP_GetCaps(pp, ctypes.byref(caps))
            if caps.UsagePage == 1 and caps.Usage == 5 and caps.InputReportByteLength >= 11:
                self.handle, self.length = h, int(caps.InputReportByteLength)
                self.out_length = int(caps.OutputReportByteLength) or 48
                w = k32.CreateFileW(path, 0x40000000, 3, None, 3, 0, None)  # a second handle, for writing only
                self.whandle = w if w not in (None, ctypes.c_void_p(-1).value) else None
                return
        self.error = "no DualSense found (plug it in by USB)" if not paths else "could not open the DualSense's data"

    @staticmethod
    def mute_bit(report):
        """True if the mic button is down in this input report (USB id 1, or Bluetooth id 0x31)."""
        if not report:
            return False
        if report[0] == 0x01 and len(report) > 10:
            return bool(report[10] & 0x04)
        if report[0] == 0x31 and len(report) > 11:
            return bool(report[11] & 0x04)
        return False

    def run(self):
        if not self.handle:
            return
        k32 = ctypes.windll.kernel32
        buf = ctypes.create_string_buffer(self.length)
        got = wintypes.DWORD()
        while self.running:
            if not k32.ReadFile(ctypes.c_void_p(self.handle), buf, self.length, ctypes.byref(got), None):
                self.error = "lost the DualSense"
                return
            self.handle_report(bytes(buf[:got.value]))

    def handle_report(self, report):
        """One input report: the mic button, R2, the touchpad and the motion sensor."""
        # (diagnosis) every change of the byte holding the PS button, touchpad click and mic button goes in the log
        if len(report) > 12:
            o = 1 if report[:1] == b"\x31" else 0
            btn = report[8 + o:12 + o]  # all four button bytes (face/d-pad, shoulders, PS/touchpad/mic, spare)
            if btn != getattr(self, "_special", None):
                self._special = btn
                log_line("controller raw: buttons " + " ".join(f"{v:08b}" for v in btn)
                         + "  (third byte: 00000100 = mic, 00000010 = touchpad click, 00000001 = PS)")
        now = self.mute_bit(report)
        if now != self._was:
            self._was = now
            self.down_since = time.time() if now else None
            self.events.put("down" if now else "up")
        off = 0 if report[:1] == b"\x01" else (1 if report[:1] == b"\x31" else None)
        if off is None or len(report) < 18 + off:
            return
        self.usb = off == 0
        # R2: a firm pull (past 200 of 255), counted again once it has been let go (under 80)
        self.r2 = report[6 + off]
        if self.r2 > 200 and not self._r2_high:
            self._r2_high = True
            self.events.put("r2pull")
        elif self.r2 < 80:
            self._r2_high = False
        # a swipe on the touchpad: the first finger's x (0-1919) from touch to lift; a long, quick slide counts
        if len(report) >= 37 + off:
            contact = report[33 + off]
            tx = report[34 + off] | ((report[35 + off] & 0x0F) << 8)
            click = bool(report[10 + off] & 0x02)
            if getattr(self, "mouse_mode", False):
                self._as_mouse(contact, tx, (report[35 + off] >> 4) | (report[36 + off] << 4), click)
            elif getattr(self, "_mlast", None) is not None or getattr(self, "_mdown", False):
                self._as_mouse(0x80, 0, 0, False)  # menu closed: let go of everything
            if click and self.click_since is None:
                self.click_since = time.time()
            elif not click:
                self.click_since = None
            if not contact & 0x80:  # a finger is down
                self.touch_x = tx
                if self._touch is None:
                    self._touch = (tx, time.time(), tx)
                else:
                    self._touch = (self._touch[0], self._touch[1], tx)
            elif self._touch is not None:
                x0, t0, x1 = self._touch
                self._touch = None
                if abs(x1 - x0) > 450 and time.time() - t0 < 0.9:
                    self.events.put("swipe_right" if x1 > x0 else "swipe_left")
        # a flick: the pitch rate jumps far past its resting value (which drifts a little, so it is followed)
        gx = int.from_bytes(report[16 + off:18 + off], "little", signed=True)
        d = gx - self._bias
        now = time.time()
        if now - self._last_flick < 0.25:
            self.last_peak = max(self.last_peak, abs(d))  # how hard that flick was (the setup uses it)
        if abs(d) < 300:
            self._bias += 0.01 * d
        elif abs(d) > self.flick_threshold and now - self._last_flick > 1.0:
            self._last_flick = now
            self.last_peak = abs(d)
            # which way: the first big swing decides (the stop after it swings the other way, and is ignored)
            self.events.put("flick+" if d > 0 else "flick-")

    def _as_mouse(self, contact, tx, ty, click):
        """The touchpad as a laptop touchpad: slide to move the pointer (faster slides go further), click = left click."""
        if not contact & 0x80:  # a finger is down
            last = getattr(self, "_mlast", None)
            self._mdown_reports = getattr(self, "_mdown_reports", 0) + 1
            if last is not None and self._mdown_reports > 3:  # (the first few readings of a touch wobble: skipped)
                dx, dy = tx - last[0], ty - last[1]
                if abs(dx) > 90 or abs(dy) > 90:
                    dx, dy = 0, 0  # a sudden leap is a misread or a second finger, not a slide: ignored
                # smoothed with the step before, slow by default, a little faster for a quick slide
                pdx, pdy = getattr(self, "_mprev", (0.0, 0.0))
                sx, sy = (dx + pdx) / 2.0, (dy + pdy) / 2.0
                self._mprev = (dx, dy)
                speed = 0.3 * (0.8 + min(1.2, (sx * sx + sy * sy) ** 0.5 / 20.0))  # (11-11: slower again, no jumps)
                self._macc = (getattr(self, "_macc", (0.0, 0.0))[0] + sx * speed,
                              getattr(self, "_macc", (0.0, 0.0))[1] + sy * speed)
                mx, my = int(self._macc[0]), int(self._macc[1])
                if mx or my:
                    mouse(mx, my)
                    self._macc = (self._macc[0] - mx, self._macc[1] - my)
            self._mlast = (tx, ty)
        else:
            self._mlast = None
            self._mdown_reports = 0
            self._mprev = (0.0, 0.0)
            self._macc = (0.0, 0.0)
        # a click that was already held when the menu opened (the press that opened the map) is not a click: its
        # release used to land on the map and jump it to another zone
        if getattr(self, "_mheld", False):
            if not click:
                self._mheld = False
            return
        # the world map: a click on the pad's left side zooms out, right side zooms in, the middle is a left click
        if click and not getattr(self, "_mdown", False) and getattr(self, "map_open", False) and not contact & 0x80:
            if tx < 640 or tx > 1280:
                self._mzoom = True
                mouse(flags=0x0800, wheel=-120 if tx < 640 else 120)
                self._mdown = True
                return
        if not click and getattr(self, "_mzoom", False):
            self._mzoom = False
            self._mdown = False
            return
        if click != getattr(self, "_mdown", False):
            self._mdown = click
            mouse(flags=0x0002 if click else 0x0004)

    def set_trigger(self, effect):
        """R2's resistance: effect = up to 11 bytes (mode, then its settings). USB only; True if the pad took it."""
        if not (self.whandle and self.usb):
            return False
        rep_ = bytearray(max(self.out_length, 33))
        rep_[0] = 0x02          # output report
        rep_[1] = 0x04          # only the right trigger's effect is valid: rumble and lights are left to the game
        rep_[11:11 + len(effect)] = bytes(effect)
        written = wintypes.DWORD()
        k32 = ctypes.windll.kernel32
        return bool(k32.WriteFile(ctypes.c_void_p(self.whandle), bytes(rep_[:self.out_length]), self.out_length,
                                  ctypes.byref(written), None))


    def set_dots(self, count):
        """The five white dots under the touchpad: 0-5 lit, filling from the middle out (they only come in white)."""
        if not (self.whandle and self.usb):
            return False
        pattern = [0x00, 0x04, 0x0A, 0x15, 0x1B, 0x1F][max(0, min(5, count))]
        written = wintypes.DWORD()
        if not getattr(self, "_leds_released", False):
            # once per connection, as its own message first: release the lights from the controller's start-up
            # show (the way that lit the dots in the 11-11 test)
            rel = bytearray(max(self.out_length, 48))
            rel[0] = 0x02
            rel[39] = 0x02           # valid flag 2: light-bar setup
            rel[42] = 0x02           # light-bar setup: hand the LEDs to the host
            ctypes.windll.kernel32.WriteFile(ctypes.c_void_p(self.whandle), bytes(rel[:self.out_length]),
                                             self.out_length, ctypes.byref(written), None)
            self._leds_released = True
            time.sleep(0.05)
        rep_ = bytearray(max(self.out_length, 48))
        rep_[0] = 0x02
        rep_[2] = 0x10           # only the player dots are valid: rumble, lights and triggers are left alone
        rep_[44] = pattern | 0x20  # 0x20: change at once, no fade
        return bool(ctypes.windll.kernel32.WriteFile(ctypes.c_void_p(self.whandle), bytes(rep_[:self.out_length]),
                                                     self.out_length, ctypes.byref(written), None))


TRIGGER_OFF = [0x05]                      # no resistance
TRIGGER_TENSION = [0x01, 30, 70]          # from 12% of the pull, a light steady resistance: a taut line
TRIGGER_TUG = [0x06, 14, 220, 20]         # the trigger throbs (14 a second): something is pulling


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTU(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTU)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _MINPUTU(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("pad", ctypes.c_byte * 32)]


class MINPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _MINPUTU)]


def mouse(dx=0, dy=0, flags=0x0001, wheel=0):
    """Move the pointer by (dx, dy) (flags 0x0001), press / let go of the left button (0x0002 / 0x0004), or turn
    the wheel (flags 0x0800, wheel = +120 one notch up / zoom in, -120 down / zoom out)."""
    inp = MINPUT()
    inp.type = 0
    inp.u.mi = MOUSEINPUT(int(dx), int(dy), ctypes.c_uint32(int(wheel)).value, flags, 0, 0)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(MINPUT))


EXTENDED_KEYS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}  # page/home/end/arrows/ins/del
KEY_NAMES = {0x76: "F7", 0x77: "F8", 0x0D: "Enter", 0x1B: "Escape", 0x20: "Space", 0x24: "Home", 0x56: "V", 0x78: "F9", 0x79: "F10",
             0x7A: "F11", 0x11: "Ctrl", 0x10: "Shift"}
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT


def _key(vk, up):
    """One key going down or up, sent the way a real keyboard sends it (with its hardware scan code; the game's
    key bindings look at the scan code, which the earlier simple presses left out)."""
    inp = INPUT()
    inp.type = 1
    flags = 0x0008  # KEYEVENTF_SCANCODE
    if vk in EXTENDED_KEYS:
        flags |= 0x0001
    if up:
        flags |= 0x0002
    inp.u.ki = KEYBDINPUT(vk, user32.MapVirtualKeyW(vk, 0), flags, 0, 0)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def tap(vk, mods=(), why=""):
    """Press a key (with Ctrl/Shift held if given), held briefly like a real press."""
    for m in mods:
        _key(m, False)
        time.sleep(0.015)
    _key(vk, False)
    time.sleep(0.04)
    _key(vk, True)
    time.sleep(0.015)
    for m in reversed(mods):
        _key(m, True)
    name = "+".join(KEY_NAMES.get(k, hex(k)) for k in list(mods) + [vk])
    log_line(f"sent key {name}" + (f" ({why})" if why else ""))


def press_key(vk):
    tap(vk)


def backspaces(n):
    """n quick Backspaces (removing what talk to type just put in)."""
    for _ in range(max(0, n)):
        _key(0x08, False)
        _key(0x08, True)
        time.sleep(0.004)
    log_line(f"sent {n} Backspaces (taking back the last spoken part)")


def type_text(text):
    """Types text as characters (any keyboard layout) into the window in front."""
    items = []
    for ch in text:
        for up in (0, 2):
            inp = INPUT()
            inp.type = 1  # keyboard
            inp.u.ki = KEYBDINPUT(0, ord(ch), 0x0004 | up, 0, 0)  # KEYEVENTF_UNICODE (| KEYUP)
            items.append(inp)
    arr = (INPUT * len(items))(*items)
    user32.SendInput(len(items), arr, ctypes.sizeof(INPUT))


# the channels in swipe order: name shown, what is typed (the game switches the chat box on the space after it)
CHANNELS = [("Say", "/s"), ("Party", "/p"), ("Raid", "/raid"), ("Instance", "/i"), ("Guild", "/g")]
SPOKEN = {"say": 0, "party": 1, "group": 1, "raid": 2, "instance": 3, "guild": 4}


def channel_open(avail, index):
    return index == 0 or bool(avail & (1 << (index - 1)))


def next_channel(avail, cur, step):
    """The next channel you can send to, going right (+1) or left (-1) from the current one."""
    start = cur if cur <= 4 else 0
    for k in range(1, 6):
        i = (start + step * k) % 5
        if channel_open(avail, i):
            return i
    return 0


def switch_channel(index):
    """Puts the chat box on that channel: Home, then /g (etc.) and a space, typed in front of whatever is there."""
    press_key(0x24)  # Home
    type_text(CHANNELS[index][1])
    press_key(0x20)  # Space: the game switches the box and keeps the rest of the line


def spoken_channel(text):
    """"Guild, anyone for Deadmines?" -> (4, "Anyone for Deadmines?"). Only a first word that is a channel name,
    followed by a comma or colon, counts."""
    import re
    m = re.match(r"^\s*(?:to\s+)?(say|party|group|raid|instance|guild)\s*[,:]\s*(.+)$", text, re.IGNORECASE)
    if not m:
        return None, text
    rest = m.group(2).strip()
    return SPOKEN[m.group(1).lower()], rest[:1].upper() + rest[1:]


def press_cast_key():
    """Ctrl+Shift+F9: the addon binds it to Fishing while a fishing pole is in your hands."""
    tap(0x78, (0x11, 0x10), "cast Fishing")


def press_hook_key():
    """Ctrl+Shift+F11: the addon binds it to "interact" while your line is in the water."""
    tap(0x7A, (0x11, 0x10), "hook / interact")


def press_wheel_key():
    """Ctrl+Shift+F10: the addon binds it to its chat channel wheel."""
    tap(0x79, (0x11, 0x10), "open the channel wheel")


class SplashDetector:
    """Listens (to the game's sound) for the bobber's splash when a fish bites: a sudden burst of bright noise far
    above the last few seconds' level. Armed 2.5 s after the cast, so the bobber landing doesn't count."""

    def __init__(self, sens=1.0):
        self.sens = sens
        self.hist = collections.deque(maxlen=250)  # 5 s of 20 ms frames (a couple of waves)
        self.buf = np.zeros(0, dtype=np.float32)
        self.armed_at = time.time() + 3.0
        self.bite_at = 0.0
        self.last_ratio = 0.0
        self.cand = None  # (when it jumped, the level before it) while a possible splash is checked

    def push(self, mono, fs):
        x = np.diff(np.concatenate([self.buf[-1:], mono.astype(np.float32)]))  # the bright part of the sound
        self.buf = np.concatenate([self.buf, x])
        n = max(1, int(fs * 0.02))
        while len(self.buf) >= n:
            frame, self.buf = self.buf[:n], self.buf[n:]
            e = float(np.mean(frame ** 2))
            now = time.time()
            if len(self.hist) >= 25:
                # the loud end of the last 3 s (a wave's crest), not the average: a bite must stand out from the surf
                base = float(np.percentile(self.hist, 90)) + 1e-9
                self.last_ratio = e / base
                if self.cand:
                    t0, b0 = self.cand
                    if e < b0 * 2.0 and now - t0 >= 0.06:  # it fell back quickly: a splash
                        self.cand = None
                        if now - self.bite_at > 4.0:
                            self.bite_at = now
                    elif now - t0 > 0.5:  # still loud after half a second: a wave, not a bite
                        self.cand = None
                elif now > self.armed_at and e > 1e-7 and e > base * (5.0 / max(0.2, self.sens)):
                    self.cand = (now, base)
            if not self.cand:
                self.hist.append(e)  # (the splash itself isn't let into the "usual level")


FLICK_FILE = os.path.join(APP_DIR, "flick.json")


def load_flick():
    """{"cast": "+" or "-", "threshold": n} from --flick-setup, or None (flick casting stays off until then)."""
    try:
        import json
        with open(FLICK_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("cast") in ("+", "-"):
            return d
    except (OSError, ValueError):
        pass
    return None


def bite_feel(ms):
    """A fish on the hook: about two seconds of wild thrashing, strongest at first (levels up to 2)."""
    k = 1.0 - 0.6 * (ms / 2000.0)
    h2 = (int(ms // 45) * 29) % 13  # an uneven, wild rhythm (the same every time)
    if h2 < 5:
        return 0.5 * k, 1.4 * k
    if h2 < 8:
        return 0.9 * k, 0.4 * k
    return 0.0, 0.15 * k


class Fisher:
    """One fishing session, driven by the addon's seventh square: line in the water -> R2 goes taut, the helper
    listens for a bite; a bite -> tugs and a throbbing trigger; R2 pulled / controller flicked -> the hook key."""

    def __init__(self, hap, padw, args):
        self.hap, self.padw, self.a = hap, padw, args
        self.active = False
        self.det = None
        self.lb = None
        self.cast_at = 0.0
        self.seen_bite = 0.0
        self.tug_until = None
        self.hooked = False
        self.mode, self.trig = 3, True
        self.flick = None if args.no_flick_cast else load_flick()
        self.last_cast_press = 0.0
        self.hooked_at = 0.0
        self.line_end = 0.0

    def _flick_kind(self, ev):
        """Any sharp flick: with the pole ready it casts, with the line out it hooks. (Which way it went no longer
        matters: a natural cast swings back before it flicks forward, and the back-swing was read as "back".)"""
        return "flick" if ev in ("flick+", "flick-") else None

    def _trigger(self, effect):
        if self.padw and self.trig and not self.a.no_triggers:
            self.padw.set_trigger(effect)

    def update(self, flag, pad_events):
        now = time.time()
        # a fishing pole in your hands and no line out: nothing to do (the helper never casts for you)
        if flag and not flag[2] and not self.active:
            return
        flag = flag if (flag and flag[2]) else None
        if flag and not self.active:
            self.active, self.hooked, self.tug_until = True, False, None
            self.mode, self.trig = flag[0], flag[1]
            self.cast_at = now
            self._trigger(TRIGGER_TENSION)
            if not self.a.no_bite_listen:
                self.det = SplashDetector(self.a.bite_sens)
                self.lb = Loopback(self.det)
                self.lb.start()
            print("Fishing: line in the water" + ("" if self.a.no_bite_listen else " (listening for a bite)"))
        elif not flag and self.active:
            self.active = False
            self.line_end = now
            self._trigger(TRIGGER_OFF)
            if self.lb:
                self.lb.running = False
            self.lb = self.det = None
            return
        if not self.active:
            return
        if flag:
            self.mode, self.trig = flag[0], flag[1]
        # a bite
        if self.det and self.det.bite_at > self.seen_bite:
            self.seen_bite = self.det.bite_at
            print("Bite! (pull R2 or flick back to hook it)" if self.mode else "Bite!")
            self.hap.play(bite_feel, 2000)
            self._trigger(TRIGGER_TUG)
            self.tug_until = now + 2.0
        if self.tug_until and now > self.tug_until:
            self.tug_until = None
            self._trigger(TRIGGER_TENSION)
        # hooking it
        wants = ()  # (the helper never hooks for you: you hook with your own interact button)
        for ev in pad_events:
            kind = "r2pull" if ev == "r2pull" else self._flick_kind(ev)
            # not in the first 2.5 s (the cast's own follow-through), and again after 1.5 s if it was too early
            if kind in wants and now - self.cast_at > 2.5 and now - self.hooked_at > 1.5:
                if game_in_front():
                    press_hook_key()
                    self.hooked = True
                    self.hooked_at = now
                    self.hap.mute(0.45)  # a moment of silence: did you get it?
                    self._trigger(TRIGGER_OFF)
                    print("Hook! (" + ("R2" if ev == "r2pull" else "flick") + ")")
                else:
                    print("(the game is not the window in front, so it wasn't hooked)")

    def close(self):
        if self.active:
            self._trigger(TRIGGER_OFF)
        if self.lb:
            self.lb.running = False


class Weather:
    """Rain and wind textures, made here (the game tells addons nothing about weather, so the addon says what is
    falling and this makes it feel like it). Rain = random droplet taps on each side, more of them and harder as it
    gets heavier; wind = low gusting rumble. chunk() gives the left and right signals for the next 10 ms."""

    def __init__(self):
        self.rng = np.random.default_rng(11)
        self.rain = self.wind = 0.0
        self.rain_t = self.wind_t = 0.0
        n = np.arange(960)
        # one drop: a soft low "plip" about 9 ms long at 115 Hz (4 ms at 150 Hz was too brief to feel)
        self.kernel = np.exp(-n / (0.009 * RATE)) * np.sin(2 * np.pi * 115.0 * n / RATE)
        self.tails = [np.zeros(len(self.kernel) - 1) for _ in range(2)]
        a = 1 - math.exp(-2 * math.pi * 70.0 / RATE)
        self.a = a
        self.ir = a * (1 - a) ** np.arange(CHUNK)
        self.decay = (1 - a) ** (np.arange(CHUNK) + 1)
        self.lp = [0.0, 0.0]
        self.t = 0.0

    def set(self, rain, wind):
        self.rain_t, self.wind_t = float(rain), float(wind)

    def chunk(self):
        n = CHUNK
        idx = np.arange(n)
        ramp = idx / n
        # glide toward the target (about 0.7 s), so walking indoors or out fades the weather rather than cutting it
        nr = self.rain + (self.rain_t - self.rain) * (0.05 if self.rain_t < self.rain else 0.015)
        nw = self.wind + (self.wind_t - self.wind) * (0.05 if self.wind_t < self.wind else 0.015)
        r = self.rain + (nr - self.rain) * ramp
        w = self.wind + (nw - self.wind) * ramp
        self.rain, self.wind = nr, nw
        outs = []
        if self.rain_t <= 0.001 and self.wind_t <= 0.001 and self.rain < 0.001 and self.wind < 0.001 \
                and not any(np.any(t) for t in self.tails):
            self.t += n / RATE
            return [np.zeros(n), np.zeros(n)]
        for ch in range(2):
            # drops per second per side: about 6 in a drizzle, 20 in rain, 34 in a downpour; each drop a clear tap
            # that is harder the heavier it is (an earlier version had up to 200 a second, which blurred into a buzz)
            p = (4.0 + 30.0 * r) / RATE
            drops = (self.rng.random(n) < p) * self.rng.uniform(0.5, 1.0, n) * (r > 0.001)
            full = np.convolve(drops, self.kernel)
            tail = self.tails[ch]
            full[:len(tail)] += tail
            sig = full[:n] * (0.7 + 0.5 * r) * np.minimum(1.0, r * 6.0)
            self.tails[ch] = full[n:]
            noise = self.rng.standard_normal(n)
            y = np.convolve(noise, self.ir)[:n] + self.lp[ch] * self.decay
            self.lp[ch] = float(y[-1])
            gust = 0.4 + 0.6 * (0.5 + 0.5 * np.sin(2 * np.pi * (0.18 * (self.t + idx / RATE) + ch * 0.3)))
            sig = sig + y * 9.0 * w * gust
            outs.append(sig)
        self.t += n / RATE
        return outs


class NullHaptics:
    """Stands in while the controller isn't on USB: the rumble textures and weather need the cable (they are sound
    sent to the controller), everything else keeps working. Swapped for the real thing once it's plugged in."""

    def set(self, weak, strong):
        pass

    def set_weather(self, rain, wind):
        pass

    def play(self, fn, dur_ms):
        pass

    def mute(self, seconds):
        pass

    def close(self):
        pass


class Haptics:
    """Streams 4-channel audio: channels 1-2 stay silent (the controller's speaker), the two actuator channels
    (default 3 and 4) get a low carrier scaled by the strong level plus a high carrier scaled by the weak level."""

    def __init__(self, device, channels=(3, 4), gain=1.0):
        self.target = [0.0, 0.0]      # weak, strong: written by the screen reader
        self.extra = None             # (start, function(ms) -> weak, strong, length ms): an effect made here
        self.mute_until = 0.0
        self.cur = [0.0, 0.0]
        self.gain = gain
        self.weather = Weather()
        self.cols = [c - 1 for c in channels]
        self.phase_w = 0.0
        self.phase_s = 0.0
        self.running = True
        fmt = WAVEFORMATEXTENSIBLE()
        fmt.wFormatTag = WAVE_FORMAT_EXTENSIBLE
        fmt.nChannels = NCH
        fmt.nSamplesPerSec = RATE
        fmt.wBitsPerSample = 16
        fmt.nBlockAlign = NCH * 2
        fmt.nAvgBytesPerSec = RATE * fmt.nBlockAlign
        fmt.cbSize = 22
        fmt.wValidBitsPerSample = 16
        fmt.dwChannelMask = 0x33  # front L/R + back L/R
        fmt.SubFormat[:] = bytes.fromhex("0100000000001000800000aa00389b71")  # KSDATAFORMAT_SUBTYPE_PCM
        self.h = ctypes.c_void_p()
        r = winmm.waveOutOpen(ctypes.byref(self.h), device, ctypes.byref(fmt), 0, 0, CALLBACK_NULL)
        if r != 0:
            raise RuntimeError(f"could not open the sound device with 4 channels (winmm error {r}). "
                               "Is the controller plugged in by USB, and set to 4 channels in Windows Sound "
                               "settings?")
        self.bufs = []
        for _ in range(NBUF):
            data = (ctypes.c_int16 * (CHUNK * NCH))()
            hdr = WAVEHDR()
            hdr.lpData = ctypes.cast(data, ctypes.c_void_p)
            hdr.dwBufferLength = CHUNK * NCH * 2
            winmm.waveOutPrepareHeader(self.h, ctypes.byref(hdr), ctypes.sizeof(hdr))
            hdr.dwFlags |= WHDR_DONE
            self.bufs.append((data, hdr))
        self.t = threading.Thread(target=self._feed, daemon=True)
        self.t.start()

    def _chunk(self):
        n = np.arange(CHUNK)
        ramp = n / CHUNK
        tw, ts = self.target
        if self.extra:  # an effect made here (a bite), laid over what the addon sends
            t0, fn, dur = self.extra
            el = (time.time() - t0) * 1000.0
            if el < dur:
                ew, es = fn(el)
                tw, ts = max(tw, ew), max(ts, es)
            else:
                self.extra = None
        if time.time() < self.mute_until:
            tw = ts = 0.0
        w = self.cur[0] + (tw - self.cur[0]) * ramp
        s = self.cur[1] + (ts - self.cur[1]) * ramp
        self.cur = [tw, ts]
        pw = self.phase_w + 2 * math.pi * WEAK_HZ / RATE * n
        ps = self.phase_s + 2 * math.pi * STRONG_HZ / RATE * n
        self.phase_w = (self.phase_w + 2 * math.pi * WEAK_HZ / RATE * CHUNK) % (2 * math.pi)
        self.phase_s = (self.phase_s + 2 * math.pi * STRONG_HZ / RATE * CHUNK) % (2 * math.pi)
        sig = (s * np.sin(ps) * 0.85 + w * np.sin(pw) * 0.55) * self.gain
        wl, wr_ = self.weather.chunk()
        out = np.zeros((CHUNK, NCH), dtype=np.int16)
        for k, c in enumerate(self.cols):
            if 0 <= c < NCH:
                one = sig + (wl if k % 2 == 0 else wr_) * self.gain
                one = np.tanh(1.6 * one) / math.tanh(1.6)  # soft squash: loud hits get fuller and punchier, not harsh
                out[:, c] = (np.clip(one, -1.0, 1.0) * 32000).astype(np.int16)
        return out.reshape(-1)

    def _feed(self):
        errors = 0
        while self.running:
            fed = False
            try:
                for data, hdr in self.bufs:
                    if hdr.dwFlags & WHDR_DONE:
                        try:
                            arr = self._chunk()
                        except Exception as e:  # noqa: BLE001  (one bad 10 ms piece: play silence, carry on)
                            errors += 1
                            if errors < 20:
                                log_line(f"vibration sound error (carrying on): {e!r}")
                            arr = np.zeros(CHUNK * NCH, dtype=np.int16)
                            self.extra = None
                        ctypes.memmove(data, arr.ctypes.data, arr.nbytes)
                        hdr.dwFlags &= ~WHDR_DONE
                        winmm.waveOutWrite(self.h, ctypes.byref(hdr), ctypes.sizeof(hdr))
                        fed = True
            except Exception as e:  # noqa: BLE001
                log_line(f"vibration stream error: {e!r}")
                time.sleep(0.05)
            if not fed:
                time.sleep(0.002)
            self.last_fed = time.time() if fed else getattr(self, "last_fed", time.time())

    def set(self, weak, strong):
        self.target = [weak, strong]

    def play(self, fn, dur_ms):
        self.extra = (time.time(), fn, dur_ms)

    def mute(self, seconds):
        """Silence for a moment (everything, including what the addon sends)."""
        self.extra = None
        self.mute_until = time.time() + seconds

    def set_weather(self, rain, wind):
        self.weather.set(rain, wind)

    def close(self):
        self.running = False
        self.t.join(timeout=1)
        winmm.waveOutReset(self.h)
        for _, hdr in self.bufs:
            winmm.waveOutUnprepareHeader(self.h, ctypes.byref(hdr), ctypes.sizeof(hdr))
        winmm.waveOutClose(self.h)


# ----------------------------------------------------------------------------------------------- main


_single = None  # keeps the one-copy-only lock for as long as the helper runs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--test-weather", action="store_true", help="play drizzle, downpour, then wind, to check them")
    ap.add_argument("--device", default="DualSense", help="part of the sound device's name")
    ap.add_argument("--gain", type=float, default=1.0)
    ap.add_argument("--channels", default="3,4")
    ap.add_argument("--all-screens", action="store_true", default=True,
                    help="look for the game on every monitor (the default)")
    ap.add_argument("--primary-screen", dest="all_screens", action="store_false",
                    help="only look on the main monitor")
    ap.add_argument("--hz", type=float, default=120)
    ap.add_argument("--debug", action="store_true", help="print every change of level it reads")
    ap.add_argument("--listen", action="store_true",
                    help="listen to what your PC plays and feel rain/ice/wind from it (off by default: the game's "
                         "ordinary background sound was mistaken for light rain)")
    ap.add_argument("--listen-test", type=float, default=0, metavar="SECONDS",
                    help="listen for that long and print what it hears, then stop")
    ap.add_argument("--listen-sens", type=float, default=1.0, help="how easily it judges weather (default 1.0)")
    ap.add_argument("--indoors-dampen", type=float, default=0.1,
                    help="with --listen: weather strength left when it hears the inside roof patter (default 0.1)")
    ap.add_argument("--no-mic", action="store_true", help="turn talk to type off (on by default)")
    ap.add_argument("--mic-keep", action="store_true",
                    help="keep your talk-to-type recordings in mic_samples (newest 60) to tune it; off by default")
    ap.add_argument("--no-mic-keep", action="store_true", help=argparse.SUPPRESS)  # (the old switch; now the default)
    ap.add_argument("--mic-read", action="store_true",
                    help="practice: read out sentences it shows, so recordings with known words are kept for tuning")
    ap.add_argument("--no-dots", action="store_true", help="don't show your health on the controller's white dots")
    ap.add_argument("--mic-engine", choices=["auto", "whisper", "windows"], default="auto",
                    help="whisper (accurate, needs faster-whisper + the model) or windows (built in); auto picks whisper")
    ap.add_argument("--mic-model", default="small.en", help="Whisper model name (default small.en)")
    ap.add_argument("--get-model", action="store_true", help="download the speech model once (about 480 MB), then stop")
    ap.add_argument("--mic-device", default="DualSense",
                    help="part of the microphone's name to use (default: DualSense - the controller's own mic)")
    ap.add_argument("--mic-fallback", action="store_true",
                    help="if that microphone sends nothing, use another one instead (off: only the controller's)")
    ap.add_argument("--mic-list", action="store_true", help="list the microphones")
    ap.add_argument("--swipe", action="store_true", help="also switch chat channels with a touchpad swipe (off: the wheel does it)")
    ap.add_argument("--no-voice-channels", action="store_true",
                    help="don't switch channel when you start with a channel name (\"Guild, ...\")")
    ap.add_argument("--no-pad", action="store_true",
                    help="don't read the controller directly (no PS5 mic button, no fishing hook or trigger feel)")
    ap.add_argument("--flick-sens", type=float, default=1.0,
                    help="how easily a flick of the controller counts (default 1.0; 2 = a gentler flick is enough)")
    ap.add_argument("--no-triggers", action="store_true", help="never change the R2 trigger's resistance")
    ap.add_argument("--pad-test", action="store_true",
                    help="for 30 s, show what the helper reads from the controller (touchpad, click, R2, motion)")
    ap.add_argument("--key-test", action="store_true",
                    help="after 5 s (switch to the game, chat closed), press the wheel key: the channel wheel should open "
                         "(checks that the game accepts the helper's keys, which casting and hooking also use)")
    ap.add_argument("--flick-setup", action="store_true",
                    help="learn your forward (cast) and back (hook) flicks; flick casting is off until this is done")
    ap.add_argument("--no-flick-cast", action="store_true", help="never cast with a flick")
    ap.add_argument("--no-bite-listen", action="store_true",
                    help="don't listen to the game's sound for a fishing bite (you watch for the splash yourself)")
    ap.add_argument("--bite-sens", type=float, default=1.0, help="how easily a splash counts as a bite (default 1.0)")
    ap.add_argument("--no-mic-button", action="store_true",
                    help="with --mic: don't use the PS5 mic button (the addon's Talk to type key still works)")
    ap.add_argument("--mic-hold", type=float, default=0.35,
                    help="seconds the PS5 mic button must be held before it starts listening (default 0.35)")
    ap.add_argument("--mic-no-paste", action="store_true", help="copy what you said to the clipboard but don't paste it")
    ap.add_argument("--mic-test", metavar="WAVFILE", default="",
                    help="run the speech recogniser on a recording and print what it hears")
    ap.add_argument("--no-clips", action="store_true", help="with --listen: keep the numbers but save no sound clips")
    ap.add_argument("--clip-every", type=float, default=180.0, help="seconds between saved clips when it is quiet "
                    "(every 30 s while it sounds stormy)")
    ap.add_argument("--log-dir", default=os.path.join(APP_DIR, "sound_log"))
    a = ap.parse_args()
    if a.get_model:
        get_model(a.mic_model)
        return
    # only one helper at a time (it starts by itself at login; a second copy would double every vibration).
    # Checked first, so a second copy leaves the running one's log alone.
    tests = (a.list, a.test, a.test_weather, a.listen_test, a.mic_test, a.mic_list, a.mic_read, a.flick_setup,
             a.pad_test, a.key_test)
    if not any(tests):
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        global _single
        _single = k32.CreateMutexW(None, False, "TheAdventureDualSenseHelper")
        if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            print("The helper is already running (it starts by itself when you log in). This copy will close.")
            return
    # a fresh log each run (the last one kept as helper_log_prev.txt)
    try:
        if os.path.exists(LOG_FILE):
            os.replace(LOG_FILE, LOG_FILE.replace(".txt", "_prev.txt"))
    except OSError:
        pass
    sys.stdout = _Tee(sys.stdout)
    log_line("helper started with: " + " ".join(sys.argv[1:]))
    import traceback

    def _crash(kind, value, tb):
        log_line("CRASH: " + "".join(traceback.format_exception(kind, value, tb)).replace("\n", " | "))
        sys.__excepthook__(kind, value, tb)
    sys.excepthook = _crash
    threading.excepthook = lambda args: log_line(
        "THREAD CRASH: " + "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        .replace("\n", " | "))
    a.mic = not a.no_mic        # everything starts on; each has a --no-... switch

    if a.list:
        for i, name, ch in devices():
            print(f"{i}: {name} ({ch} ch)")
        return

    found = find_device(a.device)
    chans = tuple(int(c) for c in a.channels.split(","))
    if found:
        dev, name = found
        print(f"Controller sound device: {name}")
        hap = Haptics(dev, chans, a.gain)
    else:
        # no cable: carry on with everything that doesn't need it; the textures start when it is plugged in
        print("The DualSense isn't on USB: rumble textures and helper weather wait for the cable (the addon's own "
              "rumble still works over Bluetooth). Talk to type, flicks and the rest carry on.")
        hap = NullHaptics()

    if a.test:
        print("Test: light buzz, then heavy rumble, then both.")
        for w, s, secs in ((0.8, 0, 0.6), (0, 0.8, 0.6), (0.6, 0.6, 0.6)):
            hap.set(w, s)
            time.sleep(secs)
            hap.set(0, 0)
            time.sleep(0.3)
        hap.close()
        print("Done.")
        return

    use_whisper = a.mic_engine == "whisper" or (a.mic_engine == "auto" and whisper_available(a.mic_model))

    if a.mic_list:
        for name in list_mics():
            print("  " + name)
        hap.close()
        return

    if a.pad_test:
        pw = PadWatcher()
        if pw.error:
            print("The controller can't be read:", pw.error)
            hap.close()
            return
        pw.start()
        print("For 30 seconds: touch and swipe the touchpad, press and hold its middle, pull R2, flick, press the mic.")
        end = time.time() + 30
        while time.time() < end:
            time.sleep(0.25)
            evs = []
            while not pw.events.empty():
                evs.append(pw.events.get_nowait())
            print(f"  finger x {pw.touch_x:4d}  {'touching' if pw._touch else '        '}  "
                  f"{'CLICK held ' + str(round(time.time() - pw.click_since, 1)) + 's' if pw.click_since else '            '}"
                  f"  R2 {pw.r2:3d}  motion rest {int(pw._bias):5d}" + (f"  events: {evs}" if evs else ""))
        pw.running = False
        hap.close()
        return

    if a.key_test:
        print("Switch to the game now (with chat closed). In 5 seconds the wheel key is pressed...")
        for k in range(5, 0, -1):
            print(f"  {k}")
            time.sleep(1)
        print("Game in front:", game_in_front())
        press_wheel_key()
        print("Sent. Did the channel wheel open in the game? (it closes itself after 8 seconds)")
        hap.close()
        return

    if a.flick_setup:
        import json
        pw = PadWatcher(2500.0)  # easier to trigger while learning
        if pw.error:
            print("The controller can't be read:", pw.error)
            hap.close()
            return
        pw.start()

        def collect(prompt, count=3):
            print(prompt)
            got = []
            while len(got) < count:
                ev = pw.events.get()
                if ev in ("flick+", "flick-"):
                    time.sleep(0.3)  # let the peak be measured
                    got.append((ev[-1], pw.last_peak))
                    print(f"   flick {len(got)}/{count}: way {ev[-1]}, strength {int(pw.last_peak)}")
            return got

        print("Flick setup. Hold the controller as you play.\n")
        fwd = collect("Flick it FORWARD and down, like casting a line, 3 times (pause between):")
        back = collect("\nNow flick it BACK toward you, like pulling a hooked fish up, 3 times:")
        f_sign = max(("+", "-"), key=lambda sgn: sum(1 for d, _ in fwd if d == sgn))
        b_sign = max(("+", "-"), key=lambda sgn: sum(1 for d, _ in back if d == sgn))
        if f_sign == b_sign:
            print("\nForward and back came out the same way, so nothing was saved. Try again with sharper flicks.")
        else:
            weakest = min(p for _, p in fwd + back)
            threshold = int(max(2500, min(9000, weakest * 0.6)))
            with open(FLICK_FILE, "w", encoding="utf-8") as f:
                json.dump({"cast": f_sign, "threshold": threshold}, f)
            print(f"\nSaved: forward = cast, back = hook (a flick counts from strength {threshold}).")
            print("Restart the helper; with a fishing pole in your hands, flick forward to cast.")
        pw.running = False
        hap.close()
        return

    if a.mic_read:
        sentences = [
            "Hello everyone, I'm on my way to the mine.",
            "Anyone want to run Deadmines with me tonight?",
            "Meet me at the inn in Stormwind in five minutes.",
            "Can somebody help me with this quest? I keep dying.",
            "I need a healer and a tank for the dungeon.",
            "Thanks for the group, that was a lot of fun.",
            "Does anyone know where to find copper ore around here?",
            "Hold on, I have to go sell my stuff first.",
            "Nice catch! That fish was huge.",
            "Good night everyone, see you tomorrow.",
        ]
        keep = os.path.join(APP_DIR, "mic_samples")
        m = WhisperMic(a.mic_model, a.mic_device or None, keep_dir=keep)
        pw = PadWatcher()
        if not m.wait_ready() or pw.error:
            print("Practice is not available:", m.error or pw.error)
            hap.close()
            return
        pw.start()
        print("Practice: hold the PS5 mic button, read the sentence out the way you'd normally talk, let go.")
        print("Your recordings and what it heard are kept in", keep, "so it can be tuned to your voice.\n")
        for i, sent in enumerate(sentences, 1):
            print(f"{i}/{len(sentences)}  Say: {sent}")
            while True:
                ev = pw.events.get()
                if ev == "down":
                    m.start()
                elif ev == "up" and m.cap is not None:
                    cap, m.cap = m.cap, None
                    audio, fs = cap.finish()
                    heard = m.transcribe(audio, fs, keep_as=sent)
                    if heard:
                        print("        heard:", heard)
                        break
                    print("        heard nothing (" + (m.why or "no speech found") + ") - say it again")
        print("\nDone - tell Claude, and it can tune talk to type from these.")
        hap.close()
        return

    if a.mic_test:
        if use_whisper:
            m = WhisperMic(a.mic_model)
            if not m.wait_ready():
                print("Talk to type is not available:", m.error)
                return
            with wave.open(a.mic_test) as w:
                fs, n, chn = w.getframerate(), w.getnframes(), w.getnchannels()
                x = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
            t1 = time.time()
            print("Heard:", m.transcribe(x.reshape(-1, chn).mean(axis=1), fs), f"({time.time() - t1:.2f} s, Whisper)")
        else:
            m = Mic(wave=a.mic_test)
            if not m.wait_ready():
                print("Talk to type is not available:", m.error)
                return
            m.start()
            time.sleep(8)
            print("Heard:", m.stop(6), "(Windows engine)")
            m.close()
        hap.close()
        return

    if a.listen_test:
        an = Analyzer(a.listen_sens)
        lb = Loopback(an)
        lb.start()
        lb.ready.wait(5)
        if lb.error:
            print("Could not listen:", lb.error)
            hap.close()
            return
        print(f"Listening to your PC's sound for {a.listen_test:.0f} s (sample rate {lb.fs})...")
        t_end = time.time() + a.listen_test
        while time.time() < t_end:
            time.sleep(1.0)
            st = an.stats
            if st:
                print(f"  total {st['total_db']:.0f} dB  low {st['low_db']:.0f}  mid {st['mid_db']:.0f}  "
                      f"high {st['high_db']:.0f}  flat {st['flat']:.2f}  steady(high) {st['cv_high']:.2f}  "
                      f"-> rain {an.rain:.2f} wind {an.wind:.2f}  roof-patter {an.patter:.2f}"
                      f"{' (inside)' if an.indoors_heard else ''}")
            else:
                print("  (nothing playing yet, or not enough sound to judge)")
        lb.running = False
        hap.close()
        return

    if a.test_weather:
        for label, rain, wind, secs in (("drizzle", 0.3, 0, 3), ("rain", 0.6, 0, 3), ("downpour", 1.0, 0.25, 3),
                                        ("storm", 0.85, 0.5, 3), ("windy", 0, 0.7, 3)):
            print("Weather test:", label)
            hap.set_weather(rain, wind)
            time.sleep(secs)
        hap.set_weather(0, 0)
        time.sleep(0.5)
        hap.close()
        print("Done.")
        return

    mic = None
    mic_on = False
    padw = None
    pad_listening = False
    chat_open = False
    if a.mic:
        if use_whisper:
            print(f"Loading the {a.mic_model} speech model (offline)...")
            mic = WhisperMic(a.mic_model, a.mic_device or None, fallback=a.mic_fallback,
                             keep_dir=None if not a.mic_keep else
                             os.path.join(APP_DIR, "mic_samples"))
        else:
            if a.mic_engine == "auto":
                print("(faster-whisper is not installed, so the less accurate built-in Windows engine is used)")
            mic = Mic()
        if not mic.wait_ready():
            print("Talk to type is not available:", mic.error)
            mic = None
        else:
            print("Talk to type is on (" + ("Whisper " + a.mic_model if use_whisper else "Windows engine") + ", mic: "
                  + (a.mic_device or "Windows' default recording device") + "). "
                  + ("It will paste into the chat box." if not a.mic_no_paste else "It will copy to the clipboard."))
            if not a.no_mic_button and not a.no_pad:
                print("PS5 mic button: click it to open or close chat; press and hold it to speak, let go to type.")
            print("The Talk to type key / touchpad zone in the game works too (set in Settings > Controller).")
    def make_pad():
        learned = load_flick()
        return PadWatcher((learned or {}).get("threshold", 4500.0) / max(0.2, a.flick_sens))

    if not a.no_pad:
        padw = make_pad()
        if padw.error:
            print("Reading the controller directly is not available:", padw.error)
            padw = None
        else:
            padw.start()
            print("Fishing: you feel the bobber and the bite (you hook with your own button).")
    fisher = Fisher(hap, padw, a)
    listener = analyzer = slog = None
    if a.listen:
        analyzer = Analyzer(a.listen_sens)
        slog = SoundLog(a.log_dir, a.clip_every, clips=not a.no_clips)
        listener = Loopback(analyzer, slog.feed)
        listener.start()
        listener.ready.wait(5)
        if listener.error:
            print("Listening is not available:", listener.error)
            listener = None
        else:
            print("Listening to your PC's sound for rain, ice and wind (only used when the addon's weather is "
                  "Nothing).")
            print(f"What it hears is logged in {a.log_dir}" + ("" if a.no_clips else
                  " with short sound clips (they contain whatever was playing, e.g. voice chat; use --no-clips to "
                  "stop that)."))

    def deliver(heard, chat_was_open):
        if not heard:
            why = getattr(mic, "why", "")
            print("(heard nothing" + (": " + why if why else "") + ")")
            return
        print("Heard:", heard)
        chan, heard = spoken_channel(heard) if not a.no_voice_channels else (None, heard)
        if chan is not None and not channel_open(chan_state[0][0] if chan_state[0] else 15, chan):
            print(f"(you are not in a {CHANNELS[chan][0].lower()} group, so it stays on the current channel)")
            chan = None
        set_clipboard(heard)
        if a.mic_no_paste:
            return
        if not game_in_front():
            print("(the game is not the window in front, so it was only copied)")
            return
        if not chat_was_open:
            press_key(0x0D)  # Enter opens chat
            time.sleep(0.2)
        if chan is not None:
            switch_channel(chan)
            time.sleep(0.08)
            print("To:", CHANNELS[chan][0])
        press_ctrl_v()
        last_said[0], last_said[1] = heard, time.time()

    chan_state = [None]  # the eighth square, read each frame (shared with deliver, which may run in a thread)
    last_said = ["", 0.0]  # what talk to type last pasted, and when (a double-tap takes it back)
    mic_tap = [None]       # when a quick tap of the mic button was let go (waiting to see if a second one comes)
    wheel = {"sent": False, "last": None, "until": 0.0, "was_click": False, "releases": 0}
    squares_seen = [None]
    usb_check = [0.0]
    steam_check = [-1000.0, False]
    lost_logged = [False]
    last_good = [None]
    usb_report = [None, 0.0]  # what the addon was last told, and when
    dots = {"shown": None, "at": 0.0}
    last_chat = [None]

    def finish_speech(chat_was_open):
        if getattr(mic, "early", False):
            m = mic
            threading.Thread(target=lambda: deliver(m.stop(), chat_was_open), daemon=True).start()
        else:
            deliver(mic.stop(), chat_was_open)

    print("On: haptics" + (", talk to type" if mic else "") + (", weather listening (--listen)" if a.listen else "")
          + (", controller (mic button, fishing, swipes, health dots)" if padw else "")
          + ".  Each can be turned off: run with --help to see how.")

    from PIL import ImageGrab
    pos = None
    last_ok = 0.0
    last_search = 0.0
    last_lv, lv_since, stuck, last_print = None, 0.0, False, None
    state = "searching"
    print("Looking for the addon's two squares in the top-left of the game...")
    print("(the addon must be installed and the game in windowed or borderless window mode)")
    period = 1.0 / a.hz
    try:
        while True:
            t0 = time.time()
            if (not isinstance(hap, NullHaptics) and (not hap.t.is_alive()
                                                       or t0 - getattr(hap, "last_fed", t0) > 3)):
                print("The vibration sound stream stopped - restarting it.")
                try:
                    hap.close()
                except Exception:  # noqa: BLE001
                    pass
                hap = NullHaptics()
                usb_check[0] = 0.0  # reopened just below
            if t0 - steam_check[0] > 30:  # Steam running? Its controller support takes over the DualSense
                steam_check[0] = t0
                running = steam_running()
                if running and not steam_check[1]:
                    print("Steam is running: its PlayStation controller support takes over the DualSense (no HD "
                          "vibration, its own player dots). Quit Steam, or turn off Steam > Settings > Controller > "
                          "PlayStation Controller Support, then unplug and replug the controller.")
                steam_check[1] = running
            if t0 - usb_check[0] > 5:  # plugged in or unplugged? (checked every 5 s)
                usb_check[0] = t0
                found = find_device(a.device)
                if found and isinstance(hap, NullHaptics):
                    try:
                        hap = Haptics(found[0], chans, a.gain)
                        fisher.hap = hap
                        print("Controller plugged in by USB: rumble textures and weather on.")
                        usb_report[1] = 0.0  # tell the addon straight away
                        if padw and not padw.usb and not a.no_pad:  # read it over the cable now (dots, R2 feel)
                            padw.running = False
                            padw = make_pad()
                            if not padw.error:
                                padw.start()
                                fisher.padw = padw
                    except RuntimeError as e:
                        print("Couldn't open the controller's sound:", e)
                elif not found and not isinstance(hap, NullHaptics):
                    try:
                        hap.close()
                    except Exception:  # noqa: BLE001
                        pass
                    hap = NullHaptics()
                    fisher.hap = hap
                    print("Controller unplugged: rumble textures paused (the addon's motors take over).")
                    usb_report[1] = 0.0
                if padw is None and not a.no_pad:  # lost (turned off, unplugged): read it again once it's back
                    try:
                        again = make_pad()
                        if not again.error:
                            again.start()
                            padw = again
                            fisher.padw = padw
                            print("Reading the controller again: mic button, touchpad, health dots and R2 are back.")
                    except Exception as e:  # noqa: BLE001
                        log_line(f"controller reopen failed: {e!r}")
            try:
                if pos is None:
                    if t0 - last_search > 1.0:
                        last_search = t0
                        img = ImageGrab.grab(all_screens=a.all_screens)
                        arr = np.asarray(img.convert("RGB"))
                        p = locate_strip(arr)
                        if p:
                            cm, w, yc = p
                            # grab just the strip's middle row, with a little room either side
                            left = max(0, int(cm - 2.5 * w) - 2)
                            right = int(cm + (STRIP - 2.5) * w) + 3
                            top = max(0, int(yc) - 1)
                            ox, oy = virtual_origin() if a.all_screens else (0, 0)  # (image -> screen coordinates)
                            pos = (left + ox, top + oy, right + ox, top + 3 + oy, cm - left, w, yc - top)
                            print(f"Found the squares at ({left + ox}, {top + oy}), each {w:.1f} pixels wide. Haptics on.")
                            state = "running"
                else:
                    try:
                        strip_px = grab_region(*pos[:4])
                    except Exception:  # noqa: BLE001  (the slower way still works)
                        strip_px = np.asarray(ImageGrab.grab(bbox=pos[:4], all_screens=a.all_screens).convert("RGB"))
                    raw = canon_strip(strip_px, pos[4], pos[5], pos[6])
                    arr, trusted = normalize_strip(raw)
                    if trusted:
                        last_good[0] = arr
                    elif last_good[0] is not None:
                        arr = last_good[0]  # chat, wheel, fishing and menus keep the last good reading (a dimmed or
                        #                     covered strip read as nonsense: picks that were never made)
                    dec = decode(arr, 0, 0) if trusted else None
                    chat_open = chat_flag(arr, 0, 0)
                    pad_events = []
                    if padw:
                        while not padw.events.empty():
                            pad_events.append(padw.events.get_nowait())
                        for ev_name in pad_events:
                            fl = fish_flag(arr, 0, 0)
                            log_line(f"controller: {ev_name}" + (f" (peak {int(padw.last_peak)})" if "flick" in ev_name else "")
                                     + f" | fishing: {'line out' if fl and fl[2] else ('pole ready' if fl else 'no pole/off')}"
                                     + f" | chat: {'open' if chan_flag(arr, 0, 0) else 'closed'}"
                                     + f" | game in front: {game_in_front()}")
                        if padw.error:
                            print("The controller stopped being read:", padw.error, "- trying again every 5 s")
                            padw = None
                            fisher.padw = None
                            if pad_listening and mic:  # it went mid-sentence: drop that recording
                                mic.cancel()
                            pad_listening = False
                    wx = (round(dec[2], 2), round(dec[3], 2)) if dec else None
                    sq = (fish_flag(arr, 0, 0), chan_flag(arr, 0, 0), wheel_request(arr, 0, 0), wx)
                    if sq != squares_seen[0]:
                        squares_seen[0] = sq
                        log_line(f"addon says: fishing {sq[0]} | chat {sq[1]} | wheel request {sq[2]} | weather rain/wind {sq[3]}")
                    fisher.update(fish_flag(arr, 0, 0), pad_events)
                    chan_state[0] = chan_flag(arr, 0, 0)
                    if chan_state[0] != last_chat[0]:  # say what it sees, so it is clear when chat is open
                        last_chat[0] = chan_state[0]
                        if chan_state[0]:
                            cur = chan_state[0][1]
                            print("Chat open:", CHANNELS[cur][0] if cur <= 4 else "another channel")
                    for ev_name in pad_events:
                        if ev_name in ("swipe_left", "swipe_right"):
                            if not a.swipe or time.time() < wheel["until"]:
                                continue
                            if not chan_state[0]:
                                print(f"(touchpad swipe {ev_name[6:]}: chat isn't open, so nothing to switch)")
                            elif game_in_front():
                                avail, cur = chan_state[0]
                                nxt = next_channel(avail, cur, 1 if ev_name == "swipe_right" else -1)
                                switch_channel(nxt)
                                print("Chat channel:", CHANNELS[nxt][0])
                                    # holding the middle of the touchpad: the addon opens its channel wheel (noted in the log only)
                    if padw and padw.click_since and not wheel["sent"] and time.time() - padw.click_since > 0.4:
                        wheel["sent"] = True
                        log_line(f"touchpad held 0.4 s at x={padw.touch_x} (the addon opens the wheel itself)")
                        wheel["until"] = time.time() + 10  # no swiping while the wheel is open
                        wheel["releases"] = 0
                    if padw and not padw.click_since:
                        wheel["sent"] = False
                    if padw:
                        clicking = padw.click_since is not None
                        if wheel["was_click"] and not clicking and time.time() < wheel["until"]:
                            wheel["releases"] += 1  # 1 = letting go of the hold that opened it, 2 = the choosing click
                            if wheel["releases"] >= 2:
                                wheel["until"] = time.time() + 0.6
                        wheel["was_click"] = clicking
                    # a channel chosen on the wheel: switch the chat box
                    req = wheel_request(arr, 0, 0)
                    if wheel["last"] is None:
                        wheel["last"] = req  # whatever was there when the helper started is old news
                    elif req and wheel["last"] and req[1] != wheel["last"][1]:  # the addon flips this on each pick
                        wheel["last"] = req
                        code = req[0]
                        log_line(f"wheel pick from the addon: code {code}")
                        if 1 <= code <= 5 and game_in_front():
                            if not chan_state[0]:
                                press_key(0x0D)
                                time.sleep(0.2)
                            switch_channel(code - 1)
                            print("Chat channel:", CHANNELS[code - 1][0])
                        elif code in (6, 7) and game_in_front():
                            # the touchpad's map / chat zone: keys the addon bound to the game's own commands
                            tap(0x74 if code == 6 else 0x75, (0x11, 0x10),
                                "open the map" if code == 6 else "open chat")
                    # tell the addon whether the helper has the cable (a hidden key; never while chat is open)
                    on_usb = not isinstance(hap, NullHaptics)
                    if (on_usb != usb_report[0] or time.time() - usb_report[1] > 15) and not chan_state[0] \
                            and not chat_flag(arr, 0, 0) and game_in_front():
                        tap(0x77 if on_usb else 0x76, (0x11, 0x10), "tell the addon: " + ("on USB" if on_usb else "no USB"))
                        usb_report[0], usb_report[1] = on_usb, time.time()
                    # a menu is open in the game: the touchpad moves the mouse (only while the game is in front)
                    if padw:
                        want = menu_flag(arr, 0, 0) and game_in_front()
                        padw.map_open = map_flag(arr, 0, 0)
                        if want != getattr(padw, "mouse_mode", False):
                            if want:  # a press still held from opening the menu is ignored until let go
                                padw._mheld = padw.click_since is not None
                                padw._mdown = False
                            padw.mouse_mode = want
                            log_line("touchpad is " + ("the mouse (a menu is open)" if want else "the touchpad again"))
                    # your health on the five white dots (re-sent every second in case the game resets them)
                    hl = health_level(arr, 0, 0)
                    if padw and hl is not None and not a.no_dots:
                        n = int(math.ceil(hl * 5 - 1e-6)) if hl > 0.004 else 0
                        if n != dots["shown"] or time.time() - dots["at"] > 1:
                            sent = padw.set_dots(n)
                            if n != dots["shown"]:
                                log_line(f"health dots: {n} of 5 (health {hl:.2f}) " + ("sent" if sent else
                                         "NOT sent (the controller isn't on USB)"))
                            if sent:
                                dots["shown"], dots["at"] = n, time.time()
                    if mic and padw and not a.no_mic_button:
                        # the PS5 mic button: a quick click opens/closes chat, holding it listens
                        for ev_name in pad_events:
                            if ev_name == "down" and mic.early and not mic_on and chat_open:
                                mic.start()  # record from the very start; thrown away if it turns out to be a click
                            elif ev_name == "up":
                                if pad_listening:
                                    pad_listening = False
                                    finish_speech(chat_open)
                                else:
                                    mic.cancel()
                                    if mic_tap[0] and time.time() - mic_tap[0] < 0.45:
                                        mic_tap[0] = None  # a double-tap: take back what was just said
                                        if chat_open and last_said[0] and time.time() - last_said[1] < 120 \
                                                and game_in_front():
                                            backspaces(len(last_said[0]))
                                            print("Took back:", last_said[0])
                                            last_said[0] = ""
                                    else:
                                        mic_tap[0] = time.time()
                        # a single tap (no second one came): open or close the chat box
                        if mic_tap[0] and time.time() - mic_tap[0] >= 0.45:
                            mic_tap[0] = None
                            if game_in_front():
                                press_key(0x1B if chat_open else 0x0D)
                        # holding it: listens - only while chat is open (tap first to open it)
                        if padw.down_since and not pad_listening and time.time() - padw.down_since > a.mic_hold:
                            if chat_open:
                                pad_listening = True
                                if not mic.early:
                                    mic.start()
                                print("Listening...")
                            elif not getattr(padw, "_told_closed", False):
                                padw._told_closed = True
                                print("(chat isn't open: tap the mic button to open it, then hold to speak)")
                        if not padw.down_since:
                            padw._told_closed = False
                    # the addon's Talk to type key / touchpad zone - only when the helper can't read the PS5 mic button
                    # itself (if both run, a press on the mic button that is also bound to that key starts and stops a
                    # second recording that cut the first one off: "heard nothing" every time)
                    if mic and not pad_listening and (padw is None or a.no_mic_button):
                        flag = mic_flag(arr, 0, 0)
                        if flag and not mic_on:
                            mic_on = True
                            mic.start()
                            print("Listening...")
                        elif mic_on and not flag:
                            mic_on = False
                            finish_speech(chat_open)
                    lv = (dec[0], dec[1]) if dec else None
                    if dec:
                        used = (dec[2], dec[3])
                        if listener and max(used) < 0.01:  # the addon says nothing is falling: go by what it hears
                            used = (analyzer.rain, analyzer.wind)
                        elif listener and analyzer.indoors_heard:  # it hears the roof pitter-patter: you are inside
                            used = (used[0] * a.indoors_dampen, used[1] * a.indoors_dampen)
                        hap.set_weather(*used)
                        if slog:
                            slog.tick(analyzer, mode_name(dec[2], dec[3]), used[0], used[1])
                    if lv:
                        last_ok = t0
                        # safety: a level that never changes and is not zero for 20 s is not a real effect
                        if lv != last_lv:
                            last_lv, lv_since = lv, t0
                        if max(lv) > 0.05 and t0 - lv_since > 20:
                            if not stuck:
                                print("A level has been frozen for 20 s: silencing it until it changes.")
                            stuck = True
                            lv = (0.0, 0.0)
                        else:
                            stuck = False
                        if a.debug and lv != last_print:
                            print(f"levels weak {lv[0]:.2f} strong {lv[1]:.2f} rain {dec[2]:.2f} wind {dec[3]:.2f}")
                            last_print = lv
                        hap.set(*lv)
                    elif t0 - last_ok > 0.5 and not lost_logged[0]:
                        lost_logged[0] = True
                        log_line("strip unreadable, squares read: " + " ".join(
                            "(%d,%d,%d)" % tuple(int(v) for v in raw[3, k * 6 + 3]) for k in range(STRIP)))
                    if not dec and t0 - last_ok > 0.5:
                        hap.set(0, 0)  # effects stop at once, but the weather carries on for a few seconds
                    if not dec and t0 - last_ok > 4.0:
                        hap.set_weather(0, 0)
                        print("Lost the squares (game closed, or helper turned off). Searching again...")
                        pos = None
                        last_search = 0
                    if dec:
                        lost_logged[0] = False
            except OSError:
                pass  # a screen grab can fail for a moment (secure desktop, lock screen)
            dt = time.time() - t0
            if dt < period:
                time.sleep(period - dt)
    except KeyboardInterrupt:
        pass
    finally:
        fisher.close()
        if padw and not a.no_dots:
            padw.set_dots(1)  # back to the single middle dot
        if mic:
            mic.close()
        hap.close()
        print("Stopped.")


if __name__ == "__main__":
    main()
