# สคริปต์ Lua (ฝั่งในเกม)

รันแค่ไฟล์นี้ไฟล์เดียวใน executor ของ Roblox:

```text
run_in_executor.lua
```

โฟลเดอร์ `internal/` เป็น template ของ backend Cronus ไม่ต้องรันเอง

| ไฟล์ | ทำอะไร | เชื่อมกับ |
| ---- | --------- | ----------- |
| `run_in_executor.lua` | ทางเข้าฝั่งผู้ใช้: ตั้ง env `getgenv`/`_G`, บูต monitor + status client ในตัว แพ็กเป็น `Api.lua` ใน zip portable | `api_routes/lua_routes.py` (token), `release.yml` (แพ็ก `run_in_executor.lua` → `Api.lua`), พาเนล Lua Manager ใน `ui` |
| `internal/rejoin_monitor.lua` | monitor rejoin/heartbeat/teleport แบบ minify (placeholder `__CRONUS_*__` ถูก backend เติม) | `runtime/lua_rejoin_events.py`, `runtime/lua_liveness_policy.py` |
| `internal/account_status_client.lua` | ไคลเอนต์สถานะบัญชีแบบ minify | `runtime/lua_identity.py`, `services/lua_session_tokens.py` |
