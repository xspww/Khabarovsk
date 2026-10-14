# config/ — ตั้งค่าแบบ local-first

พึ่งแค่ `infra/` + `domain/states` เป็นเจ้าของทุกอย่างใต้
`%LOCALAPPDATA%/Cronus Launcher/data`

| ไฟล์ | ทำอะไร | เชื่อมกับ |
| ---- | --------- | ----------- |
| `validation.py` | Schema ล้วน: `CONFIG_SCHEMA_VERSION = 2`, ช่วงตัวเลข (`max_retry`, `crash_timeout`, …), `validate_config_payload()` ไม่มี I/O | ถูก import โดย `store.py` เท่านั้น เทสอ้อมผ่าน `tests/test_settings_state.py` |
| `store.py` | `ConfigManager` + ไฟล์บัญชี/runtime แบบ text (`cronus_rt1_config.json`, `cronus_rt12_accounts.txt`, `cronus_rt12_runtime.txt`) ย้ายคีย์เก่า, lazy-import `core.flog` เพื่อตัดวงจร circular | `config/__init__.py` กับ `core.py` re-export ใช้โดย `main.py` (`cfg_mgr`), `api_routes/*` ทุกไฟล์, `services/executor_compatibility.py`, `services/executor_relauncher.py` |
