# Builds the shareable packages (run from anywhere; needs Python 3.12 with the helper's packages + PyInstaller):
#   dist\TheAdventure-<version>.zip         the addon only (for CurseForge)
#   dist\TheAdventure-Setup-<version>.zip   addon + DualSense helper program + installer (for everyone else)
# Usage: powershell -ExecutionPolicy Bypass -File build.ps1 [-AddonDir C:\path\to\TheAdventure]
param([string]$AddonDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path)
$ErrorActionPreference = "Stop"
$helperDir = Resolve-Path (Join-Path $PSScriptRoot "..")
$version = (Select-String -Path (Join-Path $AddonDir "TheAdventure.toc") -Pattern '## Version:\s*(\S+)').Matches[0].Groups[1].Value
$work = Join-Path $env:TEMP "TheAdventureBuild"
$out = Join-Path $AddonDir "dist"
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $work, $out | Out-Null
Write-Host "Building The Adventure $version" -ForegroundColor Yellow

# 1. the helper program (one folder, no Python needed on the other PC)
python -m PyInstaller --noconfirm --clean --onedir --console --name TheAdventureHelper `
  --distpath "$work\pyi" --workpath "$work\pyiwork" --specpath "$work" `
  --add-data "$helperDir\mic_listener.ps1;." `
  --collect-all faster_whisper --collect-all ctranslate2 --collect-all onnxruntime --collect-binaries av `
  --hidden-import PIL.ImageGrab --exclude-module tkinter --exclude-module matplotlib `
  "$helperDir\dualsense_haptics.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# 2. the addon: code, bindings and the art (no saved settings, logs, notes or sources)
$addon = Join-Path $work "addon\TheAdventure"
New-Item -ItemType Directory -Force (Join-Path $addon "Media") | Out-Null
Get-ChildItem $AddonDir -File | Where-Object { $_.Extension -in ".lua", ".xml", ".toc" -and $_.Name -ne "probe.lua" } |
  Copy-Item -Destination $addon
Get-ChildItem (Join-Path $AddonDir "Media") -File | Where-Object { $_.Extension -in ".tga", ".blp" } |
  Copy-Item -Destination (Join-Path $addon "Media")

# 3. the setup folder
$setup = Join-Path $work "TheAdventure-Setup-$version"
New-Item -ItemType Directory -Force (Join-Path $setup "AddOn"), (Join-Path $setup "Helper") | Out-Null
Copy-Item $addon (Join-Path $setup "AddOn") -Recurse
Copy-Item (Join-Path $work "pyi\TheAdventureHelper") (Join-Path $setup "Helper") -Recurse
foreach ($f in "install.ps1", "uninstall.ps1", "README.txt") { Copy-Item (Join-Path $PSScriptRoot $f) $setup }
foreach ($f in "Install.bat", "Uninstall.bat") {   # (Windows batch files want CRLF line endings)
  $text = (Get-Content (Join-Path $PSScriptRoot $f) -Raw) -replace "`r?`n", "`r`n"
  [IO.File]::WriteAllText((Join-Path $setup $f), $text)
}

# 4. the zips
$addonZip = Join-Path $out "TheAdventure-$version.zip"
$setupZip = Join-Path $out "TheAdventure-Setup-$version.zip"
Remove-Item $addonZip, $setupZip -ErrorAction SilentlyContinue
Compress-Archive -Path $addon -DestinationPath $addonZip
Compress-Archive -Path $setup -DestinationPath $setupZip -CompressionLevel Optimal
Get-Item $addonZip, $setupZip | ForEach-Object { "{0}  {1:N1} MB" -f $_.FullName, ($_.Length / 1MB) }
