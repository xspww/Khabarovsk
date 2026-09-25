@echo off
setlocal
title Cronus Launcher Console
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set CRONUS_CONSOLE_ACTIVITY=1
set CRONUS_CONSOLE_COLOR=1
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python not found in PATH. Install Python 3.10+ and retry.
  pause
  exit /b 1
)
python main.py %*
if errorlevel 1 (
  echo.
  echo Cronus Launcher exited with an error.
  echo Check the message above or the log file shown in this window.
  pause
)
endlocal
