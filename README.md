# The Adventure: DualSense helper

A small Windows program that gives **The Adventure**, a World of Warcraft: Forever addon, the parts of a PS5 DualSense controller that an addon can't reach on its own.

**Download:** get `TheAdventure-Setup-<version>.zip` from [Releases](../../releases), unzip it and run `Install.bat`. The installer puts the addon in WoW: Forever and the helper in your user folder. It doesn't need Python.

## What it adds (DualSense on a USB cable)

- **HD vibration textures:** footsteps, hits, spells, mining, fishing, chests, and weather (rain, wind and storms you pick on the touchpad wheel). These play through the controller's voice-coil haptics instead of the old rumble motors.
- **Health on the five white player dots.**
- **The PS5 mic button for talk to type.** Click it to open chat, hold it while you speak, and let go to type the words. Speech is turned into text on your own PC with Whisper (`faster-whisper`, the `small.en` model).
- **The touchpad as a mouse** while a menu is open (the map, bags, trade, the addon's windows).
- **A chat channel wheel** on the touchpad, and a light adaptive-trigger feel while fishing.

Over Bluetooth, the addon falls back to the controller's ordinary rumble.

## How it talks to the game

The addon draws a strip of tiny coloured squares in the top-left corner of the game. The helper reads that strip from the screen, so the game must be in windowed or borderless window mode. The helper answers through a few key bindings the addon sets up.

The helper only presses keys for things you ask for: the chat channel you pick, the words you speak, and a short "helper is connected" signal. **It never casts, attacks, targets or hooks a fish for you.**

Blizzard hasn't published a ruling on companion programs like this. Use it at your own judgement.

## Privacy

- Speech recognition runs on your PC. The model is downloaded once, only if you say yes in the installer or run `TheAdventureHelper.exe --get-model`. After that it's only read from disk, with telemetry off.
- Recordings aren't kept, unless you start the helper with `--mic-keep` to tune it.
- Nothing from the game or your microphone is sent anywhere.

## Running from source

Python 3.12 with `numpy`, `pillow` and `faster-whisper`:

```
python dualsense_haptics.py            # everything on
python dualsense_haptics.py --help     # every switch
python dualsense_haptics.py --get-model
```

To build the release zips (needs `pyinstaller`), put this folder inside the addon's folder as `helper\`, then run:

```
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

## Files

| File | What it is |
|---|---|
| `dualsense_haptics.py` | The helper. |
| `mic_listener.ps1` | Fallback speech engine (Windows' built-in one), used until the Whisper model is downloaded. |
| `run_helper.bat` | Starts it from source. |
| `packaging/` | Installer, uninstaller, end-user README and build script. |

© Nemean. All rights reserved.
