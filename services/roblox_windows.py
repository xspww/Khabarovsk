from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Any

from core import flog, flog_kv
from runtime.popup_detector import DEFAULT_POPUP_OBSERVER, classify_texts, is_inspection_held
from services.window_control import (
    TASKBAR_HIDDEN_PROP,
    arrange_windows,
    minimize_windows,
    primary_monitor_work_area,
    resize_windows,
    restore_window_styles,
)


def classify_disconnect_dialog_texts(cls, texts: list[str]) -> dict[str, Any]:
    """Seam keeper: routes the cls-bound world to the popup detector so
    callers import one place. Deleting it spreads popup imports instead."""
    return classify_texts(texts)

def _window_snapshot_for_pid(cls, pid: int | None) -> dict[str, Any]:
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
        "intentional: ignore exception"
    return snapshot

def _count_visible_windows_for_pid(cls, pid: int | None) -> int:
    return int(cls._window_snapshot_for_pid(pid).get("count") or 0)

def _placement_normal_rect(user32: Any, hwnd: int) -> tuple[int, int, int, int] | None:
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


def _dedupe_largest_per_pid(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Single owner for largest-per-pid dedupe + stable (created, pid) order."""
    by_pid: dict[int, dict[str, Any]] = {}
    for item in windows:
        try:
            pid = int(item.get("pid") or 0)
        except Exception:
            continue
        current = by_pid.get(pid)
        if current is None or int(item.get("area") or 0) > int(current.get("area") or 0):
            by_pid[pid] = item
    return sorted(by_pid.values(), key=lambda item: (float(item.get("created") or 0.0), int(item.get("pid") or 0)))


class _EnumRect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def _collect_roblox_proc_meta(iter_roblox_processes: Any) -> dict[int, dict[str, Any]]:
    """Single owner for the pid → created/name snapshot both enumerations need."""
    proc_meta: dict[int, dict[str, Any]] = {}
    for proc in iter_roblox_processes(game_only=True):
        try:
            proc_meta[int(proc.pid)] = {
                "created": float(proc.create_time() or 0.0),
                "name": str(proc.name() or ""),
            }
        except Exception:
            continue
    return proc_meta


def _measure_window_rect(user32: Any, rect: Any, hwnd: int, iconic: bool) -> tuple[int, int, int, int, int, int] | None:
    """Single owner for per-window rect extraction.

    Both enumerations used to repeat this inline with slightly different
    error handling; the visible path now also skips a failing window
    instead of aborting the whole scan.
    """
    try:
        if iconic:
            # Minimized windows report a 160x28 off-screen rect via
            # GetWindowRect, so use the restored (normal) rect instead.
            normal = _placement_normal_rect(user32, int(hwnd))
            if not normal:
                return None
            left, top, right, bottom = normal
        else:
            try:
                if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    return None
            except Exception:
                return None
            left, top, right, bottom = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
        width = max(0, right - left)
        height = max(0, bottom - top)
        return left, top, right, bottom, width, height
    except Exception:
        return None


def _window_visible(user32: Any, hwnd: Any) -> bool:
    return bool(user32.IsWindowVisible(hwnd))


def _window_pid(user32: Any, hwnd: Any) -> int:
    win_pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
    return int(win_pid.value or 0)


def _window_iconic(user32: Any, hwnd: Any) -> bool:
    return bool(user32.IsIconic(hwnd))


def _taskbar_marked(user32: Any, hwnd: Any) -> bool:
    try:
        return bool(user32.GetPropW(ctypes.c_void_p(int(hwnd)), TASKBAR_HIDDEN_PROP))
    except Exception:
        return False


def _enum_windows(user32: Any, callback: Any) -> None:
    wndenumproc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)
    user32.EnumWindows(wndenumproc(callback), 0)


def _window_record(
    pid: int,
    hwnd: int,
    measured: tuple[int, int, int, int, int, int],
    meta: dict[str, Any],
) -> dict[str, Any]:
    left, top, right, bottom, width, height = measured
    return {
        "pid": pid,
        "hwnd": hwnd,
        "left": left,
        "top": top,
        "right": right,
        "bottom": bottom,
        "width": width,
        "height": height,
        "area": width * height,
        "created": float(meta.get("created") or 0.0),
        "name": str(meta.get("name") or ""),
    }


def _visible_window_candidate(
    user32: Any,
    hwnd: Any,
    proc_meta: dict[int, dict[str, Any]],
    include_minimized: bool,
) -> dict[str, Any] | None:
    if not _window_visible(user32, hwnd):
        return None
    pid = _window_pid(user32, hwnd)
    meta = proc_meta.get(pid)
    if not meta:
        return None
    if _taskbar_marked(user32, hwnd):
        return None
    iconic = _window_iconic(user32, hwnd)
    if iconic and not include_minimized:
        return None
    measured = _measure_window_rect(user32, _EnumRect(), int(hwnd), iconic)
    if measured is None or not _passes_window_size_filter(measured[4], measured[5]):
        return None
    record = _window_record(pid, int(hwnd), measured, meta)
    record["iconic"] = iconic
    return record


def _hidden_window_candidate(
    user32: Any,
    hwnd: Any,
    proc_meta: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    try:
        visible = _window_visible(user32, hwnd)
    except Exception:
        return None
    taskbar_hidden = _taskbar_marked(user32, hwnd)
    if visible and not taskbar_hidden:
        return None
    try:
        pid = _window_pid(user32, hwnd)
    except Exception:
        return None
    meta = proc_meta.get(pid)
    if not meta:
        return None
    try:
        iconic = _window_iconic(user32, hwnd)
    except Exception:
        iconic = False
    measured = _measure_window_rect(user32, _EnumRect(), int(hwnd), iconic)
    if measured is None:
        return None
    if not _passes_window_size_filter(measured[4], measured[5]):
        return None
    record = _window_record(pid, int(hwnd), measured, meta)
    record["visible"] = visible
    record["taskbar_hidden"] = taskbar_hidden
    return record


def _visible_roblox_windows(cls, include_minimized: bool = True) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    try:
        proc_meta = _collect_roblox_proc_meta(cls._iter_roblox_processes)
        if not proc_meta:
            return []

        user32 = ctypes.windll.user32

        def _enum_callback(hwnd, lparam):
            candidate = _visible_window_candidate(user32, hwnd, proc_meta, include_minimized)
            if candidate is not None:
                windows.append(candidate)
            return True

        _enum_windows(user32, _enum_callback)
    except Exception as exc:
        flog_kv("WINDOW", "enumerate_roblox_windows_failed", "warning", error=str(exc))
        return []

    eligible = [
        item for item in windows
        if not is_inspection_held(int(item.get("pid") or 0))
    ]
    return _dedupe_largest_per_pid(eligible)

def minimize_roblox_windows(cls) -> dict[str, Any]:
    return minimize_windows(cls._visible_roblox_windows(include_minimized=False))

def resize_roblox_windows(cls, width: int, height: int, unlock_size: bool = True, exclude_pids: list[int] | None = None) -> dict[str, Any]:
    excluded = {int(pid) for pid in (exclude_pids or []) if pid}
    windows = [item for item in cls._visible_roblox_windows(include_minimized=True) if int(item.get("pid") or 0) not in excluded]
    return resize_windows(windows, width, height, unlock_size=unlock_size)

def _primary_monitor_work_area(cls) -> dict[str, int]:
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
    exclude_pids: list[int] | None = None,
    rows: int | None = None,
) -> dict[str, Any]:
    excluded = {int(pid) for pid in (exclude_pids or []) if pid}
    windows = [item for item in cls._visible_roblox_windows(include_minimized=True) if int(item.get("pid") or 0) not in excluded]
    return arrange_windows(windows, width, height, columns, gap, margin, unlock_size=unlock_size, resize=resize, rows=rows)

def restore_roblox_window_styles(cls) -> dict[str, Any]:
    return restore_window_styles(cls._visible_roblox_windows(include_minimized=True))

def unminimize_roblox_windows(cls, exclude_pids: list[int] | None = None) -> dict[str, Any]:
    """Manual-action helper: restore minimized Roblox windows so arrange/resize applies immediately.

    Auto cycles must NOT call this (would flicker + defeat auto-minimize).
    """
    from services.window_control import unminimize_windows

    excluded = {int(pid) for pid in (exclude_pids or []) if pid}
    windows = [item for item in cls._visible_roblox_windows(include_minimized=True) if int(item.get("pid") or 0) not in excluded]
    return unminimize_windows(windows)

def _hidden_roblox_windows(cls) -> list[dict[str, Any]]:
    """Windows hidden via minimize+DeleteTab (taskbar prop) or legacy SW_HIDE.

    New mode keeps IsWindowVisible=True so Task Manager still lists each
    client as its own Roblox app. Legacy SW_HIDE windows (visible=False)
    are still included so Show restores them too. Filters out tiny
    helper/message windows. Picks largest per pid like visible.
    """
    windows: list[dict[str, Any]] = []
    try:
        proc_meta = _collect_roblox_proc_meta(cls._iter_roblox_processes)
        if not proc_meta:
            return []
        user32 = ctypes.windll.user32

        def _enum_callback(hwnd, lparam):
            record = _hidden_window_candidate(user32, hwnd, proc_meta)
            if record is not None:
                windows.append(record)
            return True

        _enum_windows(user32, _enum_callback)
    except Exception as exc:
        flog_kv("WINDOW", "enumerate_hidden_roblox_windows_failed", "warning", error=str(exc))
        return []
    return _dedupe_largest_per_pid(windows)

def _merge_hidden_windows(
    windows: list[dict[str, Any]],
    hidden: list[dict[str, Any]],
    excluded: set[int],
) -> None:
    """Re-hide windows the user restored: still prop-marked and back on screen."""
    seen_hwnds = {int(item.get("hwnd") or 0) for item in windows}
    for item in hidden:
        if not bool(item.get("taskbar_hidden")) or not bool(item.get("visible")):
            continue
        if int(item.get("pid") or 0) in excluded:
            continue
        hwnd = int(item.get("hwnd") or 0)
        if hwnd in seen_hwnds:
            continue
        windows.append(item)
        seen_hwnds.add(hwnd)


def hide_roblox_windows(cls, exclude_pids: list[int] | None = None) -> dict[str, Any]:
    from services.window_control import hide_windows

    excluded = {int(pid) for pid in (exclude_pids or []) if pid}
    windows = [item for item in cls._visible_roblox_windows(include_minimized=True) if int(item.get("pid") or 0) not in excluded]
    try:
        _merge_hidden_windows(windows, cls._hidden_roblox_windows(), excluded)
    except Exception:
        "intentional: ignore exception"
    return hide_windows(windows)

def show_roblox_windows(cls, exclude_pids: list[int] | None = None) -> dict[str, Any]:
    from services.window_control import show_windows

    excluded = {int(pid) for pid in (exclude_pids or []) if pid}
    windows = [item for item in cls._hidden_roblox_windows() if int(item.get("pid") or 0) not in excluded]
    return show_windows(windows)

def _scan_hung_windows(user32: Any, pid: int) -> tuple[bool, int]:
    state = {"hung": False, "window_count": 0}

    def _enum_callback(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        win_pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
        if win_pid.value == pid:
            state["window_count"] += 1
            if user32.IsHungAppWindow(hwnd):
                state["hung"] = True
                return False
        return True

    _enum_windows(user32, _enum_callback)
    return bool(state["hung"]), int(state["window_count"])


def _nr_cached(cls, pid: int) -> bool | None:
    with cls._cache_lock:
        cached = cls._nr_cache.get(pid)
        if cached and (time.time() - cached[0]) < cls._nr_cache_ttl:
            return bool(cached[1])
    return None


def _nr_store(cls, pid: int, hung: bool) -> None:
    with cls._cache_lock:
        cls._nr_cache[pid] = (time.time(), hung)


def is_not_responding(cls, pid: int | None) -> bool:
    """
    ตรวจจับ 'Not Responding' ผ่าน Windows IsHungAppWindow()
    เหมือน Task Manager ทุกประการ
    """
    if pid is None:
        return False
    cached = _nr_cached(cls, pid)
    if cached is not None:
        return cached
    try:
        user32 = ctypes.windll.user32
        hung, window_count = _scan_hung_windows(user32, pid)
        if window_count == 0:
            _nr_store(cls, pid, False)
            return False
        if hung:
            flog(f"[PROC] PID {pid} is NOT RESPONDING (Task Manager style)")
        _nr_store(cls, pid, hung)
        return hung
    except Exception as e:
        flog(f"[PROC] is_not_responding error for PID {pid}: {e}", "warning")
        return False

def inspect_disconnect_dialog(
    cls,
    pid: int | None,
    prepare: bool = False,
    process_idle: bool = False,
    sample_count: int | None = None,
) -> dict[str, Any]:
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

def detect_connection_error(cls, pid: int | None) -> tuple[bool, str]:
    info = cls.inspect_disconnect_dialog(pid)
    if not info.get("matched") or str(info.get("action") or "") not in {"rejoin", "conditional_rejoin"}:
        return False, ""
    return True, str(info.get("detail") or "")

def _get_pid_window_rect(cls, pid: int | None) -> tuple[int, int, int, int] | None:
    if pid is None:
        return None
    try:
        user32 = ctypes.windll.user32
        rects: list[tuple[int, int, int, int, int]] = []
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
                "intentional: ignore exception"
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

def _find_window_for_pid(user32: Any, pid: int | None) -> int | None:
    target: list[int] = []

    def _enum_callback(win_hwnd, lparam):
        if not user32.IsWindowVisible(win_hwnd):
            return True
        win_pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(win_hwnd, ctypes.byref(win_pid))
        if win_pid.value == pid:
            target.append(int(win_hwnd))
            return False
        return True

    _enum_windows(user32, _enum_callback)
    return target[0] if target else None


def _bitmap_header(width: int, height: int) -> Any:
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
    return bmi


def _capture_window_bitmap(
    user32: Any,
    gdi32: Any,
    hwnd: int,
    width: int,
    height: int,
    image_cls: Any,
) -> Any:
    hwnd_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
    gdi32.SelectObject(mem_dc, bitmap)
    ok = user32.PrintWindow(hwnd, mem_dc, 0x00000002)
    if not ok:
        user32.PrintWindow(hwnd, mem_dc, 0)
    bmi = _bitmap_header(width, height)
    buffer = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(bmi), 0)
    image = image_cls.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1).convert("L")
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(hwnd, hwnd_dc)
    return image


def _capture_pid_window_image(cls, pid: int | None):
    rect = cls._get_pid_window_rect(pid)
    if not rect:
        return None
    try:
        from PIL import Image

        width = max(0, int(rect[2] - rect[0]))
        height = max(0, int(rect[3] - rect[1]))
        if width <= 0 or height <= 0:
            return None
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        target_hwnd = _find_window_for_pid(user32, pid)
        if not target_hwnd:
            return None
        return _capture_window_bitmap(user32, gdi32, target_hwnd, width, height, Image)
    except Exception:
        return None

def _inspect_disconnect_dialog_visual(cls, pid: int | None) -> dict[str, Any]:
    try:
        return DEFAULT_POPUP_OBSERVER.inspect_pid(pid, prepare=False, sample_count=2)
    except Exception as e:
        flog(f"[PROC] visual disconnect inspect error for PID {pid}: {e}", "warning")
    return {"matched": False, "action": "", "reason_key": "", "detail": "", "error_code": ""}
