# tests/ — ยูนิตเทสต์ (ไม่มี Windows, ไม่มีเน็ตเวิร์ก, ไม่มีเบราว์เซอร์)

รัน: `python -m unittest discover -s tests` (CI: `compileall` แล้วรันคำสั่งนี้)
ไม่มี `__init__.py` แต่ละไฟล์ปิดในตัวเองด้วย dict/fixture ล้วน
คลุม helper ล้วนใน `domain/` + `runtime/` (+ ส่วนล้วนของ
`services/`/`desktop/`) — การเรียก Roblox จริงกับหน้าต่าง WebView2
ตั้งใจไม่เทส

| ไฟล์ | คลุม | เชื่อมกับ |
| ---- | ------ | ----------- |
| `test_status_priority.py` | ลำดับสถานะ + identity ฝัง + แยก hold + preview ของ record | `domain/account_model.py`, `api_routes/account_records.py` (`from core import account_launch_block_reason`) |
| `test_start_readiness.py` | ตารางตัดสินความพร้อม START | `runtime/start_readiness.py` |
| `test_settings_state.py` | อะแดปเตอร์ settings แบบ dict | `api_routes/settings_state.py` |
| `test_recovery_policy.py` | helper recovery แบบ single-owner | `runtime/recovery_policy.py` |
| `test_process_binding.py` | predicate ผูก PID↔บัญชี (dict ล้วน) | `services/roblox_processes.py` |
| `test_process_proof_policy.py` | ตัวให้คะแนนทรัพยากรแบบ single-owner | `services/process_proof_policy.py` |
| `test_roblox_windows.py` | กฎแจกแจงหน้าต่าง (ไม่มี Win32) | `services/roblox_windows.py` |
| `test_manual_login.py` | เซสชัน Manual Login (ไม่มีเบราว์เซอร์/เน็ตเวิร์ก) | `services/manual_login_service.py` |
| `test_lua_hardening.py` | ความเปราะของ path ตัวช่วย Lua | `lua/run_in_executor.lua`, `api_routes/lua_routes.py` |
| `test_cookie_verdict.py` | mapping คำตัดสิน cookie (ล้วน ไม่มี store) | `services/roblox_validation.py` |
| `test_desktop_executors.py` | กันถดถอย lazy-import ของ executor มาตรฐาน (Qt/shiboken) | `desktop/webview_window.py` |
| `test_install_cleanup.py` | ล้างเวอร์ชันเก่า (ไฟล์ล้วน ไม่ต้องมี Roblox) | `services/roblox_install_manager.py` |
| `test_launch_target.py` | แตกแขนงตัวประกอบ launch-target | `services/roblox_launch_service.py` |
| `test_process_backend_wiring.py` | เส้น binder: ทุกชื่อที่ผูกไว้ต้อง resolve ได้ (AST + import) | `services/process_backend.py` |
| `test_recovery_seam.py` | ความแคบของ recovery seam (router ผ่าน 7 แอ็กชันเท่านั้น) | `runtime/recovery_seam.py`, `runtime/recovery_signal_router.py` |
| `test_vip_links.py` | helper ลิงก์ VIP (กรองราย place, shared-direct) | `domain/roblox_private_servers.py` |
| `test_game_switch.py` | สลับเกมขณะรัน: เทียบ target + rejoin/stop เมื่อ assign/config/แก้เกม | `services/game_switch.py`, `api_routes/games_routes.py`, `api_routes/config_routes.py` |
| `test_farm_runtime_reset.py` | ลำดับ reset runtime ของ farm ตอน stop/Finished (kill → reset → bump revision) | `runtime/farm.py` |
| `test_console_activity.py` | กุญแจ identity ของคอนโซล, ตัวนับ, แจ้ง start ล้มเหลว, redact ก่อนออกจอ | `infra/console_activity.py`, `infra/core_logging.py` |

### สรุป

ยูนิตเทสต์แบบปิดในตัวเอง (`python -m unittest discover -s tests`, CI รันด้วย) ใช้ dict/fixture ล้วนคลุม logic ใน domain/runtime กับส่วนล้วนของ services/desktop ไม่มี Windows ไม่มีเน็ตเวิร์ก ไม่มีเบราว์เซอร์
