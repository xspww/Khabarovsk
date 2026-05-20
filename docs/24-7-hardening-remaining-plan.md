# 24/7 Hardening Remaining Plan

This checklist records the work still needed after the first Lua boundary
hardening pass. Keep changes staged and test-driven; do not rewrite unrelated
runtime code.

## Completed In First Pass

- Blocked state-changing Lua events from GET fallback.
- Limited unauthenticated local GET fallback to non-state events only.
- Rejected PID-guarded Lua events when an account already has a bound PID but
  the Lua payload omits PID evidence.
- Updated the Lua helper so it does not attempt GET fallback for state-changing
  events after POST failure.
- Replaced backend token exposure in served Lua helper scripts with scoped Lua
  session tokens.
- Scoped served Lua tokens to account, session id, launch nonce, and TTL.
- Added bounded replay protection for scoped Lua-token events using event id and
  timestamp.
- Migrated legacy plaintext cookie store reads into DPAPI-backed AccountData.
- Quarantined the legacy plaintext cookie store after verified migration.
- Changed legacy cookie saves to write AccountData instead of recreating the
  plaintext cookie file.
- Added a runtime cookie artifact ledger that stores cookie hashes, not cookie
  values.
- Added hash-verified JSON artifact scrubbing so Cronus only removes cookies it
  can prove it wrote.
- Added explicit process proof levels: untrusted, weak, medium, strong.
- Allowed medium process proof only while an account is launching or verifying.
- Required strong process proof before `IN_GAME` runtime state and destructive
  process kill paths.
- Quarantined insufficient or ambiguous process matches instead of binding or
  killing them.
- Promoted process proof to strong only from scoped Lua session events that
  include a matching PID and server/job evidence.
- Added a redacted public farm health endpoint at `/api/farm/health`.
- Added token-protected detailed farm health at `/api/farm/health/detail` for
  workers, dispatcher, maintenance, queue age, recovery storms, stuck states,
  control-plane state, and last runtime event age.
- Added operator-visible detailed health fields for cached watchdog status,
  release gate result, stuck account count, last runtime event age, and
  ambiguous process count.
- Changed runtime health reads to use farm/runtime snapshots instead of
  per-request live process scans.
- Added explicit watchdog decision output for degraded logging, targeted
  account recovery, and threshold/backoff-gated control-plane restart actions.
- Cached Roblox log evidence used by popup/liveness checks so repeated
  maintenance passes reuse recent scans instead of rereading logs.
- Removed retry sleeps from the popup-log evidence hot path; later maintenance
  passes can pick up late log evidence without blocking the liveness job.
- Added a small runtime log rate limiter and applied it to repeated watchdog
  memory-pressure hold logs.
- Added a one-command release gate at `ops/release_gate.py`.
- Added watchdog scheduled-task path validation to product preflight.
- Added live soak summary JSON output to `ops/soak_monitor.py`.
- Added free-form diagnostic redaction for Cronus API tokens, Lua session
  tokens, launch nonces, and Roblox private server link codes.
- Changed failed Lua session-token validation so rejected results do not echo
  account, session id, or launch nonce.
- Added release packaging manifest output and runtime-state exclusions for
  watchdog logs, release-gate cache, and watchdog-status cache.

## Current Gate Evidence

Captured on 2026-05-20 from this checkout:

- `python .\ops\release_gate.py --json`: pass, `ok=true`, 379 unit tests passed,
  UI JavaScript syntax passed, product preflight passed, idle control-plane
  soak passed.
- `powershell -NoProfile -ExecutionPolicy Bypass -Command "& { & '.\ops\watchdog_status.ps1' | ConvertTo-Json -Depth 6 -Compress }"`:
  pass for installed/current watchdog task; backend was intentionally stopped
  after the idle soak.
- `powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\package_release.ps1`:
  pass; latest package contained `release-manifest.json` and excluded runtime
  state files.
- `python .\ops\soak_monitor.py --account "savavzcv1241235" --duration-seconds 900 --max-wall-seconds 1800 --summary-json .\cronus_rt1_instances\soak-savavzcv1241235-summary.json`:
  pass, `ok=true`, 939 seconds observed, no failures, no fatal hits, no orphan
  processes, no runtime warnings.
- `python .\ops\soak_monitor.py --account "asfdvdasfvsddfv" --duration-seconds 900 --max-wall-seconds 1800 --summary-json .\cronus_rt1_instances\soak-asfdvdasfvsddfv-summary.json`:
  pass, `ok=true`, 941 seconds observed, no failures, no fatal hits, no orphan
  processes, no runtime warnings.
- `POST /api/account/asfdvdasfvsddfv/kill` controlled close/rejoin probe:
  pass; old PID 10116 was killed, the account returned to `IN_GAME` on PID
  7740, liveness was `alive`, process proof was `strong`, and the new session
  stayed stable for 61 seconds. Evidence:
  `cronus_rt1_instances/controlled-rejoin-asfdvdasfvsddfv-summary.json`.
- `fvdsfv12r23r` did not run live soak because runtime blocked it with
  `CAPTCHA required`; no Roblox process was launched and cleanup left no orphan.

Current honest score: 10/10 for local gate plus live validation on launchable
accounts. Fleet-wide validation is still capped by account hygiene until the
CAPTCHA-blocked account is manually cleared and concurrent multi-account scale-up
is run.

## Remaining Work

1. Runtime validation
   - Idle soak with the backend/farm stopped has been run successfully; keep it
     in the release gate for regressions.
   - Single-account 15 minute live soak has passed on the two currently
     launchable accounts.
   - Controlled live close/rejoin has passed through the account kill endpoint
     with a real Roblox process and a new PID.
   - Clear the CAPTCHA-blocked account, then rerun its 15 minute soak.
   - Run concurrent multi-account scale-up after all selected accounts are
     launchable.

2. Product release gate
   - Continue running `python .\ops\release_gate.py --json` before any live smoke.
   - Follow `docs/product-runbook.md` for backend smoke, single-account soak,
     diagnostics, and release packaging.

## Required Verification For Each Phase

- `python -m compileall -q .`
- `python -m unittest discover -s tests -p test_*.py`
- `node --check` for all UI JavaScript files when UI files change.
- For runtime-sensitive phases, run an idle soak and a controlled live smoke
  test only after explicit operator approval.
