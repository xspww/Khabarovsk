from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Any, Dict, List, Optional

from runtime.popup_detector.popup_classifier import PopupClassification, classify_popup_observation
from runtime.popup_detector.popup_visual_detector import detect_visual_features


_HOLD_LOCK = threading.RLock()
_INSPECTION_HOLDS: Dict[int, float] = {}


def clear_expired_inspection_holds(now: Optional[float] = None) -> None:
    now = time.time() if now is None else float(now)
    with _HOLD_LOCK:
        expired = [pid for pid, until in _INSPECTION_HOLDS.items() if until <= now]
        for pid in expired:
            _INSPECTION_HOLDS.pop(pid, None)


def is_inspection_held(pid: Optional[int]) -> bool:
    if not pid:
        return False
    clear_expired_inspection_holds()
    with _HOLD_LOCK:
        return float(_INSPECTION_HOLDS.get(int(pid), 0.0) or 0.0) > time.time()


def _set_inspection_hold(pid: Optional[int], seconds: float = 15.0) -> None:
    if not pid:
        return
    with _HOLD_LOCK:
        _INSPECTION_HOLDS[int(pid)] = time.time() + max(1.0, float(seconds or 15.0))


def _release_inspection_hold(pid: Optional[int]) -> None:
    if not pid:
        return
    with _HOLD_LOCK:
        _INSPECTION_HOLDS.pop(int(pid), None)


class PopupWindowSampler:
    def __init__(self, inspection_width: int = 800, inspection_height: int = 600):
        self.inspection_width = max(640, int(inspection_width or 800))
        self.inspection_height = max(480, int(inspection_height or 600))

    def windows_for_pid(self, pid: Optional[int], include_hidden: bool = True) -> List[Dict[str, Any]]:
        if not pid:
            return []
        try:
            user32 = ctypes.windll.user32
            windows: List[Dict[str, Any]] = []
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

    def prepare_popup_inspection(self, pid: Optional[int], hold_seconds: float = 15.0) -> Dict[str, Any]:
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
            SWP_NOZORDER = 0x0004
            SWP_NOACTIVATE = 0x0010
            SWP_SHOWWINDOW = 0x0040
            if bool(target.get("iconic")):
                user32.ShowWindow(ctypes.c_void_p(hwnd), SW_RESTORE)
            elif not bool(target.get("visible")):
                user32.ShowWindow(ctypes.c_void_p(hwnd), SW_SHOWNA)
            width = int(target.get("width") or 0)
            height = int(target.get("height") or 0)
            resized = False
            if width < self.inspection_width or height < self.inspection_height:
                resized = bool(user32.SetWindowPos(
                    ctypes.c_void_p(hwnd),
                    ctypes.c_void_p(0),
                    int(target.get("left") or 0),
                    int(target.get("top") or 0),
                    self.inspection_width,
                    self.inspection_height,
                    SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW,
                ))
            return {"ok": True, "pid": pid, "hwnd": hwnd, "resized": resized, "windows": windows}
        except Exception as exc:
            return {"ok": False, "reason": str(exc), "pid": pid, "hwnd": hwnd, "windows": windows}

    def read_texts(self, hwnd: int) -> List[str]:
        if not hwnd:
            return []
        try:
            user32 = ctypes.windll.user32
            texts: List[str] = []
            seen = set()
            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

            def _read_text(win_hwnd) -> str:
                length = user32.GetWindowTextLengthW(win_hwnd)
                if length <= 0:
                    return ""
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(win_hwnd, buf, length + 1)
                return str(buf.value or "").strip()

            def _collect(win_hwnd) -> None:
                text = _read_text(win_hwnd)
                if not text:
                    return
                key = text.lower()
                if key not in seen:
                    seen.add(key)
                    texts.append(text)

            def _child_callback(child_hwnd, lparam):
                _collect(child_hwnd)
                return True

            _collect(hwnd)
            user32.EnumChildWindows(ctypes.c_void_p(hwnd), WNDENUMPROC(_child_callback), 0)
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

    def inspect_pid(
        self,
        pid: Optional[int],
        *,
        prepare: bool = False,
        process_idle: bool = False,
        presence_mismatch: bool = False,
        sample_count: Optional[int] = None,
        sample_interval: Optional[float] = None,
    ) -> Dict[str, Any]:
        if pid is None:
            return {"matched": False, "action": "", "reason_key": "", "detail": "", "error_code": ""}
        sample_total = max(1, int(sample_count or self.sample_count))
        interval = max(0.0, float(self.sample_interval if sample_interval is None else sample_interval))
        prepared: Dict[str, Any] = {}
        if prepare:
            prepared = self.sampler.prepare_popup_inspection(pid)
        try:
            samples: List[PopupClassification] = []
            hwnd = int((prepared.get("hwnd") if prepared else 0) or 0)
            for index in range(sample_total):
                if not hwnd:
                    windows = self.sampler.windows_for_pid(pid, include_hidden=True)
                    hwnd = int((windows[0].get("hwnd") if windows else 0) or 0)
                texts = self.sampler.read_texts(hwnd) if hwnd else []
                screenshot = self.sampler.capture_window_image(hwnd) if hwnd else None
                visual = detect_visual_features(screenshot)
                classification = classify_popup_observation(
                    texts,
                    visual,
                    process_idle=process_idle,
                    presence_mismatch=presence_mismatch,
                    threshold=self.threshold,
                )
                samples.append(classification)
                if index < sample_total - 1 and interval > 0:
                    if classification.error_code in {"267", "268", "273", "277"}:
                        break
                    time.sleep(interval)

            positive = [
                item for item in samples
                if item.matched and item.recovery_allowed and (item.confidence >= self.threshold or item.visual_disconnect)
            ]
            coded = [item for item in samples if item.error_code]
            best = max(samples, key=lambda item: item.confidence, default=PopupClassification(False))
            confirmed = bool(coded or len(positive) >= self.stable_samples)
            result = best.to_dict()
            result.update({
                "matched": bool(confirmed),
                "recovery_allowed": bool(confirmed and best.recovery_allowed),
                "sample_count": len(samples),
                "positive_samples": len(positive),
                "prepared": prepared,
                "stable_required": self.stable_samples,
            })
            if not confirmed:
                result["action"] = ""
                result["reason_key"] = ""
                result["disconnect_category"] = ""
                result["error_code"] = best.error_code
            return result
        finally:
            if prepare:
                _release_inspection_hold(pid)


DEFAULT_POPUP_OBSERVER = PopupObserver()
