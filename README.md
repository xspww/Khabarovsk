# Cronus Launcher

Cronus Launcher is a local launcher for Roblox on Windows.
The launcher manages more than one account and rejoins games after disconnects.
A watchdog (program that watches for faults and restarts work) provides the rejoin function.

## Functions

The launcher provides the functions below:

- Manage more than one account: you can add, organize, and launch more than one Roblox account at the same time.
- Rejoin after faults: if Roblox disconnects, shows an error popup, or crashes, the launcher restarts the account.
- Reduce system load: you can limit CPU use and lower graphics load during sessions with more than one instance.
- Work with executors: the launcher works with supported Roblox executors and sends telemetry data from the game.
- Protect secrets: the launcher encrypts account cookies and credentials on the host with Windows DPAPI.

## Requirements

The host must meet the requirements below:

- Run Windows 10 or Windows 11 64-bit.
- Run Python 3.11 or later.
- Have Roblox installed on the host.

## Quick Start

Complete the steps below:

1. Clone the repository:

```powershell
git clone https://github.com/q0HtHHftAS/Roblox-Auto-Rejoin-windown.git
cd Roblox-Auto-Rejoin-windown
```

2. Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

3. Start the launcher with one of the methods below.
To use the batch runner, run the command below:

```powershell
.\Run.cmd
```

To use Python, run the command below:

```powershell
python main.py
```

The launcher starts the local service on 127.0.0.1 and opens the desktop dashboard window.

## In-Game Lua Script

A loader script (small program that loads telemetry code into the game) speeds up rejoin events.
To send telemetry data and rejoin faster, run the file below in the Roblox executor:

```text
lua/run_in_executor.lua
```

## Data and Privacy

The launcher stores runtime configuration and account state on the host at the path below:

```text
%LOCALAPPDATA%\Cronus Launcher\data
```

The launcher encrypts account cookies with Windows DPAPI on the host.
The launcher never sends cookies to external servers.
The launcher never commits cookies to the repository.
