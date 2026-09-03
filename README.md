# Cronus Launcher

A lightweight local launcher, multi-account manager, and auto-rejoin watchdog for Roblox on Windows.

## Features

- **Multi-Account Management**: Add, organize, and launch multiple Roblox accounts simultaneously.
- **Auto-Rejoin & Crash Recovery**: Detects disconnects, error popups, and client crashes to automatically relaunch accounts.
- **Resource Optimization**: Built-in CPU limiter and graphics performance settings to reduce system usage during multi-instance sessions.
- **Executor Compatibility**: Synchronizes with supported Roblox executors and provides in-game telemetry hooks.
- **Secure Local Storage**: Account cookies and authentication credentials are encrypted locally with Windows DPAPI.

## Requirements

- Windows 10 / 11 (64-bit)
- Python 3.11+
- Roblox installed

## Quick Start

### 1. Clone the repository

```powershell
git clone https://github.com/q0HtHHftAS/Roblox-Auto-Rejoin-windown.git
cd Roblox-Auto-Rejoin-windown
```

### 2. Install dependencies

```powershell
python -m pip install -r requirements.txt
```

### 3. Run

Launch directly using the batch runner:

```powershell
.\Run.cmd
```

Or via Python:

```powershell
python main.py
```

Cronus Launcher will start its local service on `127.0.0.1` and open the desktop dashboard window.

## In-Game Lua Script

For telemetry detection and faster rejoin events, execute the loader script in your Roblox executor:

```text
lua/run_in_executor.lua
```

## Data & Privacy

- Runtime configurations and account states are stored locally at:
  ```text
  %LOCALAPPDATA%\Cronus Launcher\data
  ```
- Account cookies are encrypted with Windows DPAPI and are never committed or sent to external servers.
