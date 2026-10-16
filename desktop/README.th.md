# desktop/ — เชลล์ Windows (WebView2 + single-instance + console)

เป็นเจ้าของทุกอย่างที่ผู้ใช้เห็นเป็น *หน้าต่าง native*: บูต, tray/console,
โฮสต์ WebView2, loop เซิร์ฟเวอร์ backend เสิร์ฟ `ui/` กับ `assets/` ผ่าน
FastAPI localhost (`main.py` → `run_desktop(app, farm)`)

| ไฟล์ | ทำอะไร | เชื่อมกับ |
| ---- | --------- | ----------- |
| `host.py` | คนคุมเดสก์ท็อป (เดิม `desktop_host.py` ที่ root): หน้าต่างคอนโซล, mutex, เธรด backend, shutdown `INSTANCE_TOKEN`, `SHUTDOWN_REQUESTED`, `run_desktop()` | `main.py:179` (`from desktop.host import …`), `backend_host.py`, `webview_window.py`, `instance_guard.py`, `infra/console_activity.py` |
| `backend_host.py` | รัน uvicorn บน `127.0.0.1` + เลือกพอร์ตว่าง (`desktop/constants.py`) | `host.py`, `main.py` ผ่าน `host.py` |
| `webview_window.py` | บูต WebView2 + หน้าต่างฝัง (ใช้ `infra/app_paths`, `core.flog_kv`, `version.py`) | `host.py`, `ui/index.html` |
| `ui_dashboard.py` | เชลล์แดชบอร์ดแบบ lazy: `get_html_ui()` เสิร์ฟ `ui/` | `main.py:178`, เส้น HTML ใน `api_routes/*` shim `ui_dashboard.py` ที่ root ถูกลบแล้ว |
| `instance_guard.py` | single-instance ด้วย socket + mutex (`path_targets_current_exe`) | `host.py`, `webview_window.py` |
| `console_output.py` | จับคอนโซล (`write_line`, `paint`, เปิด VT) | แบนเนอร์ตอนเริ่มใน `main.py`, `infra/console_activity.py`, `host.py` |
| `console_icon.py` | ไอคอนคอนโซล/tray จาก `assets/cronus_icon.png` | `host.py` |
| `constants.py` | ค่าคงที่ loopback (`DEFAULT_PORT`, host) | `backend_host.py`, `webview_window.py` |

### สรุป

เชลล์ Windows แบบ native ทั้งการ์ด single-instance, loop เสิร์ฟ backend, หน้าต่าง WebView2 (มี browser fallback) และ console/tray
