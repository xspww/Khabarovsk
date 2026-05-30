# Cronus Launcher

Local Windows launcher and recovery monitor for Roblox accounts.

This repository runs from source. No `.exe` build is required.

## Requirements

- Windows 10 or newer
- Python 3.11 or newer
- Roblox installed

## Install

Open PowerShell in the project folder:

```powershell
cd C:\path\to\Roblox-Auto-Rejoin-windows
python -m pip install -r requirements.txt
```

## Run

```powershell
.\Run.cmd
```

or:

```powershell
python main.py
```

Cronus starts a local backend on `127.0.0.1` and opens the dashboard window.

## Lua Script

For Lua-based rejoin/status detection, run this script in your Roblox executor:

```text
lua/run_in_executor.lua
```

Do not paste files from `lua/internal/` directly unless you are debugging.

## Local Data

Runtime data is stored here:

```text
%LOCALAPPDATA%\Cronus Launcher\data
```

Account cookies are stored locally and encrypted with Windows DPAPI.

## Clean Generated Files

To remove local cache/build files:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\clean_generated.ps1
```

This removes generated files such as `__pycache__`, `.pytest_cache`, `build`, and `dist`.

## Troubleshooting

If dependencies are missing, run:

```powershell
python -m pip install -r requirements.txt
```

If the app does not open, run it from PowerShell so the error is visible:

```powershell
python main.py
```
