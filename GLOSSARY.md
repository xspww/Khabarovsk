# GLOSSARY — ภาษาโดเมนของ Khabarovsk

ตั้งชื่อ module ให้ตรงกันทั้งทีม อย่าเรียกชื่อไฟล์หรือชื่อ class แทน

| คำ | หมายถึง | module ที่เป็นเจ้าของ |
|---|---|---|
| Recovery | การกู้คืน / rejoin บัญชีหลัง crash disconnect guard-loss | module การกู้คืน (`runtime/recovery_*`) |
| Recovery Policy | นโยบายว่าสัญญาณไหนควรกันซ้ำ หน่วงเท่าไร งบเท่าไร | module นโยบายการกู้คืน (`runtime/recovery_policy.py`) |
| Recovery Owner | ใครถือสิทธิ์กู้คืนบัญชีนั้นอยู่กันชน | module เจ้าของการกู้คืน (`runtime/recovery_owner.py`) |
| Recovery Storm | เบรกหลังชนฝาเวลาหลายบัญชีขอกู้คืนพร้อมกัน | module พายุการกู้คืน (`runtime/recovery_storm.py`) |
| Recovery Slots | นโยบาย slot ว่ากี่บัญชีครอง slot อยู่ ใครต่อคิวได้ | module slot การกู้คืน (`runtime/recovery_slots.py`) |
| Farm | วงจร start / stop / reconcile ทั้งฟาร์ม | module ฟาร์ม (`runtime/farm*.py`) |
| Launch | สาย validate → guard → VIP → ticket → launch ของ Roblox | module การปล่อย Roblox (`services/roblox_launcher.py`, `services/roblox_launch_service.py`) |
| Process Binding | การผูก PID กับบัญชี ตัดสิน ownership / adopt / kill | module การผูก process (`services/process_service.py`) |
| Window | การจัด arrange / hide / show หน้าต่าง Roblox | module หน้าต่าง (`services/window_control.py` เป็น adapter) |
| Settings | การ normalize / clamp / fan-out ค่าตั้ง | module ตั้งค่า (`api_routes/settings_state.py`, `runtime/system_maintenance.py`) |
