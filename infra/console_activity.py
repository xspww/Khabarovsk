from __future__ import annotations

import os
import re
import sys
import threading
import time
from typing import Any

from desktop import console_output
from domain._util import contains_any as _matches_any


class _LivenessState:
    def __init__(self) -> None:
        self.required = False

    def set_required(self, enabled: bool) -> None:
        self.required = bool(enabled)


class _ColorState:
    def __init__(self) -> None:
        self.support: bool | None = None

    def enabled(self) -> bool:
        if not console_output.color_requested():
            return False
        if self.support is None:
            self.support = _enable_virtual_terminal()
        return bool(self.support)


class _CounterState:
    def __init__(self) -> None:
        self.total_accounts = 0
        self.queue_size = 0

    def set_total(self, count: Any) -> None:
        try:
            total = max(0, int(count or 0))
        except Exception:
            total = 0
        self.total_accounts = total

    def decrement_queue(self) -> None:
        self.queue_size = max(0, self.queue_size - 1)

    def set_queue(self, value: Any) -> None:
        try:
            self.queue_size = max(0, int(value or 0))
        except Exception:
            "intentional: ignore exception"

    def clear_queue(self) -> None:
        self.queue_size = 0


_LIVENESS = _LivenessState()
_COLORS = _ColorState()
_COUNTERS = _CounterState()

_LOCK = threading.Lock()
_CANONICAL_BY_NAME: dict[str, str] = {}
_ACTIVE_ACCOUNTS: set[str] = set()
_CAPTCHA_ACCOUNTS: set[str] = set()
_LAST_DISCONNECT_AT: dict[str, float] = {}
_LAST_CAPTCHA_AT: dict[str, float] = {}
_SUSPECT_LOGGED_ACCOUNTS: set[str] = set()
_SUSPECT_FINALIZED_AT_BY_ACCOUNT: dict[str, float] = {}
_LAST_PID_BY_ACCOUNT: dict[str, str] = {}
_SERVER_TYPE_BY_ACCOUNT: dict[str, str] = {}
_LAST_FOUND_AT_BY_KEY: dict[str, float] = {}
_LAST_TELEPORT_AT_BY_ACCOUNT: dict[str, float] = {}
_LAST_JOB_BY_ACCOUNT: dict[str, str] = {}
_LAST_PLACE_BY_ACCOUNT: dict[str, str] = {}
_BLOCKED_ACCOUNTS: set[str] = set()
_FINISHED_ACCOUNTS: set[str] = set()
_LAST_FINISHED_AT_BY_KEY: dict[str, float] = {}
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
_ICON_RAM = "🧹"
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
    _ICON_RAM: "\x1b[96m",
}
_KV_LINE_RE = re.compile(r"^\[[A-Z_]+\]\s+[a-z0-9_]+\b.*\b[a-zA-Z_][a-zA-Z0-9_]*=")


def _enabled() -> bool:
    value = os.environ.get("CRONUS_CONSOLE_ACTIVITY", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def set_lua_liveness_required(enabled: bool) -> None:
    with _LOCK:
        _LIVENESS.set_required(enabled)


def _enable_virtual_terminal() -> bool:
    return console_output.enable_virtual_terminal(sys.stdout)


def _colors_enabled() -> bool:
    return _COLORS.enabled()


def _paint(text: str, color: str = "") -> str:
    return console_output.paint(text, color, enabled=_colors_enabled())


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


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


def _account(fields: dict[str, Any]) -> str:
    for key in ("account", "username", "account_id", "user"):
        value = _text(fields.get(key))
        if value:
            return value
    return "Account"


def _account_key(account: Any) -> str:
    key = _text(account, "Account").lower()
    return _CANONICAL_BY_NAME.get(key, key)


def _remap_account_key(old: str, new: str) -> None:
    """Move state recorded under an old display name to its canonical key."""
    if old == new:
        return
    for entries in (_ACTIVE_ACCOUNTS, _CAPTCHA_ACCOUNTS, _BLOCKED_ACCOUNTS, _FINISHED_ACCOUNTS):
        if old in entries:
            entries.discard(old)
            entries.add(new)
    for mapping in (
        _LAST_DISCONNECT_AT,
        _LAST_CAPTCHA_AT,
        _LAST_PID_BY_ACCOUNT,
        _SERVER_TYPE_BY_ACCOUNT,
        _LAST_TELEPORT_AT_BY_ACCOUNT,
        _LAST_JOB_BY_ACCOUNT,
        _LAST_PLACE_BY_ACCOUNT,
    ):
        if old in mapping:
            mapping[new] = mapping.pop(old)
    prefix = f"{old}:"
    for key in [key for key in _LAST_FOUND_AT_BY_KEY if key.startswith(prefix)]:
        _LAST_FOUND_AT_BY_KEY[f"{new}:{key[len(prefix):]}"] = _LAST_FOUND_AT_BY_KEY.pop(key)


def _learn_identity(fields: dict[str, Any]) -> None:
    """Map a display name/alias to the stable config username.

    Emitters pass ``account`` (display name, which may be an alias) and
    ``account_id`` (config username). Console state must use one canonical
    key per account, otherwise an alias and a username become two entries.
    """
    account = _text(fields.get("account"))
    account_id = _text(fields.get("account_id") or fields.get("accountId") or fields.get("username"))
    if not account or not account_id:
        return
    display_key = account.lower()
    canonical = account_id.lower()
    _CANONICAL_BY_NAME[canonical] = canonical
    previous = _CANONICAL_BY_NAME.get(display_key)
    _CANONICAL_BY_NAME[display_key] = canonical
    if previous != canonical:
        # State may have been recorded under the raw alias before the map
        # existed; move only that alias key (never another account's key).
        _remap_account_key(display_key, canonical)


def _is_auth_blocked(account: Any) -> bool:
    # Product rule Q1: captcha / cookie-blocked / finished = blocked.
    # Sets store canonical keys, so one lookup covers every alias.
    key = _account_key(account)
    return key in _CAPTCHA_ACCOUNTS or key in _BLOCKED_ACCOUNTS or key in _FINISHED_ACCOUNTS


def _reason(fields: dict[str, Any], default: str = "") -> str:
    for key in ("reason", "trigger", "detail", "reject"):
        value = _text(fields.get(key))
        if value:
            return value.strip().lower().replace(" ", "_")
    return default


def _reason_text(value: Any, default: str = "") -> str:
    text = _text(value, default)
    return text.strip().lower().replace(" ", "_") if text else default


def _disconnect_reason(fields: dict[str, Any], default: str = "") -> str:
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


def _pid(fields: dict[str, Any]) -> str:
    return _int_text(
        fields.get("pid")
        or fields.get("PID")
        or fields.get("process_id")
        or fields.get("roblox_pid")
        or fields.get("matched_pid")
        or fields.get("lua_pid")
        or fields.get("bound_pid")
    )


def _job_id(fields: dict[str, Any]) -> str:
    for key in ("job_id", "observed_job_id", "new_job_id", "server_job_id", "jobId"):
        value = _text(fields.get(key))
        if value and value != "0":
            return value
    return ""


def _place_id(fields: dict[str, Any]) -> str:
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


def _server_kind(fields: dict[str, Any]) -> str:
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


def _found_pid(key: str, pid: Any) -> str:
    pid_text = _text(pid, "")
    if pid_text.lower() == "bound":
        pid_text = ""
    if not pid_text:
        pid_text = _LAST_PID_BY_ACCOUNT.get(key, "")
    if not pid_text:
        return "unknown"
    _LAST_PID_BY_ACCOUNT[key] = pid_text
    return pid_text


def _remember_found_fields(key: str, data: dict[str, Any]) -> str:
    job = _job_id(data)
    place = _place_id(data)
    if job:
        _LAST_JOB_BY_ACCOUNT[key] = job
    if place:
        _LAST_PLACE_BY_ACCOUNT[key] = place
    return _server_kind(data) or _SERVER_TYPE_BY_ACCOUNT.get(key, "")


def _found_deduped(dedupe_key: str, now: float) -> bool:
    previous = float(_LAST_FOUND_AT_BY_KEY.get(dedupe_key) or 0.0)
    if previous and now - previous < _FOUND_DEDUP_SECONDS:
        return True
    _LAST_FOUND_AT_BY_KEY[dedupe_key] = now
    if len(_LAST_FOUND_AT_BY_KEY) > 512:
        # PIDs change on every relaunch; keep the dedup cache bounded.
        cutoff = now - max(60.0, _FOUND_DEDUP_SECONDS)
        for stale_key, seen_at in list(_LAST_FOUND_AT_BY_KEY.items()):
            if seen_at < cutoff:
                _LAST_FOUND_AT_BY_KEY.pop(stale_key, None)
    return False


def _found_process_line(
    account: str, pid: Any, fields: dict[str, Any] | None = None
) -> str | None:
    account_text = _text(account, "Account")
    key = _account_key(account_text)
    # One logical "found" event reaches us through several event shapes
    # (STATE transition with pid="bound", VIP server_detected with pid+job,
    # WORKER rebind with pid). Key the dedup on account+pid only, resolving
    # placeholders through the last known pid — otherwise the same second
    # prints 2-3 identical Found lines.
    pid_text = _found_pid(key, pid)
    data = fields or {}
    kind = _remember_found_fields(key, data)
    now = time.monotonic()
    if _found_deduped(f"{key}:{pid_text}", now):
        return None
    suffix = f" In {_server_paren(kind)}" if kind else ""
    return _line(
        _ICON_OK,
        f"{_paint('Found', _COLOR_WHITE)} {_username_paren(account_text)} {_pid_paren(pid_text)}{suffix}",
        stamp_color=_COLOR_WHITE,
    )


def _reload_all_line(count: Any) -> str:
    account_count = _int_text(count, "0")
    return _line(_ICON_TELEPORT, f"Reload All Roblox ( {account_count} Accounts )", stamp_color=_COLOR_RELOAD_STAMP)


_CONFIG_KEY_HINTS = (
    "game_place_id",
    "game_mode",
    "max_concurrent_accounts",
    "fps_limit",
    "auto_minimize_enabled",
    "block_same_server_enabled",
    "roblox_window_resize_enabled",
    "lua_enabled",
)

_CONFIG_LABEL_RULES = (
    (("game",), ("game_place_id", "game_private", "block_same_server"), "Game saved"),
    (("queue",), ("max_concurrent", "queue_", "auto_close"), "Queue saved"),
    (("performance",), ("fps_limit", "fps_limiter"), "Performance saved"),
    (("window",), ("roblox_window", "auto_minimize", "window_size"), "Windows saved"),
    (("lua",), ("lua_",), "Lua saved"),
    (("config",), (), "Config updated"),
)


def _config_keys(fields: dict[str, Any]) -> str:
    updated = fields.get("updated") or fields.get("keys") or ""
    if isinstance(updated, (list, tuple)):
        return ", ".join(str(k) for k in updated[:6])
    keys = _text(updated)
    if keys:
        return keys
    hints = [key for key in _CONFIG_KEY_HINTS if key in fields]
    return ", ".join(hints[:4])


def _config_label(action: str, keys: str) -> str:
    label = _text(action or "updated", "updated")
    lower_action = label.lower()
    lower_keys = str(keys).lower()
    for action_tokens, key_tokens, result in _CONFIG_LABEL_RULES:
        if _matches_any(lower_action, action_tokens) or _matches_any(
            lower_keys, key_tokens
        ):
            return result
    # Fallback: never show raw "saved" / key soup. Default to clean header.
    clean = label.split(":")[0].strip()
    if not clean or clean.lower() in {"saved", "updated", "update"}:
        clean = "Config updated"
    # Title-case single words for product consistency.
    return clean[:1].upper() + clean[1:] if len(clean) > 1 else clean


def _config_line(action: str, fields: dict[str, Any]) -> str:
    label = _config_label(action, _config_keys(fields))
    return _line(_ICON_CONFIG, f"{_paint(label, _COLOR_WHITE)}", stamp_color=_COLOR_WHITE)


def _finished_line(account: str, finished: bool, count: Any = "") -> str:
    n = _int_text(count, "")
    suffix = f" ({n})" if n and n != "0" else ""
    if finished:
        return _line(_ICON_FINISH, f"{_paint('Finished', _COLOR_WHITE)} {_username_paren(account)}{_paint(suffix, _COLOR_GRAY)}")
    return _line(_ICON_FINISH, f"{_paint('Unfinished — relaunching', _COLOR_WHITE)} {_username_paren(account)}{_paint(suffix, _COLOR_GRAY)}")


def _reload_cookies_line(valid: Any, captcha: Any, invalid: Any, banned: Any = 0) -> str:
    v = _int_text(valid, "0")
    c = _int_text(captcha, "0")
    inv = _int_text(invalid, "0")
    b = _int_text(banned, "0")
    text = f"{v} valid, {c} CAPTCHA, {inv} invalid"
    if b != "0":
        text += f", {b} banned"
    return _line(_ICON_RELOAD, f"{_paint('Reload Cookies', _COLOR_WHITE)} {_paint(text, _COLOR_GRAY)}")


def _farm_line(started: bool, detail: str = "") -> str:
    if started:
        msg = f"{_paint('Farm started', _COLOR_WHITE)}"
    else:
        msg = f"{_paint('Farm stopped', _COLOR_WHITE)}"
    if detail:
        msg += f" {_paint(f'— {detail}', _COLOR_GRAY)}"
    return _line(_ICON_FARM, msg)


def _error_line(title: str, error: Any = "", detail: Any = "") -> str:
    reason = _text(error) or _text(detail) or "unknown error"
    if len(reason) > 160:
        reason = reason[:157] + "..."
    return _line(_ICON_FAIL, f"{_paint(title, _COLOR_WHITE)} {_paint(f'— {reason}', _COLOR_GRAY)}")


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


def _sanitize_ram_source(value: Any = "") -> str:
    src = _text(value).lower()
    if "auto" in src:
        return "auto"
    return "manual"


def _format_ram_percent(percent: Any = "") -> str:
    pct_text = _text(percent).replace("%", "").strip()
    if not pct_text or pct_text.lower() == "none":
        return ""
    try:
        return f"{float(pct_text):.1f}%"
    except Exception:
        return ""


def _ram_cleanup_line(source: Any = "", freed_mb: Any = "", percent: Any = "") -> str:
    src = _sanitize_ram_source(source)
    freed_text = _text(freed_mb)
    try:
        freed_val = float(str(freed_mb).replace(",", ""))
        freed_text = f"{freed_val:.1f} MB"
    except Exception:
        if freed_text and "mb" not in freed_text.lower():
            freed_text = f"{freed_text} MB"
    detail = f"freed {freed_text}" if freed_text else "done"
    pct = _format_ram_percent(percent)
    if pct:
        detail += f" (RAM {pct})"
    return _line(_ICON_RAM, f"{_paint(f'RAM cleaned ({src})', _COLOR_WHITE)} {_paint(f'— {detail}', _COLOR_GRAY)}")


def _ram_failed_line(source: Any = "", error: Any = "") -> str:
    src = _sanitize_ram_source(source)
    err = _text(error) or "unknown error"
    # Cyan broom icon + white header, gray reason (same palette as TELEPORT/CONFIG).
    short_err = err if len(err) <= 120 else err[:117] + "..."
    return _line(_ICON_RAM, f"{_paint(f'RAM cleanup failed ({src})', _COLOR_WHITE)} {_paint(f'— {short_err}', _COLOR_GRAY)}")


def _ram_skipped_line(percent: Any = "", reason: Any = "") -> str:
    pct = _format_ram_percent(percent)
    suffix = f" (RAM {pct})" if pct else ""
    why = _text(reason) or "requires admin — relaunch as administrator"
    return _line(_ICON_RAM, f"{_paint('RAM cleanup skipped', _COLOR_WHITE)} {_paint(f'— {why}{suffix}', _COLOR_GRAY)}")


def _ram_skip_reason(fields: dict[str, Any], name: Any = "") -> str:
    reason = _text(fields.get("reason") or "").lower()
    if "admin" in reason or _text(name) == "ram_cleanup_skipped_no_admin":
        return "requires admin — relaunch as administrator"
    if reason == "below_threshold":
        thr = _format_ram_percent(fields.get("threshold", ""))
        return f"below threshold {thr}" if thr else "below threshold"
    if reason == "cooldown":
        try:
            remaining = float(str(fields.get("cooldown_remaining") or 0))
            return f"cooldown {remaining:.0f}s"
        except Exception:
            return "cooldown"
    return _text(fields.get("reason")) or "requires admin — relaunch as administrator"


def _teleport_line(account: str) -> str | None:
    key = _account_key(account)
    now = time.monotonic()
    previous = float(_LAST_TELEPORT_AT_BY_ACCOUNT.get(key) or 0.0)
    if previous and now - previous < _TELEPORT_DEDUP_SECONDS:
        return None
    _LAST_TELEPORT_AT_BY_ACCOUNT[key] = now
    return _line(_ICON_TELEPORT, f"{_username_paren(account)} Teleporting")


def _server_moved_line(account: str, old_job: str, new_job: str, place: str = "") -> str | None:
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


def _disconnect_line(account: str, reason: str = "") -> str | None:
    now = time.monotonic()
    key = _account_key(account)
    previous = float(_LAST_DISCONNECT_AT.get(key) or 0.0)
    if now - previous < _DISCONNECT_DEDUP_SECONDS:
        return None
    _LAST_DISCONNECT_AT[key] = now
    suffix = f" ({reason})" if reason else ""
    status = _paint(f"({_text(account, 'Account')}) disconnected", _COLOR_DISCONNECTED)
    return _line(_ICON_WARN, f"{status}{suffix}", stamp_color=_COLOR_DISCONNECT_STAMP)


_LAUNCH_FAILED_REASON = "launch_fail"


def _is_launch_failure_reason(reason: str) -> bool:
    return _reason_text(reason) == _LAUNCH_FAILED_REASON


def _launch_failed_line(account: str, reason: str = "") -> str:
    """Launch failure: the account never joined, so never say 'disconnected'."""
    suffix = f" ({reason})" if reason else ""
    status = _paint(f"({_text(account, 'Account')}) launch failed", _COLOR_DISCONNECTED)
    return _line(_ICON_WARN, f"{status}{suffix}", stamp_color=_COLOR_DISCONNECT_STAMP)


def _captcha_line(account: str, pid: str = "") -> str | None:
    now = time.monotonic()
    key = _account_key(account)
    previous = float(_LAST_CAPTCHA_AT.get(key) or 0.0)
    if now - previous < _CAPTCHA_DEDUP_SECONDS:
        return None
    _LAST_CAPTCHA_AT[key] = now
    pid_text = f" {_pid_paren(pid)}" if pid else ""
    return _line(_ICON_VIP, f"{_username_paren(account)} CAPTCHA required{pid_text}")


def _print_lines(lines: list[str]) -> None:
    if lines:
        console_output.write_line("\n".join(lines))


def _emit_suspect_process_check(fields: dict[str, Any]) -> str | None:
    account = _account(fields)
    key = _account_key(account)
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
        return None
    # Q1: blocked accounts (captcha / cookie / finished) never get Checking.
    if _is_auth_blocked(account):
        return None
    finalized_at = float(_SUSPECT_FINALIZED_AT_BY_ACCOUNT.get(key) or 0.0)
    if finalized_at and time.monotonic() - finalized_at <= _SUSPECT_FINAL_SUPPRESS_SECONDS:
        return None
    if key in _SUSPECT_LOGGED_ACCOUNTS:
        return None
    _SUSPECT_FINALIZED_AT_BY_ACCOUNT.pop(key, None)
    _SUSPECT_LOGGED_ACCOUNTS.add(key)
    return _suspect_process_line(account)


def _emit_check_before_disconnect(account: str) -> str | None:
    # Q1: blocked accounts never get the pre-disconnect Checking line.
    if _is_auth_blocked(account):
        return None
    key = _account_key(account)
    if key in _SUSPECT_LOGGED_ACCOUNTS:
        return None
    _SUSPECT_FINALIZED_AT_BY_ACCOUNT.pop(key, None)
    _SUSPECT_LOGGED_ACCOUNTS.add(key)
    return _suspect_process_line(account)


def _title_text_locked() -> str:
    return f"Accounts: {_COUNTERS.total_accounts} | Active: {len(_ACTIVE_ACCOUNTS)} | Queue: {_COUNTERS.queue_size} | Captcha: {len(_CAPTCHA_ACCOUNTS)}"


def _set_title_locked() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(_title_text_locked())
    except Exception:
        "intentional: ignore exception"


def set_total_accounts(count: Any) -> None:
    with _LOCK:
        _COUNTERS.set_total(count)
        _set_title_locked()


_CAPTCHA_REASONS = {"captcha_required", "identity_locked", "account_recovery"}
_CAPTCHA_CLEAR_REASONS = {
    "manual_resume",
    "captcha_resume",
    "locked_resume",
    "recovery_resume",
    "verify_resume",
}
_RESUMED_STATES = {"QUEUED", "LAUNCHING", "VERIFY", "IN_GAME", "IDLE", "READY"}
_ACTIVE_RESUMED_STATES = {"QUEUED", "LAUNCHING", "VERIFY", "IN_GAME", "READY"}
_FINISHED_CLEAR_STATES = {"LAUNCHING", "VERIFY", "IN_GAME"}
_FINISHED_EVENT_NAMES = {"accounts_finished", "account_finished"}
_UNFINISHED_EVENT_NAMES = {
    "accounts_unfinished",
    "account_unfinished",
    "account_unfinished_relaunched",
}


def _is_captcha_scope(scope: str, name: str) -> bool:
    if scope == "CAPTCHA":
        return True
    if name == "captcha_dialog_hold":
        return True
    return scope == "RECOVERY" and name == "captcha_hold"


def _transition_captcha(
    key: str, reason: str, new: str, is_real_account: bool
) -> None:
    if reason in _CAPTCHA_REASONS:
        if is_real_account:
            _CAPTCHA_ACCOUNTS.add(key)
        return
    if reason in _CAPTCHA_CLEAR_REASONS or new in _RESUMED_STATES:
        _CAPTCHA_ACCOUNTS.discard(key)
        # Account is launchable again -> clear cookie/finished blocks too.
        if new in _ACTIVE_RESUMED_STATES:
            _BLOCKED_ACCOUNTS.discard(key)
            # Do not auto-clear FINISHED here; explicit unfinished event clears it.
            if new in _FINISHED_CLEAR_STATES:
                _FINISHED_ACCOUNTS.discard(key)


def _transition_active(old: str, new: str, key: str) -> None:
    if new in {"LAUNCHING", "VERIFY"}:
        _SERVER_TYPE_BY_ACCOUNT.pop(key, None)
    if new == "IN_GAME":
        _ACTIVE_ACCOUNTS.add(key)
    if old == "IN_GAME" and new != "IN_GAME":
        _ACTIVE_ACCOUNTS.discard(key)
    if new in {"FAILED", "IDLE"}:
        _ACTIVE_ACCOUNTS.discard(key)


def _track_transition(
    fields: dict[str, Any], key: str, is_real_account: bool
) -> None:
    old = _text(fields.get("old")).upper()
    new = _text(fields.get("new")).upper()
    reason = _reason(fields)
    _transition_captcha(key, reason, new, is_real_account)
    _transition_active(old, new, key)
    if new == "LAUNCHING":
        _COUNTERS.decrement_queue()
    if reason in _CAPTCHA_REASONS:
        _ACTIVE_ACCOUNTS.discard(key)


def _forget_account(key: str) -> None:
    _ACTIVE_ACCOUNTS.discard(key)
    _CAPTCHA_ACCOUNTS.discard(key)
    _BLOCKED_ACCOUNTS.discard(key)


def _track_state(
    name: str,
    fields: dict[str, Any],
    key: str,
    is_real_account: bool,
) -> None:
    if name == "transition":
        _track_transition(fields, key, is_real_account)
    elif name == "forced_reset":
        _forget_account(key)


def _track_captcha(
    name: str,
    fields: dict[str, Any],
    key: str,
    is_real_account: bool,
) -> None:
    if (
        "resume" in name
        or "clear" in name
        or _reason(fields) in _CAPTCHA_CLEAR_REASONS
    ):
        _CAPTCHA_ACCOUNTS.discard(key)
    elif is_real_account:
        _CAPTCHA_ACCOUNTS.add(key)
    _ACTIVE_ACCOUNTS.discard(key)


def _track_farm(name: str, key: str, is_real_account: bool) -> None:
    # Q1: cookie / mismatch / captcha preflight -> treat as blocked.
    if name != "account_preflight_blocked":
        return
    if is_real_account:
        _BLOCKED_ACCOUNTS.add(key)
    _ACTIVE_ACCOUNTS.discard(key)


def _account_parts(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _mark_finished(raw: str, account: str, key: str) -> None:
    if "," in raw:
        for part in _account_parts(raw):
            part_key = _account_key(part)
            if part != "Account":
                _FINISHED_ACCOUNTS.add(part_key)
            _ACTIVE_ACCOUNTS.discard(part_key)
    elif raw and raw != "Account":
        _FINISHED_ACCOUNTS.add(key)
        _ACTIVE_ACCOUNTS.discard(key)


def _mark_unfinished(raw: str, account: str) -> None:
    if "," in raw:
        for part in _account_parts(raw):
            part_key = _account_key(part)
            _FINISHED_ACCOUNTS.discard(part_key)
            _BLOCKED_ACCOUNTS.discard(part_key)
    else:
        raw_key = _account_key(raw or account)
        _FINISHED_ACCOUNTS.discard(raw_key)
        _BLOCKED_ACCOUNTS.discard(raw_key)


def _track_api(
    name: str, fields: dict[str, Any], account: str
) -> None:
    raw = _text(fields.get("account", "")) or account
    if name in _FINISHED_EVENT_NAMES:
        _mark_finished(raw, account, _account_key(account))
    elif name in _UNFINISHED_EVENT_NAMES:
        _mark_unfinished(raw, account)


def _track_queue(fields: dict[str, Any], name: str) -> None:
    if "size" in fields:
        _COUNTERS.set_queue(fields.get("size"))
    elif name == "cancel_all":
        _COUNTERS.clear_queue()


def _update_counters(scope: str, name: str, fields: dict[str, Any]) -> None:
    _learn_identity(fields)
    account = _account(fields)
    key = _account_key(account)
    # Never track the fallback "Account" placeholder as a real account.
    is_real_account = bool(account and account != "Account")
    if _is_captcha_scope(scope, name):
        _track_captcha(name, fields, key, is_real_account)
    elif scope == "STATE":
        _track_state(name, fields, key, is_real_account)
    elif scope == "FARM":
        _track_farm(name, key, is_real_account)
    elif scope == "API":
        _track_api(name, fields, account)
    elif scope == "QUEUE":
        _track_queue(fields, name)


def _state_in_game(
    account: str, pid: str, fields: dict[str, Any]
) -> str | None:
    if pid:
        _LAST_PID_BY_ACCOUNT[_account_key(account)] = pid
    if not _server_kind(fields) and not _SERVER_TYPE_BY_ACCOUNT.get(
        _account_key(account)
    ):
        return None
    return _found_process_line(account, pid or "bound", fields)


def _state_transition(
    account: str, pid: str, fields: dict[str, Any]
) -> str | None:
    new = _text(fields.get("new")).upper()
    reason = _reason(fields)
    if reason == "auto_close_cycle":
        return None
    if reason == "captcha_required":
        return _captcha_line(account, pid)
    if new == "IN_GAME":
        return _state_in_game(account, pid, fields)
    return None


def _format_state(name: str, fields: dict[str, Any]) -> str | None:
    account = _account(fields)
    pid = _pid(fields)
    if name == "transition":
        return _state_transition(account, pid, fields)
    if name == "process_bind_verified" and pid:
        _LAST_PID_BY_ACCOUNT[_account_key(account)] = pid
        if _LIVENESS.required:
            return None
        return _found_process_line(account, pid, fields)
    return None


def _format_recovery(name: str, fields: dict[str, Any]) -> str | None:
    account = _account(fields)
    reason = _reason(fields, "recovery")
    if name == "captcha_hold" or reason in {"captcha_required", "identity_locked", "account_recovery"}:
        return _captcha_line(account, _pid(fields))
    if name == "network_lost":
        return _disconnect_line(account, _disconnect_reason(fields, "network_lost"))
    if name == "cooldown":
        display_reason = _disconnect_reason(fields, reason)
        if _is_launch_failure_reason(display_reason):
            return _launch_failed_line(account, display_reason)
        return _disconnect_line(account, display_reason)
    return None


_UNHANDLED: Any = object()
_SELECTED_NAMES = {
    "selected",
    "server_selected",
    "smart_selected",
    "private_server_selected",
}
_RELOAD_EVENT_NAMES = {"reload_cookies", "accounts_reload", "reload_checked"}
_VIP_DETECTED_NAMES = {"server_detected", "detected", "server_status"}


def _finished_deduped(acct: str, is_finished: bool) -> bool:
    dedupe_key = f"{_account_key(acct)}:{'finished' if is_finished else 'unfinished'}"
    now = time.monotonic()
    previous = float(_LAST_FINISHED_AT_BY_KEY.get(dedupe_key) or 0.0)
    if previous and now - previous < _FINISHED_DEDUP_SECONDS:
        return True
    _LAST_FINISHED_AT_BY_KEY[dedupe_key] = now
    return False


def _finished_acct_label(raw: str) -> str:
    if "," not in raw:
        return raw or "Account"
    parts = _account_parts(raw)
    if len(parts) > 1:
        return f"{len(parts)} accounts"
    return parts[0] if parts else "Accounts"


def _finished_event_line(
    name: str, fields: dict[str, Any], account: str
) -> str | None:
    # Anything with "unfinished" in the name is an Unfinished event.
    # Q4: account_unfinished + account_unfinished_relaunched fire back-to-back
    # for the same account -> dedup to one line per account per window.
    is_finished = "unfinished" not in name.lower()
    raw = _text(fields.get("account", "")) or account
    acct = _finished_acct_label(raw)
    if _finished_deduped(acct, is_finished):
        return None
    # Single account: don't append redundant total count.
    return _finished_line(acct, is_finished, "")


def _reload_event_line(fields: dict[str, Any]) -> str | None:
    try:
        valid = fields.get("valid", fields.get("valid_count", fields.get("kept", "")))
        captcha = fields.get("captcha", "")
        invalid = fields.get("invalid", "")
        banned = fields.get("banned", "")
        if valid != "" or captcha != "" or invalid != "" or banned != "":
            return _reload_cookies_line(
                valid or 0, captcha or 0, invalid or 0, banned or 0
            )
    except Exception:
        "intentional: ignore exception"
    return None


def _farm_started_line(fields: dict[str, Any]) -> str:
    launchable = _text(fields.get("launchable", ""))
    total = _text(fields.get("accounts", ""))
    detail = f"{launchable}/{total} launchable" if launchable and total else ""
    blocked = _text(fields.get("blocked", ""))
    if blocked and blocked != "0":
        detail += f", {blocked} blocked" if detail else f"{blocked} blocked"
    return _farm_line(True, detail)


def _farm_event_line(name: str, fields: dict[str, Any]) -> str:
    try:
        if name in {"started", "start"}:
            return _farm_started_line(fields)
        return _farm_line(False, "")
    except Exception:
        return _farm_line(name in {"started", "start"}, "")


def _trim_detail(fields: dict[str, Any]) -> str:
    freed_text = _text(fields.get("freed_mb", ""))
    try:
        freed_text = f"{float(str(fields.get('freed_mb', '')).replace(',', '')):.1f} MB"
    except Exception:
        if freed_text and "mb" not in freed_text.lower():
            freed_text = f"{freed_text} MB"
    detail = f"freed {freed_text}" if freed_text else "done"
    pid_text = _text(fields.get("pid", ""))
    if pid_text:
        detail += f" (PID {pid_text})"
    return detail


def _trim_line(source: str, fields: dict[str, Any]) -> str:
    # ป้ายแยกจาก global clean: _sanitize_ram_source() จะกลืน "trim/auto"
    # เหลือแค่ "auto" ถ้าผ่าน _ram_cleanup_line ตรงๆ
    detail = _trim_detail(fields)
    return _line(
        _ICON_RAM,
        f"{_paint(f'Trimmed Roblox ({source})', _COLOR_WHITE)} {_paint(f'— {detail}', _COLOR_GRAY)}",
    )


def _trim_failed_line(source: str, fields: dict[str, Any]) -> str:
    err = fields.get("error")
    if err is None:
        err = fields.get("msg", fields.get("reason", ""))
    err_text = _text(err) or "unknown error"
    short_err = err_text if len(err_text) <= 120 else err_text[:117] + "..."
    return _line(
        _ICON_RAM,
        f"{_paint(f'Roblox trim failed ({source})', _COLOR_WHITE)} {_paint(f'— {short_err}', _COLOR_GRAY)}",
    )


def _perf_source(fields: dict[str, Any], name: str, default: str = "manual") -> str:
    source = _text(fields.get("source"))
    if source:
        return source
    return "auto" if "auto" in name else default


def _misc_config(
    scope: str, name: str, fields: dict[str, Any], account: str, pid: str
) -> str | None:
    if name in {"updated", "saved", "config_updated"}:
        return _config_line("Config updated", fields)
    if "saved" in name.lower():
        return _config_line(name.replace("_", " "), fields)
    return None


def _misc_api(
    scope: str, name: str, fields: dict[str, Any], account: str, pid: str
) -> str | None:
    if name in _FINISHED_EVENT_NAMES or name in _UNFINISHED_EVENT_NAMES:
        return _finished_event_line(name, fields, account)
    if name == "start_failed":
        return _error_line("Start failed", fields.get("error"))
    if name == "start_preflight_error":
        return _error_line("Start blocked", fields.get("error"))
    if name == "game_defaults_applied":
        return None
    if name in _RELOAD_EVENT_NAMES:
        return _reload_event_line(fields)
    return _UNHANDLED


def _misc_boot(
    scope: str, name: str, fields: dict[str, Any], account: str, pid: str
) -> str | None:
    if name == "farm_start_failed":
        return _error_line("Farm failed to start", fields.get("error"))
    return None


def _misc_account_data(
    scope: str, name: str, fields: dict[str, Any], account: str, pid: str
) -> str | None:
    if name in {"reload_cookie_validation", "reload_synced_running_farm"}:
        # Summary is emitted separately as RELOAD line; skip noisy sync logs.
        return None
    if name in _RELOAD_EVENT_NAMES:
        return _reload_event_line(fields)
    return None


def _misc_cookie(
    scope: str, name: str, fields: dict[str, Any], account: str, pid: str
) -> str | None:
    if name in _RELOAD_EVENT_NAMES:
        return _reload_event_line(fields)
    return None


def _misc_farm(
    scope: str, name: str, fields: dict[str, Any], account: str, pid: str
) -> str | None:
    if name in {"started", "stopped", "start", "stop"}:
        return _farm_event_line(name, fields)
    return _UNHANDLED


def _misc_server(
    scope: str, name: str, fields: dict[str, Any], account: str, pid: str
) -> str | None:
    if name in {"same_server_blocked", "same_server_hop"}:
        return _same_server_line(
            account,
            _text(fields.get("conflict_with", "")),
            _job_id(fields) or _text(fields.get("job_id", "")),
        )
    if name in _SELECTED_NAMES:
        return None
    return _UNHANDLED


def _misc_window(
    scope: str, name: str, fields: dict[str, Any], account: str, pid: str
) -> str | None:
    if name in {"auto_minimized", "minimized_roblox_windows"}:
        return _window_minimize_line(
            fields.get("minimized", fields.get("count", "")),
            fields.get("delay_seconds", fields.get("delay", "")),
        )
    if name in {
        "auto_window_arrange_cycle",
        "auto_window_resize_cycle",
        "resized_roblox_windows",
    }:
        return None
    return _UNHANDLED


def _misc_performance(
    scope: str, name: str, fields: dict[str, Any], account: str, pid: str
) -> str | None:
    if name in {"ram_cleanup_auto", "ram_cleanup_manual", "ram_cleanup"}:
        return _ram_cleanup_line(
            _perf_source(fields, name),
            fields.get("freed_mb", ""),
            fields.get("percent", ""),
        )
    if name in {
        "ram_cleanup_auto_failed",
        "ram_cleanup_manual_failed",
        "ram_cleanup_failed",
    }:
        err = fields.get("error")
        if err is None:
            err = fields.get("msg", "")
        return _ram_failed_line(_perf_source(fields, name), err)
    if name in {"ram_cleanup_skipped_no_admin", "ram_cleanup_skipped"}:
        return _ram_skipped_line(
            fields.get("percent", ""), _ram_skip_reason(fields, name)
        )
    if name in {"process_trim_auto", "process_trim_manual"}:
        return _trim_line(_perf_source(fields, name), fields)
    if name in {"process_trim_auto_failed", "process_trim_failed"}:
        return _trim_failed_line(_text(fields.get("source")) or "manual", fields)
    if "saved" in name.lower():
        return _config_line(name.replace("_", " "), fields)
    return _UNHANDLED


def _misc_queue(
    scope: str, name: str, fields: dict[str, Any], account: str, pid: str
) -> str | None:
    if name == "auto_close_cycle":
        return _reload_all_line(fields.get("killed"))
    if "saved" in name.lower():
        return _config_line(name.replace("_", " "), fields)
    return _UNHANDLED


def _misc_game(
    scope: str, name: str, fields: dict[str, Any], account: str, pid: str
) -> str | None:
    if "saved" in name.lower():
        return _config_line(name.replace("_", " "), fields)
    return None


def _misc_runtime(
    scope: str, name: str, fields: dict[str, Any], account: str, pid: str
) -> str | None:
    if name == "suspect_process_check":
        return _suspect_process_line(account)
    return None


def _misc_lua(
    scope: str, name: str, fields: dict[str, Any], account: str, pid: str
) -> str | None:
    if name != "teleport_detected":
        return None
    job = _job_id(fields)
    place = _place_id(fields)
    key = _account_key(account)
    if job:
        _LAST_JOB_BY_ACCOUNT[key] = job
    if place:
        _LAST_PLACE_BY_ACCOUNT[key] = place
    return _teleport_line(account)


def _misc_worker(
    scope: str, name: str, fields: dict[str, Any], account: str, pid: str
) -> str | None:
    if name not in {"visible_process_adopted", "rebind_refreshed"} or not pid:
        return _UNHANDLED
    _LAST_PID_BY_ACCOUNT[_account_key(account)] = pid
    if _LIVENESS.required:
        return None
    return _found_process_line(account, pid, fields)


def _vip_moved_line(
    account: str,
    key: str,
    pid: str,
    prev_job: str,
    job: str,
    place: str,
) -> str | None:
    _LAST_JOB_BY_ACCOUNT[key] = job
    if place:
        _LAST_PLACE_BY_ACCOUNT[key] = place
    moved = _server_moved_line(account, prev_job, job, place)
    if not moved:
        return None
    if pid:
        _LAST_PID_BY_ACCOUNT[key] = _text(
            pid, _LAST_PID_BY_ACCOUNT.get(key, "")
        )
    return moved


def _vip_job_event(
    fields: dict[str, Any], account: str, key: str, pid: str
) -> str | None:
    job = _job_id(fields)
    place = _place_id(fields)
    prev_job = _LAST_JOB_BY_ACCOUNT.get(key, "")
    if job and prev_job and job != prev_job:
        return _vip_moved_line(account, key, pid, prev_job, job, place)
    if job and not prev_job:
        _LAST_JOB_BY_ACCOUNT[key] = job
        if place:
            _LAST_PLACE_BY_ACCOUNT[key] = place
    return None


def _vip_server_event_line(
    fields: dict[str, Any], account: str, pid: str
) -> str | None:
    kind = _server_kind(fields)
    key = _account_key(account)
    if kind:
        _SERVER_TYPE_BY_ACCOUNT[key] = kind
    moved = _vip_job_event(fields, account, key, pid)
    if moved is not None:
        return moved
    last_pid = pid or _LAST_PID_BY_ACCOUNT.get(key, "")
    if kind and last_pid:
        return _found_process_line(account, last_pid, fields)
    return None


def _misc_vip(
    scope: str, name: str, fields: dict[str, Any], account: str, pid: str
) -> str | None:
    if name in _SELECTED_NAMES:
        return None
    if scope in {"VIP", "VIP_DETECTOR"} and name in _VIP_DETECTED_NAMES:
        return _vip_server_event_line(fields, account, pid)
    return _UNHANDLED


_MISC_SCOPE_HANDLERS = {
    "API": _misc_api,
    "PERFORMANCE": _misc_performance,
    "WINDOW": _misc_window,
    "FARM": _misc_farm,
    "SERVER": _misc_server,
    "VIP": _misc_vip,
    "VIP_TRACKER": _misc_vip,
    "VIP_DETECTOR": _misc_vip,
}

_MISC_FALLBACK_HANDLERS = {
    "CONFIG": _misc_config,
    "BOOT": _misc_boot,
    "ACCOUNT_DATA": _misc_account_data,
    "COOKIE": _misc_cookie,
    "QUEUE": _misc_queue,
    "GAME": _misc_game,
    "RUNTIME": _misc_runtime,
    "LUA": _misc_lua,
    "LUA_EVENT": _misc_lua,
    "WORKER": _misc_worker,
}


def _is_misc_captcha(scope: str, name: str, fields: dict[str, Any]) -> bool:
    if scope == "CAPTCHA":
        return True
    if name == "captcha_dialog_hold":
        return True
    return name == "account_hold" and _reason(fields) == "captcha_required"


def _misc_fallback(
    scope: str, name: str, fields: dict[str, Any], account: str, pid: str
) -> str | None:
    if _is_misc_captcha(scope, name, fields):
        return _captcha_line(account, pid)
    handler = _MISC_FALLBACK_HANDLERS.get(scope)
    if handler is None:
        return None
    line = handler(scope, name, fields, account, pid)
    return None if line is _UNHANDLED else line


def _format_misc(scope: str, name: str, fields: dict[str, Any]) -> str | None:
    account = _account(fields)
    pid = _pid(fields)
    handler = _MISC_SCOPE_HANDLERS.get(scope)
    if handler is not None:
        line = handler(scope, name, fields, account, pid)
        if line is not _UNHANDLED:
            return line
    return _misc_fallback(scope, name, fields, account, pid)


def _format_structured(scope: str, name: str, fields: dict[str, Any]) -> str | None:
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
    lines: list[str] = []
    with _LOCK:
        _update_counters(scope_text, name_text, data)
        if scope_text == "RUNTIME" and name_text == "suspect_process_check":
            line = _emit_suspect_process_check(data)
            if line:
                lines.append(line)
        else:
            line = _format_structured(scope_text, name_text, data)
            if line:
                if " disconnected" in line:
                    checking = _emit_check_before_disconnect(_account(data))
                    if checking:
                        lines.append(checking)
                lines.append(line)
        _set_title_locked()
    _print_lines(lines)


def _format_text(message: str) -> str | None:
    msg = message.strip()
    if not msg or _KV_LINE_RE.match(msg):
        return None
    if msg.startswith(("[EVENT]", "[RECOVERY] hold")):
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
    account = ""
    match = re.match(r"^\[WORKER\]\s+(.+?)\s+(?:disconnect dialog detected|Not Responding)\b", msg.strip())
    if match:
        account = match.group(1).strip()
    lines: list[str] = []
    with _LOCK:
        line = _format_text(msg)
        if not line:
            return
        if account and " disconnected" in line:
            checking = _emit_check_before_disconnect(account)
            if checking:
                lines.append(checking)
        lines.append(line)
        _set_title_locked()
    _print_lines(lines)
