from __future__ import annotations

import time
from typing import Any

from core import AccountState

LUA_WAITING_STATUS = "waiting_for_lua"
LUA_WAITING_REASON = "lua_required"


def lua_liveness_required(cfg: dict[str, Any] | None) -> bool:
    # Lua is the internal (executor/script) confirmation path. When disabled,
    # the farm falls back to the external process-detection based verification.
    data = cfg or {}
    if "lua_enabled" in data:
        return bool(data.get("lua_enabled", True))
    return bool(data.get("use_lua", True))


def lua_wait_timeout_seconds(cfg: dict[str, Any] | None) -> float:
    data = cfg or {}
    try:
        raw = data.get(
            "lua_timeout_seconds",
            data.get("lua_wait_timeout", data.get("heartbeat_timeout", 60)),
        )
        value = 60.0 if raw in (None, "") else float(raw)
    except Exception:
        value = 60.0
    return min(300.0, max(1.0, value))


def lua_event_source(reason: str = "", payload: dict[str, Any] | None = None) -> bool:
    data = payload or {}
    reason_key = str(reason or data.get("reason_key") or "").strip().lower()
    evidence_source = str(data.get("evidence_source") or "").strip().lower()
    return (
        reason_key.startswith("lua_")
        or evidence_source == "lua_helper"
        or bool(data.get("lua_username") or data.get("lua_user_id") or data.get("lua_account"))
    )


def mark_waiting_for_lua(acc: Any, runtime_state: Any, state_mgr: Any, reason: str = "") -> None:
    reason_key = str(reason or LUA_WAITING_REASON)
    now = time.time()
    with acc._lock:
        acc.last_watchdog_classification = LUA_WAITING_STATUS
        acc.liveness_state = LUA_WAITING_STATUS
        acc.liveness_score = 0.0
        acc.liveness_suspect_since = now
        acc.last_activity_reason = LUA_WAITING_STATUS
        if runtime_state:
            runtime_state.set_cooldown(acc, 0.0, reason=LUA_WAITING_REASON)
            runtime_state.set_recovery(acc, status=LUA_WAITING_STATUS, reason=reason_key, inflight=False)
        acc.recovery_scheduled_at = 0.0
        acc.sync_runtime(LUA_WAITING_STATUS)
    if state_mgr:
        state_mgr.transition(acc, AccountState.VERIFY, reason=LUA_WAITING_STATUS, force=True)


def _float_attr(acc: Any, name: str) -> float:
    return float(getattr(acc, name, 0.0) or 0.0)


def _str_attr(acc: Any, name: str) -> str:
    return str(getattr(acc, name, "") or "")


def _event_fresh(in_game_at: float, last_event_at: float, current: float, timeout: float) -> bool:
    if in_game_at <= 0 or last_event_at <= 0:
        return False
    if current - last_event_at > max(1.0, float(timeout or 60.0)):
        return False
    return True


def _identity_matches(acc: Any, first: str, second: str) -> bool:
    first_value = _str_attr(acc, first)
    second_value = _str_attr(acc, second)
    return not (first_value and second_value and first_value != second_value)


def account_lua_online(acc: Any, now: float | None = None, timeout: float = 60.0) -> bool:
    current = float(now if now is not None else time.time())
    if not _event_fresh(
        _float_attr(acc, "lua_in_game_at"),
        _float_attr(acc, "lua_last_event_at"),
        current,
        timeout,
    ):
        return False
    if not _identity_matches(acc, "session_id", "lua_session_id"):
        return False
    if not _identity_matches(acc, "launch_nonce", "lua_launch_nonce"):
        return False
    return True
