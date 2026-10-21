# tests/ — Unit tests (no Windows, no network, no browser)

Run: `python -m unittest discover -s tests` (CI: `compileall` then this).
No `__init__.py`; each file is hermetic with pure dicts/fixtures.
Covers pure helpers across `domain/` + `runtime/` (plus pure `services/`/`desktop/` fragments) — live Roblox
calls and WebView2 windows stay out of scope.

| File | Covers | Connects to |
| ---- | ------ | ----------- |
| `test_status_priority.py` | Status priority + embedded identity + hold isolation + record previews. | `domain/account_model.py`, `api_routes/account_records.py` (`from core import account_launch_block_reason`). |
| `test_start_readiness.py` | START readiness decision table. | `runtime/start_readiness.py`. |
| `test_settings_state.py` | Settings dict adapter. | `api_routes/settings_state.py`. |
| `test_recovery_policy.py` | Single-owner recovery helpers. | `runtime/recovery_policy.py`. |
| `test_process_binding.py` | PID↔account binding predicates (pure dicts). | `services/roblox_processes.py`. |
| `test_process_proof_policy.py` | Single-owner resource scorer. | `services/process_proof_policy.py`. |
| `test_roblox_windows.py` | Window-enumeration rules (no Win32). | `services/roblox_windows.py`. |
| `test_manual_login.py` | Manual Login sessions (no browser/network). | `services/manual_login_service.py`. |
| `test_lua_hardening.py` | Lua helper path fragility. | `lua/run_in_executor.lua`, `api_routes/lua_routes.py`. |
| `test_cookie_verdict.py` | Cookie-verdict mapping (pure, no store). | `services/roblox_validation.py`. |
| `test_desktop_executors.py` | Stdlib executor lazy-import regression (Qt/shiboken). | `desktop/webview_window.py`. |
| `test_install_cleanup.py` | Old-version cleanup (filesystem, no Roblox). | `services/roblox_install_manager.py`. |
| `test_launch_target.py` | Launch-target builder branches. | `services/roblox_launch_service.py`. |
| `test_process_backend_wiring.py` | Binder seam: every bound name resolves (AST + import). | `services/process_backend.py`. |
| `test_recovery_seam.py` | Recovery seam narrowness (router works through 7 actions). | `runtime/recovery_seam.py`, `runtime/recovery_signal_router.py`. |
| `test_vip_links.py` | VIP link helpers (per-place filter, shared-direct). | `domain/roblox_private_servers.py`. |
| `test_game_switch.py` | Live game switching: target diffing + rejoin/stop on assignment/config/game edits. | `services/game_switch.py`, `api_routes/games_routes.py`, `api_routes/config_routes.py`. |
| `test_farm_runtime_reset.py` | Farm stop/Finished runtime reset ordering (kill → reset → bump revision). | `runtime/farm.py`. |

### Summary

Hermetic unit tests (`python -m unittest discover -s tests`, also run in CI): pure dicts/fixtures over domain/runtime logic plus pure services/desktop fragments — no Windows, network, or browser.
