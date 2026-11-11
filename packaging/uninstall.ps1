# The Adventure - removes the DualSense helper, its shortcuts and (if you say yes) the addon and the speech model.
$ErrorActionPreference = "Continue"
function Ask($question) { return (Read-Host "$question [y/n]") -match '^(y|yes)$' }

Get-Process TheAdventureHelper -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500
$name = "The Adventure - DualSense helper.lnk"
foreach ($dir in [Environment]::GetFolderPath("Startup"), [Environment]::GetFolderPath("Programs")) {
  $l = Join-Path $dir $name
  if (Test-Path $l) { Remove-Item $l -Force }
}
$helper = Join-Path $env:LOCALAPPDATA "TheAdventure"
if (Test-Path $helper) { Remove-Item $helper -Recurse -Force; Write-Host "Helper removed." -ForegroundColor Green }

if (Ask "Also remove the addon from World of Warcraft: Forever?") {
  foreach ($root in "C:\Program Files (x86)\World of Warcraft", "C:\Program Files\World of Warcraft") {
    $a = Join-Path $root "_forever_\Interface\AddOns\TheAdventure"
    $item = Get-Item $a -ErrorAction SilentlyContinue
    if ($item -and -not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
      Remove-Item $a -Recurse -Force; Write-Host "Addon removed: $a" -ForegroundColor Green
    }
  }
}
$model = Join-Path $env:USERPROFILE ".cache\huggingface\hub\models--Systran--faster-whisper-small.en"
if ((Test-Path $model) -and (Ask "Also delete the speech model (about 480 MB)?")) {
  Remove-Item $model -Recurse -Force; Write-Host "Speech model deleted." -ForegroundColor Green
}
Read-Host "Done. Press Enter to close"
