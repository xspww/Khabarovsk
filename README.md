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

## Install

The easy way is the compiled build from the Releases page.
No Python is needed for this option.

1. Open `https://github.com/q0HtHHftAS/Roblox-Auto-Rejoin-windown/releases`.
2. Download `CronusLauncher-<version>.exe` and run it.
3. The portable build `CronusLauncher-<version>-portable.zip` holds the same exe plus the Lua loader.

Windows SmartScreen may warn because the exe is not code signed.
The builds come only from the Releases page above.
Press More info, then Run anyway when the file name matches the release.
Verify the download with `checksums.txt` from the same release when unsure:

```powershell
(Get-FileHash CronusLauncher-1.0.5.exe -Algorithm SHA256).Hash
```

## Update

The launcher checks GitHub Releases quietly after startup and shows a badge in the sidebar when a new version exists.
Click the version number to check by hand.
When an update is downloaded and verified, press Restart to install.
Installing stops Auto Rejoin and closes all Roblox windows first, so it is only offered while the farm is stopped.
Settings: `auto_check_update`, `update_check_interval_hours`, `update_channel` (`stable` or `beta`).
Beta builds are prereleases. Test them before stable when possible.

## Quick Start (from source)

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

## Build the exe

```powershell
python -m pip install -r requirements.txt pyinstaller
python -c "from PIL import Image; Image.open('assets/cronus_icon.png').save('assets/cronus_icon.ico')"
pyinstaller cronus_launcher.spec
```

The output is `dist/CronusLauncher.exe`.
Releases are built the same way by GitHub Actions when a `v*` tag is pushed.
The tag must match `APP_VERSION` in `version.py`.

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
Self updates replace only the exe file. The data folder above is never touched.
