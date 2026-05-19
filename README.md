# Cronus Launcher

**Windows local Roblox account launcher/rejoin monitor.**

Cronus Launcher is a Windows-only desktop app for launching Roblox accounts, watching local Roblox processes, and recovering accounts when a process exits or needs to be relaunched. It runs on the user's PC, binds its FastAPI backend to `127.0.0.1`, opens a desktop webview when PySide6 is available, and falls back to a browser window when needed.

## Status

Cronus Launcher is currently a beta project. It is suitable for local testing and controlled use, but it should not be treated as a polished public release yet.

Product release validation is documented in `docs/product-runbook.md`. The release gate requires compile/tests, JavaScript syntax checks, product preflight, backend smoke, and a single-account live soak against the real target game.

## Scope

Cronus is designed for:

- Local account launch and rejoin monitoring.
- Process health checks and reconnect recovery.
- Queue and runtime status visibility.
- Local performance controls for Roblox windows.

Cronus is not designed as:

- A hosted cloud dashboard.
- A Roblox executor package.
- A guaranteed unattended production service.

## Features

- Encrypted Roblox cookie storage using Windows DPAPI.
- Account import, reload, validation, and removal of invalid stored cookies.
- Local launch controls for Roblox accounts, private server links, and duplicate-instance cleanup.
- Runtime queue, recovery, reconnect, process liveness, and status reporting.
- FPS limiter, low graphics settings, process priority, CPU limiter, and Roblox window resize/arrange controls.
- Roblox install troubleshooting actions.

Roblox Account Manager / RAM cookie source integration is disabled in this version. Account data is stored by Cronus Launcher itself.

## Requirements

- Windows 10 or newer.
- Python 3.11+ recommended.
- Roblox installed for launch-related features.

Install runtime dependencies:

```powershell
python -m pip install -r requirements.txt
```

Install test dependencies:

```powershell
python -m pip install -r requirements-dev.txt
```

## Run

```powershell
.\Run.cmd
```

or:

```powershell
python main.py
```

The backend binds to `127.0.0.1` and uses a per-process local API token for mutating API requests. The dashboard receives that token from the local HTML page and sends it automatically.

## 24/7 Watchdog

Cronus includes an external Windows Task Scheduler watchdog for unattended overnight runs. It checks `http://127.0.0.1:7777/api/status` and starts the backend-only runner if the local API stops responding. The task runs as the current Windows user, not `SYSTEM`, so DPAPI-protected cookies remain readable.

Install or update the watchdog task:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\install_watchdog_task.ps1
```

Start the task immediately:

```powershell
Start-ScheduledTask -TaskName "Cronus Launcher Watchdog"
```

Check watchdog state and recent logs:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\watchdog_status.ps1
```

Remove the watchdog task:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\uninstall_watchdog_task.ps1
```

Watchdog logs are written to:

```text
%LOCALAPPDATA%\Cronus Launcher\data\cronus_watchdog.log
```

## Lua Executor Script

For normal auto-rejoin, run this single file in your Roblox executor:

```text
lua/run_in_executor.lua
```

The files under `lua/internal/` are backend templates served by Cronus through `/api/lua/rejoin-helper` and `/api/lua/account-module`. Do not paste those internal files into the executor unless you are debugging.

The Lua contract wraps `/api/lua/rejoin-event` and supports safe runtime signals such as heartbeat, disconnect, rejoin request, description update, and mark-finished. It deliberately does not expose Roblox cookies, CSRF tokens, or RAM passwords to Lua.

## Data Location

Runtime data currently stays under the existing compatibility folder:

```text
%LOCALAPPDATA%\Cronus Launcher\data
```

Cookies are encrypted with Windows DPAPI before being written to disk. Local runtime data, logs, databases, and caches are intentionally ignored by Git.

## Tests

```powershell
python -m compileall -q .
python -m unittest discover -s tests -p test_*.py
```
