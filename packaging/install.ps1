# The Adventure - installer. Puts the addon in World of Warcraft: Forever, the DualSense helper in your user folder,
# and (if you say yes) starts the helper with Windows and downloads the speech model for talk to type.
# Nothing is sent anywhere except the one speech-model download you agree to.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$addonSrc = Join-Path $here "AddOn\TheAdventure"
$helperSrc = Join-Path $here "Helper\TheAdventureHelper"
$helperDest = Join-Path $env:LOCALAPPDATA "TheAdventure\Helper"
$helperExe = Join-Path $helperDest "TheAdventureHelper.exe"

function Ask($question) {
  $a = Read-Host "$question [y/n]"
  return $a -match '^(y|yes)$'
}
function Say($text, $color = "Gray") { Write-Host $text -ForegroundColor $color }

Say ""
Say "The Adventure - setup" "Yellow"
Say "----------------------" "Yellow"

# 1. Find the game -------------------------------------------------------------------------------------------
$roots = @()
foreach ($key in "HKLM:\SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft",
                 "HKLM:\SOFTWARE\Blizzard Entertainment\World of Warcraft") {
  try {
    $p = (Get-ItemProperty $key -ErrorAction Stop).InstallPath
    if ($p) { $roots += (Split-Path ($p.TrimEnd('\')) -Parent) }
  } catch {}
}
$roots += "C:\Program Files (x86)\World of Warcraft", "C:\Program Files\World of Warcraft"
$flavours = @()
foreach ($r in ($roots | Select-Object -Unique)) {
  if (Test-Path $r) {
    Get-ChildItem $r -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^_.*_$' } |
      ForEach-Object { $flavours += $_.FullName }
  }
}
$flavours = $flavours | Select-Object -Unique
$game = $flavours | Where-Object { $_ -match '_forever_$' } | Select-Object -First 1
if (-not $game) {
  if ($flavours.Count -gt 0) {
    Say "WoW: Forever wasn't found. Game folders found:"
    for ($i = 0; $i -lt $flavours.Count; $i++) { Say ("  {0}. {1}" -f ($i + 1), $flavours[$i]) }
    $n = Read-Host "Type a number to use one, or paste the path of your _forever_ folder"
    if ($n -match '^\d+$') { $game = $flavours[[int]$n - 1] } else { $game = $n.Trim('"') }
  } else {
    $game = (Read-Host "Paste the path of your World of Warcraft _forever_ folder").Trim('"')
  }
}
if (-not $game -or -not (Test-Path $game)) { Say "That folder doesn't exist - setup stopped." "Red"; Read-Host "Press Enter to close"; exit 1 }

# 2. The addon -------------------------------------------------------------------------------------------------
$addons = Join-Path $game "Interface\AddOns"
$addonDest = Join-Path $addons "TheAdventure"
New-Item -ItemType Directory -Force $addons | Out-Null
$existing = Get-Item $addonDest -ErrorAction SilentlyContinue
if ($existing -and ($existing.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
  Say "The addon folder is a link to a development copy - left as it is." "Yellow"
} else {
  try {
    if ($existing) { Remove-Item $addonDest -Recurse -Force }
    Copy-Item $addonSrc $addonDest -Recurse -Force
    Say "Addon installed: $addonDest" "Green"
  } catch {
    Say "Couldn't write to the game's AddOns folder. Right-click Install.bat and choose 'Run as administrator'." "Red"
    Read-Host "Press Enter to close"; exit 1
  }
}

# 3. The helper ------------------------------------------------------------------------------------------------
Get-Process TheAdventureHelper -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500
New-Item -ItemType Directory -Force $helperDest | Out-Null
Get-ChildItem $helperDest -Exclude "helper_log*.txt", "mic_samples", "sound_log", "flick.json" -ErrorAction SilentlyContinue |
  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item (Join-Path $helperSrc "*") $helperDest -Recurse -Force
Say "DualSense helper installed: $helperDest" "Green"

$shell = New-Object -ComObject WScript.Shell
$menu = Join-Path ([Environment]::GetFolderPath("Programs")) "The Adventure - DualSense helper.lnk"
$lnk = $shell.CreateShortcut($menu); $lnk.TargetPath = $helperExe; $lnk.WorkingDirectory = $helperDest; $lnk.WindowStyle = 7; $lnk.Save()
Say "Start menu shortcut added." "Green"

$startup = Join-Path ([Environment]::GetFolderPath("Startup")) "The Adventure - DualSense helper.lnk"
if (Ask "Start the helper by itself when Windows starts? (recommended)") {
  $lnk = $shell.CreateShortcut($startup); $lnk.TargetPath = $helperExe; $lnk.WorkingDirectory = $helperDest; $lnk.WindowStyle = 7; $lnk.Save()
  Say "It will start with Windows (minimised)." "Green"
} elseif (Test-Path $startup) { Remove-Item $startup -Force }

# 4. The speech model (talk to type) ---------------------------------------------------------------------------
$model = Join-Path $env:USERPROFILE ".cache\huggingface\hub\models--Systran--faster-whisper-small.en"
if (Test-Path $model) {
  Say "The speech model for talk to type is already on this PC." "Green"
} else {
  Say ""
  Say "Talk to type works best with a speech model that runs on this PC (about 480 MB, downloaded once from"
  Say "Hugging Face). After that, everything you say is turned into text on this PC only. Without it, the"
  Say "built-in Windows speech engine is used (less accurate)."
  if (Ask "Download the speech model now?") {
    & $helperExe --get-model
  }
}

# 5. Done ------------------------------------------------------------------------------------------------------
Say ""
Say "All set. In the game: plug the DualSense in by USB for the full feel (vibration textures, weather, health dots," "Yellow"
Say "mic button, touchpad mouse). Play in windowed or borderless window mode. Restart WoW fully the first time." "Yellow"
if (Ask "Start the helper now?") { Start-Process -FilePath $helperExe -WorkingDirectory $helperDest -WindowStyle Minimized }
Read-Host "Press Enter to close"
