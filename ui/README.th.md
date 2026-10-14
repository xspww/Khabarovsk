# ui/ — หน้าแดชบอร์ด (ไม่ต้องบิลด์, ESM + CSS ล้วน)

ถูกเสิร์ฟโดย `desktop/ui_dashboard.py` ที่ `/` กับ `/ui/` พร้อม header
`no-store` (middleware ใน `main.py`) เรียก `api_routes/*` ผ่าน
`runtime/api.js` ไม่มี `__init__.py` (asset เว็บ ไม่ใช่แพ็กเกจ Python)

| เส้นทาง | ทำอะไร | เชื่อมกับ |
| ---- | --------- | ----------- |
| `index.html` | เชลล์หน้าเดียว (ปุ่ม START, nav, ช่อง token/เวอร์ชัน) | `app.js`, `desktop/webview_window.py` เป็นโฮสต์ |
| `app.js` | ทางเข้า ESM → `components/customSelect.js`, `app/dashboard.js` |  |
| `dashboard.css` | bundle เก่า (สไตล์จริงอยู่ใน `styles/`) |  |
| `app/dashboard.js` | logic หลัก (ค้นหา, ฟิลเตอร์, START) | `runtime/api.js`, `components/accountsTable.js` |
| `components/accountsTable.js` | ตาราง `#/STATUS/USERNAME/GAME/DESCRIPTION` + ข้อความตอนว่าง | `api_routes/accounts_routes.py` |
| `components/customSelect.js` | วิดเจ็ต select แบบ custom |  |
| `components/feedback.js` | toast/modal |  |
| `components/icons.js` | ชุดไอคอน |  |
| `events/bindings.js` | ผูกอีเวนต์ DOM |  |
| `panels/settingsPanels.js` | พาเนลตั้งค่า | `api_routes/config_routes.py`, `api_routes/settings_state.py` |
| `panels/ramExtra.js` | พาเนล RAM เสริม | `api_routes/performance_routes.py` |
| `runtime/api.js` | ไคลเอนต์ fetch ทุก route | `api_routes/*` |
| `runtime/status.js` | เม็ดสถานะ (Online/Queued/Finished/Captcha/Banned/… + timer Running) | `runtime/runtime_view_model.py` ผ่าน `runtime_routes.py` |
| `runtime/accountStatus.js` | ชิปสถานะรายบัญชี |  |
| `runtime/gamesManager.js` | UI คลังเกม | `api_routes/games_routes.py` + `domain/games.py` |
| `runtime/executorCompatibility.js` | ป้าย compat ของ executor + แบนเนอร์รอแพตช์ | `services/executor_compatibility.py` ผ่าน `system_routes.py` |
| `runtime/cronusManager.js` | การ์ดเวอร์ชัน/ติดตั้งไคลเอนต์ | `services/roblox_install_manager.py` ผ่าน `system_routes.py` |
| `runtime/robloxSettingsCard.js` | การ์ด graphics/FPS | `services/performance_settings.py` ผ่าน `performance_routes.py` |
| `runtime/perfGuard.js` | กราฟสด CPU/RAM/VIRTUAL | `services/resource_monitor.py` ผ่าน `system_routes.py` |
| `runtime/updateNotice.js` | toast อัปเดต | `services/app_version_check.py`, `services/app_updater.py` |
| `runtime/customUi.js` | UI เบ็ดเตล็ดแยกจาก `ui_dashboard.py` |  |
| `runtime/hoverScrollbar.js`, `runtime/zoomLock.js` | เก็บงาน scroll/zoom |  |
| `styles/*.css` (11) | ธีมแยกส่วน: `base-layout`, `navigation`, `controls-tables`, `accounts-dashboard`, `cards-modals-responsive` ฯลฯ |  |
| `fonts/` | เว็บฟอนต์ `woff2` (ต้นฉบับ: `assets/fonts/` + `OFL.txt`) |  |
