@echo off
setlocal
title Cronus Launcher Console
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set CRONUS_CONSOLE_ACTIVITY=1
set CRONUS_CONSOLE_COLOR=1
cd /d "%~dp0"
rem Pick a working Python 3.11+ interpreter. A broken `python` on PATH
rem (missing stdlib) must not be used, otherwise startup dies with
rem "Fatal Python error: Failed to import encodings module".
set "CRONUS_PY="
set "CRONUS_PY_FALLBACK="
call :try_python python
if not defined CRONUS_PY call :try_python py -3.11
if not defined CRONUS_PY call :try_python py -3
if not defined CRONUS_PY set "CRONUS_PY=%CRONUS_PY_FALLBACK%"

if not defined CRONUS_PY (
  echo.
  echo Python 3.11+ was not found, or the installed Python is broken.
  echo Install Python 3.11+ from https://www.python.org/downloads/
  echo and enable "Add python.exe to PATH", then run this file again.
  echo.
  pause
  exit /b 1
)

%CRONUS_PY% main.py %*
if errorlevel 1 (
  echo.
  echo Cronus Launcher exited with an error.
  echo Check the message above or the log file shown in this window.
  pause
)
endlocal
exit /b 0

:try_python
rem %~1 %~2 is the candidate command. Accept it only when it starts, loads
rem its standard library, and is Python 3.11+. Prefer an interpreter that
rem already has the launcher's GUI dependency installed; otherwise keep the
rem first valid one as a fallback so main.py can print its dependency report.
%~1 %~2 -c "import sys, encodings; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 exit /b 0
if not defined CRONUS_PY_FALLBACK set "CRONUS_PY_FALLBACK=%~1 %~2"
%~1 %~2 -c "import PySide6" >nul 2>nul
if errorlevel 1 exit /b 0
set "CRONUS_PY=%~1 %~2"
exit /b 0
