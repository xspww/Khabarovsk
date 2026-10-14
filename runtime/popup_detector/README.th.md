# runtime/popup_detector/ — ซับแพ็กเกจจำแนก popup

แยกกฎ screenshot → คำตัดสิน popup ให้ `runtime/account_worker.py`
บางลง ฟังก์ชันล้วนบนเฟรมที่ sample มา ไม่ import farm

| ไฟล์ | ทำอะไร | เชื่อมกับ |
| ---- | --------- | ----------- |
| `popup_classifier.py` | ทางเข้าคำตัดสิน: map สัญญาณเป็นชนิด popup | `runtime/account_worker.py`, `runtime/captcha_detector.py` |
| `popup_confidence.py` | คะแนนความมั่นใจ / threshold | `popup_classifier.py` |
| `popup_sampler.py` | sample + dedup เฟรม | `popup_text_detector.py`, `popup_visual_detector.py` |
| `popup_text_detector.py` | กฎ OCR/ข้อความ | `popup_classifier.py` |
| `popup_visual_detector.py` | กฎ template/ภาพ | `popup_classifier.py` |
