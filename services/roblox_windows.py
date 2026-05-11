from __future__ import annotations

import ctypes
import math
import time
from ctypes import wintypes
from typing import Any, Dict, List, Optional, Tuple

from app_paths import resource_path
from core import flog, flog_kv
from runtime.popup_detector import DEFAULT_POPUP_OBSERVER, classify_texts, is_inspection_held
from services.roblox_processes import ROBLOX_GAME_NAMES
from services.window_control import (
    arrange_windows,
    minimize_windows,
    primary_monitor_work_area,
    resize_windows,
    restore_window_styles,
)

def classify_disconnect_dialog_texts(cls, texts: List[str]) -> Dict[str, Any]:
    return classify_texts(texts)

def _window_snapshot_for_pid(cls, pid: Optional[int]) -> Dict[str, Any]:
    snapshot = {"count": 0, "hwnd": 0, "responsive": False, "hung": False}
    if pid is None:
        return snapshot
    try:
        user32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

        def _enum_callback(hwnd, lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            win_pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
            if win_pid.value == pid:
                snapshot["count"] += 1
                if not snapshot["hwnd"]:
                    snapshot["hwnd"] = int(hwnd)
                try:
                    if user32.IsHungAppWindow(hwnd):
                        snapshot["hung"] = True
                    else:
                        snapshot["responsive"] = True
                except Exception:
                    snapshot["responsive"] = True
            return True

        user32.EnumWindows(WNDENUMPROC(_enum_callback), 0)
    except Exception:
        pass
    return snapshot

def _count_visible_windows_for_pid(cls, pid: Optional[int]) -> int:
    return int(cls._window_snapshot_for_pid(pid).get("count") or 0)

def _visible_roblox_windows(cls) -> List[Dict[str, Any]]:
    windows: List[Dict[str, Any]] = []
    try:
        import psutil
        proc_meta: Dict[int, Dict[str, Any]] = {}
        for proc in cls._iter_roblox_processes(game_only=True):
            try:
                proc_meta[int(proc.pid)] = {
                    "created": float(proc.create_time() or 0.0),
                    "name": str(proc.name() or ""),
                }
            except Exception:
                continue

        if not proc_meta:
            return []

        user32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        def _enum_callback(hwnd, lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            win_pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
            pid = int(win_pid.value or 0)
            meta = proc_meta.get(pid)
            if not meta:
                return True
            rect = RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            width = max(0, int(rect.right - rect.left))
            height = max(0, int(rect.bottom - rect.top))
            area = width * height
            if width < 60 or height < 45 or area <= 0:
                return True
            windows.append({
                "pid": pid,
                "hwnd": int(hwnd),
                "left": int(rect.left),
                "top": int(rect.top),
                "right": int(rect.right),
                "bottom": int(rect.bottom),
                "width": width,
                "height": height,
                "area": area,
                "created": float(meta.get("created") or 0.0),
                "name": str(meta.get("name") or ""),
            })
            return True

        user32.EnumWindows(WNDENUMPROC(_enum_callback), 0)
    except Exception as exc:
        flog_kv("WINDOW", "enumerate_roblox_windows_failed", "warning", error=str(exc))
        return []

    by_pid: Dict[int, Dict[str, Any]] = {}
    for item in windows:
        if is_inspection_held(int(item.get("pid") or 0)):
            continue
        current = by_pid.get(int(item["pid"]))
        if current is None or int(item.get("area") or 0) > int(current.get("area") or 0):
            by_pid[int(item["pid"])] = item
    return sorted(by_pid.values(), key=lambda item: (float(item.get("created") or 0.0), int(item.get("pid") or 0)))

def minimize_roblox_windows(cls) -> Dict[str, Any]:
    return minimize_windows(cls._visible_roblox_windows())

def resize_roblox_windows(cls, width: int, height: int, exclude_pids: Optional[List[int]] = None) -> Dict[str, Any]:
    excluded = {int(pid) for pid in (exclude_pids or []) if pid}
    windows = [item for item in cls._visible_roblox_windows() if int(item.get("pid") or 0) not in excluded]
    return resize_windows(windows, width, height)

def _primary_monitor_work_area(cls) -> Dict[str, int]:
    return primary_monitor_work_area()

def arrange_roblox_windows(
    cls,
    width: int,
    height: int,
    columns: int = 6,
    gap: int = 2,
    margin: int = 0,
    exclude_pids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    excluded = {int(pid) for pid in (exclude_pids or []) if pid}
    windows = [item for item in cls._visible_roblox_windows() if int(item.get("pid") or 0) not in excluded]
    return arrange_windows(windows, width, height, columns, gap, margin)

def restore_roblox_window_styles(cls) -> Dict[str, Any]:
    return restore_window_styles(cls._visible_roblox_windows())

def is_not_responding(cls, pid: Optional[int]) -> bool:
    """
    ตรวจจับ 'Not Responding' ผ่าน Windows IsHungAppWindow()
    เหมือน Task Manager ทุกประการ
    """
    if pid is None:
        return False
    with cls._cache_lock:
        cached = cls._nr_cache.get(pid)
        if cached and (time.time() - cached[0]) < cls._nr_cache_ttl:
            return cached[1]
    try:
        user32 = ctypes.windll.user32
        result = {"hung": False, "window_count": 0}
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

        def _enum_callback(hwnd, lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            win_pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
            if win_pid.value == pid:
                result["window_count"] += 1
                if user32.IsHungAppWindow(hwnd):
                    result["hung"] = True
                    return False
            return True

        callback = WNDENUMPROC(_enum_callback)
        user32.EnumWindows(callback, 0)

        if result["window_count"] == 0:
            with cls._cache_lock:
                cls._nr_cache[pid] = (time.time(), False)
            return False
        if result["hung"]:
            flog(f"[PROC] PID {pid} is NOT RESPONDING (Task Manager style)")
        with cls._cache_lock:
            cls._nr_cache[pid] = (time.time(), result["hung"])
        return result["hung"]

    except Exception as e:
        flog(f"[PROC] is_not_responding error for PID {pid}: {e}", "warning")
        return False

def inspect_disconnect_dialog(
    cls,
    pid: Optional[int],
    prepare: bool = False,
    presence_mismatch: bool = False,
    process_idle: bool = False,
    sample_count: Optional[int] = None,
) -> Dict[str, Any]:
    if pid is None:
        return {"matched": False, "action": "", "reason_key": "", "detail": "", "error_code": ""}
    try:
        return DEFAULT_POPUP_OBSERVER.inspect_pid(
            pid,
            prepare=prepare,
            presence_mismatch=presence_mismatch,
            process_idle=process_idle,
            sample_count=sample_count,
        )
    except Exception as e:
        flog(f"[PROC] inspect_disconnect_dialog error for PID {pid}: {e}", "warning")
        return cls._inspect_disconnect_dialog_visual(pid)

def detect_connection_error(cls, pid: Optional[int]) -> Tuple[bool, str]:
    info = cls.inspect_disconnect_dialog(pid)
    if not info.get("matched") or str(info.get("action") or "") not in {"rejoin", "conditional_rejoin"}:
        return False, ""
    return True, str(info.get("detail") or "")

def _template_path(cls, name: str) -> str:
    return resource_path("vision_templates", name)

def _load_visual_template(cls, name: str):
    cached = cls._visual_template_cache.get(name)
    if cached is not None:
        return cached
    try:
        from PIL import Image
        img = Image.open(cls._template_path(name)).convert("L")
        cls._visual_template_cache[name] = img
        return img
    except Exception:
        cls._visual_template_cache[name] = None
        return None

def _get_pid_window_rect(cls, pid: Optional[int]) -> Optional[Tuple[int, int, int, int]]:
    if pid is None:
        return None
    try:
        user32 = ctypes.windll.user32
        rects: List[Tuple[int, int, int, int, int]] = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        def _enum_callback(hwnd, lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            win_pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
            if win_pid.value != pid:
                return True
            rect = RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            width = max(0, int(rect.right - rect.left))
            height = max(0, int(rect.bottom - rect.top))
            area = width * height
            if width >= 300 and height >= 200 and area > 0:
                rects.append((area, int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)))
            return True

        user32.EnumWindows(WNDENUMPROC(_enum_callback), 0)
        if not rects:
            return None
        rects.sort(reverse=True)
        _area, left, top, right, bottom = rects[0]
        return left, top, right, bottom
    except Exception:
        return None

def _capture_pid_window_image(cls, pid: Optional[int]):
    rect = cls._get_pid_window_rect(pid)
    if not rect:
        return None
    try:
        from PIL import Image
        left, top, right, bottom = rect
        width = max(0, int(right - left))
        height = max(0, int(bottom - top))
        if width <= 0 or height <= 0:
            return None
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        hwnd = None
        try:
            hwnd = user32.WindowFromPoint(wintypes.POINT(left + 8, top + 8))
        except Exception:
            hwnd = None
        if not hwnd:
            hwnd = user32.GetForegroundWindow()

        target_hwnd = None
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)
        def _enum_callback(win_hwnd, lparam):
            nonlocal target_hwnd
            if not user32.IsWindowVisible(win_hwnd):
                return True
            win_pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(win_hwnd, ctypes.byref(win_pid))
            if win_pid.value == pid:
                target_hwnd = win_hwnd
                return False
            return True
        user32.EnumWindows(WNDENUMPROC(_enum_callback), 0)
        if not target_hwnd:
            return None

        hwnd_dc = user32.GetWindowDC(target_hwnd)
        mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
        gdi32.SelectObject(mem_dc, bitmap)
        PW_RENDERFULLCONTENT = 0x00000002
        ok = user32.PrintWindow(target_hwnd, mem_dc, PW_RENDERFULLCONTENT)
        if not ok:
            user32.PrintWindow(target_hwnd, mem_dc, 0)

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

        buf_len = width * height * 4
        buffer = ctypes.create_string_buffer(buf_len)
        gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(bmi), 0)
        image = Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1).convert("L")

        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(target_hwnd, hwnd_dc)
        return image
    except Exception:
        return None

def _scaled_box(cls, box: Tuple[int, int, int, int], size: Tuple[int, int]) -> Tuple[int, int, int, int]:
    base_w, base_h = cls._VISUAL_TEMPLATE_BASE_SIZE
    width, height = size
    sx = float(width) / float(base_w)
    sy = float(height) / float(base_h)
    left, top, right, bottom = box
    return (
        max(0, int(round(left * sx))),
        max(0, int(round(top * sy))),
        min(width, int(round(right * sx))),
        min(height, int(round(bottom * sy))),
    )

def _rmsdiff(img_a, img_b) -> float:
    try:
        from PIL import ImageChops
        diff = ImageChops.difference(img_a, img_b)
        hist = diff.histogram()
        sq = sum((value * ((idx % 256) ** 2)) for idx, value in enumerate(hist))
        total = max(1, img_a.size[0] * img_a.size[1])
        return math.sqrt(float(sq) / float(total))
    except Exception:
        return 9999.0

def _inspect_disconnect_dialog_visual(cls, pid: Optional[int]) -> Dict[str, Any]:
    try:
        return DEFAULT_POPUP_OBSERVER.inspect_pid(pid, prepare=False, sample_count=2)
    except Exception as e:
        flog(f"[PROC] visual disconnect inspect error for PID {pid}: {e}", "warning")
    return {"matched": False, "action": "", "reason_key": "", "detail": "", "error_code": ""}
