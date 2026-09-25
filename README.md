<p align="center">
  <img src="assets/cronus_icon.png" width="120" alt="Cronus Launcher logo" />
</p>

<h1 align="center">Cronus Launcher</h1>

<p align="center">Roblox account manager with auto-rejoin.</p>

<p align="center">
  <a href="https://github.com/xspww/Khabarovsk/releases"><img src="https://img.shields.io/github/v/release/xspww/Khabarovsk?label=release" alt="release" /></a>
  <a href="https://github.com/xspww/Khabarovsk/actions/workflows/release.yml"><img src="https://github.com/xspww/Khabarovsk/actions/workflows/release.yml/badge.svg" alt="build" /></a>
  <img src="https://img.shields.io/badge/platform-Windows-blue" alt="platform" />
  <img src="https://img.shields.io/badge/python-3.11+-blue" alt="python" />
</p>

<p align="center">
  English | <a href="README.th.md">ไทย</a>
</p>

<p align="center">
  <a href="#functions">Features</a> •
  <a href="#requirements">Requirements</a> •
  <a href="#install">Install</a> •
  <a href="#quick-start-from-source">Quick Start</a> •
  <a href="#build-the-exe">Build</a> •
  <a href="#in-game-lua-script">Lua</a> •
  <a href="#data-and-privacy">Privacy</a> •
  <a href="#intention">Intention</a>
</p>

<img width="1280" height="820" alt="r1" src="https://github.com/user-attachments/assets/84c32026-0316-46bb-999b-4c4b5f6b9698" />

## Functions

The launcher provides the functions below:

- 👥 Manage more than one account: like a Roblox account manager.
- 🔄 Automatic rejoin: the program rejoins the game for you, even while you sleep.
- ⚡ Reduce system load: you can limit CPU use and lower graphics load during sessions with more than one instance.
- 🧩 Work with executors: restarts the Roblox script executor for you when the executor updates, and pauses rejoining when Roblox itself updates.
- 🔒 Protect secrets: the launcher encrypts account cookies and credentials on the host with Windows DPAPI.

## Requirements

The host must meet the requirements below:

- Run Windows 10 or Windows 11 64-bit.
- Run Python 3.11 or later.
- Have Roblox installed on the host.

## Install

The easy way is the compiled build from the Releases page.
No Python is needed for this option.

1. Open `https://github.com/xspww/Khabarovsk/releases`.
2. Download `CronusLauncher-<version>.exe` and run it.
3. The portable build `CronusLauncher-<version>-portable.zip` holds the same exe plus the Lua loader.

The in-app updater supports compiled release builds on Windows and needs an internet connection. If the exe is in a protected folder such as `Program Files`, the updater requests administrator approval before writing. If the account has no administrator access, move the exe to a user-writable folder. Source runs do not self-update.

Release builds made by the current workflow require a code-signing certificate trusted by Windows. Store it in the `CRONUS_SIGNING_CERT_PFX_BASE64` and `CRONUS_SIGNING_CERT_PASSWORD` GitHub Actions secrets, and keep the same signing key for later releases. The updater checks the Authenticode signature and pinned publisher key before installing. When Windows SmartScreen warns, download only from the Releases page above. A SHA256 check detects incomplete or corrupted downloads; it does not replace publisher signature verification:

```powershell
(Get-FileHash CronusLauncher-2.5.1.exe -Algorithm SHA256).Hash
```

## Quick Start (from source)

Complete the steps below:

1. Clone the repository:

```powershell
git clone https://github.com/xspww/Khabarovsk.git
cd Khabarovsk
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
When a new version is out, the launcher shows a button that opens the Releases page so you can download the new exe yourself. The data folder above is never touched.

## Intention
This program was built for AFK farmers to save costs. It is open source — feel free to modify it however you like.
