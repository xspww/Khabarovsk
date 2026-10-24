from __future__ import annotations

import ctypes
import math
from typing import Any, NamedTuple

from core import flog_kv


def primary_monitor_work_area() -> dict[str, int]:
    try:
        user32 = ctypes.windll.user32

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        rect = RECT()
        SPI_GETWORKAREA = 48
        if user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            width = max(0, int(rect.right - rect.left))
            height = max(0, int(rect.bottom - rect.top))
            if width > 0 and height > 0:
                return {"left": int(rect.left), "top": int(rect.top), "width": width, "height": height}
        width = int(user32.GetSystemMetrics(0) or 1920)
        height = int(user32.GetSystemMetrics(1) or 1080)
        return {"left": 0, "top": 0, "width": max(320, width), "height": max(240, height)}
    except Exception:
        return {"left": 0, "top": 0, "width": 1920, "height": 1080}


def _window_long_api(user32: Any):
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        return user32.GetWindowLongPtrW, user32.SetWindowLongPtrW
    return user32.GetWindowLongW, user32.SetWindowLongW


def _window_rect(user32: Any, hwnd: int) -> tuple[int, int]:
    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    rect = RECT()
    if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
        return 0, 0
    return max(0, int(rect.right - rect.left)), max(0, int(rect.bottom - rect.top))


_GWL_STYLE = -16
_SW_MINIMIZE = 6
_SW_RESTORE = 9
_WS_VISIBLE = 0x10000000
_WS_CLIPCHILDREN = 0x02000000
_WS_CLIPSIBLINGS = 0x04000000
_WS_POPUP = 0x80000000
_WS_CAPTION = 0x00C00000
_WS_SYSMENU = 0x00080000
_WS_THICKFRAME = 0x00040000
_WS_MINIMIZEBOX = 0x00020000
_WS_MAXIMIZEBOX = 0x00010000
_WS_OVERLAPPEDWINDOW = _WS_CAPTION | _WS_SYSMENU | _WS_THICKFRAME | _WS_MINIMIZEBOX | _WS_MAXIMIZEBOX
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_FRAMECHANGED = 0x0020
_SWP_SHOWWINDOW = 0x0040
_SWP_FLAGS = _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED | _SWP_SHOWWINDOW


def _hwnd_of(item: dict[str, Any]) -> int:
    return int(item.get("hwnd") or 0)


def _pid_of(item: dict[str, Any]) -> int:
    return int(item.get("pid") or 0)


def _pids_csv(items: list[dict[str, Any]]) -> str:
    return ",".join(str(item.get("pid")) for item in items)


def _is_iconic(user32: Any, item: dict[str, Any], hwnd: int) -> bool:
    try:
        iconic = bool(item.get("iconic"))
    except Exception:
        iconic = False
    if iconic:
        return True
    try:
        return bool(user32.IsIconic(ctypes.c_void_p(hwnd)))
    except Exception:
        return False


def _is_visible(user32: Any, hwnd: int) -> bool:
    try:
        return bool(user32.IsWindowVisible(ctypes.c_void_p(hwnd)))
    except Exception:
        return True


def _compact_style(style: int) -> int:
    return (
        (style & ~(_WS_CAPTION | _WS_SYSMENU | _WS_THICKFRAME | _WS_MINIMIZEBOX | _WS_MAXIMIZEBOX))
        | _WS_POPUP
        | _WS_VISIBLE
        | _WS_CLIPCHILDREN
        | _WS_CLIPSIBLINGS
    )


def _normal_style() -> int:
    return _WS_OVERLAPPEDWINDOW | _WS_VISIBLE | _WS_CLIPCHILDREN | _WS_CLIPSIBLINGS


def _has_normal_frame(style: int) -> bool:
    return bool(style & _WS_CAPTION) and bool(style & _WS_THICKFRAME)


def _compact_required(width: int, height: int, unlock_size: bool, resize: bool = True) -> bool:
    return bool(resize) and bool(unlock_size) and (width < 816 or height < 638)


def _clamp_window_size(width: Any, height: Any, unlock_size: bool) -> tuple[int, int]:
    min_width = 80 if unlock_size else 320
    min_height = 60 if unlock_size else 240
    clamped_width = max(min_width, min(int(width or 200), 1920))
    clamped_height = max(min_height, min(int(height or 150), 1080))
    return clamped_width, clamped_height


def minimize_windows(windows: list[dict[str, Any]]) -> dict[str, Any]:
    if not windows:
        return {"ok": True, "count": 0, "minimized": 0, "windows": []}
    try:
        user32 = ctypes.windll.user32
        minimized: list[dict[str, Any]] = []
        skipped_minimized = 0
        for item in windows:
            hwnd = _hwnd_of(item)
            if not hwnd:
                continue
            if _is_iconic(user32, item, hwnd):
                skipped_minimized += 1
                continue
            if bool(user32.ShowWindow(ctypes.c_void_p(hwnd), _SW_MINIMIZE)):
                minimized.append({"pid": _pid_of(item), "hwnd": hwnd})
        if minimized:
            flog_kv(
                "WINDOW",
                "minimized_roblox_windows",
                count=len(windows),
                minimized=len(minimized),
                pids=_pids_csv(minimized),
            )
        return {"ok": True, "count": len(windows), "minimized": len(minimized), "skipped_minimized": skipped_minimized, "windows": windows}
    except Exception as exc:
        flog_kv("WINDOW", "minimize_roblox_windows_failed", "warning", error=str(exc), count=len(windows))
        return {"ok": False, "count": len(windows), "minimized": 0, "error": str(exc), "windows": windows}


def _restore_if_iconic(user32: Any, item: dict[str, Any]) -> dict[str, Any] | None:
    hwnd = _hwnd_of(item)
    if not hwnd or not _is_iconic(user32, item, hwnd):
        return None
    try:
        user32.ShowWindow(ctypes.c_void_p(hwnd), _SW_RESTORE)
        return {"pid": _pid_of(item), "hwnd": hwnd}
    except Exception:
        return None


def unminimize_windows(windows: list[dict[str, Any]]) -> dict[str, Any]:
    """Manual-action helper: restore minimized windows before arrange/resize.

    Never call from auto cycles (would flicker + defeat auto-minimize).
    """
    if not windows:
        return {"ok": True, "count": 0, "restored": 0, "windows": []}
    try:
        user32 = ctypes.windll.user32
        restored: list[dict[str, Any]] = []
        for item in windows:
            entry = _restore_if_iconic(user32, item)
            if entry is not None:
                restored.append(entry)
        if restored:
            flog_kv(
                "WINDOW",
                "unminimized_roblox_windows",
                count=len(windows),
                restored=len(restored),
                pids=_pids_csv(restored),
            )
        return {"ok": True, "count": len(windows), "restored": len(restored), "windows": windows}
    except Exception as exc:
        flog_kv("WINDOW", "unminimize_roblox_windows_failed", "warning", error=str(exc), count=len(windows))
        return {"ok": False, "count": len(windows), "restored": 0, "error": str(exc), "windows": windows}


TASKBAR_HIDDEN_PROP = "CronusTaskbarHidden"
_TASKBAR_HIDDEN_PROP = TASKBAR_HIDDEN_PROP


def _is_taskbar_hidden(user32: Any, hwnd: int) -> bool:
    try:
        return bool(user32.GetPropW(ctypes.c_void_p(hwnd), TASKBAR_HIDDEN_PROP))
    except Exception:
        return False


def _mark_taskbar_hidden(user32: Any, hwnd: int) -> None:
    try:
        user32.SetPropW(ctypes.c_void_p(hwnd), TASKBAR_HIDDEN_PROP, ctypes.c_void_p(1))
    except Exception:
        "intentional: ignore exception"


def _unmark_taskbar_hidden(user32: Any, hwnd: int) -> None:
    try:
        user32.RemovePropW(ctypes.c_void_p(hwnd), TASKBAR_HIDDEN_PROP)
    except Exception:
        "intentional: ignore exception"


def _acquire_taskbar() -> dict[str, Any] | None:
    """Create an ITaskbarList instance for AddTab/DeleteTab. Returns None on failure."""
    try:
        ole32 = ctypes.windll.ole32

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        clsid = GUID(
            0x56FDF344,
            0xFD6D,
            0x11D0,
            (ctypes.c_ubyte * 8)(0x95, 0x8A, 0x00, 0x60, 0x97, 0xC9, 0xA0, 0x90),
        )
        iid = GUID(
            0x56FDF342,
            0xFD6D,
            0x11D0,
            (ctypes.c_ubyte * 8)(0x95, 0x8A, 0x00, 0x60, 0x97, 0xC9, 0xA0, 0x90),
        )
        try:
            ole32.CoInitializeEx(None, 0x2)
        except Exception:
            "intentional: ignore exception"
        ole32.CoCreateInstance.argtypes = [
            ctypes.POINTER(GUID),
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(GUID),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        ole32.CoCreateInstance.restype = ctypes.c_long
        ppv = ctypes.c_void_p(0)
        hr = int(ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(ppv)))
        if hr != 0 or not ppv.value:
            return None
        obj_addr = int(ppv.value)
        try:
            vtbl_addr = ctypes.cast(obj_addr, ctypes.POINTER(ctypes.c_void_p)).contents.value
        except Exception:
            return None
        if not vtbl_addr:
            return None
        return {"obj": obj_addr, "vtbl": int(vtbl_addr)}
    except Exception:
        return None


def _taskbar_vfunc(acq: dict[str, Any], index: int, hwnd: int = 0, no_hwnd: bool = False) -> bool:
    try:
        ptrsize = ctypes.sizeof(ctypes.c_void_p)
        func_addr = ctypes.cast(
            int(acq["vtbl"]) + int(index) * ptrsize,
            ctypes.POINTER(ctypes.c_void_p),
        ).contents.value
        if not func_addr:
            return False
        if no_hwnd:
            proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
            return int(proto(func_addr)(ctypes.c_void_p(int(acq["obj"])))) == 0
        proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)
        return int(proto(func_addr)(ctypes.c_void_p(int(acq["obj"])), ctypes.c_void_p(int(hwnd)))) == 0
    except Exception:
        return False


def _release_taskbar(acq: dict[str, Any] | None) -> None:
    if not acq:
        return
    try:
        ptrsize = ctypes.sizeof(ctypes.c_void_p)
        func_addr = ctypes.cast(
            int(acq["vtbl"]) + 2 * ptrsize,
            ctypes.POINTER(ctypes.c_void_p),
        ).contents.value
        if func_addr:
            proto = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
            proto(func_addr)(ctypes.c_void_p(int(acq["obj"])))
    except Exception:
        "intentional: ignore exception"


def _hide_one(user32: Any, acq: dict[str, Any] | None, item: dict[str, Any]) -> str:
    """Minimize + remove the taskbar tab for one window.

    Returns ``hidden``, ``already`` (idempotent skip) or ``failed``.
    Window stays IsWindowVisible=True so Task Manager keeps listing it as
    its own Roblox app instead of grouping it under Cronus background.
    """
    hwnd = _hwnd_of(item)
    if not hwnd:
        return "failed"
    if not _is_visible(user32, hwnd):
        # Migrate legacy SW_HIDE windows to the new mode.
        try:
            user32.ShowWindow(ctypes.c_void_p(hwnd), _SW_RESTORE)
        except Exception:
            "intentional: ignore exception"
    if _is_iconic(user32, item, hwnd) and _is_taskbar_hidden(user32, hwnd):
        return "already"
    if acq:
        _taskbar_vfunc(acq, 5, hwnd=hwnd)
    try:
        user32.ShowWindow(ctypes.c_void_p(hwnd), _SW_MINIMIZE)
        _mark_taskbar_hidden(user32, hwnd)
    except Exception:
        return "failed"
    return "hidden"


def hide_windows(windows: list[dict[str, Any]]) -> dict[str, Any]:
    """Minimize + ITaskbarList::DeleteTab: off screen, no taskbar button.

    Window stays IsWindowVisible=True so Task Manager keeps listing it as
    its own Roblox app instead of grouping it under Cronus background.
    Process keeps running. Idempotent via window prop + iconic check.
    """
    if not windows:
        return {"ok": True, "count": 0, "hidden": 0, "windows": []}
    try:
        user32 = ctypes.windll.user32
        hidden: list[dict[str, Any]] = []
        skipped_hidden = 0
        acq = _acquire_taskbar()
        if acq:
            _taskbar_vfunc(acq, 3, no_hwnd=True)
        try:
            for item in windows:
                outcome = _hide_one(user32, acq, item)
                if outcome == "already":
                    skipped_hidden += 1
                elif outcome == "hidden":
                    hidden.append({"pid": _pid_of(item), "hwnd": _hwnd_of(item)})
        finally:
            _release_taskbar(acq)
        if hidden:
            flog_kv(
                "WINDOW",
                "hidden_roblox_windows",
                count=len(windows),
                hidden=len(hidden),
                pids=_pids_csv(hidden),
            )
        return {"ok": True, "count": len(windows), "hidden": len(hidden), "skipped_hidden": skipped_hidden, "windows": windows}
    except Exception as exc:
        flog_kv("WINDOW", "hide_roblox_windows_failed", "warning", error=str(exc), count=len(windows))
        return {"ok": False, "count": len(windows), "hidden": 0, "error": str(exc), "windows": windows}


def _show_one(user32: Any, acq: dict[str, Any] | None, item: dict[str, Any]) -> dict[str, Any] | None:
    hwnd = _hwnd_of(item)
    if not hwnd:
        return None
    _unmark_taskbar_hidden(user32, hwnd)
    if acq:
        _taskbar_vfunc(acq, 4, hwnd=hwnd)
    try:
        user32.ShowWindow(ctypes.c_void_p(hwnd), _SW_RESTORE)
        return {"pid": _pid_of(item), "hwnd": hwnd}
    except Exception:
        return None


def show_windows(windows: list[dict[str, Any]]) -> dict[str, Any]:
    """AddTab + SW_RESTORE. Handles new minimize+DeleteTab hides and legacy SW_HIDE."""
    if not windows:
        return {"ok": True, "count": 0, "shown": 0, "windows": []}
    try:
        user32 = ctypes.windll.user32
        shown: list[dict[str, Any]] = []
        acq = _acquire_taskbar()
        if acq:
            _taskbar_vfunc(acq, 3, no_hwnd=True)
        try:
            for item in windows:
                entry = _show_one(user32, acq, item)
                if entry is not None:
                    shown.append(entry)
        finally:
            _release_taskbar(acq)
        if shown:
            flog_kv(
                "WINDOW",
                "shown_roblox_windows",
                count=len(windows),
                shown=len(shown),
                pids=_pids_csv(shown),
            )
        return {"ok": True, "count": len(windows), "shown": len(shown), "windows": windows}
    except Exception as exc:
        flog_kv("WINDOW", "show_roblox_windows_failed", "warning", error=str(exc), count=len(windows))
        return {"ok": False, "count": len(windows), "shown": 0, "error": str(exc), "windows": windows}


def _resize_style(get_long: Any, set_long: Any, hwnd: int, compact_required: bool) -> None:
    style = int(get_long(ctypes.c_void_p(hwnd), _GWL_STYLE) or 0)
    if compact_required:
        compact_style = _compact_style(style)
        if compact_style != style:
            set_long(ctypes.c_void_p(hwnd), _GWL_STYLE, compact_style)
        return
    if _has_normal_frame(style):
        return
    set_long(ctypes.c_void_p(hwnd), _GWL_STYLE, _normal_style())


def _current_size(item: dict[str, Any]) -> tuple[int, int]:
    return int(item.get("width") or 0), int(item.get("height") or 0)


def _apply_resize(
    user32: Any,
    item: dict[str, Any],
    hwnd: int,
    width: int,
    height: int,
) -> tuple[bool, int, int]:
    ok = bool(
        user32.SetWindowPos(
            ctypes.c_void_p(hwnd),
            ctypes.c_void_p(0),
            int(item.get("left") or 0),
            int(item.get("top") or 0),
            width,
            height,
            _SWP_FLAGS,
        )
    )
    actual_width, actual_height = _window_rect(user32, hwnd)
    return ok, actual_width, actual_height


def _resize_item(
    user32: Any,
    get_long: Any,
    set_long: Any,
    item: dict[str, Any],
    width: int,
    height: int,
    compact_required: bool,
) -> tuple[str, dict[str, Any]]:
    hwnd = _hwnd_of(item)
    if not hwnd:
        return "ignore", {}
    if _is_iconic(user32, item, hwnd):
        # SetWindowPos on an iconic window is a no-op for its normal
        # position (verified live). Skip here; the window keeps its grid
        # slot via enumeration order and is fixed on restore/next visible
        # cycle. Manual rearrange restores first via unminimize_windows.
        return "skipped_minimized", {}
    current_size = _current_size(item)
    if current_size == (width, height):
        return "skipped", {}
    _resize_style(get_long, set_long, hwnd, compact_required)
    ok, actual_width, actual_height = _apply_resize(user32, item, hwnd, width, height)
    if ok and abs(actual_width - width) <= 2 and abs(actual_height - height) <= 2:
        return "resized", {
            "pid": _pid_of(item),
            "hwnd": hwnd,
            "from_width": current_size[0],
            "from_height": current_size[1],
            "actual_width": actual_width,
            "actual_height": actual_height,
            "compact": compact_required,
        }
    return "failed", {"pid": _pid_of(item), "hwnd": hwnd, "actual_width": actual_width, "actual_height": actual_height}


def resize_windows(windows: list[dict[str, Any]], width: int, height: int, unlock_size: bool = True) -> dict[str, Any]:
    width, height = _clamp_window_size(width, height, unlock_size)
    compact_required = _compact_required(width, height, unlock_size)
    if not windows:
        return {"ok": True, "count": 0, "resized": 0, "skipped": 0, "width": width, "height": height, "unlock_size": bool(unlock_size), "windows": []}
    try:
        user32 = ctypes.windll.user32
        get_window_long, set_window_long = _window_long_api(user32)
        resized: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        skipped = 0
        skipped_minimized = 0
        for item in windows:
            status, payload = _resize_item(
                user32,
                get_window_long,
                set_window_long,
                item,
                width,
                height,
                compact_required,
            )
            if status == "skipped_minimized":
                skipped_minimized += 1
            elif status == "skipped":
                skipped += 1
            elif status == "resized":
                resized.append(payload)
            elif status == "failed":
                failed.append(payload)
        if resized:
            flog_kv(
                "WINDOW",
                "resized_roblox_windows",
                count=len(windows),
                resized=len(resized),
                width=width,
                height=height,
                compact=compact_required,
                pids=_pids_csv(resized),
            )
        return {
            "ok": not failed,
            "count": len(windows),
            "resized": len(resized),
            "skipped": skipped,
            "skipped_minimized": skipped_minimized,
            "failed": len(failed),
            "width": width,
            "height": height,
            "compact": compact_required,
            "unlock_size": bool(unlock_size),
            "failed_windows": failed,
            "windows": windows,
        }
    except Exception as exc:
        flog_kv("WINDOW", "resize_roblox_windows_failed", "warning", error=str(exc), count=len(windows), width=width, height=height)
        return {"ok": False, "count": len(windows), "resized": 0, "skipped": 0, "failed": len(windows), "width": width, "height": height, "unlock_size": bool(unlock_size), "error": str(exc), "windows": windows}


def _auto_arrange_gap(
    target_width: int,
    target_height: int,
    columns: int,
    rows: int,
    work_width: int,
    work_height: int,
    margin: int,
) -> int:
    if columns <= 1 and rows <= 1:
        return 0
    usable_width = max(80, work_width - (margin * 2))
    usable_height = max(60, work_height - (margin * 2))
    scale_without_gap = min(
        usable_width / float(max(1, columns * target_width)),
        usable_height / float(max(1, rows * target_height)),
    )
    density = columns * rows
    preferred = 8
    if target_width <= 360 or target_height <= 260 or density > 16:
        preferred = 2
    elif density > 8:
        preferred = 4
    if scale_without_gap < 0.6:
        return 0
    if scale_without_gap < 0.8:
        preferred = min(preferred, 2)
    return preferred


class _ArrangeParams(NamedTuple):
    target_width: int
    target_height: int
    columns: int
    requested_rows: int | None
    gap: int
    margin: int
    auto_gap: bool


class _Arrangement(NamedTuple):
    work: dict[str, int]
    work_left: int
    work_top: int
    columns: int
    rows: int
    gap: int
    margin: int
    tile_width: int
    tile_height: int
    compact: bool


def _arrange_params(
    width: Any,
    height: Any,
    columns: Any,
    gap: Any,
    margin: Any,
    rows: Any,
    unlock_size: bool,
) -> _ArrangeParams:
    target_width, target_height = _clamp_window_size(width, height, unlock_size)
    clamped_columns = max(1, min(int(columns or 6), 32))
    requested_rows = max(1, min(int(rows or 0), 32)) if rows else None
    auto_gap = int(gap or 0) <= 0
    clamped_gap = 0 if auto_gap else max(0, min(int(gap or 0), 80))
    clamped_margin = max(0, min(int(margin or 0), 300))
    return _ArrangeParams(
        target_width=target_width,
        target_height=target_height,
        columns=clamped_columns,
        requested_rows=requested_rows,
        gap=clamped_gap,
        margin=clamped_margin,
        auto_gap=auto_gap,
    )


def _arrange_geometry(
    windows_count: int,
    params: _ArrangeParams,
    unlock_size: bool,
    resize: bool,
) -> _Arrangement:
    work = primary_monitor_work_area()
    work_left = int(work.get("left") or 0)
    work_top = int(work.get("top") or 0)
    work_width = max(80, int(work.get("width") or 1920))
    work_height = max(60, int(work.get("height") or 1080))
    effective_columns = min(params.columns, max(1, windows_count))
    actual_rows = int(math.ceil(windows_count / float(effective_columns)))
    rows = max(actual_rows, params.requested_rows or actual_rows)
    gap = params.gap
    if params.auto_gap:
        gap = _auto_arrange_gap(
            params.target_width,
            params.target_height,
            effective_columns,
            rows,
            work_width,
            work_height,
            params.margin,
        )
    available_width = max(80, work_width - (params.margin * 2) - (gap * max(0, effective_columns - 1)))
    available_height = max(60, work_height - (params.margin * 2) - (gap * max(0, rows - 1)))
    scale = min(
        1.0,
        available_width / float(max(1, effective_columns * params.target_width)),
        available_height / float(max(1, rows * params.target_height)),
    )
    tile_width = max(80, int(params.target_width * scale))
    tile_height = max(60, int(params.target_height * scale))
    return _Arrangement(
        work=work,
        work_left=work_left,
        work_top=work_top,
        columns=effective_columns,
        rows=rows,
        gap=gap,
        margin=params.margin,
        tile_width=tile_width,
        tile_height=tile_height,
        compact=_compact_required(tile_width, tile_height, unlock_size, resize),
    )


def _arrange_item(
    user32: Any,
    get_long: Any,
    set_long: Any,
    item: dict[str, Any],
    index: int,
    placement: _Arrangement,
    resize: bool,
) -> tuple[str, dict[str, Any]]:
    hwnd = _hwnd_of(item)
    if not hwnd:
        return "ignore", {}
    if _is_iconic(user32, item, hwnd):
        # Reserve the grid slot but don't touch the minimized window:
        # SetWindowPos is a no-op on iconic windows. It is fixed on
        # restore/next visible cycle; manual rearrange restores first via
        # unminimize_windows.
        return "skipped_minimized", {}
    row = index // placement.columns
    col = index % placement.columns
    x = placement.work_left + placement.margin + (col * (placement.tile_width + placement.gap))
    y = placement.work_top + placement.margin + (row * (placement.tile_height + placement.gap))
    style = int(get_long(ctypes.c_void_p(hwnd), _GWL_STYLE) or 0)
    if placement.compact:
        compact_style = _compact_style(style)
        if compact_style != style:
            set_long(ctypes.c_void_p(hwnd), _GWL_STYLE, compact_style)
    current_width = int(item.get("width") or placement.tile_width)
    current_height = int(item.get("height") or placement.tile_height)
    apply_width = placement.tile_width if resize else max(80, current_width)
    apply_height = placement.tile_height if resize else max(60, current_height)
    ok = bool(
        user32.SetWindowPos(
            ctypes.c_void_p(hwnd),
            ctypes.c_void_p(0),
            x,
            y,
            apply_width,
            apply_height,
            _SWP_FLAGS,
        )
    )
    payload = {
        "pid": _pid_of(item),
        "hwnd": hwnd,
        "x": x,
        "y": y,
        "width": apply_width,
        "height": apply_height,
        "row": row,
        "column": col,
    }
    return ("arranged" if ok else "failed"), payload


def _requested_rows(params: _ArrangeParams, fallback: int) -> int:
    return params.requested_rows or fallback


def arrange_windows(
    windows: list[dict[str, Any]],
    width: int,
    height: int,
    columns: int = 6,
    gap: int = 2,
    margin: int = 0,
    unlock_size: bool = True,
    resize: bool = True,
    rows: int | None = None,
) -> dict[str, Any]:
    params = _arrange_params(width, height, columns, gap, margin, rows, unlock_size)
    if not windows:
        return {"ok": True, "count": 0, "arranged": 0, "failed": 0, "width": params.target_width, "height": params.target_height, "columns": params.columns, "rows": _requested_rows(params, 0), "gap": params.gap, "gap_auto": params.auto_gap, "margin": params.margin, "unlock_size": bool(unlock_size), "resize": bool(resize), "windows": []}
    try:
        user32 = ctypes.windll.user32
        get_window_long, set_window_long = _window_long_api(user32)
        placement = _arrange_geometry(len(windows), params, unlock_size, resize)
        arranged: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        skipped_minimized = 0
        for index, item in enumerate(windows):
            status, payload = _arrange_item(
                user32,
                get_window_long,
                set_window_long,
                item,
                index,
                placement,
                resize,
            )
            if status == "skipped_minimized":
                skipped_minimized += 1
            elif status == "arranged":
                arranged.append(payload)
            elif status == "failed":
                failed.append(payload)
        if arranged:
            flog_kv(
                "WINDOW",
                "arranged_roblox_windows",
                count=len(windows),
                arranged=len(arranged),
                failed=len(failed),
                width=placement.tile_width,
                height=placement.tile_height,
                columns=placement.columns,
                rows=placement.rows,
                gap=placement.gap,
                gap_auto=params.auto_gap,
                margin=params.margin,
                pids=_pids_csv(arranged),
                unlock_size=bool(unlock_size),
                resize=bool(resize),
            )
        return {
            "ok": not failed,
            "count": len(windows),
            "arranged": len(arranged),
            "failed": len(failed),
            "skipped_minimized": skipped_minimized,
            "width": placement.tile_width,
            "height": placement.tile_height,
            "requested_width": params.target_width,
            "requested_height": params.target_height,
            "columns": placement.columns,
            "requested_columns": params.columns,
            "rows": placement.rows,
            "requested_rows": _requested_rows(params, placement.rows),
            "gap": placement.gap,
            "gap_auto": params.auto_gap,
            "margin": params.margin,
            "unlock_size": bool(unlock_size),
            "resize": bool(resize),
            "work_area": placement.work,
            "windows": arranged,
            "failed_windows": failed,
        }
    except Exception as exc:
        flog_kv("WINDOW", "arrange_roblox_windows_failed", "warning", error=str(exc), count=len(windows), width=params.target_width, height=params.target_height, columns=params.columns)
        return {"ok": False, "count": len(windows), "arranged": 0, "failed": len(windows), "width": params.target_width, "height": params.target_height, "columns": params.columns, "gap": params.gap, "gap_auto": params.auto_gap, "margin": params.margin, "unlock_size": bool(unlock_size), "resize": bool(resize), "error": str(exc), "windows": windows}


def _restore_rect(item: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        int(item.get("left") or 0),
        int(item.get("top") or 0),
        max(int(item.get("width") or 0), 816),
        max(int(item.get("height") or 0), 638),
    )


def _restore_window_style(user32: Any, get_long: Any, set_long: Any, item: dict[str, Any]) -> bool:
    hwnd = _hwnd_of(item)
    if not hwnd:
        return False
    if _is_iconic(user32, item, hwnd):
        try:
            user32.ShowWindow(ctypes.c_void_p(hwnd), _SW_RESTORE)
        except Exception:
            "intentional: ignore exception"
    style = int(get_long(ctypes.c_void_p(hwnd), _GWL_STYLE) or 0)
    if _has_normal_frame(style):
        return False
    set_long(ctypes.c_void_p(hwnd), _GWL_STYLE, _normal_style())
    left, top, width, height = _restore_rect(item)
    user32.SetWindowPos(
        ctypes.c_void_p(hwnd),
        ctypes.c_void_p(0),
        left,
        top,
        width,
        height,
        _SWP_FLAGS,
    )
    return True


def restore_window_styles(windows: list[dict[str, Any]]) -> dict[str, Any]:
    if not windows:
        return {"ok": True, "count": 0, "restored": 0, "windows": []}
    try:
        user32 = ctypes.windll.user32
        get_window_long, set_window_long = _window_long_api(user32)
        restored = 0
        for item in windows:
            if _restore_window_style(user32, get_window_long, set_window_long, item):
                restored += 1
        if restored:
            flog_kv("WINDOW", "restored_roblox_window_styles", count=len(windows), restored=restored)
        return {"ok": True, "count": len(windows), "restored": restored, "windows": windows}
    except Exception as exc:
        flog_kv("WINDOW", "restore_roblox_window_styles_failed", "warning", error=str(exc), count=len(windows))
        return {"ok": False, "count": len(windows), "restored": 0, "error": str(exc), "windows": windows}
