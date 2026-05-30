from __future__ import annotations

import ctypes
import math
from typing import Any, Dict, List, Optional, Tuple

from core import flog_kv


def primary_monitor_work_area() -> Dict[str, int]:
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


def _window_rect(user32: Any, hwnd: int) -> Tuple[int, int]:
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


def minimize_windows(windows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not windows:
        return {"ok": True, "count": 0, "minimized": 0, "windows": []}
    try:
        user32 = ctypes.windll.user32
        SW_MINIMIZE = 6
        minimized: List[Dict[str, Any]] = []
        for item in windows:
            hwnd = int(item.get("hwnd") or 0)
            if not hwnd:
                continue
            ok = bool(user32.ShowWindow(ctypes.c_void_p(hwnd), SW_MINIMIZE))
            if ok:
                minimized.append({"pid": int(item.get("pid") or 0), "hwnd": hwnd})
        if minimized:
            flog_kv(
                "WINDOW",
                "minimized_roblox_windows",
                count=len(windows),
                minimized=len(minimized),
                pids=",".join(str(item.get("pid")) for item in minimized),
            )
        return {"ok": True, "count": len(windows), "minimized": len(minimized), "windows": windows}
    except Exception as exc:
        flog_kv("WINDOW", "minimize_roblox_windows_failed", "warning", error=str(exc), count=len(windows))
        return {"ok": False, "count": len(windows), "minimized": 0, "error": str(exc), "windows": windows}


def resize_windows(windows: List[Dict[str, Any]], width: int, height: int, unlock_size: bool = True) -> Dict[str, Any]:
    min_width = 80 if unlock_size else 320
    min_height = 60 if unlock_size else 240
    width = max(min_width, min(int(width or 200), 1920))
    height = max(min_height, min(int(height or 150), 1080))
    if not windows:
        return {"ok": True, "count": 0, "resized": 0, "skipped": 0, "width": width, "height": height, "unlock_size": bool(unlock_size), "windows": []}
    try:
        user32 = ctypes.windll.user32
        get_window_long, set_window_long = _window_long_api(user32)
        GWL_STYLE = -16
        WS_VISIBLE = 0x10000000
        WS_CLIPCHILDREN = 0x02000000
        WS_CLIPSIBLINGS = 0x04000000
        WS_POPUP = 0x80000000
        WS_CAPTION = 0x00C00000
        WS_SYSMENU = 0x00080000
        WS_THICKFRAME = 0x00040000
        WS_MINIMIZEBOX = 0x00020000
        WS_MAXIMIZEBOX = 0x00010000
        WS_OVERLAPPEDWINDOW = WS_CAPTION | WS_SYSMENU | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        SWP_FRAMECHANGED = 0x0020
        SWP_SHOWWINDOW = 0x0040
        compact_required = bool(unlock_size) and (width < 816 or height < 638)

        resized: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        skipped = 0
        for item in windows:
            hwnd = int(item.get("hwnd") or 0)
            if not hwnd:
                continue
            current_width = int(item.get("width") or 0)
            current_height = int(item.get("height") or 0)
            if current_width == width and current_height == height:
                skipped += 1
                continue
            style = int(get_window_long(ctypes.c_void_p(hwnd), GWL_STYLE) or 0)
            if compact_required:
                compact_style = (
                    (style & ~(WS_CAPTION | WS_SYSMENU | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX))
                    | WS_POPUP
                    | WS_VISIBLE
                    | WS_CLIPCHILDREN
                    | WS_CLIPSIBLINGS
                )
                if compact_style != style:
                    set_window_long(ctypes.c_void_p(hwnd), GWL_STYLE, compact_style)
            elif not (style & WS_CAPTION) or not (style & WS_THICKFRAME):
                normal_style = WS_OVERLAPPEDWINDOW | WS_VISIBLE | WS_CLIPCHILDREN | WS_CLIPSIBLINGS
                set_window_long(ctypes.c_void_p(hwnd), GWL_STYLE, normal_style)
            ok = bool(user32.SetWindowPos(
                ctypes.c_void_p(hwnd),
                ctypes.c_void_p(0),
                int(item.get("left") or 0),
                int(item.get("top") or 0),
                width,
                height,
                SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW,
            ))
            actual_width, actual_height = _window_rect(user32, hwnd)
            if ok and abs(actual_width - width) <= 2 and abs(actual_height - height) <= 2:
                resized.append({
                    "pid": int(item.get("pid") or 0),
                    "hwnd": hwnd,
                    "from_width": current_width,
                    "from_height": current_height,
                    "actual_width": actual_width,
                    "actual_height": actual_height,
                    "compact": compact_required,
                })
            else:
                failed.append({"pid": int(item.get("pid") or 0), "hwnd": hwnd, "actual_width": actual_width, "actual_height": actual_height})
        if resized:
            flog_kv(
                "WINDOW",
                "resized_roblox_windows",
                count=len(windows),
                resized=len(resized),
                width=width,
                height=height,
                compact=compact_required,
                pids=",".join(str(item.get("pid")) for item in resized),
            )
        return {
            "ok": not failed,
            "count": len(windows),
            "resized": len(resized),
            "skipped": skipped,
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


def arrange_windows(
    windows: List[Dict[str, Any]],
    width: int,
    height: int,
    columns: int = 6,
    gap: int = 2,
    margin: int = 0,
    unlock_size: bool = True,
    resize: bool = True,
    rows: Optional[int] = None,
) -> Dict[str, Any]:
    min_width = 80 if unlock_size else 320
    min_height = 60 if unlock_size else 240
    target_width = max(min_width, min(int(width or 200), 1920))
    target_height = max(min_height, min(int(height or 150), 1080))
    columns = max(1, min(int(columns or 6), 32))
    requested_rows = max(1, min(int(rows or 0), 32)) if rows else None
    auto_gap = int(gap or 0) <= 0
    gap = 0 if auto_gap else max(0, min(int(gap or 0), 80))
    margin = max(0, min(int(margin or 0), 300))
    if not windows:
        return {"ok": True, "count": 0, "arranged": 0, "failed": 0, "width": target_width, "height": target_height, "columns": columns, "rows": requested_rows or 0, "gap": gap, "gap_auto": auto_gap, "margin": margin, "unlock_size": bool(unlock_size), "resize": bool(resize), "windows": []}
    try:
        user32 = ctypes.windll.user32
        get_window_long, set_window_long = _window_long_api(user32)
        GWL_STYLE = -16
        WS_VISIBLE = 0x10000000
        WS_CLIPCHILDREN = 0x02000000
        WS_CLIPSIBLINGS = 0x04000000
        WS_POPUP = 0x80000000
        WS_CAPTION = 0x00C00000
        WS_SYSMENU = 0x00080000
        WS_THICKFRAME = 0x00040000
        WS_MINIMIZEBOX = 0x00020000
        WS_MAXIMIZEBOX = 0x00010000
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        SWP_FRAMECHANGED = 0x0020
        SWP_SHOWWINDOW = 0x0040

        work = primary_monitor_work_area()
        work_left = int(work.get("left") or 0)
        work_top = int(work.get("top") or 0)
        work_width = max(80, int(work.get("width") or 1920))
        work_height = max(60, int(work.get("height") or 1080))
        effective_columns = min(columns, max(1, len(windows)))
        actual_rows = int(math.ceil(len(windows) / float(effective_columns)))
        rows = max(actual_rows, requested_rows or actual_rows)
        if auto_gap:
            gap = _auto_arrange_gap(target_width, target_height, effective_columns, rows, work_width, work_height, margin)
        available_width = max(80, work_width - (margin * 2) - (gap * max(0, effective_columns - 1)))
        available_height = max(60, work_height - (margin * 2) - (gap * max(0, rows - 1)))
        scale = min(
            1.0,
            available_width / float(max(1, effective_columns * target_width)),
            available_height / float(max(1, rows * target_height)),
        )
        tile_width = max(80, int(target_width * scale))
        tile_height = max(60, int(target_height * scale))
        compact_required = bool(resize) and bool(unlock_size) and (tile_width < 816 or tile_height < 638)

        arranged: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        for index, item in enumerate(windows):
            hwnd = int(item.get("hwnd") or 0)
            if not hwnd:
                continue
            row = index // effective_columns
            col = index % effective_columns
            x = work_left + margin + (col * (tile_width + gap))
            y = work_top + margin + (row * (tile_height + gap))
            style = int(get_window_long(ctypes.c_void_p(hwnd), GWL_STYLE) or 0)
            if compact_required:
                compact_style = (
                    (style & ~(WS_CAPTION | WS_SYSMENU | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX))
                    | WS_POPUP
                    | WS_VISIBLE
                    | WS_CLIPCHILDREN
                    | WS_CLIPSIBLINGS
                )
                if compact_style != style:
                    set_window_long(ctypes.c_void_p(hwnd), GWL_STYLE, compact_style)
            current_width = int(item.get("width") or tile_width)
            current_height = int(item.get("height") or tile_height)
            apply_width = tile_width if resize else max(80, current_width)
            apply_height = tile_height if resize else max(60, current_height)
            ok = bool(user32.SetWindowPos(
                ctypes.c_void_p(hwnd),
                ctypes.c_void_p(0),
                x,
                y,
                apply_width,
                apply_height,
                SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW,
            ))
            row_data = {"pid": int(item.get("pid") or 0), "hwnd": hwnd, "x": x, "y": y, "width": apply_width, "height": apply_height, "row": row, "column": col}
            if ok:
                arranged.append(row_data)
            else:
                failed.append(row_data)
        if arranged:
            flog_kv(
                "WINDOW",
                "arranged_roblox_windows",
                count=len(windows),
                arranged=len(arranged),
                failed=len(failed),
                width=tile_width,
                height=tile_height,
                columns=effective_columns,
                rows=rows,
                gap=gap,
                gap_auto=auto_gap,
                margin=margin,
                pids=",".join(str(item.get("pid")) for item in arranged),
                unlock_size=bool(unlock_size),
                resize=bool(resize),
            )
        return {
            "ok": not failed,
            "count": len(windows),
            "arranged": len(arranged),
            "failed": len(failed),
            "width": tile_width,
            "height": tile_height,
            "requested_width": target_width,
            "requested_height": target_height,
            "columns": effective_columns,
            "requested_columns": columns,
            "rows": rows,
            "requested_rows": requested_rows or rows,
            "gap": gap,
            "gap_auto": auto_gap,
            "margin": margin,
            "unlock_size": bool(unlock_size),
            "resize": bool(resize),
            "work_area": work,
            "windows": arranged,
            "failed_windows": failed,
        }
    except Exception as exc:
        flog_kv("WINDOW", "arrange_roblox_windows_failed", "warning", error=str(exc), count=len(windows), width=target_width, height=target_height, columns=columns)
        return {"ok": False, "count": len(windows), "arranged": 0, "failed": len(windows), "width": target_width, "height": target_height, "columns": columns, "gap": gap, "gap_auto": auto_gap, "margin": margin, "unlock_size": bool(unlock_size), "resize": bool(resize), "error": str(exc), "windows": windows}


def restore_window_styles(windows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not windows:
        return {"ok": True, "count": 0, "restored": 0, "windows": []}
    try:
        user32 = ctypes.windll.user32
        get_window_long, set_window_long = _window_long_api(user32)
        GWL_STYLE = -16
        WS_VISIBLE = 0x10000000
        WS_CLIPCHILDREN = 0x02000000
        WS_CLIPSIBLINGS = 0x04000000
        WS_CAPTION = 0x00C00000
        WS_SYSMENU = 0x00080000
        WS_THICKFRAME = 0x00040000
        WS_MINIMIZEBOX = 0x00020000
        WS_MAXIMIZEBOX = 0x00010000
        WS_OVERLAPPEDWINDOW = WS_CAPTION | WS_SYSMENU | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        SWP_FRAMECHANGED = 0x0020
        SWP_SHOWWINDOW = 0x0040
        restored = 0
        for item in windows:
            hwnd = int(item.get("hwnd") or 0)
            if not hwnd:
                continue
            style = int(get_window_long(ctypes.c_void_p(hwnd), GWL_STYLE) or 0)
            if (style & WS_CAPTION) and (style & WS_THICKFRAME):
                continue
            normal_style = WS_OVERLAPPEDWINDOW | WS_VISIBLE | WS_CLIPCHILDREN | WS_CLIPSIBLINGS
            set_window_long(ctypes.c_void_p(hwnd), GWL_STYLE, normal_style)
            user32.SetWindowPos(
                ctypes.c_void_p(hwnd),
                ctypes.c_void_p(0),
                int(item.get("left") or 0),
                int(item.get("top") or 0),
                max(int(item.get("width") or 0), 816),
                max(int(item.get("height") or 0), 638),
                SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW,
            )
            restored += 1
        if restored:
            flog_kv("WINDOW", "restored_roblox_window_styles", count=len(windows), restored=restored)
        return {"ok": True, "count": len(windows), "restored": restored, "windows": windows}
    except Exception as exc:
        flog_kv("WINDOW", "restore_roblox_window_styles_failed", "warning", error=str(exc), count=len(windows))
        return {"ok": False, "count": len(windows), "restored": 0, "error": str(exc), "windows": windows}
