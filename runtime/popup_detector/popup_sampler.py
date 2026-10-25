from __future__ import annotations

import ctypes
import json
import os
import threading
import time
from ctypes import wintypes
from typing import Any, NamedTuple

from runtime.popup_detector.popup_classifier import PopupClassification, classify_popup_observation
from runtime.recovery_context import VISUAL_DISCONNECT
from runtime.window_text import collect_window_text, make_text_collector
from services.captcha_guard import CAPTCHA_REASON


def _detect_visual_features_lazy(screenshot):
    # Lazy import to avoid loading heavy PIL/visual pipeline when disabled
    if screenshot is None:
        return {"matched": False, "score": 0.0, "reason": "no_screenshot"}
    # Allow disabling visual pipeline via env/config for perf
    if not _visual_enabled():
        return {"matched": False, "score": 0.0, "reason": "visual_disabled"}
    try:
        from runtime.popup_detector.popup_visual_detector import detect_visual_features
        return detect_visual_features(screenshot)
    except Exception as exc:
        return {"matched": False, "score": 0.0, "reason": f"visual_import:{exc}"}

def _visual_enabled() -> bool:
    # Visual pipeline is heavy (6 screenshots + PIL). Disable by default if env flag says text-only.
    # Keep enabled when popup_disconnected_enabled is true but can be overridden.
    import os
    if os.environ.get("CRONUS_POPUP_VISUAL", "").strip().lower() in {"0", "false", "off", "text"}:
        return False
    return True


_HOLD_LOCK = threading.RLock()
_INSPECTION_HOLDS: dict[int, float] = {}


def clear_expired_inspection_holds(now: float | None = None) -> None:
    now = time.time() if now is None else float(now)
    with _HOLD_LOCK:
        expired = [pid for pid, until in _INSPECTION_HOLDS.items() if until <= now]
        for pid in expired:
            _INSPECTION_HOLDS.pop(pid, None)


def is_inspection_held(pid: int | None) -> bool:
    if not pid:
        return False
    clear_expired_inspection_holds()
    with _HOLD_LOCK:
        return float(_INSPECTION_HOLDS.get(int(pid), 0.0) or 0.0) > time.time()


def _set_inspection_hold(pid: int | None, seconds: float = 15.0) -> None:
    if not pid:
        return
    with _HOLD_LOCK:
        _INSPECTION_HOLDS[int(pid)] = time.time() + max(1.0, float(seconds or 15.0))


def _release_inspection_hold(pid: int | None) -> None:
    if not pid:
        return
    with _HOLD_LOCK:
        _INSPECTION_HOLDS.pop(int(pid), None)


class PopupWindowSampler:
    def __init__(self, inspection_width: int = 320, inspection_height: int = 240):
        self.inspection_width = max(320, int(inspection_width or 320))
        self.inspection_height = max(240, int(inspection_height or 240))

    def windows_for_pid(self, pid: int | None, include_hidden: bool = True) -> list[dict[str, Any]]:
        if not pid:
            return []
        try:
            user32 = ctypes.windll.user32
            windows: list[dict[str, Any]] = []
            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            def _enum_callback(hwnd, lparam):
                if not include_hidden and not user32.IsWindowVisible(hwnd):
                    return True
                win_pid = ctypes.c_ulong(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
                if int(win_pid.value or 0) != int(pid):
                    return True
                rect = RECT()
                if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    return True
                width = max(0, int(rect.right - rect.left))
                height = max(0, int(rect.bottom - rect.top))
                area = width * height
                if area <= 0:
                    return True
                windows.append({
                    "pid": int(pid),
                    "hwnd": int(hwnd),
                    "left": int(rect.left),
                    "top": int(rect.top),
                    "right": int(rect.right),
                    "bottom": int(rect.bottom),
                    "width": width,
                    "height": height,
                    "area": area,
                    "visible": bool(user32.IsWindowVisible(hwnd)),
                    "iconic": bool(user32.IsIconic(hwnd)),
                })
                return True

            user32.EnumWindows(WNDENUMPROC(_enum_callback), 0)
            return sorted(windows, key=lambda item: int(item.get("area") or 0), reverse=True)
        except Exception:
            return []

    def prepare_popup_inspection(self, pid: int | None, hold_seconds: float = 15.0) -> dict[str, Any]:
        windows = self.windows_for_pid(pid, include_hidden=True)
        if not windows:
            return {"ok": False, "reason": "no_hwnd", "pid": pid, "windows": []}
        target = windows[0]
        hwnd = int(target.get("hwnd") or 0)
        if not hwnd:
            return {"ok": False, "reason": "missing_hwnd", "pid": pid, "windows": windows}
        _set_inspection_hold(pid, hold_seconds)
        try:
            user32 = ctypes.windll.user32
            SW_RESTORE = 9
            SW_SHOWNA = 8
            if bool(target.get("iconic")):
                user32.ShowWindow(ctypes.c_void_p(hwnd), SW_RESTORE)
            elif not bool(target.get("visible")):
                user32.ShowWindow(ctypes.c_void_p(hwnd), SW_SHOWNA)
            return {"ok": True, "pid": pid, "hwnd": hwnd, "resized": False, "windows": windows}
        except Exception as exc:
            return {"ok": False, "reason": str(exc), "pid": pid, "hwnd": hwnd, "windows": windows}

    def focus_pid_window(self, pid: int | None) -> dict[str, Any]:
        windows = self.windows_for_pid(pid, include_hidden=True)
        if not windows:
            return {"ok": False, "reason": "no_hwnd", "pid": pid, "windows": []}
        target = windows[0]
        hwnd = int(target.get("hwnd") or 0)
        if not hwnd:
            return {"ok": False, "reason": "missing_hwnd", "pid": pid, "windows": windows}
        try:
            user32 = ctypes.windll.user32
            SW_RESTORE = 9
            SW_SHOW = 5
            if bool(target.get("iconic")):
                user32.ShowWindow(ctypes.c_void_p(hwnd), SW_RESTORE)
            else:
                user32.ShowWindow(ctypes.c_void_p(hwnd), SW_SHOW)
            user32.BringWindowToTop(ctypes.c_void_p(hwnd))
            focused = bool(user32.SetForegroundWindow(ctypes.c_void_p(hwnd)))
            return {"ok": True, "focused": focused, "pid": pid, "hwnd": hwnd, "windows": windows}
        except Exception as exc:
            return {"ok": False, "reason": str(exc), "pid": pid, "hwnd": hwnd, "windows": windows}

    def read_texts(self, hwnd: int) -> list[str]:
        if not hwnd:
            return []
        try:
            user32 = ctypes.windll.user32
            texts: list[str] = []
            seen = set()
            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)
            child_callback = make_text_collector(user32, seen, texts)

            collect_window_text(user32, hwnd, seen, texts)
            user32.EnumChildWindows(ctypes.c_void_p(hwnd), WNDENUMPROC(child_callback), 0)
            return texts
        except Exception:
            return []

    def capture_window_image(self, hwnd: int):
        if not hwnd:
            return None
        try:
            from PIL import Image

            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32

            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            rect = RECT()
            if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
                return None
            width = max(0, int(rect.right - rect.left))
            height = max(0, int(rect.bottom - rect.top))
            if width <= 0 or height <= 0:
                return None

            hwnd_dc = user32.GetWindowDC(ctypes.c_void_p(hwnd))
            mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
            bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
            gdi32.SelectObject(mem_dc, bitmap)
            ok = user32.PrintWindow(ctypes.c_void_p(hwnd), mem_dc, 0x00000002)
            if not ok:
                user32.PrintWindow(ctypes.c_void_p(hwnd), mem_dc, 0)

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize", wintypes.DWORD),
                    ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD),
                ]

            class BITMAPINFO(ctypes.Structure):
                _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = width
            bmi.bmiHeader.biHeight = -height
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0

            buffer = ctypes.create_string_buffer(width * height * 4)
            gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(bmi), 0)
            image = Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1).convert("L")

            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(ctypes.c_void_p(hwnd), hwnd_dc)
            return image
        except Exception:
            return None


class _SampleTally(NamedTuple):
    samples: list[PopupClassification]
    candidates: list[PopupClassification]
    positive: list[PopupClassification]
    coded: list[PopupClassification]
    text_confirmed: list[PopupClassification]
    visual_positive: list[PopupClassification]
    captcha_confirmed: list[PopupClassification]
    best: PopupClassification
    visual_recovery_confirmed: bool
    confirmed: bool


class PopupObserver:
    def __init__(
        self,
        sample_count: int = 6,
        sample_interval: float = 0.25,
        threshold: float = 1.0,
        stable_samples: int = 2,
    ):
        self.sample_count = max(1, int(sample_count or 6))
        self.sample_interval = max(0.0, float(sample_interval or 0.25))
        self.threshold = max(0.1, float(threshold or 1.0))
        self.stable_samples = max(1, int(stable_samples or 2))
        self.sampler = PopupWindowSampler()

    def _is_strong_visual_disconnect(self, item: PopupClassification) -> bool:
        return bool(
            item.matched
            and item.visual_disconnect
            and item.evidence_source == "visual_strong"
            and item.visual_strength == "strong"
            and item.confidence >= self.threshold
        )

    def _is_recoverable_visual_candidate(self, item: PopupClassification) -> bool:
        if not self._is_strong_visual_disconnect(item):
            return False
        if item.visual_stage not in {"modal_button", "structural_button"}:
            return False
        if item.button_pattern not in {"single", "double"}:
            return False
        if item.visual_stage == "structural_button":
            # The structural pipeline confirms overlay + modal + separator + an
            # in-modal button (structural_score >= 0.96); its strength is carried
            # by structural_score, not modal_score.
            return bool(
                item.overlay_score >= 0.25
                and item.structural_score >= 0.96
                and item.button_score >= 0.38
            )
        return bool(
            item.overlay_score >= 0.25
            and item.modal_score >= 1.0
            and item.button_score >= 0.38
        )

    def _maybe_save_captcha_sample(self, pid, texts, screenshot, classification) -> None:
        """Debug hook: when CRONUS_CAPTCHA_SAMPLE_DIR is set, save every screen
        classified as captcha (plus a metadata JSON) so the visual detector can
        be tuned against real screenshots. No-op by default."""
        sample_dir = os.environ.get("CRONUS_CAPTCHA_SAMPLE_DIR", "").strip()
        if not sample_dir or screenshot is None:
            return
        try:
            os.makedirs(sample_dir, exist_ok=True)
            stamp = f"{int(time.time() * 1000)}_{int(pid or 0)}"
            png_path = os.path.join(sample_dir, f"captcha_{stamp}.png")
            screenshot.save(png_path)
            meta = {
                "pid": pid,
                "time": time.time(),
                "texts": list(texts or []),
                "reason_key": classification.reason_key,
                "action": classification.action,
                "evidence_source": classification.evidence_source,
                "visual_strength": classification.visual_strength,
                "visual_stage": classification.visual_stage,
                "confidence": classification.confidence,
                "confidence_breakdown": dict(classification.confidence_breakdown or {}),
            }
            meta_path = os.path.join(sample_dir, f"captcha_{stamp}.json")
            with open(meta_path, "w", encoding="utf-8") as handle:
                json.dump(meta, handle, ensure_ascii=False, indent=2)
        except Exception:
            "intentional: ignore exception"

    def _resolve_hwnd(self, pid: int, prepared: dict[str, Any]) -> int:
        hwnd = int((prepared.get("hwnd") if prepared else 0) or 0)
        if not hwnd:
            windows = self.sampler.windows_for_pid(pid, include_hidden=True)
            hwnd = int((windows[0].get("hwnd") if windows else 0) or 0)
        return hwnd

    def _maybe_save_captcha(
        self,
        pid: int,
        hwnd: int,
        texts: list[str],
        screenshot: Any,
        classification: PopupClassification,
    ) -> None:
        if classification.reason_key != CAPTCHA_REASON:
            return
        if screenshot is None and hwnd:
            screenshot = self.sampler.capture_window_image(hwnd)
        self._maybe_save_captcha_sample(pid, texts, screenshot, classification)

    def _collect_samples(
        self,
        pid: int,
        hwnd: int,
        sample_total: int,
        interval: float,
        process_idle: bool,
    ) -> list[PopupClassification]:
        samples: list[PopupClassification] = []
        for index in range(sample_total):
            if not hwnd:
                hwnd = self._resolve_hwnd(pid, {})
            texts = self.sampler.read_texts(hwnd) if hwnd else []
            screenshot = None
            classification = classify_popup_observation(
                texts,
                {},
                process_idle=process_idle,
                threshold=self.threshold,
            )
            if not classification.matched:
                screenshot = self.sampler.capture_window_image(hwnd) if hwnd else None
                classification = classify_popup_observation(
                    texts,
                    _detect_visual_features_lazy(screenshot),
                    process_idle=process_idle,
                    threshold=self.threshold,
                )
            samples.append(classification)
            self._maybe_save_captcha(pid, hwnd, texts, screenshot, classification)
            if index < sample_total - 1 and interval > 0:
                if classification.error_code:
                    break
                time.sleep(interval)
        return samples

    def _positive_item(
        self,
        item: PopupClassification,
        candidates: list[PopupClassification],
    ) -> bool:
        if not item.matched:
            return False
        if item.recovery_allowed and (
            item.confidence >= self.threshold or item.visual_disconnect
        ):
            return True
        return item in candidates

    def _is_confirmed(
        self,
        coded: list[PopupClassification],
        text_confirmed: list[PopupClassification],
        visual_positive: list[PopupClassification],
        captcha_confirmed: list[PopupClassification],
        visual_recovery_confirmed: bool,
    ) -> bool:
        return bool(
            coded
            or len(text_confirmed) >= self.stable_samples
            or len(visual_positive) >= self.stable_samples
            or len(captcha_confirmed) >= self.stable_samples
            or visual_recovery_confirmed
        )

    def _tally_samples(self, samples: list[PopupClassification]) -> _SampleTally:
        candidates = [
            item for item in samples
            if self._is_recoverable_visual_candidate(item)
        ]
        positive = [
            item for item in samples
            if self._positive_item(item, candidates)
        ]
        coded = [item for item in samples if item.error_code]
        text_confirmed = [
            item for item in samples
            if item.recovery_allowed and item.evidence_source == "text"
        ]
        visual_positive = [
            item for item in positive
            if item.evidence_source == "visual_strong"
        ]
        captcha_confirmed = [
            item for item in samples
            if item.reason_key == "captcha_required"
        ]
        best = max(samples, key=lambda item: item.confidence, default=PopupClassification(False))
        visual_recovery_confirmed = len(candidates) >= self.stable_samples
        confirmed = self._is_confirmed(
            coded,
            text_confirmed,
            visual_positive,
            captcha_confirmed,
            visual_recovery_confirmed,
        )
        return _SampleTally(
            samples,
            candidates,
            positive,
            coded,
            text_confirmed,
            visual_positive,
            captcha_confirmed,
            best,
            visual_recovery_confirmed,
            confirmed,
        )

    def _inspection_result(
        self,
        tally: _SampleTally,
        prepared: dict[str, Any],
    ) -> dict[str, Any]:
        result = tally.best.to_dict()
        if tally.visual_recovery_confirmed and not result.get("recovery_allowed"):
            visual_best = max(tally.candidates, key=lambda item: item.confidence)
            result = visual_best.to_dict()
            result.update({
                "action": "rejoin",
                "reason_key": "connection_error",
                "disconnect_category": VISUAL_DISCONNECT,
                "recovery_allowed": True,
            })
        recovery_allowed = bool(result.get("recovery_allowed"))
        result.update({
            "matched": bool(tally.confirmed),
            "recovery_allowed": bool(tally.confirmed and recovery_allowed),
            "sample_count": len(tally.samples),
            "positive_samples": len(tally.positive),
            "samples_confirmed": len(tally.coded) or len(tally.text_confirmed) or len(tally.visual_positive) or len(tally.captcha_confirmed),
            "visual_positive_samples": len(tally.visual_positive),
            "text_positive_samples": len(tally.text_confirmed),
            "captcha_samples": len(tally.captcha_confirmed),
            "prepared": prepared,
            "stable_required": self.stable_samples,
        })
        if not tally.confirmed:
            result["action"] = ""
            result["reason_key"] = ""
            result["disconnect_category"] = ""
            result["error_code"] = tally.best.error_code
        return result

    def inspect_pid(
        self,
        pid: int | None,
        *,
        prepare: bool = False,
        process_idle: bool = False,
        sample_count: int | None = None,
        sample_interval: float | None = None,
    ) -> dict[str, Any]:
        if pid is None:
            return {"matched": False, "action": "", "reason_key": "", "detail": "", "error_code": ""}
        sample_total = max(1, int(sample_count or self.sample_count))
        interval = max(0.0, float(self.sample_interval if sample_interval is None else sample_interval))
        prepared = self.sampler.prepare_popup_inspection(pid) if prepare else {}
        try:
            hwnd = self._resolve_hwnd(pid, prepared)
            samples = self._collect_samples(
                pid,
                hwnd,
                sample_total,
                interval,
                process_idle,
            )
            return self._inspection_result(self._tally_samples(samples), prepared)
        finally:
            if prepare:
                _release_inspection_hold(pid)


DEFAULT_POPUP_OBSERVER = PopupObserver()
