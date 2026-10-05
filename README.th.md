<p align="center">
  <img src="assets/cronus_icon.png" width="120" alt="โลโก้ Cronus Launcher" />
</p>

<h1 align="center">Cronus Launcher</h1>

<p align="center">ตัวจัดการ Roblox หลายบัญชี พร้อมรีจอยอัตโนมัติ ควบคุมทรัพยากรเครื่อง และรองรับ executor</p>

<p align="center">
  <a href="https://github.com/xspww/Khabarovsk/releases"><img src="https://img.shields.io/github/v/release/xspww/Khabarovsk?label=release" alt="release" /></a>
  <a href="https://github.com/xspww/Khabarovsk/actions/workflows/release.yml"><img src="https://github.com/xspww/Khabarovsk/actions/workflows/release.yml/badge.svg" alt="build" /></a>
  <img src="https://img.shields.io/badge/platform-Windows-blue" alt="platform" />
  <img src="https://img.shields.io/badge/python-3.11+-blue" alt="python" />
</p>

<p align="center">
  <a href="README.md">English</a> | ไทย
</p>

<p align="center">
  <a href="#ภาพรวม">ภาพรวม</a> •
  <a href="#ฟีเจอร์">ฟีเจอร์</a> •
  <a href="#สิ่งที่ต้องมี">สิ่งที่ต้องมี</a> •
  <a href="#การติดตั้ง">การติดตั้ง</a> •
  <a href="#เริ่มต้นใช้งานจากซอร์ส">เริ่มใช้</a> •
  <a href="#สร้างไฟล์-exe-จากซอร์ส">สร้าง exe</a> •
  <a href="#สคริปต์-lua-ในเกม">สคริปต์ Lua</a> •
  <a href="#ข้อมูลและความเป็นส่วนตัว">ความเป็นส่วนตัว</a>
</p>

<img width="1024" height="768" alt="sadc" src="https://github.com/user-attachments/assets/baf120ff-95ba-4d66-91b3-099c9648a08b" />

## ภาพรวม

Cronus Launcher จัดการบัญชี Roblox หลายบัญชีจากแดชบอร์ดเดียว ตรวจสอบสถานะการเล่นเกม รีจอยอัตโนมัติเมื่อหลุด และคุมโหลด CPU หน่วยความจำ และกราฟิกเวลาเปิดหลายจอในเครื่องเดียว

## ฟีเจอร์

- **จัดการหลายบัญชี** — เพิ่ม จัดการ และเปิดบัญชี Roblox หลายบัญชีจากแดชบอร์ดเดียว
- **รีจอยอัตโนมัติ** — ตรวจจับการหลุดและกลับเข้าเซิร์ฟเวอร์ที่ตั้งไว้โดยไม่ต้องกดเอง
- **ควบคุมทรัพยากร** — จำกัด CPU จัดการ RAM จำกัด FPS โหมดกราฟิกต่ำ และจัดเลย์เอาต์หน้าต่างสำหรับการเปิดหลายจอ
- **รองรับ executor** — ตรวจสอบความเข้ากันได้ของ executor เทียบกับเวอร์ชัน Roblox ทางการ พร้อมเปิดใหม่ให้เอง
- **จัดการเวอร์ชัน Roblox** — ตรวจสอบเวอร์ชันไคลเอนต์ทางการ ติดตั้งหรืออัปเดตตัวเกม และลบเวอร์ชันเก่าออก
- **ปลอดภัยในเครื่อง** — เข้ารหัส cookie และข้อมูลบัญชีด้วย Windows DPAPI ไม่ส่งออกไปเซิร์ฟเวอร์ภายนอก

## สิ่งที่ต้องมี

| องค์ประกอบ | ความต้องการ |
| --------- | ----------- |
| ระบบปฏิบัติการ | Windows 10 หรือ Windows 11 รุ่น 64-bit |
| รันไทม์ (ตัว release) | ไม่ต้องลงเพิ่ม — ไฟล์ `.exe` จากหน้า Releases พร้อมใช้งาน |
| รันไทม์ (รันจากซอร์ส) | Python 3.11 ขึ้นไป |
| ตัวเกม | ติดตั้ง Roblox Player ไว้บนเครื่องแล้ว |

## การติดตั้ง

วิธีแนะนำสำหรับผู้ใช้ทั่วไป ไม่ต้องลง Python

1. เปิด[หน้า Releases](https://github.com/xspww/Khabarovsk/releases)
2. ดาวน์โหลด `CronusLauncher-<version>.exe` แล้วรันได้ทันที
3. ตัว portable `CronusLauncher-<version>-portable.zip` ประกอบด้วย `CronusLauncher.exe` และ `Api.lua` ไฟล์ exe ใช้ชื่อคงเดิมเพื่อให้อัปเดตแบบแทนที่ไฟล์ได้ ส่วน `Api.lua` คือสคริปต์ loader สำหรับรันใน executor

### ตรวจสอบไฟล์ดาวน์โหลด

```powershell
(Get-FileHash CronusLauncher-<version>.exe -Algorithm SHA256).Hash
```

นำค่าที่ได้ไปเทียบกับค่า SHA256 ที่ระบุไว้ในหน้า release

### การอัปเดตและลายเซ็น

- ตัวอัปเดตในแอปใช้ได้กับไฟล์ exe จาก Releases บน Windows เท่านั้น และต้องเชื่อมต่ออินเทอร์เน็ต ไม่รองรับการรันจากซอร์ส
- ไฟล์ release ทุกชิ้นเซ็นด้วยคีย์ release ของโปรเจกต์ โปรแกรมตรวจสอบลายเซ็นและ SHA256 ก่อนติดตั้งอัปเดต ผู้ใช้ไม่ต้องตั้งค่า certificate เอง
- ไฟล์ exe ไม่ได้เซ็นด้วยใบรับรอง publisher เชิงพาณิชย์ของ Windows SmartScreen ก็เลยอาจเตือนเมื่อรันครั้งแรก ให้ดาวน์โหลดจากหน้า Releases ที่ลิงก์ไว้ด้านบนเท่านั้น
- หากเก็บไฟล์ exe ไว้ในโฟลเดอร์ที่ต้องใช้สิทธิ์ผู้ดูแลระบบ เช่น `Program Files` ตัวอัปเดตจะขอสิทธิ์ administrator ก่อนเขียนไฟล์ ถ้าไม่มีสิทธิ์ให้ย้ายไฟล์ exe ไปไว้ในโฟลเดอร์ที่เขียนได้

## เริ่มต้นใช้งานจากซอร์ส

1. Clone repository:

```powershell
git clone https://github.com/xspww/Khabarovsk.git
cd Khabarovsk
```

2. ติดตั้ง dependencies:

```powershell
python -m pip install -r requirements.txt
```

3. เปิด launcher ด้วยวิธีใดวิธีหนึ่ง:

```powershell
.\Run.cmd
```

```powershell
python main.py
```

เมื่อรันแล้ว local service จะรันที่ `127.0.0.1` พร้อมเปิดหน้าต่างแดชบอร์ดให้อัตโนมัติ

## สร้างไฟล์ exe จากซอร์ส

```powershell
python -m pip install -r requirements.txt pyinstaller
python -c "from PIL import Image; Image.open('assets/cronus_icon.png').save('assets/cronus_icon.ico')"
pyinstaller cronus_launcher.spec
```

ผลลัพธ์อยู่ที่ `dist/CronusLauncher.exe`

ฝั่ง Releases ก็ build ด้วยวิธีเดียวกันผ่าน GitHub Actions ทุกครั้งที่ดัน tag `v*` ชื่อ tag ต้องตรงกับ `APP_VERSION` ใน `version.py`

## สคริปต์ Lua ในเกม

รันในตัวเกมแล้ว สคริปต์ loader จะช่วยส่ง telemetry และรีจอยได้เร็วขึ้น ให้นำไฟล์นี้ไปรันใน executor ของ Roblox:

```text
lua/run_in_executor.lua
```

ไฟล์ portable มีไฟล์เดียวกันนี้ในชื่อ `Api.lua` วางไว้ข้างไฟล์ exe

## ข้อมูลและความเป็นส่วนตัว

config และสถานะบัญชีเก็บไว้บนเครื่องที่:

```text
%LOCALAPPDATA%\Cronus Launcher\data
```

- cookie ของบัญชีเข้ารหัสด้วย Windows DPAPI บนเครื่อง
- ไม่ส่ง cookie ออกไปเซิร์ฟเวอร์ภายนอก และไม่ commit cookie ลง repository
- เมื่อมีเวอร์ชันใหม่ โปรแกรมจะแสดงปุ่มให้เปิดหน้า Releases เพื่อดาวน์โหลดไฟล์ใหม่ โฟลเดอร์ data ด้านบนจะไม่ถูกแตะต้องระหว่างอัปเดต
