# tests/ — ยูนิตเทสต์ (ไม่มี Windows, ไม่มีเน็ตเวิร์ก, ไม่มีเบราว์เซอร์)

รัน: `python -m unittest discover -s tests` (CI: `compileall` แล้วรันคำสั่งนี้)
ไม่มี `__init__.py` แต่ละไฟล์ปิดในตัวเองด้วย dict/fixture ล้วน
คลุม helper ใน `domain/` + `runtime/` — การเรียก Roblox จริงใน
`services/` กับ WebView2 ใน `desktop/` ตั้งใจไม่เทส

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
