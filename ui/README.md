# ui/ — Dashboard frontend (no build step, plain ESM + CSS)

Served by `desktop/ui_dashboard.py` at `/` and `/ui/`, with `no-store`
headers (`main.py` middleware). Calls `api_routes/*` via `runtime/api.js`.
No `__init__.py` (web assets, not a Python package).

| Path | Does what | Connects to |
| ---- | --------- | ----------- |
| `index.html` | Single-page shell (START btn, nav, token/version placeholders). | `app.js`, `desktop/webview_window.py` hosts it. |
| `app.js` | ESM entry → `components/customSelect.js`, `app/dashboard.js`. |  |
| `dashboard.css` | Legacy bundle (real styles live in `styles/`). |  |
| `app/dashboard.js` | Main dashboard logic (search, filters, START). | `runtime/api.js`, `components/accountsTable.js`. |
| `components/accountsTable.js` | `#/STATUS/USERNAME/GAME/DESCRIPTION` table + empty state. | `api_routes/accounts_routes.py`. |
| `components/customSelect.js` | Custom select widget. |  |
| `components/feedback.js` | Toasts/modals. |  |
| `components/icons.js` | Icon set. |  |
| `events/bindings.js` | DOM event bindings. |  |
| `panels/settingsPanels.js` | Settings panels. | `api_routes/config_routes.py`, `api_routes/settings_state.py`. |
| `panels/ramExtra.js` | Extra RAM panel. | `api_routes/performance_routes.py`. |
| `runtime/api.js` | Fetch client for all routes. | `api_routes/*`. |
| `runtime/status.js` | Status pills (Online/Queued/Finished/Captcha/Banned/… + Running timer). | `runtime/runtime_view_model.py` via `runtime_routes.py`. |
| `runtime/accountStatus.js` | Per-account status chips. |  |
| `runtime/gamesManager.js` | Game library UI. | `api_routes/games_routes.py` + `domain/games.py`. |
| `runtime/executorCompatibility.js` | Executor compat badge + wait-for-patch hold banner. | `services/executor_compatibility.py` via `system_routes.py`. |
| `runtime/cronusManager.js` | Client version/install card. | `services/roblox_install_manager.py` via `system_routes.py`. |
| `runtime/robloxSettingsCard.js` | Graphics/FPS card. | `services/performance_settings.py` via `performance_routes.py`. |
| `runtime/perfGuard.js` | Live CPU/RAM/VIRTUAL graphs. | `services/resource_monitor.py` via `system_routes.py`. |
| `runtime/updateNotice.js` | Update toast. | `services/app_version_check.py`, `services/app_updater.py`. |
| `runtime/customUi.js` | Misc UI extracted from `ui_dashboard.py`. |  |
| `runtime/hoverScrollbar.js`, `runtime/zoomLock.js` | Scroll/zoom polish. |  |
| `styles/*.css` (11) | Split theme: `base-layout`, `navigation`, `controls-tables`, `accounts-dashboard`, `cards-modals-responsive`, etc. |  |
| `fonts/` | `woff2` webfonts (sources: `assets/fonts/` + `OFL.txt`). |  |

### Summary

Dashboard frontend as plain ESM + CSS with no build step, served at `/` and `/ui/` with `no-store` headers; all data comes from `api_routes/*` via `runtime/api.js`.
