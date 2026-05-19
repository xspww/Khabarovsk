# Product Runbook

Cronus is not ready for a public product release until this runbook passes on
the target machine with the real target game configured.

## Preflight

Run this before opening Roblox:

```powershell
python .\ops\product_preflight.py --json
```

Hard blockers:

- no configured account
- duplicate account username
- missing stored cookie
- missing launch target
- backend port already occupied by an unexpected process

Launch target means one of:

- global `game_place_id`
- global `game_private_server_url`
- per-account `place_id`
- per-account VIP link

## Backend Smoke

Run backend only:

```powershell
python .\ops\run_backend.py --host 127.0.0.1 --port 7777 --log-level warning
```

Then check:

```powershell
Invoke-RestMethod http://127.0.0.1:7777/api/status
Invoke-RestMethod http://127.0.0.1:7777/api/farm/health
```

The farm should still be stopped before live smoke.

## Single-Account Live Soak

Use one account first. Do not start the whole farm for release validation.

```powershell
python .\ops\soak_monitor.py --account "<username>" --duration-seconds 900 --max-wall-seconds 1800
```

Pass criteria:

- account reaches `IN_GAME`
- exactly the expected Roblox process count exists
- no fatal log pattern appears
- runtime health has no warnings for the stable window
- stop/cleanup leaves no orphaned Roblox process for the tested account

## Diagnostics

Runtime diagnostics are available only with the local instance token:

```powershell
Invoke-RestMethod http://127.0.0.1:7777/api/runtime/diagnostics -Headers @{"X-Cronus-Token"="<instance token>"}
```

Diagnostics are redacted and should not expose `.ROBLOSECURITY`, private server
link codes, backend tokens, or launch nonces.

## Release Package

Create a source release zip after tests and live smoke pass:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\package_release.ps1
```

The package script excludes runtime user data, logs, cookies, local databases,
git metadata, and Python cache files.

## Verification Gate

Minimum gate before product release:

```powershell
python -m compileall -q .
python -m pytest -q
Get-ChildItem .\ui -Recurse -Filter *.js | ForEach-Object { node --check $_.FullName }
python .\ops\product_preflight.py --json
```

Then run the single-account live soak above with the real target game.
