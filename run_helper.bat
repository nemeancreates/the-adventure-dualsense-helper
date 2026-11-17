@echo off
rem works from a copy anywhere (the Desktop too): uses the helper folder if the program isn't beside this file
cd /d "%~dp0"
if not exist "dualsense_haptics.py" cd /d "C:\Dev\TheAdventure\helper"
title The Adventure - DualSense helper
python dualsense_haptics.py %*
echo.
echo The helper stopped. Press a key to close this window.
pause >nul
