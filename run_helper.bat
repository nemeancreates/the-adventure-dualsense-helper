@echo off
cd /d "%~dp0"
title The Adventure - DualSense helper
python dualsense_haptics.py %*
echo.
echo The helper stopped. Press a key to close this window.
pause >nul
