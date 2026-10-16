# services/ — External integrations (one boundary per file)

Each module owns one external boundary (Roblox API, OS process, browser,
updater, Discord). May import `infra/`, `config/`, `domain/`, `core.py`.
Must **not** import `runtime/` (direction is `runtime/` → `services/`)
to avoid cycles. `main.py` wires singletons (`EXECUTOR_TRACKER`,
`EXECUTOR_RELAUNCHER`, `APP_UPDATER`, `ROBLOX_INSTALLER`).

## Account / cookie

| File | Does what | Connects to |
| ---- | --------- | ----------- |
| `account_hybrid.py` | Hybrid cookie/credential resolution, `ACCOUNT_STORE` singleton. | `main.py` boot (`ACCOUNT_STORE.to_cronus_accounts()`), `services/account_reload.py`. |
| `account_reload.py` | Reload/refresh account store from disk. | `api_routes/accounts_routes.py` (`Reload Cookies`). |
| `cookie_service.py` | Cookie CRUD + VIP rotation tracker. | `api_routes/*`, `runtime/account_worker.py`. |
| `cookie_artifact_ledger.py` | Append-only ledger of cookie artifacts (audit). | `services/cookie_service.py`, `infra/core_logging.py` audit file. |
| `credential_check.py` | Real check via Roblox Auth v2 login API (uses `infra/net_tls`). | `api_routes/accounts_routes.py`, `tests/test_status_priority.py` fixtures. |
| `auth_gate.py` | Token/auth gate for local API (`Account` block reasons). | `api_routes/auth.py` middleware. |
| `manual_login_service.py` | Manual Login Single/Multi popup sessions (Playwright, no network in tests). | `api_routes/manual_login_routes.py`, tested by `tests/test_manual_login.py`. |
| `ban_guard.py` | Ban detection guard (confirmed-ban vs preview). | `runtime/maintenance_moderation.py`, `api_routes/account_records.py`. |
| `captcha_guard.py` | Strong captcha-only markers (excludes auto-pass Security Verification). | `runtime/captcha_detector.py`, `runtime/maintenance_captcha.py`. |
| `verify_guard.py` | Face/ID lock + passkey/2FA recovery holds. | `runtime/verify_detector.py`, `runtime/maintenance_verify.py`. |
| `same_server_guard.py` | Same-server clustering guard. | `runtime/machine_supervisor.py`, `services/vip_tracker.py`. |

## Roblox client

| File | Does what | Connects to |
| ---- | --------- | ----------- |
| `roblox_hybrid.py` | Thin facade re-exporting the 6 modules below (no logic allowed) | Fully migrated — every site imports from the owning module; the facade stays for compat only. |
| `roblox_http.py` | `RobloxHTTP` (cookie+CSRF ผ่าน `infra/net_tls`), header browser-like, endpoint base, `open_roblox_uri` + version pin | `roblox_moderation.py`, `roblox_identity.py`, `roblox_validation.py`, `roblox_launcher.py`, `services/private_server_preflight.py` |
| `roblox_moderation.py` | live moderation/ban (`fetch_moderation_status`, `fetch_user_ban_status`, `fetch_ban_status_by_username`) | `roblox_identity.py`, `roblox_validation.py`, `services/credential_check.py` |
| `roblox_identity.py` | identity ออฟไลน์ (`cookie_embedded_identity`, `username_for_cookie_in_store`) + แยก dead-cookie (`resolve_ban/moderated_dead_cookie`, `reprobe`) | `roblox_validation.py`, `services/account_hybrid.py`, `api_routes/accounts_routes.py`, `api_routes/account_records.py` |
| `roblox_validation.py` | `validate_cookie_details`, `validate_record_cookie_identity`, `validate_cookie` | `runtime/account_worker.py`, `runtime/captcha_detector.py`, `runtime/verify_detector.py`, `runtime/maintenance_moderation.py`, `services/manual_login_service.py`, `services/roblox_launcher.py` |
| `roblox_multibox.py` | guard subprocess (`ensure/release/status`, spawn `services/multi_roblox_guard.py`, `atexit`) | `runtime/farm_lifecycle.py`, `runtime/recovery_engine.py`, `runtime/machine_supervisor.py`, `services/roblox_launcher.py` |
| `roblox_launch_service.py` | Launch service: builds deeplink/private-server join args. | `runtime/launch_attempt.py`, `runtime/account_worker.py`. |
| `roblox_launcher.py` | `HybridLauncher`: validate → guard → VIP → ticket → launch orchestration. | `services/roblox_launch_service.py`, `services/roblox_hybrid.py` (facade), `services/roblox_http.py`, `services/roblox_multibox.py`. |
| `roblox_install_manager.py` | Official-endpoints-only installer/updater + old-version cleanup. | `main.py` (`ROBLOX_INSTALLER`), `api_routes/system_routes.py` (Cronus Manager). |
| `roblox_processes.py` | Process enumeration/binding (PID ↔ account). | `services/process_service.py`, tested by `tests/test_process_binding.py`. |
| `roblox_liveness.py` | Client liveness probes (window + heartbeat). | `runtime/maintenance_liveness.py`. |
| `roblox_windows.py` | Shared window-enumeration rules (pure, no Win32 in tests). | `services/window_control.py`, tested by `tests/test_roblox_windows.py`. |
| `roblox_log_evidence.py` | Log-evidence collection for recovery holds. | `runtime/recovery_engine.py`, `runtime/diagnostic_bundle.py`. |
| `multi_roblox_guard.py` | Win32 mutex/event guard (`ROBLOX_singletonMutex`) allowing multi-client; `main()` for `--multi-roblox-guard` mode. Moved from root. | `main.py`, `services/roblox_hybrid.py`. Smoked by `release.yml` (`--multi-roblox-guard mutex`). |
| `private_server_preflight.py` | START-time Auto-Create Private Server preflight. | `runtime/start_readiness.py`, `api_routes/runtime_routes.py`. |
| `vip_tracker.py` | VIP/server tracking + process manager bridge. | `runtime/machine_supervisor.py`. |
| `window_control.py` | Focus/min/max/layout for many clients. | `api_routes/performance_routes.py` (Windows section), `services/roblox_windows.py`. |

## Executor

| File | Does what | Connects to |
| ---- | --------- | ----------- |
| `executor_compatibility.py` | Executor status (WEAO for status only, official Roblox for version) + `on_transition` callbacks. | `main.py` (`EXECUTOR_TRACKER`), `ui/runtime/executorCompatibility.js`. |
| `executor_relauncher.py` | Close Roblox+executor → reopen → verify loader → rejoin. | `main.py` (`EXECUTOR_RELAUNCHER`), `runtime/farm.py` start guard. |
| `lua_session_tokens.py` | 24h farm session/nonce tokens for Lua clients. | `api_routes/lua_routes.py`, `runtime/lua_identity.py`. |

## Machine / performance

| File | Does what | Connects to |
| ---- | --------- | ----------- |
| `performance_settings.py` | Roblox graphics `.json` (FPS limit, low-gfx) + process priority. Moved from root. | `main.py`, `api_routes/performance_routes.py`, `api_routes/config_routes.py`, `runtime/maintenance_performance.py`. |
| `cpu_limiter.py` | CPU throttling for Roblox PIDs. | `runtime/maintenance_performance.py`. |
| `process_service.py` | Ownership boundary: which PID belongs to which account. | `runtime/account_runtime_controller.py`, `services/process_backend.py`. |
| `process_backend.py` | Internal process backend (psutil snapshot cache). | `services/process_service.py`, `services/resource_monitor.py`. |
| `process_account_runtime.py` | Account-runtime helpers for ownership decisions. | `services/process_service.py`. |
| `process_proof_policy.py` | Single-owner resource-signal scorer. | Tested by `tests/test_process_proof_policy.py`, used by `runtime/invariant_monitor.py`. |
| `process_trim.py` | Per-PID `EmptyWorkingSet` trim. | `runtime/maintenance_performance.py`. |
| `ram_cleanup.py` | RAM cleanup (MemReduct port). | `api_routes/performance_routes.py` (RAM section). |
| `virtual_memory.py` | Pagefile control (C: only). | `api_routes/performance_routes.py` (VIRTUAL card). |
| `resource_monitor.py` | CPU/RAM singleton sampler. | `api_routes/system_routes.py`, `ui` live graphs. |
| `network_monitor.py` | Network up/down sampler (uses `infra/net_tls`). | `api_routes/system_routes.py`. |
| `browser_tracker.py` | Tracks login/popup browser PIDs (Playwright/Chrome/Edge). | `services/manual_login_service.py`. |
| `startup_manager.py` | Windows Startup shortcut manager. | `api_routes/system_routes.py`. |

## App updates / presence

| File | Does what | Connects to |
| ---- | --------- | ----------- |
| `app_updater.py` | One-click in-app updater for compiled exe (embedded `_UPDATER_PS1`, signature + SHA256 verify). | `main.py` (`APP_UPDATER`), `api_routes/system_routes.py`. Rendered + syntax-checked by `release.yml`. |
| `app_version_check.py` | Notify-only update check (opencode-style, uses `infra/net_tls`). | `api_routes/system_routes.py` version badge. |
| `release_signing.py` | Project-owned Ed25519 asset signatures for exe/zip. | `release.yml` (`python -m services.release_signing`), `services/app_updater.py` verify side. |
| `discord_presence.py` | Discord Rich Presence (built-in App ID, `assets/cronus_icon.png`). | `main.py` (`DISCORD_PRESENCE`), `api_routes/discord_routes.py`. |

## Logging plumbing

| File | Does what | Connects to |
| ---- | --------- | ----------- |
| `safe_rotating_log.py` | Crash-safe rotating file handler. | `infra/core_logging.py`. |
| `log_rate_limiter.py` | Per-scope spam throttle. | `infra/core_logging.py`, `runtime/*` hot loops. |

### Summary

One module per external boundary (Roblox, OS, browser, updater, Discord). Depends downward on `infra/`/`config/`/`domain/` only; `runtime/` calls into it, never the reverse.
