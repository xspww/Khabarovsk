from __future__ import annotations

import os
import re
import sys
import threading
import time
from typing import Any, Dict, Optional


_LOCK = threading.Lock()
_ACTIVE_ACCOUNTS: set[str] = set()
_CAPTCHA_ACCOUNTS: set[str] = set()
_QUEUE_SIZE = 0
_LAST_DISCONNECT_AT: Dict[str, float] = {}
_LAST_CAPTCHA_AT: Dict[str, float] = {}
_SUSPECT_LOGGED_ACCOUNTS: set[str] = set()
_SUSPECT_FINALIZED_AT_BY_ACCOUNT: Dict[str, float] = {}
_LUA_LIVENESS_REQUIRED = False
_DISCONNECT_DEDUP_SECONDS = 3.0
_CAPTCHA_DEDUP_SECONDS = 3.0
_SUSPECT_FINAL_SUPPRESS_SECONDS = 5.0

_ICON_OK = "✔"
_ICON_WARN = "⚠️"
_ICON_FAIL = "❌"
_ICON_VIP = "🔐"
_ICON_VIP_SERVER = "👑"
_ICON_PUBLIC_SERVER = "🦺"
_ICON_CHECKING = "🚧"
_ICON_ALIASES = {
    "OK": _ICON_OK,
    "CHECK": _ICON_OK,
    "SUCCESS": _ICON_OK,
    "READY": _ICON_OK,
    "!!": _ICON_WARN,
    "WARN": _ICON_WARN,
    "WARNING": _ICON_WARN,
    "XX": _ICON_FAIL,
    "FAIL": _ICON_FAIL,
    "FAILED": _ICON_FAIL,
    "ERROR": _ICON_FAIL,
    "SERVER": _ICON_VIP,
    "SMART": _ICON_VIP,
    "VIP": _ICON_VIP,
    "LOCK": _ICON_VIP,
    "PRIVATE": _ICON_VIP,
}

_COLOR_RESET = "\x1b[0m"
_COLOR_DIM = "\x1b[90m"
_COLOR_WHITE = "\x1b[97m"
_COLOR_GOLD = "\x1b[38;2;255;215;0m"
_COLOR_GRAY = "\x1b[38;2;128;128;128m"
_COLOR_USERNAME = "\x1b[38;2;34;139;34m"
_COLOR_DISCONNECTED = "\x1b[38;2;255;0;0m"
_COLOR_DISCONNECT_STAMP = "\x1b[38;2;255;127;80m"
_COLOR_BY_ICON = {
    _ICON_OK: "\x1b[92m",
    _ICON_WARN: "\x1b[93m",
    _ICON_FAIL: "\x1b[91m",
    _ICON_VIP: "\x1b[93m",
    _ICON_VIP_SERVER: _COLOR_GRAY,
    _ICON_PUBLIC_SERVER: _COLOR_GRAY,
    _ICON_CHECKING: "\x1b[93m",
}
_COLOR_SUPPORT: Optional[bool] = None

_KV_LINE_RE = re.compile(r"^\[[A-Z_]+\]\s+[a-z0-9_]+\b.*\b[a-zA-Z_][a-zA-Z0-9_]*=")


def _enabled() -> bool:
    value = os.environ.get("CRONUS_CONSOLE_ACTIVITY", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def set_lua_liveness_required(enabled: bool) -> None:
    global _LUA_LIVENESS_REQUIRED
    with _LOCK:
        _LUA_LIVENESS_REQUIRED = bool(enabled)


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
    requested = os.environ.get("CRONUS_CONSOLE_COLOR", "").strip().lower()
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


def _normalize_icon(value: Any, default: str = _ICON_OK) -> str:
    icon = _text(value)
    if not icon:
        return default
    return _ICON_ALIASES.get(icon.upper(), icon)


def _int_text(value: Any, default: str = "") -> str:
    try:
        if value in (None, ""):
            return default
        return str(int(value))
    except Exception:
        return _text(value, default)


def _boolish(value: Any, default: bool = False) -> bool:
    text = _text(value).lower()
    if text in {"1", "true", "yes", "on", "vip", "private", "private_server"}:
        return True
    if text in {"0", "false", "no", "off", "public"}:
        return False
    return default


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
    return _paint(_text(value, default), _COLOR_GRAY)


def _pid_paren(value: Any, default: str = "unknown") -> str:
    return _paint(f"(PID: {_text(value, default)})", _COLOR_GRAY)


def _username_paren(value: Any, *, color: str = _COLOR_USERNAME) -> str:
    return _paint(f"({_text(value, 'Account')})", color)


def _gray_text(value: str) -> str:
    return _paint(value, _COLOR_GRAY)


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


def _suspect_process_line(account: str) -> str:
    stamp = f"[{time.strftime('%H:%M:%S')}]"
    if _colors_enabled():
        stamp = _paint(stamp, _COLOR_GOLD)
    return f"{stamp} {_ICON_CHECKING} Checking Roblox process ({_text(account, 'Account')})"


def _line(icon: str, message: str, *, indent: bool = False, stamp_color: str = _COLOR_DIM) -> str:
    stamp = f"[{time.strftime('%H:%M:%S')}]"
    icon_text = _normalize_icon(icon, default="")
    gap = "   " if indent else " "
    if _colors_enabled():
        stamp = _paint(stamp, stamp_color)
        icon_text = _paint(icon_text, _COLOR_BY_ICON.get(icon_text, "")) if icon_text else ""
    if icon_text:
        return f"{stamp}{gap}{icon_text} {message}"
    return f"{stamp}{gap}{message}"


def format_console_line(icon: str, message: str, *, indent: bool = False) -> str:
    return _line(_normalize_icon(icon), _text(message), indent=indent)


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
    status = _paint(f"({_text(account, 'Account')}) disconnected", _COLOR_DISCONNECTED)
    return _line(_ICON_WARN, f"{status}{suffix}", stamp_color=_COLOR_DISCONNECT_STAMP)


def _captcha_line(account: str, pid: str = "", detail: str = "") -> Optional[str]:
    now = time.monotonic()
    key = _text(account, "Account").lower()
    previous = float(_LAST_CAPTCHA_AT.get(key) or 0.0)
    if now - previous < _CAPTCHA_DEDUP_SECONDS:
        return None
    _LAST_CAPTCHA_AT[key] = now
    pid_text = f" {_pid_paren(pid)}" if pid else ""
    return _line(_ICON_VIP, f"{_username_paren(account)} CAPTCHA required{pid_text} - paused, solve manually then Resume")


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


def _emit_suspect_process_check(fields: Dict[str, Any]) -> None:
    account = _account(fields)
    key = account.lower()
    final = _boolish(fields.get("final"), False)
    finalized_at = float(_SUSPECT_FINALIZED_AT_BY_ACCOUNT.get(key) or 0.0)
    if finalized_at and time.monotonic() - finalized_at <= _SUSPECT_FINAL_SUPPRESS_SECONDS:
        return
    if final:
        _SUSPECT_LOGGED_ACCOUNTS.discard(key)
        _SUSPECT_FINALIZED_AT_BY_ACCOUNT[key] = time.monotonic()
        return
    if key in _SUSPECT_LOGGED_ACCOUNTS:
        return
    _SUSPECT_FINALIZED_AT_BY_ACCOUNT.pop(key, None)
    _SUSPECT_LOGGED_ACCOUNTS.add(key)
    _print_line(_suspect_process_line(account))


def _title_text_locked() -> str:
    return f"Cronus | Active: {len(_ACTIVE_ACCOUNTS)} | Queue: {_QUEUE_SIZE} | Captcha: {len(_CAPTCHA_ACCOUNTS)}"


def _set_title_locked() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(_title_text_locked())
    except Exception:
        pass


def _update_counters(scope: str, name: str, fields: Dict[str, Any]) -> None:
    global _QUEUE_SIZE
    account = _account(fields)
    if scope == "STATE" and name == "transition":
        old = _text(fields.get("old")).upper()
        new = _text(fields.get("new")).upper()
        reason = _reason(fields)
        if reason == "captcha_required":
            _CAPTCHA_ACCOUNTS.add(account)
        elif reason in {"manual_resume", "captcha_resume"} or new in {"QUEUED", "LAUNCHING", "VERIFY", "IN_GAME", "IDLE", "READY"}:
            _CAPTCHA_ACCOUNTS.discard(account)
        if new == "IN_GAME":
            _ACTIVE_ACCOUNTS.add(account)
        if old == "IN_GAME" and new != "IN_GAME":
            _ACTIVE_ACCOUNTS.discard(account)
        if new in {"FAILED", "IDLE"}:
            _ACTIVE_ACCOUNTS.discard(account)
        if new == "LAUNCHING":
            _QUEUE_SIZE = max(0, _QUEUE_SIZE - 1)
        if reason == "captcha_required":
            _ACTIVE_ACCOUNTS.discard(account)
    elif scope == "STATE" and name == "forced_reset":
        _ACTIVE_ACCOUNTS.discard(account)
        _CAPTCHA_ACCOUNTS.discard(account)
    elif scope == "CAPTCHA" or name == "captcha_dialog_hold" or (scope == "RECOVERY" and name == "captcha_hold"):
        if "resume" in name or "clear" in name or _reason(fields) in {"manual_resume", "captcha_resume"}:
            _CAPTCHA_ACCOUNTS.discard(account)
        else:
            _CAPTCHA_ACCOUNTS.add(account)
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
        if _reason(fields) == "captcha_required":
            return _captcha_line(account, pid, _text(fields.get("detail")))
        if new == "IN_GAME":
            return _line(_ICON_OK, f"{_username_paren(account)} {_pid_paren(pid or 'bound')}", stamp_color=_COLOR_GRAY)
        return None
    if name == "process_bind_verified" and pid and not _LUA_LIVENESS_REQUIRED:
        return _line("", f"Found Roblox process {_pid_value(pid)} for user {_username_paren(account)}", stamp_color=_COLOR_WHITE)
    return None


def _format_recovery(name: str, fields: Dict[str, Any]) -> Optional[str]:
    account = _account(fields)
    reason = _reason(fields, "recovery")
    if name == "captcha_hold" or reason == "captcha_required":
        return _captcha_line(account, _pid(fields), _text(fields.get("detail") or fields.get("captcha_detail")))
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
    if scope == "RUNTIME" and name == "suspect_process_check":
        return _suspect_process_line(account)
    if scope == "CAPTCHA" or name == "captcha_dialog_hold" or (name == "account_hold" and _reason(fields) == "captcha_required"):
        return _captcha_line(account, pid, _text(fields.get("detail") or fields.get("captcha_detail")))
    if scope == "WORKER" and name in {"visible_process_adopted", "rebind_refreshed"} and pid and not _LUA_LIVENESS_REQUIRED:
        return _line("", f"Found Roblox process {_pid_value(pid)} for user {_username_paren(account)}", stamp_color=_COLOR_WHITE)
    if scope in {"SERVER", "VIP", "VIP_TRACKER"} and name in {"selected", "server_selected", "smart_selected", "private_server_selected"}:
        return None
    if scope in {"VIP", "VIP_DETECTOR"} and name in {"server_detected", "detected", "server_status"}:
        is_vip = _boolish(fields.get("is_vip") or fields.get("is_vip_server") or fields.get("private_server"))
        server_type = _text(fields.get("server_type"), "VIP" if is_vip else "PUBLIC").upper()
        private_id = _text(fields.get("private_server_id"))
        place_id = _text(fields.get("place_id"))
        detail = "VIP server" if is_vip or server_type in {"VIP", "PRIVATE", "PRIVATE_SERVER"} else "public server"
        suffix_parts = []
        if private_id:
            suffix_parts.append(f"id: {private_id}")
        if place_id:
            suffix_parts.append(f"place: {place_id}")
        suffix = f"({', '.join(suffix_parts)})" if suffix_parts else ""
        icon = _ICON_VIP_SERVER if detail == "VIP server" else _ICON_PUBLIC_SERVER
        message = _gray_text(f"{account} {detail} detected")
        if suffix:
            message = f"{message} {_gray_text(suffix)}"
        return _line(icon, message, stamp_color=_COLOR_GRAY)
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
        if scope_text == "RUNTIME" and name_text == "suspect_process_check":
            _emit_suspect_process_check(data)
        else:
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

    match = re.match(r"^(?:\[(?:SERVER|VIP|VIP_TRACKER)\]\s+)?Smart server selected:\s*(.+)$", msg, re.IGNORECASE)
    if match:
        return None

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
