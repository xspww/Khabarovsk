# Features
🚀 ระบบจัดการหลายบัญชี (Multi-Account Runtime)
เปิด Roblox หลายบัญชีพร้อมกันได้
แยก runtime ของแต่ละบัญชีออกจากกัน
ตรวจสอบ ownership ของ process แต่ละตัว
ลดปัญหา runtime ชนกันหรือ bind ผิด instance
🔄 ระบบ Rejoin & Recovery อัตโนมัติ
Rejoin อัตโนมัติเมื่อเกมหลุด
ตรวจจับ crash / freeze / disconnect
Recovery ตาม state จริงของ runtime
ลดปัญหา loop rejoin มั่วหรือ retry ซ้อนกัน
มี recovery context สำหรับจัดการสถานะต่อเนื่อง
🛡️ Runtime Watchdog System
ตรวจสอบ health ของ Roblox runtime ตลอดเวลา
ตรวจจับ popup / error window / unexpected state
ป้องกัน runtime ค้างโดยไม่มี owner ดูแล
มี invariant validation ลด state เพี้ยน
⚙️ Process & Resource Control
ตรวจจับ Roblox process แบบแยก instance
จัดการ CPU limiter
ปรับ process priority ได้
ควบคุม resource usage สำหรับหลายจอหลายบัญชี
🌐 Web Dashboard UI
Dashboard แบบ realtime
ดูสถานะ account ได้ทั้งหมด
แสดง runtime state ปัจจุบัน
ดู log และ timeline การ recovery
ควบคุม launcher ผ่าน browser ได้
📊 Structured Logging System
เก็บ log แบบ structured
รองรับ JSONL event logging
มี runtime audit events
ใช้ debug และ trace ปัญหา runtime ได้ง่ายขึ้น
🧠 State-Driven Architecture
ใช้ state machine แทน retry loop ธรรมดา
แยก lifecycle ของ runtime ชัดเจน
ลด chaos จาก watchdog ซ้อนกัน
ทำให้ระบบ maintain และ stabilize ง่ายขึ้น
