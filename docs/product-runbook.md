# Product Runbook

Cronus is not ready for a public product release until this runbook passes on
the target machine with the real target game configured.

## Release Gate

Run the non-live release gate before opening Roblox:

```powershell
python .\ops\release_gate.py --skip-idle-soak --json
```

Run the full local release gate when you are ready to let the backend start and
exercise the idle control plane:

```powershell
python .\ops\release_gate.py --json
```

The gate runs Python compile checks, unit tests, UI JavaScript syntax checks,
product preflight, and the optional idle backend soak. It must return `ok: true`
before live smoke.

## Current Release Evidence

Last local gate run on 2026-05-20:

- `python .\ops\release_gate.py --json`: pass, `ok=true`, `fail_count=0`, `warn_count=0`.
- Unit suite inside the gate: 379 tests passed.
- UI JavaScript syntax inside the gate: 10 files passed `node --check`.
- Product preflight inside the gate: pass; watchdog task points to this checkout.
- Idle control-plane soak inside the gate: pass, 35.1 seconds, 12 requests, 0 errors, 0 fatal log hits.
- `powershell -NoProfile -ExecutionPolicy Bypass -Command "& { & '.\ops\watchdog_status.ps1' | ConvertTo-Json -Depth 6 -Compress }"`: pass for `TaskInstalled=true`, `ProjectRootMatches=true`, `WatchdogScriptExists=true`.
- `powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\package_release.ps1`: pass; latest zip contained `release-manifest.json` and excluded `AccountData.json`, `cronus_rt_instance.json`, and `cronus_watchdog.log`.

Live checks run on 2026-05-20:

- `fvdsfv12r23r`: blocked before launch by `CAPTCHA required`; no Roblox
  process launched and cleanup left no orphan.
- `savavzcv1241235`: 15 minute live soak passed; summary
  `cronus_rt1_instances/soak-savavzcv1241235-summary.json` returned `ok=true`,
  939 seconds observed, no failures, no fatal hits, no orphan processes, no
  runtime warnings.
- `asfdvdasfvsddfv`: 15 minute live soak passed; summary
  `cronus_rt1_instances/soak-asfdvdasfvsddfv-summary.json` returned `ok=true`,
  941 seconds observed, no failures, no fatal hits, no orphan processes, no
  runtime warnings.
- Controlled close/rejoin on `asfdvdasfvsddfv`: `POST
  /api/account/asfdvdasfvsddfv/kill` killed old PID 10116, runtime relaunched
  the account to PID 7740, final state was `IN_GAME`, liveness was `alive`,
  process proof was `strong`, and the new session stayed stable for 61 seconds.
  Evidence:
  `cronus_rt1_instances/controlled-rejoin-asfdvdasfvsddfv-summary.json`.

Remaining live release work is account hygiene plus scale-up: clear the
CAPTCHA-blocked account, then run concurrent multi-account validation starting
at 2 accounts and increasing to the target farm size.

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
python .\ops\soak_monitor.py --account "<username>" --duration-seconds 900 --max-wall-seconds 1800 --summary-json .\cronus_rt1_instances\soak-summary.json
```

Pass criteria:

- account reaches `IN_GAME`
- exactly the expected Roblox process count exists
- no fatal log pattern appears
- runtime health has no warnings for the stable window
- stop/cleanup leaves no orphaned Roblox process for the tested account
- summary JSON returns `ok: true`

Live validation should move in phases:

1. Idle control-plane gate with no Roblox launch for at least 35 seconds.
2. One account for 15 minutes with no injected failure.
3. One account with a controlled Roblox close; the account must rejoin or enter bounded cooldown.
4. Multi-account scale-up: start at 2 accounts, then 5, then the target farm size.

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
git metadata, and Python cache files. It also writes `release-manifest.json`
with the source commit and runtime-data exclusion policy.

## Verification Gate

Minimum gate before product release:

```powershell
python .\ops\release_gate.py --json
```

Then run the single-account live soak above with the real target game.
