# infra/ — Foundation layer (no app dependencies)

Lowest layer. Everything may import from here; this package must **not**
import from `config/`, `domain/`, `services/`, `runtime/`, `api_routes/`, `desktop/`.

| File | Does what | Connects to |
| ---- | --------- | ----------- |
| `app_paths.py` | Single source for `APP_NAME`, `APP_ROOT_DIR`, `APP_DATA_DIR` (`%LOCALAPPDATA%/Cronus Launcher/data`), `LOG_DIR`, `resource_path()` (handles `sys._MEIPASS` in frozen exe). | Imported by ~18 modules: `core.py`, `config/store.py`, `infra/core_logging.py`, `desktop/*`, `services/*`, `api_routes/*`. `cronus_launcher.spec` relies on `resource_path()` for `assets/`, `ui/`, `lua/` bundling. |
| `core_logging.py` | `flog` / `flog_kv` / `flog_struct` + log-file rotation via `services.safe_rotating_log`. Moves legacy log artifacts into `LOG_DIR`. | Re-exported by `core.py` (public facade). Used by `runtime/event_bus.py`, `runtime/state_machine.py`, `runtime/smart_queue.py`. |
| `console_activity.py` | In-memory console state: active accounts, captcha/verify holds, Lua-liveness flag. Formats lines for `desktop/host.py` console window. | Used by `infra/core_logging.py` (emit), `desktop/host.py` (`format_console_line`), `runtime/farm.py` (`set_total_accounts`, `set_lua_liveness_required`). |
| `net_tls.py` | One shared TLS context: `certifi` bundle + Windows CA/ROOT store. `default_context()` for every outbound HTTPS call. | Imported by 10 modules: `services/roblox_hybrid.py`, `services/roblox_install_manager.py`, `services/credential_check.py`, `services/network_monitor.py`, `services/executor_compatibility.py`, `services/app_version_check.py`, `services/app_updater.py`, `domain/roblox_private_servers.py`, `api_routes/accounts_routes.py`, `runtime/account_worker.py`. |

### Summary

Foundation only: paths, logging, console state, and one shared TLS context. Nothing here may import app layers, so every package above can depend on it safely.
