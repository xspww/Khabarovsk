# desktop/ — Windows shell (WebView2 + single-instance + console)

Owns everything the user sees as a *native window*: boot, tray/console,
WebView2 hosting, backend serve loop. Serves `ui/` and `assets/` over
localhost FastAPI (`main.py` → `run_desktop(app, farm)`).

| File | Does what | Connects to |
| ---- | --------- | ----------- |
| `host.py` | Desktop orchestrator (ex-`desktop_host.py` at root): console window, mutex, backend thread, shutdown. `INSTANCE_TOKEN`, `SHUTDOWN_REQUESTED`, `run_desktop()`. | `main.py:179` (`from desktop.host import …`), `backend_host.py`, `webview_window.py`, `instance_guard.py`, `infra/console_activity.py`. |
| `backend_host.py` | Runs uvicorn on `127.0.0.1` + picks a free port (`desktop/constants.py`). | `host.py`, `main.py` via `host.py`. |
| `webview_window.py` | WebView2 bootstrap + embedded window (uses `infra/app_paths`, `core.flog_kv`, `version.py`). | `host.py`, `ui/index.html`. |
| `ui_dashboard.py` | Lazy dashboard shell: `get_html_ui()` serves `ui/`. | `main.py:178`, `api_routes/*` HTML route. Root `ui_dashboard.py` shim deleted. |
| `instance_guard.py` | Single-instance socket + mutex (`path_targets_current_exe`). | `host.py`, `webview_window.py`. |
| `console_output.py` | Console capture (`write_line`, `paint`, VT enable). | `main.py` startup banner, `infra/console_activity.py`, `host.py`. |
| `console_icon.py` | Console/tray icon from `assets/cronus_icon.png`. | `host.py`. |
| `constants.py` | Loopback constants (`DEFAULT_PORT`, host). | `backend_host.py`, `webview_window.py`. |

### Summary

The native Windows shell: single-instance guard, backend serve loop, WebView2 window (with browser fallback), and console/tray presence.
