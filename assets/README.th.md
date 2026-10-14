# assets/ — asset ไบนารีที่ bundle (ไม่มีโค้ด)

| เส้นทาง | ทำอะไร | เชื่อมกับ |
| ---- | --------- | ----------- |
| `cronus_icon.png` | ไอคอนแอป/tray, ภาพ Discord Rich Presence, ต้นฉบับ `.ico` ของ exe | `desktop/console_icon.py`, `services/discord_presence.py`, `cronus_launcher.spec` (`icon=`), `release.yml` (PIL → `.ico`) |
| `fonts/Kanit-*.ttf`, `fonts/Inter-*.ttf` | ฟอนต์ UI ที่ bundle | `ui/styles/*`, `ui/fonts/*.woff2` |
| `fonts/OFL.txt` | ไลเซนส์ฟอนต์ (SIL OFL) | ติดไปดิบๆ |

หมายเหตุบิลด์: `cronus_launcher.spec` bundle `assets/` → `sys._MEIPASS/assets`
แก้ผ่าน `infra/app_paths.resource_path()` ห้ามโหลดด้วย relative path
ดิบๆ ไม่งั้น exe จะหาไม่เจอ
