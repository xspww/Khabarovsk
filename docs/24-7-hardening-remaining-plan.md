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

## Remaining Work

1. Runtime validation
   - Idle soak with the backend/farm stopped has been run successfully; keep it
     in the release gate for regressions.
   - Run a controlled live smoke test with operator approval to confirm popup
     and log evidence still detect disconnects after removing retry sleeps.
     This requires a real launch target (`game_place_id`, private server URL,
     per-account place id, or VIP link).

2. Product release gate
   - Run `python .\ops\product_preflight.py --json` before any live smoke.
   - Follow `docs/product-runbook.md` for backend smoke, single-account soak,
     diagnostics, and release packaging.

## Required Verification For Each Phase

- `python -m compileall -q .`
- `python -m unittest discover -s tests -p test_*.py`
- `node --check` for all UI JavaScript files when UI files change.
- For runtime-sensitive phases, run an idle soak and a controlled live smoke
  test only after explicit operator approval.
