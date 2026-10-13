<p align="center">
  <a href="https://github.com/xspww/Khabarovsk">
    <img src="assets/cronus_icon.png" width="120" alt="Cronus Launcher logo" />
  </a>
</p>

<p align="center">The open source multi-account Roblox launcher.</p>

<p align="center">
  <a href="https://github.com/xspww/Khabarovsk/releases"><img alt="release" src="https://img.shields.io/github/v/release/xspww/Khabarovsk?style=flat-square&label=release" /></a>
  <a href="https://github.com/xspww/Khabarovsk/actions/workflows/release.yml"><img alt="Build status" src="https://img.shields.io/github/actions/workflow/status/xspww/Khabarovsk/release.yml?style=flat-square&branch=main" /></a>
  <a href="LICENSE"><img alt="license" src="https://img.shields.io/badge/license-GPLv3-blue?style=flat-square" /></a>
  <img alt="platform" src="https://img.shields.io/badge/platform-Windows-blue?style=flat-square" />
  <img alt="python" src="https://img.shields.io/badge/python-3.11+-blue?style=flat-square" />
</p>

<p align="center">
  <a href="README.md">English</a> |
  <a href="README.th.md">ไทย</a>
</p>

<p align="center">
  <a href="https://github.com/xspww/Khabarovsk/releases"><img src="https://github.com/user-attachments/assets/baf120ff-95ba-4d66-91b3-099c9648a08b" alt="Cronus Launcher Dashboard" width="1024" /></a>
</p>

---

### Installation

```powershell
# Recommended — standalone .exe, no Python required
# 1. Open https://github.com/xspww/Khabarovsk/releases
# 2. Download CronusLauncher-<version>.exe and run it

# Verify the download
(Get-FileHash CronusLauncher-<version>.exe -Algorithm SHA256).Hash
```

> [!TIP]
> Compare the hash output with the SHA256 value published on the release page. Download only from the Releases page above.

#### Portable build

| File | Description |
| ---- | ----------- |
| `CronusLauncher-<version>.exe` | Standalone launcher, stable file name for in-place updates |
| `CronusLauncher-<version>-portable.zip` | `CronusLauncher.exe` + `Api.lua` (executor loader script) |

#### Updates and signatures

Each release artifact is signed with the project release key. The app verifies the signature and SHA256 digest before installing an update. No certificate setup is required.

- In-app updater works with compiled release builds on Windows only and requires internet. It does not apply to source checkouts.
- The `.exe` is not signed with a commercial Windows publisher certificate, so SmartScreen may warn on first run.
- If the `.exe` lives in a protected location such as `Program Files`, the updater requests administrator approval. Without admin access, move it to a user-writable folder.

### Quick Start (from source)

```powershell
# Clone
git clone https://github.com/xspww/Khabarovsk.git
cd Khabarovsk

# Install
python -m pip install -r requirements.txt

# Run (either one)
.\Run.cmd
python main.py
```

The launcher starts the local service on `127.0.0.1` and opens the desktop dashboard.

> [!TIP]
> Requires Windows 10/11 64-bit, Python 3.11+, and Roblox Player installed on the host. Release `.exe` builds need no Python.

### Dashboard

The screenshot above is the home screen: live resource graphs, account search, and a filterable account table.

- **CPU / RAM / VIRTUAL** — live usage with thread count, `used / total GB`, and page-file usage
- **Search accounts... + Set game** — filter by username and assign the target Place ID to selected accounts
- **All / Online / Queued + Reset columns** — quick filters plus one-click column reset
- **Account table** — `# / STATUS / USERNAME / GAME / DESCRIPTION`, empty state reads `No accounts match the current filter`
- **Status pills (bottom-left)** — `Online / Queued / Finished / Captcha / Banned / Suspended / Locked / Invalid` + `Running: 0s` timer
- **Top actions** — `START`, `Close Roblox`, `Add Accounts`, `Reload Cookies`

### Launcher

Queue and launch multiple accounts from one place, with automatic rejoin on disconnect.

- **Add Accounts** — popup-browser sign-in, `Single` or `Multi` (one `user:pass` per line). 2FA, captcha, and passkey are completed by you in the popup; the cookie is captured automatically. Uses installed Chrome/Edge, otherwise downloads Chromium once.
- **Queue / Game** — stage accounts, set the target game/server, then `START` them together
- **Automatic rejoin** — detects disconnects/kicks and rejoins the configured server without manual clicks
- **Close Roblox / Reload Cookies** — kill all clients at once or refresh stored sessions

### Performance

Keep CPU, memory, graphics, and window load under control when running several clients on one machine.

- **Graphic** — FPS limit, low-graphics mode
- **RAM** — memory management for multi-instance sessions
- **Windows** — automatic window layout for many clients
- **CPU** — limiter + process priority applied to Roblox clients

### Tools

Helpers for scripts, executors, and client versions.

- **Lua Manager** — in-game loader script for faster rejoin telemetry. Run in your executor:
  ```text
  lua/run_in_executor.lua
  ```
  Portable zips ship the same file as `Api.lua` next to the `.exe`.
- **Exploits Manager** — tracks executor compatibility against the official Roblox version, with optional auto-relaunch flow (close Roblox + executor, reopen, verify loader, rejoin).
- **Cronus Manager** — checks the official client version, installs/updates the player, auto-updates when enabled, and cleans up old versions.

### Data Directory

Runtime config and account state stay local-first:

```text
%LOCALAPPDATA%\Cronus Launcher\data
```

1. Account cookies are encrypted on the host with Windows DPAPI
2. Cookies are never sent to external servers and never committed to the repo
3. Updates leave the data directory untouched — the launcher only opens the Releases page / replaces the `.exe`

### Requirements

| Component | Requirement |
| --------- | ----------- |
| OS | Windows 10 or Windows 11, 64-bit |
| Runtime (release build) | None — `.exe` from Releases is standalone |
| Runtime (from source) | Python 3.11+ (`pip install -r requirements.txt`) |
| Game client | Roblox Player installed on the host |

### Build from source

```powershell
python -m pip install -r requirements.txt pyinstaller
python -c "from PIL import Image; Image.open('assets/cronus_icon.png').save('assets/cronus_icon.ico')"
pyinstaller cronus_launcher.spec
```

Output: `dist/CronusLauncher.exe`

GitHub Actions builds releases the same way when a `v*` tag is pushed. The tag must match `APP_VERSION` in `version.py`.

### Documentation

For version checks, local API routes, and service layout, start from `main.py`, `api_routes/`, `services/`, and `ui/`.

### Contributing

If you're interested in contributing, open an issue or pull request against `main`. Keep account cookies, local data, and signed artifacts out of commits.

### License

GPLv3 — see [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt).

Corresponding Source for release binaries is the tagged source on the [Releases page](https://github.com/xspww/Khabarovsk/releases) (tag matching `APP_VERSION` in `version.py`).

---

**Join our community** [Releases](https://github.com/xspww/Khabarovsk/releases) | [Issues](https://github.com/xspww/Khabarovsk/issues) | [Actions](https://github.com/xspww/Khabarovsk/actions)
