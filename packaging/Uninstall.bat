@echo off
title The Adventure - remove
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1"
