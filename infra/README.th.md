# infra/ — ชั้นล่างสุด (ห้ามพึ่งโค้ดแอปชั้นอื่น)

ชั้นต่ำสุด โมดูลไหนก็ import ได้ แต่แพ็กเกจนี้ต้อง **ไม่** import
จาก `config/`, `domain/`, `services/`, `runtime/`, `api_routes/`, `desktop/`

| ไฟล์ | ทำอะไร | เชื่อมกับ |
| ---- | --------- | ----------- |
| `app_paths.py` | จุดเดียวของ `APP_NAME`, `APP_ROOT_DIR`, `APP_DATA_DIR` (`%LOCALAPPDATA%/Cronus Launcher/data`), `LOG_DIR`, `resource_path()` (รองรับ `sys._MEIPASS` ใน exe) | ถูก import ~18 โมดูล: `core.py`, `config/store.py`, `infra/core_logging.py`, `desktop/*`, `services/*`, `api_routes/*` `cronus_launcher.spec` พึ่ง `resource_path()` ตอน bundle `assets/`, `ui/`, `lua/` |
| `core_logging.py` | `flog` / `flog_kv` / `flog_struct` + หมุนไฟล์ล็อกผ่าน `services.safe_rotating_log` ย้ายไฟล์ล็อกเก่าเข้า `LOG_DIR` | `core.py` re-export (facade สาธารณะ) ใช้โดย `runtime/event_bus.py`, `runtime/state_machine.py`, `runtime/smart_queue.py` |
| `console_activity.py` | State หน้าคอนโซลในหน่วยความจำ: บัญชีที่ active, hold captcha/verify, flag Lua-liveness จัดรูปบรรทัดให้หน้าต่างคอนโซลของ `desktop/host.py` | ใช้โดย `infra/core_logging.py` (emit), `desktop/host.py` (`format_console_line`), `runtime/farm.py` (`set_total_accounts`, `set_lua_liveness_required`) |
| `net_tls.py` | TLS context กลางอันเดียว: bundle `certifi` + Windows CA/ROOT store `default_context()` สำหรับทุก HTTPS ขาออก | ถูก import 10 โมดูล: `services/roblox_hybrid.py`, `services/roblox_install_manager.py`, `services/credential_check.py`, `services/network_monitor.py`, `services/executor_compatibility.py`, `services/app_version_check.py`, `services/app_updater.py`, `domain/roblox_private_servers.py`, `api_routes/accounts_routes.py`, `runtime/account_worker.py` |
