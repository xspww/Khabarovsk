# runtime/ — Farm orchestration (control plane)

Biggest package (~62 modules). Owns START/STOP, queue, launch, recovery,
and all watchdog loops. Depends on `domain/`, `services/`, `core.py`;
exposes `FarmController` (`farm.py`) to `main.py` and `api_routes/`.
Sub-package `popup_detector/` isolates popup CV/OCR rules.

## Entry / core loop

| File | Does what | Connects to |
| ---- | --------- | ----------- |
| `farm.py` | `FarmController`: owns accounts, `running` flag, executor hold, `close_all_roblox()`, delegates to lifecycle/health/orchestrator. | `main.py` (`farm = FarmController(cfg_mgr)`), `api_routes/runtime_routes.py`, `infra/console_activity.py`. Root `farm.py` shim deleted (zero users). |
| `farm_lifecycle.py` | `start()` / `stop()` transitions. | `farm.py`. |
| `farm_initial_sync.py` | First sync of disk accounts → runtime store on boot. | `farm.py`, `main.py` boot. |
| `farm_health.py` | Health snapshot (uses `infra/app_paths`). | `api_routes/system_routes.py`, `ui` status pills. |
| `runtime_orchestrator.py` | Top reconciler: `request_reconcile_all()` fan-out. | Called by `main.py` hold-resume paths, `runtime_routes.py`. |
| `runtime_scheduler.py` + `runtime_scheduler_dispatch.py` | Tick scheduler + dispatch table. | `farm.py` loop, `maintenance_*` modules. |
| `supervisor_runtime.py` + `machine_supervisor.py` | Machine-wide supervisor (multi-roblox guard status, same-server). | `services/multi_roblox_guard.py`, `services/same_server_guard.py`. |
| `dispatcher.py` + `command_tracker.py` + `command_rate_limit.py` | Internal command bus + dedup + rate limits. | `api_routes/runtime_routes.py` START path. |
| `event_bus.py` | `EventBus`/`EventName` (account data-model bus). | Re-exported by `core.py`. |
| `runtime_store.py` + `runtime_truth.py` + `runtime_state_manager.py` + `state_machine.py` + `launch_limiter.py` | Source-of-truth store, allowed transitions, `GlobalLaunchLimiter`. Re-exported by `core.py` (`StateManager`, `SmartQueue`, `EventBus`). | `dispatcher.py`, `account_worker.py`, `tests/test_status_priority.py`. |

## Per-account path

| File | Does what | Connects to |
| ---- | --------- | ----------- |
| `account_runtime_controller.py` | Per-account controller (owns worker lifecycle). | `farm.py`, `services/process_service.py`. |
| `account_worker.py` | Worker loop (uses `infra/net_tls`, `core.Account`). | `account_runtime_controller.py`, `account_worker_disconnects.py`. |
| `account_worker_disconnects.py` | Disconnect/kick → rejoin classification. | `account_worker.py`. |
| `account_selection.py` + `smart_queue.py` | Queue order (SmartQueue re-exported by `core.py`). | `api_routes/runtime_routes.py` queue APIs. |
| `launch_controller.py` + `launch_attempt.py` + `launch_limiter.py` | Launch orchestration, single attempt, global throttle. | `services/roblox_launch_service.py`. |
| `start_readiness.py` | START readiness decision (single owner). Tested. | `api_routes/runtime_routes.py` START gate, `tests/test_start_readiness.py`. |
| `process_account_runtime.py` (in `services/`) | PID↔account binding helpers — runtime calls into services here, not vice-versa. |  |

## Recovery

| File | Does what | Connects to |
| ---- | --------- | ----------- |
| `recovery_engine.py` | Recovery state machine driver. | `account_worker.py`, `recovery_evaluator.py`. |
| `recovery_policy.py` | Single-owner policy predicates. Tested. | `tests/test_recovery_policy.py`. |
| `recovery_evaluator.py` + `recovery_context.py` + `recovery_support.py` | Score signals → verdict + context. | `recovery_engine.py`. |
| `recovery_signal_router.py` + `recovery_storm.py` + `recovery_owner.py` | Route signals, suppress storms, single owner. | `event_bus.py`. |
| `recovery_view.py` | UI projection of recovery state. | `runtime_view_model.py`. |
| `orphan_sweeper.py` | Reaps ownerless Roblox PIDs. | `machine_supervisor.py`. |

## Watchdog maintenance (one file per concern, aggregated by `system_maintenance.py`)

| File | Watches | Connects to |
| ---- | ------- | ----------- |
| `maintenance_queue.py` | Stuck/empty queue. | `smart_queue.py`. |
| `maintenance_liveness.py` | Dead clients (`services/roblox_liveness`). |  |
| `maintenance_captcha.py` | Captcha holds (persisted-evidence). | `captcha_detector.py`, `services/captcha_guard.py`. |
| `maintenance_verify.py` | Face/ID + passkey holds. | `verify_detector.py`, `services/verify_guard.py`. |
| `maintenance_moderation.py` | Ban/suspend/lock transitions. | `services/ban_guard.py`. |
| `maintenance_performance.py` | FPS/CPU/RAM/trim (lazy `services/performance_settings`). | `services/cpu_limiter.py`, `services/process_trim.py`. |
| `maintenance_lua_timeout.py` | Lua heartbeat timeouts. | `lua_liveness_policy.py`, `lua_event_guard.py`. |
| `maintenance_watchdog_actions.py` | Shared kill/relaunch actions. | `services/roblox_processes.py`. |
| `system_maintenance.py` | Aggregator that ticks every maintenance above. | `runtime_scheduler.py`. |
| `invariant_monitor.py` + `runtime_invariants.py` + `runtime_health.py` | Cross-cutting invariants + health. | `services/process_proof_policy.py`. |

## Lua bridge (in-game `lua/run_in_executor.lua` side)

| File | Does what | Connects to |
| ---- | --------- | ----------- |
| `lua_identity.py` | Binds Lua session to account nonce (`domain/session_identity.py`). | `services/lua_session_tokens.py`. |
| `lua_liveness_policy.py` | Heartbeat timeout policy. | `maintenance_lua_timeout.py`. |
| `lua_event_guard.py` | Dedups Lua rejoin/teleport events. | `lua_rejoin_events.py`. |
| `lua_rejoin_events.py` | Rejoin handling for `FarmController`. | `farm.py`. |
| `lua_server_detection.py` | Server/VIP detection from Lua payloads. | `domain/roblox_private_servers.py`. |
| `runtime_transactions.py` | Rejoin transaction lifecycle. | `launch_controller.py`. |

## Detectors

| File | Does what | Connects to |
| ---- | --------- | ----------- |
| `captcha_detector.py` | Standalone captcha detector. | `maintenance_captcha.py`. |
| `verify_detector.py` | Standalone verify detector (face/ID + passkey). | `maintenance_verify.py`. |
| `popup_detector/` | Popup CV/OCR sub-package — see `popup_detector/README.md`. | `account_worker.py` screenshot path. |

## Observability (read-only projections for UI/API)

| File | Does what | Connects to |
| ---- | --------- | ----------- |
| `runtime_view_model.py` | UI view model (status pills, Running timer). | `api_routes/runtime_routes.py`, `ui/runtime/status.js`. |
| `telemetry_view.py` + `runtime_timeline.py` + `runtime_state_observability.py` | Graphs, timeline, state-change log. | `api_routes/system_routes.py`, `ui` live graphs. |
| `diagnostic_bundle.py` | One-click diagnostics zip (logs + config snapshot). | `api_routes/system_routes.py`. |
