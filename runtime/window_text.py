from __future__ import annotations

import ctypes
from typing import Any


def read_window_text(user32: Any, hwnd: int) -> str:
    """Return the caption text for a Win32 window handle."""
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return str(buf.value or "").strip()


def collect_window_text(user32: Any, hwnd: int, seen: set[str], texts: list[str]) -> None:
    """Append a window caption to ``texts`` once, ignoring case-only repeats."""
    text = read_window_text(user32, hwnd)
    if not text:
        return
    key = text.lower()
    if key not in seen:
        seen.add(key)
        texts.append(text)


def make_text_collector(user32: Any, seen: set[str], texts: list[str]) -> Any:
    """Build a Win32 EnumChildWindows callback that collects child captions."""

    def callback(hwnd: Any, lparam: Any) -> bool:
        collect_window_text(user32, hwnd, seen, texts)
        return True

    return callback
