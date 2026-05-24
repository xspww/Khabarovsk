from __future__ import annotations

import time
from typing import Any, Dict, Optional

from core import AccountState


LUA_WAITING_STATUS = "waiting_for_lua"
LUA_WAITING_REASON = "lua_required"


def lua_liveness_required(cfg: Dict[str, Any] | None) -> bool:
    return bool((cfg or {}).get("use_lua", False))


def lua_event_source(reason: str = "", payload: Optional[Dict[str, Any]] = None) -> bool:
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


def account_lua_online(acc: Any, now: Optional[float] = None, timeout: float = 60.0) -> bool:
    current = float(now if now is not None else time.time())
    in_game_at = float(getattr(acc, "lua_in_game_at", 0.0) or 0.0)
    last_event_at = float(getattr(acc, "lua_last_event_at", 0.0) or 0.0)
    if in_game_at <= 0 or last_event_at <= 0:
        return False
    if current - last_event_at > max(1.0, float(timeout or 60.0)):
        return False
    session_id = str(getattr(acc, "session_id", "") or "")
    lua_session_id = str(getattr(acc, "lua_session_id", "") or "")
    if session_id and lua_session_id and session_id != lua_session_id:
        return False
    launch_nonce = str(getattr(acc, "launch_nonce", "") or "")
    lua_launch_nonce = str(getattr(acc, "lua_launch_nonce", "") or "")
    if launch_nonce and lua_launch_nonce and launch_nonce != lua_launch_nonce:
        return False
    return True
