from __future__ import annotations

import atexit
import ctypes
import os
from typing import List

from app_paths import APP_DATA_DIR, resource_path
from core import flog_kv


APP_ICON_FILE = "cronus_icon.png"
_CONSOLE_ICON_HANDLES: List[int] = []


def destroy_console_icon_handles() -> None:
    if os.name != "nt":
        return
    while _CONSOLE_ICON_HANDLES:
        handle = _CONSOLE_ICON_HANDLES.pop()
        try:
            ctypes.windll.user32.DestroyIcon(ctypes.c_void_p(int(handle)))
        except Exception:
            pass


def ensure_console_icon_file() -> str:
    source_path = resource_path("assets", APP_ICON_FILE)
    if not os.path.exists(source_path):
        return ""
    icon_path = os.path.join(APP_DATA_DIR, "cronus_console_icon.ico")
    try:
        if (
            os.path.exists(icon_path)
            and os.path.getmtime(icon_path) >= os.path.getmtime(source_path)
            and os.path.getsize(icon_path) > 0
        ):
            return icon_path
    except Exception:
        pass
    try:
        from PIL import Image

        with Image.open(source_path) as image:
            image.convert("RGBA").save(
                icon_path,
                format="ICO",
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
            )
        return icon_path
    except Exception as exc:
        flog_kv("MAIN", "console_icon_create_failed", "warning", source=source_path, error=str(exc))
        return ""


def set_console_window_icon() -> bool:
    if os.name != "nt":
        return False
    icon_path = ensure_console_icon_file()
    if not icon_path:
        return False
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = int(kernel32.GetConsoleWindow() or 0)
        if not hwnd:
            return False

        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x00000010
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1
        GCLP_HICON = -14
        GCLP_HICONSM = -34

        def _load_icon(size: int) -> int:
            return int(user32.LoadImageW(None, icon_path, IMAGE_ICON, size, size, LR_LOADFROMFILE) or 0)

        small_icon = _load_icon(16)
        big_icon = _load_icon(32) or _load_icon(48)
        if not small_icon and not big_icon:
            return False
        if small_icon:
            user32.SendMessageW(ctypes.c_void_p(hwnd), WM_SETICON, ICON_SMALL, ctypes.c_void_p(small_icon))
            _CONSOLE_ICON_HANDLES.append(small_icon)
        if big_icon:
            user32.SendMessageW(ctypes.c_void_p(hwnd), WM_SETICON, ICON_BIG, ctypes.c_void_p(big_icon))
            _CONSOLE_ICON_HANDLES.append(big_icon)
        try:
            set_class_long_ptr = getattr(user32, "SetClassLongPtrW", None) or getattr(user32, "SetClassLongW", None)
            if set_class_long_ptr:
                if big_icon:
                    set_class_long_ptr(ctypes.c_void_p(hwnd), GCLP_HICON, ctypes.c_void_p(big_icon))
                if small_icon:
                    set_class_long_ptr(ctypes.c_void_p(hwnd), GCLP_HICONSM, ctypes.c_void_p(small_icon))
        except Exception:
            pass
        return True
    except Exception as exc:
        flog_kv("MAIN", "console_icon_set_failed", "warning", icon=icon_path, error=str(exc))
        return False


atexit.register(destroy_console_icon_handles)
