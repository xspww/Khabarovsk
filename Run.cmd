@echo off
setlocal
title Argus Launcher Console
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set ARGUS_CONSOLE_ACTIVITY=1
set ARGUS_CONSOLE_COLOR=1
cd /d "%~dp0"
python main.py
if errorlevel 1 (
  echo.
  echo Argus Launcher exited with an error.
  echo Check the message above or the log file shown in this window.
  pause
)
endlocal
