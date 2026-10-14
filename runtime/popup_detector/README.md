# runtime/popup_detector/ — Popup classification sub-package

Isolates screenshot → popup-verdict rules so `runtime/account_worker.py`
stays thin. Pure functions over sampled frames; no farm imports.

| File | Does what | Connects to |
| ---- | --------- | ----------- |
| `popup_classifier.py` | Verdict entry: maps signals to popup type. | `runtime/account_worker.py`, `runtime/captcha_detector.py`. |
| `popup_confidence.py` | Confidence scoring / thresholds. | `popup_classifier.py`. |
| `popup_sampler.py` | Frame sampling + dedup. | `popup_text_detector.py`, `popup_visual_detector.py`. |
| `popup_text_detector.py` | OCR/text rules. | `popup_classifier.py`. |
| `popup_visual_detector.py` | Template/visual rules. | `popup_classifier.py`. |
