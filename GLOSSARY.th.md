# GLOSSARY — ภาษาโดเมนของ Khabarovsk

[English](GLOSSARY.md) | ไทย

ใช้คำเหล่านี้ให้ตรงกันทั้งใน code, commit และเอกสาร อย่าเอาชื่อไฟล์หรือชื่อ class มาใช้แทนคำ โมดูลเจ้าของคือที่เดียวที่นิยามแนวคิดนั้น ลิงก์ไปที่โมดูลแทนการเขียนกฎซ้ำ

| คำ | ความหมาย | โมดูลเจ้าของ |
| --- | --- | --- |
| Recovery | พาบัญชีกลับเข้าเกมหลัง crash, หลุดการเชื่อมต่อ หรือ guard หลุด รวมถึงขั้นตอน rejoin | โมดูล recovery (`runtime/recovery_*.py`) |
| Recovery Policy | กฎว่าควร dedupe สัญญาณ recovery แบบไหน หน่วงนานเท่าไร และใช้ budget เท่าไร | นโยบาย recovery (`runtime/recovery_policy.py`) |
| Recovery Owner | คนเดียวที่ถือสิทธิ์กู้คืนบัญชีนั้น เพื่อไม่ให้ recovery สองทางแย่งกัน | เจ้าของ recovery (`runtime/recovery_owner.py`) |
| Recovery Storm | circuit breaker ที่หน่วงหรือเว้นจังหวะ recovery เมื่อหลายบัญชีขอกู้คืนพร้อมกัน | Recovery storm (`runtime/recovery_storm.py`) |
| Recovery Slots | นโยบาย slot ว่ากี่บัญชีกู้คืนพร้อมกันได้ และบัญชีไหนต้องรอคิว | Recovery slots (`runtime/recovery_slots.py`) |
| Farm | บัญชีทั้งหมดที่ระบบดูแล รวมถึงวงจร start / stop / reconcile ที่ขับเคลื่อนบัญชีเหล่านั้น | โมดูล farm (`runtime/farm*.py`) |
| Launch | ไปป์ไลน์เปิด Roblox: validate → guard → VIP → ticket → launch | Roblox launcher (`services/roblox_launcher.py`, `services/roblox_launch_service.py`) |
| Process Binding | การผูก PID เข้ากับบัญชี แล้วตัดสินเรื่อง ownership, adopt และสิทธิ์ kill | Process service (`services/process_service.py`) |
| Window | การจัดวาง ซ่อน และแสดงหน้าต่าง Roblox | Window control (`services/window_control.py` — adapter) |
| Settings | การ normalize, clamp และ fan-out ค่าตั้ง | Settings state (`api_routes/settings_state.py`, `runtime/system_maintenance.py`) |
