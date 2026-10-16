# domain/ — โมเดลล้วน (ไม่มี I/O, ไม่มี singleton)

dataclass, enum, และ predicate ล้วน import จากที่ไหนก็ปลอดภัย
รวม `tests/` โดยไม่ต้องมี Windows API หรือเน็ตเวิร์ก

| ไฟล์ | ทำอะไร | เชื่อมกับ |
| ---- | --------- | ----------- |
| `account_model.py` | `Account`, `ServerType`, `STATE_META`, `account_launchable()`, `account_launch_block_reason()`, predicate ban/lock/suspend/recovery | `core.py` re-export ใช้โดย `runtime/*` (state machine, worker), `services/auth_gate.py`, `services/roblox_launch_service.py`, `api_routes/settings_state.py`, `api_routes/account_records.py` เทสโดย `tests/test_status_priority.py` |
| `states.py` | `AccountState` / `RuntimeState`, `LIFECYCLE_STATE`, map `PUBLIC_TO_RUNTIME_STATE`, `public_state_for_runtime()` / `runtime_state_for_public()` | `core.py` re-export ใช้โดย `config/store.py`, `runtime/*`, เม็ดสถานะใน `desktop/host.py` |
| `runtime_models.py` | dataclass ฝั่ง runtime (launch attempt, queue entry, health snapshot) | ใช้โดย `runtime/runtime_store.py`, `runtime/runtime_view_model.py`, `runtime/telemetry_view.py` |
| `runtime_lifecycle.py` | enum/helper วงจรชีวิตที่ farm กับ worker ใช้ร่วมกัน | ใช้โดย `runtime/farm_lifecycle.py`, `runtime/account_runtime_controller.py` |
| `session_identity.py` | ตัวตน session / launch-nonce (ผูก Lua heartbeat กับบัญชี) | ใช้โดย `runtime/lua_identity.py`, `services/lua_session_tokens.py`, `api_routes/lua_routes.py` |
| `games.py` | คลังหลายเกม: รายชื่อประสบการณ์ Roblox ที่บัญชีเข้าร่วมได้ | เสิร์ฟโดย `api_routes/games_routes.py`, ใช้โดย `ui/runtime/gamesManager.js` |
| `roblox_private_servers.py` | แก้ VIP/private-server + verdict cache (ใช้ `infra/net_tls`) | ใช้โดย `services/private_server_preflight.py`, `services/vip_tracker.py` |

### สรุป

dataclass, enum และ predicate ล้วน ไม่มี I/O import จากที่ไหนก็ปลอดภัย รวมทั้งยูนิตเทสต์แบบปิดในตัวเอง
