````md id="q8v2mk"
# 🚀 Cronus Launcher

**Local Windows launcher and recovery monitor for Roblox accounts.**

Cronus Launcher is a Windows desktop app made for launching Roblox accounts, watching local Roblox processes, and handling reconnect/rejoin recovery automatically.

The app runs completely on your PC. The backend binds to `127.0.0.1`, opens a desktop window with PySide6 when available, and falls back to a normal browser window if needed.

---

# 📌 Status

Cronus Launcher is currently in beta.

It works well for local testing and controlled use, but it should not be treated as a polished public release yet.

Release validation is documented in:

```text
docs/product-runbook.md
````

Current release checks include:

* Compile checks
* Unit tests
* JavaScript syntax checks
* Backend smoke tests
* Single-account live soak tests

---

# 🎯 Scope

Cronus is designed for:

* Local Roblox account launching
* Rejoin and reconnect monitoring
* Runtime queue and status visibility
* Roblox performance controls
* Long-running local farm sessions

Cronus is **not** intended to be:

* A hosted cloud dashboard
* A Roblox executor package
* A guaranteed unattended production service

---

# ✨ Features

* 🔒 Encrypted Roblox cookie storage using Windows DPAPI
* 👤 Account import, reload, validation, and invalid cookie cleanup
* 🚀 Roblox launch controls and private server support
* 🔄 Runtime recovery, reconnect, queue, and status monitoring
* 🎮 FPS limiter, low graphics mode, CPU limiter, and process priority controls
* 🖥️ Roblox window resize and arrange tools
* 🛠️ Roblox install troubleshooting actions

RAM / Roblox Account Manager integration is disabled in this version.

Cronus stores account data locally by itself.

---

# 📦 Requirements

* Windows 10 or newer
* Python 3.11+
* Roblox installed

Install runtime dependencies:

```powershell
python -m pip install -r requirements.txt
```

Install development/test dependencies:

```powershell
python -m pip install -r requirements-dev.txt
```

---

# ▶️ Run

```powershell
.\Run.cmd
```

or:

```powershell
python main.py
```

The backend uses a local API token for protected API requests.

The dashboard automatically receives and sends that token locally.

---

# 🛡️ 24/7 Watchdog

Cronus includes a Windows Task Scheduler watchdog for overnight or unattended runs.

The watchdog checks:

```text
http://127.0.0.1:7777/api/status
```

If the backend stops responding, Cronus can automatically restart the backend runner.

The watchdog runs as the current Windows user instead of `SYSTEM`, so DPAPI-protected cookies still work correctly.

Install or update the watchdog:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\install_watchdog_task.ps1
```

Start the watchdog task:

```powershell
Start-ScheduledTask -TaskName "Cronus Launcher Watchdog"
```

Check watchdog status:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\watchdog_status.ps1
```

Remove the watchdog:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\uninstall_watchdog_task.ps1
```

Watchdog logs:

```text
%LOCALAPPDATA%\Cronus Launcher\data\logs\cronus_watchdog.log
```

---

# 📡 Lua Executor Script

For auto-rejoin support, run this file inside your Roblox executor:

```text
lua/run_in_executor.lua
```

Files under:

```text
lua/internal/
```

are internal backend templates used by Cronus APIs.

Do not paste those internal files directly into the executor unless you are debugging.

The Lua runtime supports:

* Heartbeat signals
* Disconnect detection
* Rejoin requests
* Description updates
* Runtime finished events

Lua scripts do **not** expose Roblox cookies, CSRF tokens, or account passwords.

---

# 💾 Data Location

Runtime data is stored under:

```text
%LOCALAPPDATA%\Cronus Launcher\data
```

Logs are stored under `data\logs`. Generated icon/cache files are stored under `data\cache`.

Cookies are encrypted with Windows DPAPI before being written to disk.

Logs, databases, caches, and runtime files are ignored by Git.

---

# 🧪 Tests

```powershell
python -m compileall -q .
python -m unittest discover -s tests -p test_*.py
```

```
```
