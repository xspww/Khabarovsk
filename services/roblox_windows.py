from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Any, Dict, List, Optional, Tuple

from core import flog, flog_kv
from runtime.popup_detector import DEFAULT_POPUP_OBSERVER, classify_texts, is_inspection_held
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

def _placement_normal_rect(user32: Any, hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """Return the restored (normal) rect for a window, even when minimized.

    SetWindowPos on an iconic window is a no-op for its normal position
    (verified live: returns True but rcNormal never moves), so callers need
    the normal rect to reserve grid slots without touching minimized windows.
    """
    try:
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        class WINDOWPLACEMENT(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_uint),
                ("flags", ctypes.c_uint),
                ("showCmd", ctypes.c_uint),
                ("ptMinPosition", POINT),
                ("ptMaxPosition", POINT),
                ("rcNormalPosition", RECT),
            ]

        placement = WINDOWPLACEMENT()
        placement.length = ctypes.sizeof(WINDOWPLACEMENT)
        if not user32.GetWindowPlacement(ctypes.c_void_p(hwnd), ctypes.byref(placement)):
            return None
        rc = placement.rcNormalPosition
        return int(rc.left), int(rc.top), int(rc.right), int(rc.bottom)
    except Exception:
        return None


_MIN_WINDOW_WIDTH = 60
_MIN_WINDOW_HEIGHT = 45


def _passes_window_size_filter(width: Any, height: Any) -> bool:
    """Single owner for the tiny-window filter (helper/message windows out)."""
    try:
        width = max(0, int(width))
        height = max(0, int(height))
    except Exception:
        return False
    return width >= _MIN_WINDOW_WIDTH and height >= _MIN_WINDOW_HEIGHT and (width * height) > 0


def _dedupe_largest_per_pid(windows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Single owner for largest-per-pid dedupe + stable (created, pid) order."""
    by_pid: Dict[int, Dict[str, Any]] = {}
    for item in windows:
        try:
            pid = int(item.get("pid") or 0)
        except Exception:
            continue
        current = by_pid.get(pid)
        if current is None or int(item.get("area") or 0) > int(current.get("area") or 0):
            by_pid[pid] = item
    return sorted(by_pid.values(), key=lambda item: (float(item.get("created") or 0.0), int(item.get("pid") or 0)))


def _visible_roblox_windows(cls, include_minimized: bool = True) -> List[Dict[str, Any]]:
    windows: List[Dict[str, Any]] = []
    try:
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
            try:
                if bool(user32.GetPropW(ctypes.c_void_p(int(hwnd)), "CronusTaskbarHidden")):
                    return True
            except Exception:
                pass
            iconic = bool(user32.IsIconic(hwnd))
            if iconic and not include_minimized:
                return True
            if iconic:
                normal = _placement_normal_rect(user32, hwnd)
                if not normal:
                    return True
                left, top, right, bottom = normal
                width = max(0, right - left)
                height = max(0, bottom - top)
            else:
                rect = RECT()
                if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    return True
                left, top, right, bottom = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
                width = max(0, right - left)
                height = max(0, bottom - top)
            area = width * height
            if not _passes_window_size_filter(width, height):
                return True
            windows.append({
                "pid": pid,
                "hwnd": int(hwnd),
                "left": left,
                "top": top,
                "right": right,
                "bottom": bottom,
                "width": width,
                "height": height,
                "area": area,
                "created": float(meta.get("created") or 0.0),
                "name": str(meta.get("name") or ""),
                "iconic": iconic,
            })
            return True

        user32.EnumWindows(WNDENUMPROC(_enum_callback), 0)
    except Exception as exc:
        flog_kv("WINDOW", "enumerate_roblox_windows_failed", "warning", error=str(exc))
        return []

    eligible = [
        item for item in windows
        if not is_inspection_held(int(item.get("pid") or 0))
    ]
    return _dedupe_largest_per_pid(eligible)

def minimize_roblox_windows(cls) -> Dict[str, Any]:
    return minimize_windows(cls._visible_roblox_windows(include_minimized=False))

def resize_roblox_windows(cls, width: int, height: int, unlock_size: bool = True, exclude_pids: Optional[List[int]] = None) -> Dict[str, Any]:
    excluded = {int(pid) for pid in (exclude_pids or []) if pid}
    windows = [item for item in cls._visible_roblox_windows(include_minimized=True) if int(item.get("pid") or 0) not in excluded]
    return resize_windows(windows, width, height, unlock_size=unlock_size)

def _primary_monitor_work_area(cls) -> Dict[str, int]:
    return primary_monitor_work_area()

def arrange_roblox_windows(
    cls,
    width: int,
    height: int,
    columns: int = 6,
    gap: int = 2,
    margin: int = 0,
    unlock_size: bool = True,
    resize: bool = True,
    exclude_pids: Optional[List[int]] = None,
    rows: Optional[int] = None,
) -> Dict[str, Any]:
    excluded = {int(pid) for pid in (exclude_pids or []) if pid}
    windows = [item for item in cls._visible_roblox_windows(include_minimized=True) if int(item.get("pid") or 0) not in excluded]
    return arrange_windows(windows, width, height, columns, gap, margin, unlock_size=unlock_size, resize=resize, rows=rows)

def restore_roblox_window_styles(cls) -> Dict[str, Any]:
    return restore_window_styles(cls._visible_roblox_windows(include_minimized=True))

def unminimize_roblox_windows(cls, exclude_pids: Optional[List[int]] = None) -> Dict[str, Any]:
    """Manual-action helper: restore minimized Roblox windows so arrange/resize applies immediately.

    Auto cycles must NOT call this (would flicker + defeat auto-minimize).
    """
    from services.window_control import unminimize_windows

    excluded = {int(pid) for pid in (exclude_pids or []) if pid}
    windows = [item for item in cls._visible_roblox_windows(include_minimized=True) if int(item.get("pid") or 0) not in excluded]
    return unminimize_windows(windows)

def _hidden_roblox_windows(cls) -> List[Dict[str, Any]]:
    """Windows hidden via minimize+DeleteTab (taskbar prop) or legacy SW_HIDE.

    New mode keeps IsWindowVisible=True so Task Manager still lists each
    client as its own Roblox app. Legacy SW_HIDE windows (visible=False)
    are still included so Show restores them too. Filters out tiny
    helper/message windows. Picks largest per pid like visible.
    """
    windows: List[Dict[str, Any]] = []
    try:
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
            try:
                visible = bool(user32.IsWindowVisible(hwnd))
            except Exception:
                return True
            try:
                taskbar_hidden = bool(user32.GetPropW(ctypes.c_void_p(int(hwnd)), "CronusTaskbarHidden"))
            except Exception:
                taskbar_hidden = False
            if visible and not taskbar_hidden:
                return True
            win_pid = ctypes.c_ulong(0)
            try:
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
            except Exception:
                return True
            pid = int(win_pid.value or 0)
            meta = proc_meta.get(pid)
            if not meta:
                return True
            try:
                iconic = bool(user32.IsIconic(hwnd))
            except Exception:
                iconic = False
            if iconic:
                # Minimized windows report a 160x28 off-screen rect via
                # GetWindowRect, so use the restored (normal) rect instead.
                normal = _placement_normal_rect(user32, int(hwnd))
                if not normal:
                    return True
                left, top, right, bottom = normal
                width = max(0, right - left)
                height = max(0, bottom - top)
            else:
                rect = RECT()
                try:
                    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                        return True
                except Exception:
                    return True
                left, top, right, bottom = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
                width = max(0, right - left)
                height = max(0, bottom - top)
            area = width * height
            if not _passes_window_size_filter(width, height):
                return True
            windows.append({
                "pid": pid,
                "hwnd": int(hwnd),
                "left": left,
                "top": top,
                "right": right,
                "bottom": bottom,
                "width": width,
                "height": height,
                "area": area,
                "created": float(meta.get("created") or 0.0),
                "name": str(meta.get("name") or ""),
                "visible": visible,
                "taskbar_hidden": taskbar_hidden,
            })
            return True

        user32.EnumWindows(WNDENUMPROC(_enum_callback), 0)
    except Exception as exc:
        flog_kv("WINDOW", "enumerate_hidden_roblox_windows_failed", "warning", error=str(exc))
        return []
    return _dedupe_largest_per_pid(windows)

def hide_roblox_windows(cls, exclude_pids: Optional[List[int]] = None) -> Dict[str, Any]:
    from services.window_control import hide_windows

    excluded = {int(pid) for pid in (exclude_pids or []) if pid}
    windows = [item for item in cls._visible_roblox_windows(include_minimized=True) if int(item.get("pid") or 0) not in excluded]
    try:
        # Re-hide windows the user restored manually: still prop-marked and
        # back on screen, so _visible excludes them but auto-hide must catch them.
        seen_hwnds = {int(item.get("hwnd") or 0) for item in windows}
        for item in cls._hidden_roblox_windows():
            if not bool(item.get("taskbar_hidden")) or not bool(item.get("visible")):
                continue
            if int(item.get("pid") or 0) in excluded:
                continue
            if int(item.get("hwnd") or 0) in seen_hwnds:
                continue
            windows.append(item)
            seen_hwnds.add(int(item.get("hwnd") or 0))
    except Exception:
        pass
    return hide_windows(windows)

def show_roblox_windows(cls, exclude_pids: Optional[List[int]] = None) -> Dict[str, Any]:
    from services.window_control import show_windows

    excluded = {int(pid) for pid in (exclude_pids or []) if pid}
    windows = [item for item in cls._hidden_roblox_windows() if int(item.get("pid") or 0) not in excluded]
    return show_windows(windows)

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
    process_idle: bool = False,
    sample_count: Optional[int] = None,
) -> Dict[str, Any]:
    if pid is None:
        return {"matched": False, "action": "", "reason_key": "", "detail": "", "error_code": ""}
    try:
        return DEFAULT_POPUP_OBSERVER.inspect_pid(
            pid,
            prepare=prepare,
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
            try:
                if user32.IsIconic(hwnd):
                    return True
            except Exception:
                pass
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
            if _passes_window_size_filter(width, height):
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

def _inspect_disconnect_dialog_visual(cls, pid: Optional[int]) -> Dict[str, Any]:
    try:
        return DEFAULT_POPUP_OBSERVER.inspect_pid(pid, prepare=False, sample_count=2)
    except Exception as e:
        flog(f"[PROC] visual disconnect inspect error for PID {pid}: {e}", "warning")
    return {"matched": False, "action": "", "reason_key": "", "detail": "", "error_code": ""}
