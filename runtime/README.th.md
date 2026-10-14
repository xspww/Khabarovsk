# runtime/ — คนคุม farm (control plane)

แพ็กเกจใหญ่สุด (~62 โมดูล) เป็นเจ้าของ START/STOP, คิว, การเปิดเกม,
recovery และ watchdog loop ทั้งหมด พึ่ง `domain/`, `services/`,
`core.py` เปิด `FarmController` (`farm.py`) ให้ `main.py` กับ
`api_routes/` ซับแพ็กเกจ `popup_detector/` แยกกฎ CV/OCR ของ popup

## แกนทางเข้า / loop หลัก

| ไฟล์ | ทำอะไร | เชื่อมกับ |
| ---- | --------- | ----------- |
| `farm.py` | `FarmController`: ถือบัญชี, flag `running`, hold executor, `close_all_roblox()`, มอบงานให้ lifecycle/health/orchestrator | `main.py` (`farm = FarmController(cfg_mgr)`), `api_routes/runtime_routes.py`, `infra/console_activity.py` shim `farm.py` ที่ root ถูกลบแล้ว (ไม่มีคนใช้) |
| `farm_lifecycle.py` | ทรานซิชัน `start()` / `stop()` | `farm.py` |
| `farm_initial_sync.py` | sync บัญชีจากดิสก์ → runtime store ครั้งแรกตอนบูต | `farm.py`, ตอนบูตใน `main.py` |
| `farm_health.py` | snapshot สุขภาพ (ใช้ `infra/app_paths`) | `api_routes/system_routes.py`, เม็ดสถานะใน `ui` |
| `runtime_orchestrator.py` | ตัวประสานบนสุด: `request_reconcile_all()` กระจายงาน | เส้น hold-resume ใน `main.py`, `runtime_routes.py` |
| `runtime_scheduler.py` + `runtime_scheduler_dispatch.py` | ตัว tick + ตาราง dispatch | loop ใน `farm.py`, โมดูล `maintenance_*` |
| `supervisor_runtime.py` + `machine_supervisor.py` | supervisor ทั้งเครื่อง (สถานะ multi-roblox guard, same-server) | `services/multi_roblox_guard.py`, `services/same_server_guard.py` |
| `dispatcher.py` + `command_tracker.py` + `command_rate_limit.py` | command bus ภายใน + dedup + จำกัดเรต | เส้น START ใน `api_routes/runtime_routes.py` |
| `event_bus.py` | `EventBus`/`EventName` (บัสโมเดลข้อมูลบัญชี) | `core.py` re-export |
| `runtime_store.py` + `runtime_truth.py` + `runtime_state_manager.py` + `state_machine.py` + `launch_limiter.py` | store ความจริง, ทรานซิชันที่อนุญาต, `GlobalLaunchLimiter` `core.py` re-export (`StateManager`, `SmartQueue`, `EventBus`) | `dispatcher.py`, `account_worker.py`, `tests/test_status_priority.py` |

## เส้นรายบัญชี

| ไฟล์ | ทำอะไร | เชื่อมกับ |
| ---- | --------- | ----------- |
| `account_runtime_controller.py` | คอนโทรลเลอร์รายบัญชี (คุมวงจร worker) | `farm.py`, `services/process_service.py` |
| `account_worker.py` | loop ของ worker (ใช้ `infra/net_tls`, `core.Account`) | `account_runtime_controller.py`, `account_worker_disconnects.py` |
| `account_worker_disconnects.py` | หลุด/kick → จำแนก rejoin | `account_worker.py` |
| `account_selection.py` + `smart_queue.py` | ลำดับคิว (SmartQueue ถูก `core.py` re-export) | API คิวใน `api_routes/runtime_routes.py` |
| `launch_controller.py` + `launch_attempt.py` + `launch_limiter.py` | คนคุมการเปิดเกม, ความพยายามหนึ่งครั้ง, คันเร่งรวม | `services/roblox_launch_service.py` |
| `start_readiness.py` | ตัดสินความพร้อม START (single owner) มีเทส | ประตู START ใน `api_routes/runtime_routes.py`, `tests/test_start_readiness.py` |

## Recovery

| ไฟล์ | ทำอะไร | เชื่อมกับ |
| ---- | --------- | ----------- |
| `recovery_engine.py` | facade บางๆ: `RecoveryCoordinator` = 4 mixin ล่าง + alias `RecoveryEngine` ไม่มี logic | `farm_lifecycle.py` สร้าง; `farm.py`, `dispatcher.py`, `account_worker.py`, `system_maintenance.py` ใช้ type-hint |
| `recovery_core.py` | wiring/`__init__`, lifecycle, helper slot/cfg, ทางเข้า public (`handle_runtime_signal`, `evaluate`, `reconcile_all`) | บูตใน `farm.py`, สัญญาณใน `dispatcher.py` |
| `recovery_txn.py` | budget/begin/clear, cooldown, `_queue_account`, กาว scheduler | ทุก reporter เรียกผ่าน `self` |
| `recovery_reports.py` | `report_crash/launch_failure/lua_waiting/launch_success`, `fail_account`, `maybe_revive_transient_failed`, `force_rejoin` | อีเวนต์ใน `account_worker.py` |
| `recovery_guard_net.py` | loop self-heal multi-roblox, `mark_network_lost/on_network_restored` | `services/multi_roblox_guard.py`, `services/network_monitor.py` |
| `recovery_policy.py` | predicate นโยบายแบบ single-owner มีเทส | `tests/test_recovery_policy.py` |
| `recovery_evaluator.py` + `recovery_context.py` + `recovery_support.py` | ให้คะแนนสัญญาณ → คำตัดสิน + บริบท | `recovery_engine.py` |
| `recovery_signal_router.py` + `recovery_storm.py` + `recovery_owner.py` | เส้นทางสัญญาณ, กัน storm, เจ้าของเดียว | `event_bus.py` |
| `recovery_view.py` | ภาพ recovery สำหรับ UI | `runtime_view_model.py` |
| `orphan_sweeper.py` | เก็บ PID Roblox ไร้เจ้าของ | `machine_supervisor.py` |

## Watchdog maintenance (เรื่องละไฟล์ รวมโดย `system_maintenance.py`)

| ไฟล์ | เฝ้า | เชื่อมกับ |
| ---- | ------- | ----------- |
| `maintenance_queue.py` | คิวค้าง/คิวว่าง | `smart_queue.py` |
| `maintenance_liveness.py` | ไคลเอนต์ตาย (`services/roblox_liveness`) |  |
| `maintenance_captcha.py` | hold captcha (มีหลักฐาน persist) | `captcha_detector.py`, `services/captcha_guard.py` |
| `maintenance_verify.py` | hold หน้า/บัตร + passkey | `verify_detector.py`, `services/verify_guard.py` |
| `maintenance_moderation.py` | ทรานซิชัน ban/suspend/lock | `services/ban_guard.py` |
| `maintenance_performance.py` | FPS/CPU/RAM/trim (lazy `services/performance_settings`) | `services/cpu_limiter.py`, `services/process_trim.py` |
| `maintenance_lua_timeout.py` | heartbeat Lua ขาด | `lua_liveness_policy.py`, `lua_event_guard.py` |
| `maintenance_watchdog_actions.py` | แอ็กชัน kill/relaunch ร่วม | `services/roblox_processes.py` |
| `system_maintenance.py` | ตัวรวมที่ tick ทุก maintenance ข้างบน | `runtime_scheduler.py` |
| `invariant_monitor.py` + `runtime_invariants.py` + `runtime_health.py` | invariant ข้ามเรื่อง + สุขภาพ | `services/process_proof_policy.py` |

## สะพาน Lua (ฝั่งในเกม `lua/run_in_executor.lua`)

| ไฟล์ | ทำอะไร | เชื่อมกับ |
| ---- | --------- | ----------- |
| `lua_identity.py` | ผูก session Lua กับ nonce บัญชี (`domain/session_identity.py`) | `services/lua_session_tokens.py` |
| `lua_liveness_policy.py` | นโยบาย timeout ของ heartbeat | `maintenance_lua_timeout.py` |
| `lua_event_guard.py` | dedup อีเวนต์ rejoin/teleport ของ Lua | `lua_rejoin_events.py` |
| `lua_rejoin_events.py` | งาน rejoin ให้ `FarmController` | `farm.py` |
| `lua_server_detection.py` | จับเซิร์ฟเวอร์/VIP จาก payload Lua | `domain/roblox_private_servers.py` |
| `runtime_transactions.py` | วงจร transaction ของ rejoin | `launch_controller.py` |

## ตัวจับ

| ไฟล์ | ทำอะไร | เชื่อมกับ |
| ---- | --------- | ----------- |
| `captcha_detector.py` | ตัวจับ captcha แบบ standalone | `maintenance_captcha.py` |
| `verify_detector.py` | ตัวจับ verify แบบ standalone (หน้า/บัตร + passkey) | `maintenance_verify.py` |
| `popup_detector/` | ซับแพ็กเกจ CV/OCR ของ popup — ดู `popup_detector/README.th.md` | เส้น screenshot ใน `account_worker.py` |

## ภาพสำหรับดู (read-only ให้ UI/API)

| ไฟล์ | ทำอะไร | เชื่อมกับ |
| ---- | --------- | ----------- |
| `runtime_view_model.py` | view model ของ UI (เม็ดสถานะ, timer Running) | `api_routes/runtime_routes.py`, `ui/runtime/status.js` |
| `telemetry_view.py` + `runtime_timeline.py` + `runtime_state_observability.py` | กราฟ, ไทม์ไลน์, ล็อกเปลี่ยน state | `api_routes/system_routes.py`, กราฟสดใน `ui` |
| `diagnostic_bundle.py` | zip วินิจฉัยคลิกเดียว (ล็อก + snapshot ตั้งค่า) | `api_routes/system_routes.py` |
