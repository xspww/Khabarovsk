# api_routes/ — Localhost HTTP boundary (FastAPI)

Thin adapters: parse HTTP → call `runtime/`/`services/`/`config/`, return
JSON. No business logic here. Wired by `register_api_routes(app, ctx)`
(`__init__.py`), built from `ApiContext` (`context.py`) in `main.py:578-594`.
UI calls these from `ui/runtime/api.js`.

| File | Routes | Connects to |
| ---- | ------ | ----------- |
| `context.py` | `ApiContext` dataclass (cfg, farm, installer, updater, tracker, guards). | `main.py`, every `*_routes.py`. |
| `auth.py` | Token middleware + auth endpoints. | `services/auth_gate.py`. |
| `accounts_routes.py` | Accounts CRUD, place lookup, cookie reload (uses `infra/net_tls`). | `services/account_hybrid.py`, `services/cookie_service.py`, `services/credential_check.py`. |
| `account_records.py` | Record-level block预览 (`banned/cookie_invalid/…`). | `domain/account_model.py`, tested via `tests/test_status_priority.py`. |
| `runtime_routes.py` | START/STOP, queue, farm control. | `runtime/farm.py`, `runtime/start_readiness.py`. |
| `performance_routes.py` | FPS/graphics, CPU limiter, RAM, windows (uses `services/performance_settings`). | `services/cpu_limiter.py`, `services/ram_cleanup.py`, `services/window_control.py`. |
| `config_routes.py` | Config get/set (uses `services/performance_settings` for gfx keys). | `config/store.py`, `config/validation.py`. |
| `settings_state.py` | Settings dict-in/out adapter. Tested without HTTP. | `domain/account_model.py`, `tests/test_settings_state.py`. |
| `games_routes.py` | Multi-game library CRUD. | `domain/games.py`, `ui/runtime/gamesManager.js`. |
| `lua_routes.py` | Lua helper + session-token endpoints (uses `infra/app_paths`). | `services/lua_session_tokens.py`, `lua/run_in_executor.lua`. |
| `manual_login_routes.py` | Manual Login Single/Multi. | `services/manual_login_service.py`. |
| `discord_routes.py` | Presence toggle/status. | `services/discord_presence.py`. |
| `system_routes.py` | Health/version/Cronus Manager/update (uses `infra/app_paths`, `version.py`). | `services/roblox_install_manager.py`, `services/app_updater.py`, `services/app_version_check.py`, `runtime/diagnostic_bundle.py`. |
| `idempotency.py` | Idempotency-Key guard for retries. | `runtime_routes.py`, `accounts_routes.py`. |
