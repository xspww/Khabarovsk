from __future__ import annotations
import os
import re
import sys
from typing import Any, Dict, Iterable, Optional
COLOR_RESET = "\x1b[0m"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
def enable_virtual_terminal(stream: Any = None) -> bool:
    stream = stream or sys.stdout
    if not getattr(stream, "isatty", lambda: False)():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        if handle in (0, -1):
            return False
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False
def color_requested() -> bool:
    requested = os.environ.get("CRONUS_CONSOLE_COLOR", "").strip().lower()
    return requested in {"1", "true", "yes", "on"}
def paint(text: str, color: str, *, enabled: bool) -> str:
    return f"{color}{text}{COLOR_RESET}" if color and enabled else text
def visible_len(text: str) -> int:
    return len(ANSI_RE.sub("", str(text)))
def _ascii_safe(message: Any, replacements: Optional[Dict[str, str]] = None) -> str:
    safe = str(message)
    for old, new in (replacements or {}).items():
        safe = safe.replace(old, new)
    return safe.encode("ascii", "replace").decode("ascii")
def write_line(message: Any = "", replacements: Optional[Dict[str, str]] = None) -> None:
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        try:
            print(_ascii_safe(message, replacements), flush=True)
        except Exception:
            pass
    except Exception:
        pass
def write_inline(message: Any = "", replacements: Optional[Dict[str, str]] = None, spinner_frames: Iterable[str] = ()) -> None:
    try:
        sys.stdout.write(str(message))
        sys.stdout.flush()
    except UnicodeEncodeError:
        try:
            safe_message = str(message)
            for frame in spinner_frames:
                safe_message = safe_message.replace(frame, "*")
            sys.stdout.write(_ascii_safe(safe_message, replacements))
            sys.stdout.flush()
        except Exception:
            pass
    except Exception:
        pass


def clear_screen() -> None:
    try:
        os.system("cls" if os.name == "nt" else "clear")
    except Exception:
        pass
