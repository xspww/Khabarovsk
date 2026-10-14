# api_routes/ — ขอบ HTTP localhost (FastAPI)

อะแดปเตอร์บางๆ: แปลง HTTP → เรียก `runtime/`/`services/`/`config/` แล้วคืน
JSON ไม่มี business logic ตรงนี้ wire โดย `register_api_routes(app, ctx)`
(`__init__.py`) สร้างจาก `ApiContext` (`context.py`) ใน `main.py:578-594`
UI เรียกผ่าน `ui/runtime/api.js`

| ไฟล์ | เส้นทาง | เชื่อมกับ |
| ---- | ------ | ----------- |
| `context.py` | dataclass `ApiContext` (cfg, farm, installer, updater, tracker, guard) | `main.py`, ทุก `*_routes.py` |
| `auth.py` | middleware token + endpoint auth | `services/auth_gate.py` |
| `accounts_routes.py` | CRUD บัญชี, lookup place, reload cookie (ใช้ `infra/net_tls`) | `services/account_hybrid.py`, `services/cookie_service.py`, `services/credential_check.py` |
| `account_records.py` | block preview ราย record (`banned/cookie_invalid/…`) | `domain/account_model.py`, เทสผ่าน `tests/test_status_priority.py` |
| `runtime_routes.py` | START/STOP, คิว, คุม farm | `runtime/farm.py`, `runtime/start_readiness.py` |
| `performance_routes.py` | FPS/graphics, ล็อก CPU, RAM, หน้าต่าง (ใช้ `services/performance_settings`) | `services/cpu_limiter.py`, `services/ram_cleanup.py`, `services/window_control.py` |
| `config_routes.py` | อ่าน/เขียนตั้งค่า (คีย์ gfx ใช้ `services/performance_settings`) | `config/store.py`, `config/validation.py` |
| `settings_state.py` | อะแดปเตอร์ settings แบบ dict-in/out มีเทสไม่ใช้ HTTP | `domain/account_model.py`, `tests/test_settings_state.py` |
| `games_routes.py` | CRUD คลังหลายเกม | `domain/games.py`, `ui/runtime/gamesManager.js` |
| `lua_routes.py` | endpoint ตัวช่วย Lua + session-token (ใช้ `infra/app_paths`) | `services/lua_session_tokens.py`, `lua/run_in_executor.lua` |
| `manual_login_routes.py` | Manual Login แบบ Single/Multi | `services/manual_login_service.py` |
| `discord_routes.py` | เปิด/ปิด presence + สถานะ | `services/discord_presence.py` |
| `system_routes.py` | health/เวอร์ชัน/Cronus Manager/update (ใช้ `infra/app_paths`, `version.py`) | `services/roblox_install_manager.py`, `services/app_updater.py`, `services/app_version_check.py`, `runtime/diagnostic_bundle.py` |
| `idempotency.py` | การ์ด Idempotency-Key กันยิงซ้ำ | `runtime_routes.py`, `accounts_routes.py` |
