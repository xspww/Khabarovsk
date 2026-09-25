from __future__ import annotations

import os
import re
import sys
import threading
import time
from typing import Optional, Any, Dict
from desktop import console_output

_LOCK = threading.Lock()
_ACTIVE_ACCOUNTS: set[str] = set()
_CAPTCHA_ACCOUNTS: set[str] = set()
_TOTAL_ACCOUNTS = 0
_QUEUE_SIZE = 0
_LAST_DISCONNECT_AT: Dict[str, float] = {}
_LAST_CAPTCHA_AT: Dict[str, float] = {}
_SUSPECT_LOGGED_ACCOUNTS: set[str] = set()
_SUSPECT_FINALIZED_AT_BY_ACCOUNT: Dict[str, float] = {}
_LAST_PID_BY_ACCOUNT: Dict[str, str] = {}
_SERVER_TYPE_BY_ACCOUNT: Dict[str, str] = {}
_LAST_FOUND_AT_BY_KEY: Dict[str, float] = {}
_LAST_TELEPORT_AT_BY_ACCOUNT: Dict[str, float] = {}
_LAST_JOB_BY_ACCOUNT: Dict[str, str] = {}
_LAST_PLACE_BY_ACCOUNT: Dict[str, str] = {}
_BLOCKED_ACCOUNTS: set[str] = set()
_FINISHED_ACCOUNTS: set[str] = set()
_LAST_FINISHED_AT_BY_KEY: Dict[str, float] = {}
_LUA_LIVENESS_REQUIRED = False
_DISCONNECT_DEDUP_SECONDS = 3.0
_CAPTCHA_DEDUP_SECONDS = 3.0
_FOUND_DEDUP_SECONDS = 3.0
_TELEPORT_DEDUP_SECONDS = 3.0
_FINISHED_DEDUP_SECONDS = 3.0
_SUSPECT_FINAL_SUPPRESS_SECONDS = 5.0

_ICON_OK = "✔"
_ICON_WARN = "⚠️"
_ICON_FAIL = "❌"
_ICON_VIP = "🔐"
_ICON_VIP_SERVER = "👑"
_ICON_PUBLIC_SERVER = "🦺"
_ICON_CHECKING = "🚧"
_ICON_TELEPORT = "🌀"
_ICON_CONFIG = "⚙️"
_ICON_RELOAD = "🔄"
_ICON_FINISH = "🏁"
_ICON_FARM = "🚀"
_ICON_WINDOW = "💻"
_ICON_SERVER = "🌐"
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

_COLOR_DIM = "\x1b[90m"
_COLOR_WHITE = "\x1b[97m"
_COLOR_GOLD = "\x1b[38;2;255;215;0m"
_COLOR_PUBLIC_SERVER = "\x1b[38;2;121;85;72m"
_COLOR_RELOAD_STAMP = "\x1b[38;2;135;206;235m"
_COLOR_GRAY = "\x1b[38;2;128;128;128m"
_COLOR_USERNAME = _COLOR_GRAY
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
    _ICON_TELEPORT: "\x1b[96m",
    _ICON_CONFIG: "\x1b[96m",
    _ICON_RELOAD: "\x1b[96m",
    _ICON_FINISH: "\x1b[92m",
    _ICON_FARM: "\x1b[92m",
    _ICON_WINDOW: "\x1b[90m",
    _ICON_SERVER: "\x1b[93m",
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
    return console_output.enable_virtual_terminal(sys.stdout)


def _colors_enabled() -> bool:
    global _COLOR_SUPPORT
    if not console_output.color_requested():
        return False
    if _COLOR_SUPPORT is None:
        _COLOR_SUPPORT = _enable_virtual_terminal()
    return bool(_COLOR_SUPPORT)


def _paint(text: str, color: str = "") -> str:
    return console_output.paint(text, color, enabled=_colors_enabled())


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


def _account_key(account: Any) -> str:
    return _text(account, "Account").lower()


def _is_auth_blocked(account: Any) -> bool:
    # Product rule Q1: captcha / cookie-blocked / finished = blocked.
    # All sets store display names; compare case-insensitive.
    key = _account_key(account)
    for blocked_set in (_CAPTCHA_ACCOUNTS, _BLOCKED_ACCOUNTS, _FINISHED_ACCOUNTS):
        for entry in blocked_set:
            if str(entry or "").lower() == key:
                return True
    return False


def _reason(fields: Dict[str, Any], default: str = "") -> str:
    for key in ("reason", "trigger", "detail", "reject"):
        value = _text(fields.get(key))
        if value:
            return value.strip().lower().replace(" ", "_")
    return default


def _reason_text(value: Any, default: str = "") -> str:
    text = _text(value, default)
    return text.strip().lower().replace(" ", "_") if text else default


def _disconnect_reason(fields: Dict[str, Any], default: str = "") -> str:
    detail = " ".join(
        _text(fields.get(key)).lower()
        for key in ("detail", "reason_msg", "message")
        if _text(fields.get(key))
    )
    if "waiting for lua" in detail or "lua did not confirm" in detail:
        return "lua_wait_timeout"
    for key in ("display_reason", "actual_reason", "root_reason", "reason_key", "trigger", "watchdog_reason", "cooldown_reason", "reason"):
        reason = _reason_text(fields.get(key))
        if reason:
            return reason
    return _reason_text(default)


def _pid(fields: Dict[str, Any]) -> str:
    return _int_text(
        fields.get("pid")
        or fields.get("PID")
        or fields.get("process_id")
        or fields.get("roblox_pid")
        or fields.get("matched_pid")
        or fields.get("lua_pid")
        or fields.get("bound_pid")
    )


def _job_id(fields: Dict[str, Any]) -> str:
    for key in ("job_id", "observed_job_id", "new_job_id", "server_job_id", "jobId"):
        value = _text(fields.get(key))
        if value and value != "0":
            return value
    return ""


def _place_id(fields: Dict[str, Any]) -> str:
    for key in ("place_id", "observed_place_id", "teleport_place_id", "placeId"):
        value = _text(fields.get(key))
        if value and value != "0":
            return value
    return ""


def _short_job(value: Any) -> str:
    text = _text(value)
    return text[:8] if len(text) > 8 else text


def _pid_paren(value: Any, default: str = "unknown") -> str:
    return _paint(f"(PID: {_text(value, default)})", _COLOR_GRAY)


def _username_paren(value: Any, *, color: str = _COLOR_USERNAME) -> str:
    return _paint(f"({_text(value, 'Account')})", color)


def _server_kind(fields: Dict[str, Any]) -> str:
    server_type = _text(fields.get("server_type") or fields.get("observed_server_type")).upper()
    if _boolish(fields.get("is_vip") or fields.get("is_vip_server") or fields.get("private_server")):
        return "VIP"
    if server_type in {"VIP", "PRIVATE", "PRIVATE_SERVER"}:
        return "VIP"
    if server_type in {"PUBLIC", "PUBLIC_SERVER"}:
        return "PUBLIC"
    return ""


def _server_paren(kind: str) -> str:
    if kind == "VIP":
        return _paint("(Private server)", _COLOR_GOLD)
    if kind == "PUBLIC":
        return _paint("(Public  server)", _COLOR_PUBLIC_SERVER)
    return ""


def _found_process_line(account: str, pid: Any, fields: Dict[str, Any] | None = None) -> Optional[str]:
    account_text = _text(account, "Account")
    key = _account_key(account_text)
    # One logical "found" event reaches us through several event shapes
    # (STATE transition with pid="bound", VIP server_detected with pid+job,
    # WORKER rebind with pid). Key the dedup on account+pid only, resolving
    # placeholders through the last known pid — otherwise the same second
    # prints 2-3 identical Found lines.
    pid_text = _text(pid, "")
    if pid_text.lower() == "bound":
        pid_text = ""
    if not pid_text:
        pid_text = _LAST_PID_BY_ACCOUNT.get(key, "")
    if not pid_text:
        pid_text = "unknown"
    else:
        _LAST_PID_BY_ACCOUNT[key] = pid_text
    data = fields or {}
    job = _job_id(data)
    place = _place_id(data)
    if job:
        _LAST_JOB_BY_ACCOUNT[key] = job
    if place:
        _LAST_PLACE_BY_ACCOUNT[key] = place
    kind = _server_kind(data) or _SERVER_TYPE_BY_ACCOUNT.get(key, "")
    dedupe_key = f"{key}:{pid_text}"
    now = time.monotonic()
    previous = float(_LAST_FOUND_AT_BY_KEY.get(dedupe_key) or 0.0)
    if previous and now - previous < _FOUND_DEDUP_SECONDS:
        return None
    _LAST_FOUND_AT_BY_KEY[dedupe_key] = now
    suffix = f" In {_server_paren(kind)}" if kind else ""
    return _line(
        _ICON_OK,
        f"{_paint('Found', _COLOR_WHITE)} {_username_paren(account_text)} {_pid_paren(pid_text)}{suffix}",
        stamp_color=_COLOR_WHITE,
    )


def _reload_all_line(count: Any) -> str:
    account_count = _int_text(count, "0")
    return _line(_ICON_TELEPORT, f"Reload All Roblox ( {account_count} Accounts )", stamp_color=_COLOR_RELOAD_STAMP)


def _config_line(action: str, fields: Dict[str, Any]) -> str:
    # Product style: header only, no key details.
    # e.g. "Game saved" / "Windows saved" / "Queue saved"
    updated = fields.get("updated") or fields.get("keys") or ""
    if isinstance(updated, (list, tuple)):
        keys = ", ".join(str(k) for k in updated[:6])
    else:
        keys = _text(updated)
        if not keys:
            hints = []
            for k in ("game_place_id", "game_mode", "max_concurrent_accounts", "fps_limit",
                      "auto_minimize_enabled", "block_same_server_enabled",
                      "roblox_window_resize_enabled", "lua_enabled"):
                if k in fields:
                    hints.append(k)
            keys = ", ".join(hints[:4])
    label = _text(action or "updated", "updated")
    lower_action = label.lower()
    lower_keys = str(keys).lower()
    if "game" in lower_action or "game_place_id" in lower_keys or "game_private" in lower_keys or "block_same_server" in lower_keys:
        label = "Game saved"
    elif "queue" in lower_action or "max_concurrent" in lower_keys or "queue_" in lower_keys or "auto_close" in lower_keys:
        label = "Queue saved"
    elif "performance" in lower_action or "fps_limit" in lower_keys or "fps_limiter" in lower_keys:
        label = "Performance saved"
    elif "window" in lower_action or "roblox_window" in lower_keys or "auto_minimize" in lower_keys or "window_size" in lower_keys:
        label = "Windows saved"
    elif "lua" in lower_action or "lua_" in lower_keys:
        label = "Lua saved"
    elif "config" in lower_action:
        label = "Config updated"
    else:
        # Fallback: never show raw "saved" / key soup. Default to clean header.
        clean = label.split(":")[0].strip()
        if not clean or clean.lower() in {"saved", "updated", "update"}:
            clean = "Config updated"
        # Title-case single words for product consistency.
        label = clean[:1].upper() + clean[1:] if len(clean) > 1 else clean
    return _line(_ICON_CONFIG, f"{_paint(label, _COLOR_WHITE)}", stamp_color=_COLOR_WHITE)


def _finished_line(account: str, finished: bool, count: Any = "") -> str:
    n = _int_text(count, "")
    suffix = f" ({n})" if n and n != "0" else ""
    if finished:
        return _line(_ICON_FINISH, f"{_paint('Finished', _COLOR_WHITE)} {_username_paren(account)}{_paint(suffix, _COLOR_GRAY)}")
    return _line(_ICON_FINISH, f"{_paint('Unfinished — relaunching', _COLOR_WHITE)} {_username_paren(account)}{_paint(suffix, _COLOR_GRAY)}")


def _reload_cookies_line(valid: Any, captcha: Any, invalid: Any) -> str:
    v = _int_text(valid, "0")
    c = _int_text(captcha, "0")
    inv = _int_text(invalid, "0")
    return _line(_ICON_RELOAD, f"{_paint('Reload Cookies', _COLOR_WHITE)} {_paint(f'{v} valid, {c} CAPTCHA, {inv} invalid', _COLOR_GRAY)}")


def _farm_line(started: bool, detail: str = "") -> str:
    if started:
        msg = f"{_paint('Farm started', _COLOR_WHITE)}"
    else:
        msg = f"{_paint('Farm stopped', _COLOR_WHITE)}"
    if detail:
        msg += f" {_paint(f'— {detail}', _COLOR_GRAY)}"
    return _line(_ICON_FARM, msg)


def _same_server_line(account: str, conflict: str = "", job: str = "") -> str:
    short = _short_job(job)
    if conflict and short:
        return _line(_ICON_SERVER, f"{_username_paren(account)} {_paint(f'same server as ({conflict}) Job {short} — hopping', _COLOR_DISCONNECT_STAMP)}")
    if conflict:
        return _line(_ICON_SERVER, f"{_username_paren(account)} {_paint(f'same server as ({conflict}) — hopping', _COLOR_DISCONNECT_STAMP)}")
    return _line(_ICON_SERVER, f"{_username_paren(account)} {_paint('same server — hopping', _COLOR_DISCONNECT_STAMP)}")


def _window_minimize_line(minimized: Any, delay: Any = "") -> str:
    n = _int_text(minimized, "0")
    d = _text(delay)
    suffix = f" after {d}s" if d else ""
    return _line(_ICON_WINDOW, f"{_paint(f'Minimized {n} Roblox window(s){suffix}', _COLOR_GRAY)}")


def _teleport_line(account: str) -> Optional[str]:
    key = _account_key(account)
    now = time.monotonic()
    previous = float(_LAST_TELEPORT_AT_BY_ACCOUNT.get(key) or 0.0)
    if previous and now - previous < _TELEPORT_DEDUP_SECONDS:
        return None
    _LAST_TELEPORT_AT_BY_ACCOUNT[key] = now
    return _line(_ICON_TELEPORT, f"{_username_paren(account)} Teleporting")


def _server_moved_line(account: str, old_job: str, new_job: str, place: str = "") -> Optional[str]:
    key = _account_key(account)
    now = time.monotonic()
    previous = float(_LAST_TELEPORT_AT_BY_ACCOUNT.get(key) or 0.0)
    if previous and now - previous < _TELEPORT_DEDUP_SECONDS:
        return None
    _LAST_TELEPORT_AT_BY_ACCOUNT[key] = now
    old_short = _short_job(old_job)
    new_short = _short_job(new_job)
    if old_short and new_short:
        detail = f"Moved server {old_short} -> {new_short}"
    elif new_short:
        detail = f"Moved server -> {new_short}"
    else:
        detail = "Moved server"
    if place:
        detail = f"{detail} (place {place})"
    return _line(_ICON_TELEPORT, f"{_username_paren(account)} {detail}")


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
    return f"{stamp} {_ICON_CHECKING} {_username_paren(account)} Checking Roblox process"


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


def _disconnect_line(account: str, reason: str = "") -> Optional[str]:
    now = time.monotonic()
    key = _text(account, "Account").lower()
    previous = float(_LAST_DISCONNECT_AT.get(key) or 0.0)
    if now - previous < _DISCONNECT_DEDUP_SECONDS:
        return None
    _LAST_DISCONNECT_AT[key] = now
    suffix = f" ({reason})" if reason else ""
    status = _paint(f"({_text(account, 'Account')}) disconnected", _COLOR_DISCONNECTED)
    return _line(_ICON_WARN, f"{status}{suffix}", stamp_color=_COLOR_DISCONNECT_STAMP)


def _captcha_line(account: str, pid: str = "") -> Optional[str]:
    now = time.monotonic()
    key = _text(account, "Account").lower()
    previous = float(_LAST_CAPTCHA_AT.get(key) or 0.0)
    if now - previous < _CAPTCHA_DEDUP_SECONDS:
        return None
    _LAST_CAPTCHA_AT[key] = now
    pid_text = f" {_pid_paren(pid)}" if pid else ""
    return _line(_ICON_VIP, f"{_username_paren(account)} CAPTCHA required{pid_text}")


def _print_line(line: str) -> None:
    console_output.write_line(line)


def _emit_suspect_process_check(fields: Dict[str, Any]) -> None:
    account = _account(fields)
    key = account.lower()
    final = _boolish(fields.get("final"), False)
    # Final must always clear, even for blocked accounts, otherwise a stale
    # logged entry would suppress the next legit Checking after unblock.
    # Only set the 5s suppress window if we actually showed a Checking
    # before — otherwise an unblock right after would be delayed for no reason.
    if final:
        had_logged = key in _SUSPECT_LOGGED_ACCOUNTS
        _SUSPECT_LOGGED_ACCOUNTS.discard(key)
        if had_logged:
            _SUSPECT_FINALIZED_AT_BY_ACCOUNT[key] = time.monotonic()
        else:
            _SUSPECT_FINALIZED_AT_BY_ACCOUNT.pop(key, None)
        return
    # Q1: blocked accounts (captcha / cookie / finished) never get Checking.
    if _is_auth_blocked(account):
        return
    finalized_at = float(_SUSPECT_FINALIZED_AT_BY_ACCOUNT.get(key) or 0.0)
    if finalized_at and time.monotonic() - finalized_at <= _SUSPECT_FINAL_SUPPRESS_SECONDS:
        return
    if key in _SUSPECT_LOGGED_ACCOUNTS:
        return
    _SUSPECT_FINALIZED_AT_BY_ACCOUNT.pop(key, None)
    _SUSPECT_LOGGED_ACCOUNTS.add(key)
    _print_line(_suspect_process_line(account))


def _emit_check_before_disconnect(account: str) -> None:
    # Q1: blocked accounts never get the pre-disconnect Checking line.
    if _is_auth_blocked(account):
        return
    key = _account_key(account)
    if key in _SUSPECT_LOGGED_ACCOUNTS:
        return
    _SUSPECT_FINALIZED_AT_BY_ACCOUNT.pop(key, None)
    _SUSPECT_LOGGED_ACCOUNTS.add(key)
    _print_line(_suspect_process_line(account))


def _title_text_locked() -> str:
    return f"Accounts: {_TOTAL_ACCOUNTS} | Active: {len(_ACTIVE_ACCOUNTS)} | Queue: {_QUEUE_SIZE} | Captcha: {len(_CAPTCHA_ACCOUNTS)}"


def _set_title_locked() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(_title_text_locked())
    except Exception:
        pass


def set_total_accounts(count: Any) -> None:
    global _TOTAL_ACCOUNTS
    try:
        total = max(0, int(count or 0))
    except Exception:
        total = 0
    with _LOCK:
        _TOTAL_ACCOUNTS = total
        _set_title_locked()


def _update_counters(scope: str, name: str, fields: Dict[str, Any]) -> None:
    global _QUEUE_SIZE
    account = _account(fields)
    # Never track the fallback "Account" placeholder as a real account.
    is_real_account = bool(account and account != "Account")
    if scope == "STATE" and name == "transition":
        old = _text(fields.get("old")).upper()
        new = _text(fields.get("new")).upper()
        reason = _reason(fields)
        if reason == "captcha_required":
            if is_real_account:
                _CAPTCHA_ACCOUNTS.add(account)
        elif reason in {"manual_resume", "captcha_resume"} or new in {"QUEUED", "LAUNCHING", "VERIFY", "IN_GAME", "IDLE", "READY"}:
            _CAPTCHA_ACCOUNTS.discard(account)
            # Account is launchable again -> clear cookie/finished blocks too.
            if new in {"QUEUED", "LAUNCHING", "VERIFY", "IN_GAME", "READY"}:
                _BLOCKED_ACCOUNTS.discard(account)
                # Do not auto-clear FINISHED here; explicit unfinished event clears it.
                if new in {"LAUNCHING", "VERIFY", "IN_GAME"}:
                    _FINISHED_ACCOUNTS.discard(account)
        if new in {"LAUNCHING", "VERIFY"}:
            _SERVER_TYPE_BY_ACCOUNT.pop(_account_key(account), None)
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
        _BLOCKED_ACCOUNTS.discard(account)
    elif scope == "CAPTCHA" or name == "captcha_dialog_hold" or (scope == "RECOVERY" and name == "captcha_hold"):
        if "resume" in name or "clear" in name or _reason(fields) in {"manual_resume", "captcha_resume"}:
            _CAPTCHA_ACCOUNTS.discard(account)
        elif is_real_account:
            _CAPTCHA_ACCOUNTS.add(account)
        _ACTIVE_ACCOUNTS.discard(account)
    elif scope == "FARM" and name == "account_preflight_blocked":
        # Q1: cookie / mismatch / captcha preflight -> treat as blocked.
        if is_real_account:
            _BLOCKED_ACCOUNTS.add(account)
        _ACTIVE_ACCOUNTS.discard(account)
    elif scope == "API" and name in {"accounts_finished", "account_finished"}:
        raw = _text(fields.get("account", "")) or account
        if "," in raw:
            for part in [p.strip() for p in raw.split(",") if p.strip()]:
                if part != "Account":
                    _FINISHED_ACCOUNTS.add(part)
                _ACTIVE_ACCOUNTS.discard(part)
        elif raw and raw != "Account":
            _FINISHED_ACCOUNTS.add(raw)
            _ACTIVE_ACCOUNTS.discard(raw)
    elif scope == "API" and name in {"accounts_unfinished", "account_unfinished", "account_unfinished_relaunched"}:
        raw = _text(fields.get("account", "")) or account
        if "," in raw:
            for part in [p.strip() for p in raw.split(",") if p.strip()]:
                _FINISHED_ACCOUNTS.discard(part)
                _BLOCKED_ACCOUNTS.discard(part)
        else:
            _FINISHED_ACCOUNTS.discard(raw or account)
            _BLOCKED_ACCOUNTS.discard(raw or account)
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
        if _reason(fields) == "auto_close_cycle":
            return None
        if _reason(fields) == "captcha_required":
            return _captcha_line(account, pid)
        if new == "IN_GAME":
            if pid:
                _LAST_PID_BY_ACCOUNT[_account_key(account)] = pid
            if _server_kind(fields) or _SERVER_TYPE_BY_ACCOUNT.get(_account_key(account)):
                return _found_process_line(account, pid or "bound", fields)
            return None
        return None
    if name == "process_bind_verified" and pid:
        _LAST_PID_BY_ACCOUNT[_account_key(account)] = pid
        if not _LUA_LIVENESS_REQUIRED:
            return _found_process_line(account, pid, fields)
        return None
    return None


def _format_recovery(name: str, fields: Dict[str, Any]) -> Optional[str]:
    account = _account(fields)
    reason = _reason(fields, "recovery")
    if name == "captcha_hold" or reason == "captcha_required":
        return _captcha_line(account, _pid(fields))
    if name == "network_lost":
        return _disconnect_line(account, _disconnect_reason(fields, "network_lost"))
    if name == "cooldown":
        display_reason = _disconnect_reason(fields, reason)
        return _disconnect_line(account, display_reason)
    return None


def _format_misc(scope: str, name: str, fields: Dict[str, Any]) -> Optional[str]:
    account = _account(fields)
    pid = _pid(fields)
    # ── Product actions: config / farm / finished / reload ──
    if scope == "CONFIG" and name in {"updated", "saved", "config_updated"}:
        return _config_line("Config updated", fields)
    if scope == "API" and name in {"accounts_finished", "accounts_unfinished", "account_finished", "account_unfinished", "account_unfinished_relaunched"}:
        # Anything with "unfinished" in the name is an Unfinished event.
        # Q4: account_unfinished + account_unfinished_relaunched fire back-to-back
        # for the same account -> dedup to one line per account per window.
        is_finished = "unfinished" not in name.lower()
        acct = _text(fields.get("account", "")) or account
        # account field may be comma-joined list — show compact + dedup too.
        if "," in acct:
            parts = [p.strip() for p in acct.split(",") if p.strip()]
            acct = f"{len(parts)} accounts" if len(parts) > 1 else (parts[0] if parts else "Accounts")
            dedupe_key = f"{acct.lower()}:{'finished' if is_finished else 'unfinished'}"
            now = time.monotonic()
            previous = float(_LAST_FINISHED_AT_BY_KEY.get(dedupe_key) or 0.0)
            if previous and now - previous < _FINISHED_DEDUP_SECONDS:
                return None
            _LAST_FINISHED_AT_BY_KEY[dedupe_key] = now
            return _finished_line(acct, is_finished, "")
        acct = acct or "Account"
        dedupe_key = f"{acct.lower()}:{'finished' if is_finished else 'unfinished'}"
        now = time.monotonic()
        previous = float(_LAST_FINISHED_AT_BY_KEY.get(dedupe_key) or 0.0)
        if previous and now - previous < _FINISHED_DEDUP_SECONDS:
            return None
        _LAST_FINISHED_AT_BY_KEY[dedupe_key] = now
        # Single account: don't append redundant total count.
        return _finished_line(acct, is_finished, "")
    if scope == "API" and name in {"start_preflight_error", "start_failed", "game_defaults_applied"}:
        return None
    if scope == "ACCOUNT_DATA" and name in {"reload_cookie_validation", "reload_synced_running_farm"}:
        # Summary is emitted separately as RELOAD line; skip noisy sync logs.
        return None
    if scope in {"COOKIE", "ACCOUNT_DATA", "API"} and name in {"reload_cookies", "accounts_reload", "reload_checked"}:
        try:
            valid = fields.get("valid", fields.get("valid_count", fields.get("kept", "")))
            captcha = fields.get("captcha", "")
            invalid = fields.get("invalid", "")
            if valid != "" or captcha != "" or invalid != "":
                return _reload_cookies_line(valid or 0, captcha or 0, invalid or 0)
        except Exception:
            pass
        return None
    if scope == "FARM" and name in {"started", "stopped", "start", "stop"}:
        try:
            if name in {"started", "start"}:
                launchable = _text(fields.get("launchable", ""))
                total = _text(fields.get("accounts", ""))
                detail = f"{launchable}/{total} launchable" if launchable and total else ""
                blocked = _text(fields.get("blocked", ""))
                if blocked and blocked != "0":
                    detail += f", {blocked} blocked" if detail else f"{blocked} blocked"
                return _farm_line(True, detail)
            return _farm_line(False, "")
        except Exception:
            return _farm_line(name in {"started", "start"}, "")
    if scope == "SERVER" and name in {"same_server_blocked", "same_server_hop"}:
        return _same_server_line(account, _text(fields.get("conflict_with", "")), _job_id(fields) or _text(fields.get("job_id", "")))
    if scope == "WINDOW" and name in {"auto_minimized", "minimized_roblox_windows"}:
        return _window_minimize_line(fields.get("minimized", fields.get("count", "")), fields.get("delay_seconds", fields.get("delay", "")))
    if scope == "WINDOW" and name in {"auto_window_arrange_cycle", "auto_window_resize_cycle", "resized_roblox_windows"}:
        return None
    if scope in {"CONFIG", "PERFORMANCE", "QUEUE", "GAME"} and "saved" in name.lower():
        return _config_line(name.replace("_", " "), fields)
    if scope == "RUNTIME" and name == "suspect_process_check":
        return _suspect_process_line(account)
    if scope in {"LUA", "LUA_EVENT"} and name == "teleport_detected":
        job = _job_id(fields)
        place = _place_id(fields)
        key = _account_key(account)
        if job:
            _LAST_JOB_BY_ACCOUNT[key] = job
        if place:
            _LAST_PLACE_BY_ACCOUNT[key] = place
        return _teleport_line(account)
    if scope == "QUEUE" and name == "auto_close_cycle":
        return _reload_all_line(fields.get("killed"))
    if scope == "CAPTCHA" or name == "captcha_dialog_hold" or (name == "account_hold" and _reason(fields) == "captcha_required"):
        return _captcha_line(account, pid)
    if scope == "WORKER" and name in {"visible_process_adopted", "rebind_refreshed"} and pid:
        _LAST_PID_BY_ACCOUNT[_account_key(account)] = pid
        if not _LUA_LIVENESS_REQUIRED:
            return _found_process_line(account, pid, fields)
        return None
    if scope in {"SERVER", "VIP", "VIP_TRACKER"} and name in {"selected", "server_selected", "smart_selected", "private_server_selected"}:
        return None
    if scope in {"VIP", "VIP_DETECTOR"} and name in {"server_detected", "detected", "server_status"}:
        kind = _server_kind(fields)
        key = _account_key(account)
        if kind:
            _SERVER_TYPE_BY_ACCOUNT[key] = kind
        job = _job_id(fields)
        place = _place_id(fields)
        prev_job = _LAST_JOB_BY_ACCOUNT.get(key, "")
        if job and prev_job and job != prev_job:
            _LAST_JOB_BY_ACCOUNT[key] = job
            if place:
                _LAST_PLACE_BY_ACCOUNT[key] = place
            moved = _server_moved_line(account, prev_job, job, place)
            if moved:
                if pid:
                    _LAST_PID_BY_ACCOUNT[key] = _text(pid, _LAST_PID_BY_ACCOUNT.get(key, ""))
                return moved
        elif job and not prev_job:
            _LAST_JOB_BY_ACCOUNT[key] = job
            if place:
                _LAST_PLACE_BY_ACCOUNT[key] = place
        last_pid = pid or _LAST_PID_BY_ACCOUNT.get(key, "")
        if kind and last_pid:
            return _found_process_line(account, last_pid, fields)
        return None
    return None


def _format_structured(scope: str, name: str, fields: Dict[str, Any]) -> Optional[str]:
    if scope == "STATUS":
        return None
    if scope == "STATE":
        return _format_state(name, fields)
    if scope == "RECOVERY":
        return _format_recovery(name, fields)
    return _format_misc(scope, name, fields)


def emit_structured(scope: str, name: str, **fields: Any) -> None:
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
            line = _format_structured(scope_text, name_text, data)
            if line:
                if " disconnected" in line:
                    _emit_check_before_disconnect(_account(data))
                _print_line(line)
        _set_title_locked()


def _format_text(message: str) -> Optional[str]:
    msg = message.strip()
    if not msg or _KV_LINE_RE.match(msg):
        return None
    if msg.startswith("[EVENT]") or msg.startswith("[RECOVERY] hold"):
        return None

    match = re.match(r"^\[WORKER\]\s+(.+?)\s+disconnect dialog detected - will recover in\s+([0-9.]+)s\b", msg)
    if match:
        return _disconnect_line(match.group(1).strip(), "disconnect_dialog")

    match = re.match(r"^\[WORKER\]\s+(.+?)\s+Not Responding\b", msg)
    if match:
        return _disconnect_line(match.group(1).strip(), "not_responding")

    match = re.match(r"^(?:\[(?:SERVER|VIP|VIP_TRACKER)\]\s+)?Smart server selected:\s*(.+)$", msg, re.IGNORECASE)
    if match:
        return None

    return None


def emit_text(message: str) -> None:
    if not _enabled():
        return
    msg = str(message or "")
    line = _format_text(msg)
    if not line:
        return
    account = ""
    match = re.match(r"^\[WORKER\]\s+(.+?)\s+(?:disconnect dialog detected|Not Responding)\b", msg.strip())
    if match:
        account = match.group(1).strip()
    with _LOCK:
        if account and " disconnected" in line:
            _emit_check_before_disconnect(account)
        _print_line(line)
        _set_title_locked()
