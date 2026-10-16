# domain/ — Pure models (no I/O, no singletons)

Dataclasses, enums, and pure predicates. Safe to import from anywhere,
including `tests/` without Windows APIs or network.

| File | Does what | Connects to |
| ---- | --------- | ----------- |
| `account_model.py` | `Account`, `ServerType`, `STATE_META`, `account_launchable()`, `account_launch_block_reason()`, ban/lock/suspend/recovery predicates. | Re-exported by `core.py`. Used by `runtime/*` (state machine, workers), `services/auth_gate.py`, `services/roblox_launch_service.py`, `api_routes/settings_state.py`, `api_routes/account_records.py`. Tested by `tests/test_status_priority.py`. |
| `states.py` | `AccountState` / `RuntimeState`, `LIFECYCLE_STATE`, `PUBLIC_TO_RUNTIME_STATE` maps, `public_state_for_runtime()` / `runtime_state_for_public()`. | Re-exported by `core.py`. Used by `config/store.py`, `runtime/*`, `desktop/host.py` status pills. |
| `runtime_models.py` | Runtime-side dataclasses (launch attempts, queue entries, health snapshots). | Used by `runtime/runtime_store.py`, `runtime/runtime_view_model.py`, `runtime/telemetry_view.py`. |
| `runtime_lifecycle.py` | Lifecycle enums/helpers shared by farm and workers. | Used by `runtime/farm_lifecycle.py`, `runtime/account_runtime_controller.py`. |
| `session_identity.py` | Session / launch-nonce identity (binds Lua heartbeat to account). | Used by `runtime/lua_identity.py`, `services/lua_session_tokens.py`, `api_routes/lua_routes.py`. |
| `games.py` | Multi-game library: named Roblox experiences accounts can join. | Served by `api_routes/games_routes.py`, consumed by `ui/runtime/gamesManager.js`. |
| `roblox_private_servers.py` | VIP/private-server resolve + verdict cache (uses `infra/net_tls`). | Used by `services/private_server_preflight.py`, `services/vip_tracker.py`. |

### Summary

Pure dataclasses, enums, and predicates with no I/O — safe to import from anywhere, including hermetic unit tests.
