# services/ — เชื่อมโลกภายนอก (ไฟล์ละหนึ่งขอบเขต)

แต่ละโมดูลเป็นเจ้าของขอบเขตภายนอกหนึ่งอย่าง (Roblox API, โปรเซส OS,
เบราว์เซอร์, updater, Discord) import ได้จาก `infra/`, `config/`,
`domain/`, `core.py` **ห้าม** import `runtime/` (ทิศคือ
`runtime/` → `services/`) เพื่อกันวงจร `main.py` เป็นคน wire singleton
(`EXECUTOR_TRACKER`, `EXECUTOR_RELAUNCHER`, `APP_UPDATER`, `ROBLOX_INSTALLER`)

## บัญชี / cookie

| ไฟล์ | ทำอะไร | เชื่อมกับ |
| ---- | --------- | ----------- |
| `account_hybrid.py` | แก้บัญชีแบบ hybrid cookie/credential, singleton `ACCOUNT_STORE` | `main.py` ตอนบูต (`ACCOUNT_STORE.to_cronus_accounts()`), `services/account_reload.py` |
| `account_reload.py` | โหลด/รีเฟรช store บัญชีจากดิสก์ | `api_routes/accounts_routes.py` (ปุ่ม Reload Cookies) |
| `cookie_service.py` | CRUD cookie + ตัวตาม VIP rotation | `api_routes/*`, `runtime/account_worker.py` |
| `cookie_artifact_ledger.py` | บัญชีแยกประเภท artifact ของ cookie แบบ append-only (audit) | `services/cookie_service.py`, ไฟล์ audit ของ `infra/core_logging.py` |
| `credential_check.py` | เช็ก credential จริงผ่าน Roblox Auth v2 login API (ใช้ `infra/net_tls`) | `api_routes/accounts_routes.py`, fixture ใน `tests/test_status_priority.py` |
| `auth_gate.py` | ประตู token/auth ของ Local API (block reason ของ `Account`) | middleware ใน `api_routes/auth.py` |
| `manual_login_service.py` | เซสชัน Manual Login แบบ Single/Multi (Playwright, ในเทสไม่มีเน็ตเวิร์ก) | `api_routes/manual_login_routes.py`, เทสโดย `tests/test_manual_login.py` |
| `ban_guard.py` | การ์ดจับแบน (แยก confirmed-ban กับ preview) | `runtime/maintenance_moderation.py`, `api_routes/account_records.py` |
| `captcha_guard.py` | marker เฉพาะ captcha จริง (ไม่นับ Security Verification ที่ผ่านเอง) | `runtime/captcha_detector.py`, `runtime/maintenance_captcha.py` |
| `verify_guard.py` | hold ล็อกหน้า/บัตร + กู้ passkey/2FA | `runtime/verify_detector.py`, `runtime/maintenance_verify.py` |
| `same_server_guard.py` | การ์ดจับกลุ่มเซิร์ฟเวอร์เดียวกัน | `runtime/machine_supervisor.py`, `services/vip_tracker.py` |

## ไคลเอนต์ Roblox

| ไฟล์ | ทำอะไร | เชื่อมกับ |
| ---- | --------- | ----------- |
| `roblox_hybrid.py` | Roblox API แบบ header เหมือนเบราว์เซอร์ + spawn `--multi-roblox-guard` | สาขา `--multi-roblox-guard` ใน `main.py`, `runtime/launch_controller.py`, `services/roblox_launch_service.py` |
| `roblox_launch_service.py` | บริการเปิดเกม: ประกอบ deeplink / join private-server | `runtime/launch_attempt.py`, `runtime/account_worker.py` |
| `roblox_install_manager.py` | ตัวติดตั้ง/อัปเดตจาก official endpoint เท่านั้น + ล้างเวอร์ชันเก่า | `main.py` (`ROBLOX_INSTALLER`), `api_routes/system_routes.py` (Cronus Manager) |
| `roblox_processes.py` | แจกแจง/ผูกโปรเซส (PID ↔ บัญชี) | `services/process_service.py`, เทสโดย `tests/test_process_binding.py` |
| `roblox_liveness.py` | probe ความมีชีวิตของไคลเอนต์ (หน้าต่าง + heartbeat) | `runtime/maintenance_liveness.py` |
| `roblox_windows.py` | กฎแจกแจงหน้าต่างร่วม (ล้วน ไม่มี Win32 ในเทส) | `services/window_control.py`, เทสโดย `tests/test_roblox_windows.py` |
| `roblox_log_evidence.py` | เก็บหลักฐานจากล็อกสำหรับ recovery hold | `runtime/recovery_engine.py`, `runtime/diagnostic_bundle.py` |
| `multi_roblox_guard.py` | การ์ด mutex/event ของ Win32 (`ROBLOX_singletonMutex`) ให้รันหลายไคลเอนต์ได้ `main()` สำหรับโหมด `--multi-roblox-guard` ย้ายจาก root | `main.py`, `services/roblox_hybrid.py` ถูก smoke โดย `release.yml` (`--multi-roblox-guard mutex`) |
| `private_server_preflight.py` | preflight สร้าง Private Server อัตโนมัติตอน START | `runtime/start_readiness.py`, `api_routes/runtime_routes.py` |
| `vip_tracker.py` | ตาม VIP/เซิร์ฟเวอร์ + สะพาน process manager | `runtime/machine_supervisor.py` |
| `window_control.py` | โฟกัส/ย่อ/ขยาย/จัดเลย์เอาต์หลายไคลเอนต์ | `api_routes/performance_routes.py` (หมวด Windows), `services/roblox_windows.py` |

## Executor

| ไฟล์ | ทำอะไร | เชื่อมกับ |
| ---- | --------- | ----------- |
| `executor_compatibility.py` | สถานะ executor (WEAO ดูสถานะอย่างเดียว, official Roblox ดูเวอร์ชัน) + callback `on_transition` | `main.py` (`EXECUTOR_TRACKER`), `ui/runtime/executorCompatibility.js` |
| `executor_relauncher.py` | ปิด Roblox+executor → เปิดใหม่ → ยืนยัน loader → rejoin | `main.py` (`EXECUTOR_RELAUNCHER`), start guard ใน `runtime/farm.py` |
| `lua_session_tokens.py` | token session/nonce ของ farm อายุ 24 ชม. สำหรับไคลเอนต์ Lua | `api_routes/lua_routes.py`, `runtime/lua_identity.py` |

## เครื่อง / ประสิทธิภาพ

| ไฟล์ | ทำอะไร | เชื่อมกับ |
| ---- | --------- | ----------- |
| `performance_settings.py` | ไฟล์ graphics `.json` ของ Roblox (ล็อก FPS, โหมดกราฟิกต่ำ) + priority โปรเซส ย้ายจาก root | `main.py`, `api_routes/performance_routes.py`, `api_routes/config_routes.py`, `runtime/maintenance_performance.py` |
| `cpu_limiter.py` | จำกัด CPU ของ PID Roblox | `runtime/maintenance_performance.py` |
| `process_service.py` | เส้นแบ่ง ownership: PID ไหนเป็นของบัญชีไหน | `runtime/account_runtime_controller.py`, `services/process_backend.py` |
| `process_backend.py` | backend โปรเซสภายใน (cache snapshot psutil) | `services/process_service.py`, `services/resource_monitor.py` |
| `process_account_runtime.py` | helper ฝั่ง account-runtime ช่วยตัดสิน ownership | `services/process_service.py` |
| `process_proof_policy.py` | ตัวให้คะแนนสัญญาณทรัพยากรแบบ single-owner | เทสโดย `tests/test_process_proof_policy.py`, ใช้โดย `runtime/invariant_monitor.py` |
| `process_trim.py` | trim `EmptyWorkingSet` ราย PID | `runtime/maintenance_performance.py` |
| `ram_cleanup.py` | ล้าง RAM (พอร์ต MemReduct) | `api_routes/performance_routes.py` (หมวด RAM) |
| `virtual_memory.py` | คุม pagefile (เฉพาะไดรฟ์ C:) | `api_routes/performance_routes.py` (การ์ด VIRTUAL) |
| `resource_monitor.py` | ตัวอย่าง CPU/RAM แบบ singleton | `api_routes/system_routes.py`, กราฟสดใน `ui` |
| `network_monitor.py` | ตัวอย่างเน็ตเวิร์กขึ้น/ลง (ใช้ `infra/net_tls`) | `api_routes/system_routes.py` |
| `browser_tracker.py` | ตาม PID เบราว์เซอร์ล็อกอิน/popup (Playwright/Chrome/Edge) | `services/manual_login_service.py` |
| `startup_manager.py` | จัดการ shortcut Windows Startup | `api_routes/system_routes.py` |

## อัปเดตแอป / presence

| ไฟล์ | ทำอะไร | เชื่อมกับ |
| ---- | --------- | ----------- |
| `app_updater.py` | อัปเดตในแอปคลิกเดียวสำหรับ exe (ฝัง `_UPDATER_PS1`, ตรวจลายเซ็น + SHA256) | `main.py` (`APP_UPDATER`), `api_routes/system_routes.py` ถูก render + เช็ก syntax โดย `release.yml` |
| `app_version_check.py` | เช็กอัปเดตแบบแจ้งเตือนอย่างเดียว (opencode-style, ใช้ `infra/net_tls`) | ป้ายเวอร์ชันใน `api_routes/system_routes.py` |
| `release_signing.py` | ลายเซ็น Ed25519 ของ release asset (exe/zip) | `release.yml` (`python -m services.release_signing`), ฝั่งตรวจใน `services/app_updater.py` |
| `discord_presence.py` | Discord Rich Presence (App ID ในตัว, `assets/cronus_icon.png`) | `main.py` (`DISCORD_PRESENCE`), `api_routes/discord_routes.py` |

## งานล็อก

| ไฟล์ | ทำอะไร | เชื่อมกับ |
| ---- | --------- | ----------- |
| `safe_rotating_log.py` | file handler หมุนไฟล์แบบกันพัง | `infra/core_logging.py` |
| `log_rate_limiter.py` | จำกัดความถี่ล็อกราย scope | `infra/core_logging.py`, loop ร้อนใน `runtime/*` |
