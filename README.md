# Argus Launcher

Argus Launcher is a Windows-only local desktop control panel for managing Roblox account launch workflows. It runs a FastAPI backend on `127.0.0.1`, opens a desktop webview when PySide6 is available, and falls back to a browser window when needed.

## Features

- Encrypted Roblox cookie storage using Windows DPAPI.
- Account import, reload, validation, and removal of invalid stored cookies.
- Local launch controls for Roblox accounts, private server links, and duplicate-instance cleanup.
- Runtime queue, recovery, reconnect, process liveness, and status reporting.
- FPS limiter, low graphics settings, process priority, CPU limiter, and Roblox window resize/arrange controls.
- Roblox install troubleshooting actions.

Roblox Account Manager / RAM cookie source integration is disabled in this version. Account data is stored by Argus Launcher itself.

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

## Lua Auto Rejoin Sensor

Argus includes an optional Roblox executor sensor for faster disconnect/error-code recovery. Run the loader script in the Roblox executor:

```text
lua/argus_rejoin_loader.lua
```

Use the loader, not `lua/argus_rejoin_helper.lua`. The loader downloads the current helper from the running Argus backend, so fixes and token changes are picked up automatically.

Expected executor log after running the loader:

```text
[ArgusRejoinLoader] helper compiled
[ArgusRejoin] ready version=1.7.0
[ArgusRejoin] post loaded ok status=200 accepted=true
```

Some executors do not preserve custom POST headers. In that case the helper falls back to a local GET transport. This is still valid if the log shows:

```text
[ArgusRejoin] get fallback loaded ok status=200 accepted=true
```

When Roblox shows an error code such as `267`, `268`, `273`, `277`, or `279`, the helper reports it to Argus. Argus then kills the bound Roblox process and relaunches the account through the normal recovery path.

After a successful rejoin, the new Roblox process does not automatically inherit the previous in-game Lua script unless the executor has auto-exec configured. Run the loader again, or configure the executor to auto-run the loader for each new Roblox process.

## Data Location

Runtime data is stored under:

```text
%LOCALAPPDATA%\Argus Launcher\data
```

Cookies are encrypted with Windows DPAPI before being written to disk. Local runtime data, logs, databases, and caches are intentionally ignored by Git.

## Tests

```powershell
python -m compileall -q .
python -m unittest discover -s tests -p test_*.py
```
