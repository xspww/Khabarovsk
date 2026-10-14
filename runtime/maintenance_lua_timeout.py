from __future__ import annotations

from typing import Any

from core import AccountState, flog_kv
from runtime.lua_liveness_policy import account_lua_online, lua_liveness_required, lua_wait_timeout_seconds
from runtime.maintenance_captcha import detect_and_hold_captcha
from services.captcha_guard import CAPTCHA_REASON
from services.process_service import ProcessManager


def emit_lua_wait_timeout_recovery(
    owner: Any,
    acc: Any,
    *,
    age: float,
    timeout: float,
    state_name: str,
    runtime_generation: Any,
    session_id: str,
    launch_nonce: str,
    transaction_id: str,
    pid: Any,
    log_event: str,
    captcha_probe: str,
) -> bool:
    # Single owner for Lua-wait timeout action. Callers preserve their own
    # age/state/log_event — only the captcha-probe + signal sequence is shared.
    if detect_and_hold_captcha(owner, acc, pid, captcha_probe):
        return True
    try:
        from runtime.maintenance_verify import detect_and_hold_verify

        if detect_and_hold_verify(owner, acc, pid, captcha_probe):
            return True
    except Exception:
        pass
    flog_kv(
        "MAINT",
        log_event,
        "warning",
        account=acc.display_name,
        age=f"{age:.1f}",
        timeout=f"{timeout:.1f}",
        pid=pid or "",
    )
    owner._runtime_signal(
        acc,
        "loading_freeze",
        "lua_wait_timeout",
        payload={
            "trigger": "lua_wait_timeout",
            "detail": f"Lua did not confirm in-game state within {timeout:.1f}s",
            "reason_msg": "Waiting For Lua timed out",
            "state": state_name,
        },
        expected_runtime_generation=runtime_generation,
        expected_session_id=session_id,
        expected_launch_nonce=launch_nonce,
        expected_transaction_id=transaction_id,
    )
    return True


def handle_in_game_lua_wait_timeout(owner: Any, acc: Any, cfg: dict[str, Any], now: float) -> bool:
    if not lua_liveness_required(cfg):
        return False

    lua_timeout = lua_wait_timeout_seconds(cfg)
    with acc._lock:
        if acc.state != AccountState.IN_GAME or acc.desired_state != AccountState.IN_GAME:
            return False
        try:
            from services.verify_guard import LOCKED_REASON as _LOCKED_R
            from services.verify_guard import RECOVERY_REASON as _REC_R

            _verify_reasons = {_LOCKED_R, _REC_R}
        except Exception:
            _verify_reasons = set()
        if acc.recovery_inflight or str(acc.recovery_status or "") in ({CAPTCHA_REASON} | _verify_reasons):
            return False
        if account_lua_online(acc, now=now, timeout=lua_timeout):
            return False

        wait_reference = float(acc.last_state_change_at or acc.in_game_since or acc.last_launch_at or now)
        missing_age = max(0.0, now - wait_reference)
        if missing_age < lua_timeout:
            return False

        pid = acc.pid
        identity = acc.bound_process_identity
        runtime_generation = acc.runtime_generation
        session_id = acc.session_id
        launch_nonce = acc.launch_nonce
        transaction_id = acc.rejoin_transaction_id

    pid_live = bool(pid and ProcessManager.is_bound_game_alive(
        pid,
        owner_key=acc._config_username,
        expected_identity=identity,
        expected_browser_tracker_id=acc.browser_tracker_id,
    ))
    if not pid_live:
        return False
    return emit_lua_wait_timeout_recovery(
        owner,
        acc,
        age=missing_age,
        timeout=lua_timeout,
        state_name=AccountState.IN_GAME.name,
        runtime_generation=runtime_generation,
        session_id=session_id,
        launch_nonce=launch_nonce,
        transaction_id=transaction_id,
        pid=pid,
        log_event="lua_missing_in_game_timeout_recovery",
        captcha_probe="lua_wait_timeout_in_game",
    )
