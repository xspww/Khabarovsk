<p align="center">
  <img src="assets/cronus_icon.png" width="120" alt="โลโก้ Cronus Launcher" />
</p>

<h1 align="center">Cronus Launcher</h1>

<p align="center">ตัวจัดการหลายบัญชี Roblox พร้อมรีจอยอัตโนมัติ</p>

<p align="center">
  <a href="https://github.com/q0HtHHftAS/Roblox-Auto-Rejoin-windown/releases"><img src="https://img.shields.io/github/v/release/q0HtHHftAS/Roblox-Auto-Rejoin-windown?label=release" alt="release" /></a>
  <a href="https://github.com/q0HtHHftAS/Roblox-Auto-Rejoin-windown/actions/workflows/release.yml"><img src="https://github.com/q0HtHHftAS/Roblox-Auto-Rejoin-windown/actions/workflows/release.yml/badge.svg" alt="build" /></a>
  <img src="https://img.shields.io/badge/platform-Windows-blue" alt="platform" />
  <img src="https://img.shields.io/badge/python-3.11+-blue" alt="python" />
</p>

<p align="center">
  <a href="README.md">English</a> | <a href="README.th.md">ไทย</a>
</p>

<p align="center">
  <a href="#ฟีเจอร์">ฟีเจอร์</a> •
  <a href="#สิ่งที่ต้องมี">สิ่งที่ต้องมี</a> •
  <a href="#ติดตั้ง">ติดตั้ง</a> •
  <a href="#อัปเดต">อัปเดต</a> •
  <a href="#เริ่มใช้จากซอร์ส">เริ่มใช้</a> •
  <a href="#สร้างไฟล์-exe">สร้าง exe</a> •
  <a href="#สคริปต์-lua-ในเกม">Lua</a> •
  <a href="#ข้อมูลและความเป็นส่วนตัว">ความเป็นส่วนตัว</a>
</p>

Cronus Launcher คือ launcher สำหรับ Roblox บน Windows รันบนเครื่องตัวเองทั้งหมด ดูแลหลายบัญชีพร้อมกัน หลุดเมื่อไหร่ก็พากลับเข้าเกมให้เอง มี watchdog คอยเฝ้าอยู่ เจอปัญหาก็ดึงกลับมาทำงานต่อให้เลย

## ฟีเจอร์

มีอะไรให้ใช้บ้าง:

- 👥 จัดการหลายบัญชีพร้อมกัน: เพิ่ม จัดระเบียบ แล้วเปิด Roblox หลายบัญชีในเวลาเดียวกันได้เลย
- 🔄 รีจอยเองตอนมีปัญหา: พอ Roblox หลุด เด้ง error หรือแครชไป ก็ดึงบัญชีนั้นกลับเข้าเกมให้อัตโนมัติ
- ⚡ ลดโหลดเครื่อง: จำกัด CPU กับลดกราฟิกลงได้ พอเปิดหลายจอเครื่องก็ยังไหว
- 🧩 ต่อกับ executor ได้: ทำงานกับ executor ที่รองรับ พร้อมส่ง telemetry จากในเกมออกมาให้ด้วย
- 🔒 เก็บความลับไว้บนเครื่อง: เข้ารหัส cookie กับรหัสผ่านด้วย Windows DPAPI บนเครื่องตัวเอง ไม่ส่งออกไปไหน

## สิ่งที่ต้องมี

เครื่องต้องพร้อมตามนี้:

- Windows 10 หรือ Windows 11 64-bit
- Python 3.11 ขึ้นไป
- ลง Roblox ไว้บนเครื่องแล้ว

## ติดตั้ง

วิธีง่ายสุดคือโหลดตัว build สำเร็จจากหน้า Releases ไม่ต้องลง Python เพิ่ม

1. เปิด `https://github.com/q0HtHHftAS/Roblox-Auto-Rejoin-windown/releases`
2. โหลด `CronusLauncher-<version>.exe` แล้วรันได้เลย
3. ตัว portable `CronusLauncher-<version>-portable.zip` ข้างในมี exe ตัวเดียวกันพร้อม Lua loader

พอรันครั้งแรก Windows SmartScreen อาจเตือนเพราะ exe ยังไม่มีลายเซ็น โหลดจากหน้า Releases ด้านบนเท่านั้น ถ้าชื่อไฟล์ตรงกับ release ก็กด More info แล้ว Run anyway ได้เลย ไม่แน่ใจก็เช็ค SHA256 เทียบกับ `checksums.txt` ใน release เดียวกัน

```powershell
(Get-FileHash CronusLauncher-1.0.5.exe -Algorithm SHA256).Hash
```

## อัปเดต

โหลดครั้งเดียว อัปเดตได้ตลอด แนะนำให้ลง exe แบบ per-user (`%LOCALAPPDATA%\Cronus Launcher\`) ปุ่ม Restart จะกดอัปเดตได้เลยไม่ติด UAC ถ้าลงไว้ใน Program Files จะได้เป็นลิงก์ให้โหลดเองแทน

เปิดแอปทิ้งไว้ 5 วินาทีแล้วจะเริ่มเช็ค Releases บน GitHub แบบเงียบๆ จากนั้นเช็คซ้ำทุก `update_check_interval_hours` ถ้าเปิด `auto_check_update` กับ `auto_download_update` ไว้ก็โหลดรอไว้ให้ในพื้นหลัง พอ verify ผ่านก็กด Restart เพื่อติดตั้งได้เลย ตัวแอปจะหยุด Auto Rejoin เอง สลับ exe ยิง `/api/status` เช็คเวอร์ชันใหม่ ถ้าพังก็ rollback กลับเป็น `.bak` ให้ โฟลเดอร์ settings กับบัญชีใน `%LOCALAPPDATA%\Cronus Launcher\data` ไม่แตะเลย โหลดค้างก็ resume ต่อได้

ค่าที่ตั้งได้: `auto_check_update`, `auto_download_update`, `auto_install_update`, `update_check_interval_hours`, `update_channel` (`stable` หรือ `beta`) ตัว beta คือ prerelease ลองก่อน stable ได้ถ้าสะดวก

อัปเดตบังคับ: ใส่บรรทัดเดียว (`!mandatory`, `[mandatory]`, `[force]` หรือ `mandatory:true`) ไว้ใน release body แอปจะขึ้นแถบบังคับแล้วเน้นปุ่ม Restart จนกว่าจะกดติดตั้ง

release แบบเซ็น (แนะนำถ้าทำได้): ตั้ง repo secret `UPDATE_SIGNING_KEY` (Ed25519 private key 32-byte แบบ hex) workflow จะเซ็น `checksums.txt` ออกมาเป็น `checksums.txt.sig` เอา public key ไปวางให้ client เป็น `update_pubkey.hex` (ข้าง exe หรือในโฟลเดอร์ data) หรือผ่าน env `CRONUS_UPDATE_PUBKEY` ส่วน release เก่าที่ไม่มีลายเซ็นก็ยังใช้ต่อได้ด้วย SHA256 ถ้ายังไม่ได้ตั้ง pubkey

## เริ่มใช้จากซอร์ส

ทำตามนี้:

1. Clone repo:

```powershell
git clone https://github.com/q0HtHHftAS/Roblox-Auto-Rejoin-windown.git
cd Roblox-Auto-Rejoin-windown
```

2. ลง dependencies:

```powershell
python -m pip install -r requirements.txt
```

3. เปิด launcher เลือกวิธีใดวิธีหนึ่ง ถ้าใช้ batch runner รันคำสั่งนี้:

```powershell
.\Run.cmd
```

ถ้าใช้ Python รันคำสั่งนี้:

```powershell
python main.py
```

พอรันแล้ว service ก็ขึ้นที่ 127.0.0.1 พร้อมเปิดหน้าจอ dashboard ให้เอง

## สร้างไฟล์ exe

```powershell
python -m pip install -r requirements.txt pyinstaller
python -c "from PIL import Image; Image.open('assets/cronus_icon.png').save('assets/cronus_icon.ico')"
pyinstaller cronus_launcher.spec
```

ผลลัพธ์อยู่ที่ `dist/CronusLauncher.exe` ฝั่ง Releases ก็ build ด้วยวิธีเดียวกันผ่าน GitHub Actions ทุกครั้งที่ดัน tag `v*` ชื่อ tag ต้องตรงกับ `APP_VERSION` ใน `version.py`

## สคริปต์ Lua ในเกม

สคริปต์ loader (ตัวโหลดโค้ด telemetry เข้าเกม) ช่วยให้รีจอยไวขึ้น อยากส่ง telemetry แล้วรีจอยเร็วขึ้นก็เอาไฟล์นี้ไปรันใน executor ของ Roblox:

```text
lua/run_in_executor.lua
```

## ข้อมูลและความเป็นส่วนตัว

config กับสถานะบัญชีเก็บไว้บนเครื่องตรงนี้:

```text
%LOCALAPPDATA%\Cronus Launcher\data
```

cookie ของบัญชีเข้ารหัสด้วย Windows DPAPI บนเครื่องตัวเอง ไม่ส่ง cookie ออกไปเซิร์ฟเวอร์ข้างนอก ไม่ commit cookie ลง repo อัปเดตตัวเองเปลี่ยนแค่ไฟล์ exe โฟลเดอร์ data ด้านบนไม่แตะเลย
