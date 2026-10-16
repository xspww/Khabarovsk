# Lua scripts (in-game side)

Use only this file in a Roblox executor:

```text
run_in_executor.lua
```

The `internal/` folder is for Cronus backend templates. You do not need to run those files manually.

| File | Does what | Connects to |
| ---- | --------- | ----------- |
| `run_in_executor.lua` | User entry: sets `getgenv`/`_G` env, boots the internal monitor + status client. Shipped as `Api.lua` in portable zips. | `api_routes/lua_routes.py` (tokens), `release.yml` (packs `run_in_executor.lua` → `Api.lua`), `ui` Lua Manager panel. |
| `internal/rejoin_monitor.lua` | Minified rejoin/heartbeat/teleport monitor (`__CRONUS_*__` placeholders filled by backend). | `runtime/lua_rejoin_events.py`, `runtime/lua_liveness_policy.py`. |
| `internal/account_status_client.lua` | Minified account-status client. | `runtime/lua_identity.py`, `services/lua_session_tokens.py`. |

### Summary

Only `run_in_executor.lua` runs in an executor; everything under `internal/` is a backend-filled template. Portable zips ship the entry file as `Api.lua`.
