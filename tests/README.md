# tests/ — Unit tests (no Windows, no network, no browser)

Run: `python -m unittest discover -s tests` (CI: `compileall` then this).
No `__init__.py`; each file is hermetic with pure dicts/fixtures.
Covers `domain/` + `runtime/` policy helpers — `services/` live Roblox
calls and `desktop/` WebView2 are intentionally out of scope.

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
