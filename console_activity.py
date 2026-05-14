from __future__ import annotations

import os
import re
import sys
import threading
import time
from typing import Any, Dict, Optional


_LOCK = threading.Lock()
_ACTIVE_ACCOUNTS: set[str] = set()
_QUEUE_SIZE = 0
_LAST_DISCONNECT_AT: Dict[str, float] = {}
_DISCONNECT_DEDUP_SECONDS = 3.0

_ICON_OK = "OK"
_ICON_WARN = "!!"
_ICON_FAIL = "XX"

_COLOR_RESET = "\x1b[0m"
_COLOR_DIM = "\x1b[90m"
_COLOR_BY_ICON = {
    _ICON_OK: "\x1b[92m",
    _ICON_WARN: "\x1b[93m",
    _ICON_FAIL: "\x1b[91m",
}
_COLOR_SUPPORT: Optional[bool] = None

_KV_LINE_RE = re.compile(r"^\[[A-Z_]+\]\s+[a-z0-9_]+\b.*\b[a-zA-Z_][a-zA-Z0-9_]*=")


def _enabled() -> bool:
    value = os.environ.get("ARGUS_CONSOLE_ACTIVITY", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _enable_virtual_terminal() -> bool:
    if not getattr(sys.stdout, "isatty", lambda: False)():
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


def _colors_enabled() -> bool:
    global _COLOR_SUPPORT
    requested = os.environ.get("ARGUS_CONSOLE_COLOR", "").strip().lower()
    if requested in {"0", "false", "no", "off"}:
        return False
    if requested not in {"1", "true", "yes", "on"}:
        return False
    if _COLOR_SUPPORT is None:
        _COLOR_SUPPORT = _enable_virtual_terminal()
    return bool(_COLOR_SUPPORT)


def _paint(text: str, color: str = "") -> str:
    if not color or not _colors_enabled():
        return text
    return f"{color}{text}{_COLOR_RESET}"


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _int_text(value: Any, default: str = "") -> str:
    try:
        if value in (None, ""):
            return default
        return str(int(value))
    except Exception:
        return _text(value, default)


def _account(fields: Dict[str, Any]) -> str:
    for key in ("account", "username", "account_id", "user"):
        value = _text(fields.get(key))
        if value:
            return value
    return "Account"


def _reason(fields: Dict[str, Any], default: str = "") -> str:
    for key in ("reason", "trigger", "detail", "reject"):
        value = _text(fields.get(key))
        if value:
            return value.strip().lower().replace(" ", "_")
    return default


def _pid(fields: Dict[str, Any]) -> str:
    return _int_text(fields.get("pid") or fields.get("PID"))


def _pid_value(value: Any, default: str = "unknown") -> str:
    return _paint(_text(value, default), _COLOR_DIM)


def _pid_paren(value: Any, default: str = "unknown") -> str:
    return _paint(f"(PID: {_text(value, default)})", _COLOR_DIM)


def _duration_text(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    try:
        seconds = max(0.0, float(text))
        if seconds == 0:
            return "now"
        if seconds.is_integer():
            return f"{int(seconds)}s"
        return f"{seconds:.1f}s"
    except Exception:
        return text


def _line(icon: str, message: str, *, indent: bool = False) -> str:
    stamp = f"[{time.strftime('%H:%M:%S')}]"
    prefix = f"{icon:<2}"
    gap = "   " if indent else " "
    if _colors_enabled():
        stamp = _paint(stamp, _COLOR_DIM)
        prefix = _paint(prefix, _COLOR_BY_ICON.get(icon, ""))
    return f"{stamp}{gap}{prefix} {message}"


def format_console_line(icon: str, message: str, *, indent: bool = False) -> str:
    return _line(_text(icon, _ICON_OK).upper(), _text(message), indent=indent)


def _disconnect_line(account: str, reason: str = "", delay: str = "", action: str = "restart") -> Optional[str]:
    now = time.monotonic()
    key = _text(account, "Account").lower()
    previous = float(_LAST_DISCONNECT_AT.get(key) or 0.0)
    if now - previous < _DISCONNECT_DEDUP_SECONDS:
        return None
    _LAST_DISCONNECT_AT[key] = now
    details = []
    if reason:
        details.append(reason)
    if delay:
        details.append(f"{action} in {delay}")
    suffix = f" ({', '.join(details)})" if details else ""
    return _line(_ICON_WARN, f"{account} disconnected{suffix}")


def _print_line(line: str) -> None:
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        try:
            print(line.encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass
    except Exception:
        pass


def _set_title_locked() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        title = f"Argus | Active: {len(_ACTIVE_ACCOUNTS)} | Queue: {_QUEUE_SIZE}"
        ctypes.windll.kernel32.SetConsoleTitleW(title)
    except Exception:
        pass


def _update_counters(scope: str, name: str, fields: Dict[str, Any]) -> None:
    global _QUEUE_SIZE
    account = _account(fields)
    if scope == "STATE" and name == "transition":
        old = _text(fields.get("old")).upper()
        new = _text(fields.get("new")).upper()
        if new == "IN_GAME":
            _ACTIVE_ACCOUNTS.add(account)
        if old == "IN_GAME" and new != "IN_GAME":
            _ACTIVE_ACCOUNTS.discard(account)
        if new in {"FAILED", "IDLE"}:
            _ACTIVE_ACCOUNTS.discard(account)
        if new == "LAUNCHING":
            _QUEUE_SIZE = max(0, _QUEUE_SIZE - 1)
    elif scope == "STATE" and name == "forced_reset":
        _ACTIVE_ACCOUNTS.discard(account)
    elif scope == "QUEUE":
        if "size" in fields:
            try:
                _QUEUE_SIZE = max(0, int(fields.get("size") or 0))
            except Exception:
                pass
        elif name == "cancel_all":
            _QUEUE_SIZE = 0


def _format_state(name: str, fields: Dict[str, Any]) -> Optional[str]:
    account = _account(fields)
    pid = _pid(fields)
    if name == "transition":
        new = _text(fields.get("new")).upper()
        if new == "IN_GAME":
            return _line(_ICON_OK, f"{account} {_pid_paren(pid or 'bound')}", indent=True)
        return None
    if name == "process_bind_verified" and pid:
        return _line(_ICON_OK, f"Found Roblox process {_pid_value(pid)} for user {account}")
    return None


def _format_recovery(name: str, fields: Dict[str, Any]) -> Optional[str]:
    account = _account(fields)
    reason = _reason(fields, "recovery")
    if name == "network_lost":
        return _disconnect_line(account, "network_lost", action="reconnect")
    if name == "cooldown":
        delay = _duration_text(fields.get("delay") or fields.get("delay_seconds"))
        action = "reconnect" if reason in {"network_drop", "connection_error", "network_lost"} else "restart"
        return _disconnect_line(account, reason, delay, action)
    return None


def _format_misc(scope: str, name: str, fields: Dict[str, Any]) -> Optional[str]:
    account = _account(fields)
    pid = _pid(fields)
    if scope == "WORKER" and name in {"visible_process_adopted", "rebind_refreshed"} and pid:
        return _line(_ICON_OK, f"Found Roblox process {_pid_value(pid)} for user {account}")
    return None


def _format_structured(scope: str, name: str, level: str, fields: Dict[str, Any]) -> Optional[str]:
    if scope == "STATUS":
        return None
    if scope == "STATE":
        return _format_state(name, fields)
    if scope == "RECOVERY":
        return _format_recovery(name, fields)
    return _format_misc(scope, name, fields)


def emit_structured(scope: str, name: str, level: str = "info", **fields: Any) -> None:
    if not _enabled():
        return
    scope_text = _text(scope).upper()
    name_text = _text(name)
    data = dict(fields)
    with _LOCK:
        _update_counters(scope_text, name_text, data)
        line = _format_structured(scope_text, name_text, level, data)
        if line:
            _print_line(line)
        _set_title_locked()


def _format_text(message: str, level: str = "info") -> Optional[str]:
    msg = message.strip()
    if not msg or _KV_LINE_RE.match(msg):
        return None
    if msg.startswith("[EVENT]") or msg.startswith("[RECOVERY] hold"):
        return None

    match = re.match(r"^\[WORKER\]\s+(.+?)\s+disconnect dialog detected - will recover in\s+([0-9.]+)s\b", msg)
    if match:
        delay = _duration_text(match.group(2))
        return _disconnect_line(match.group(1).strip(), "disconnect_dialog", delay)

    match = re.match(r"^\[WORKER\]\s+(.+?)\s+Not Responding\b", msg)
    if match:
        return _disconnect_line(match.group(1).strip(), "not_responding")

    return None


def emit_text(message: str, level: str = "info") -> None:
    if not _enabled():
        return
    line = _format_text(str(message or ""), level)
    if not line:
        return
    with _LOCK:
        _print_line(line)
        _set_title_locked()
