"""Lua rejoin event handling for FarmController."""
from __future__ import annotations

import time
from typing import Any, NamedTuple

from core import AccountState, flog_kv
from domain.states import RuntimeSignal
from runtime.lua_event_guard import validate_lua_event_payload
from runtime.lua_identity import lua_event_requires_pid_guard, resolve_lua_account
from services.process_service import ProcessService


def _lua_has_server_job(payload: dict[str, Any]) -> bool:
    place_id = str(payload.get("observed_place_id") or payload.get("place_id") or "").strip()
    job_id = str(payload.get("observed_job_id") or payload.get("job_id") or "").strip()
    return bool(place_id and place_id != "0" and job_id)


def _read_session_fields(acc: Any) -> tuple[str, str]:
    return (
        str(getattr(acc, "session_id", "") or "").strip(),
        str(getattr(acc, "launch_nonce", "") or "").strip(),
    )


def _current_session_binding(acc: Any) -> tuple[str, str]:
    lock = getattr(acc, "_lock", None)
    if lock is None:
        return _read_session_fields(acc)
    with lock:
        return _read_session_fields(acc)


def _payload_session_binding(payload: dict[str, Any]) -> tuple[str, str]:
    return (
        str(payload.get("session_id") or "").strip(),
        str(payload.get("launch_nonce") or "").strip(),
    )


def _binding_match(
    current_sid: str,
    current_nonce: str,
    payload_sid: str,
    payload_nonce: str,
) -> bool:
    return bool(
        current_sid
        and current_nonce
        and payload_sid
        and payload_nonce
        and payload_sid == current_sid
        and payload_nonce == current_nonce
    )


def _lua_session_binding_matches(acc: Any, payload: dict[str, Any]) -> bool:
    """Check payload session_id/launch_nonce against the account's live session.

    Executors without a PID API (no getprocessid/getpid/...) send pid="".
    The per-launch session_id/launch_nonce pair is an equally strong binding
    secret (random per launch, verified server-side): if it matches the
    farm's current session, the event provably comes from our own helper
    instance, not a spoofed cross-account signal.
    """
    try:
        current_sid, current_nonce = _current_session_binding(acc)
    except Exception:
        return False
    payload_sid, payload_nonce = _payload_session_binding(payload)
    return _binding_match(current_sid, current_nonce, payload_sid, payload_nonce)
class _LuaEventBase(NamedTuple):
    event_name: str = ""
    acc: Any = None
    resolution: Any = None
    identity: Any = None
    identity_name: str = ""
    error: dict[str, Any] | None = None


_ACTIVITY_EVENTS = {"loaded", "in_game", "heartbeat", "teleport_state"}
_DESCRIPTION_EVENTS = {"description", "set_description", "status_note"}
_FINISHED_EVENTS = {"finished", "mark_finished"}
_LOADED_EVENTS = {"loaded", "in_game"}
_DISCONNECT_EVENTS = {"disconnect", "error_code"}
_HEARTBEAT_EVENTS = {"heartbeat", "teleport_state"}


def _lua_error(status_code: int, msg: str, *, event: str = "", account: str = "", **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "status_code": status_code, "accepted": False}
    if event:
        result["event"] = event
    if account:
        result["account"] = account
    result.update(extra)
    result["msg"] = msg
    return result


def _lua_ignored(
    event_name: str,
    acc: Any,
    resolution: Any,
    msg: str,
    *,
    lua_pid: Any = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "ok": True,
        "accepted": False,
        "event": event_name,
        "account": acc._config_username,
        "signal": "",
        "matched_pid": resolution.bound_pid,
        "lua_pid": lua_pid,
    }
    if extra:
        result.update(extra)
    result["msg"] = msg
    return result


def _identity_name(identity: Any) -> str:
    return identity.username or identity.account or identity.configured_account


def _finished_lua_result(farm: Any, acc: Any, event_name: str, resolution: Any) -> dict[str, Any] | None:
    try:
        from domain.account_model import is_account_finished

        if is_account_finished(acc):
            farm._bump_status_revision()
            return {
                "ok": True,
                "accepted": False,
                "event": event_name,
                "account": acc._config_username,
                "matched_pid": resolution.bound_pid,
                "identity_match": resolution.match_reason,
                "signal": "finished_skipped",
                "msg": "Account is Finished; Lua signal ignored",
            }
    except Exception:
        "intentional: ignore exception"
    return None


def _payload_size(payload: Any) -> int:
    return len(str(payload or ""))


def _event_name(payload: dict[str, Any]) -> str:
    return str(payload.get("event") or "").strip().lower()


def _validate_and_resolve(
    farm: Any,
    payload: dict[str, Any],
    validate_payload: Any,
    resolve_account: Any,
    log: Any,
) -> _LuaEventBase:
    valid_payload, payload_error = validate_payload(payload)
    if not valid_payload:
        log(
            "LUA_EVENT",
            "invalid_payload",
            "warning",
            error=payload_error,
            payload_size=_payload_size(payload),
        )
        return _LuaEventBase(error=_lua_error(400, payload_error))
    event_name = _event_name(payload)
    if not event_name:
        return _LuaEventBase(error=_lua_error(400, "Missing Lua event name"))
    try:
        resolution = resolve_account(farm._accounts, payload)
    except Exception as e:
        log("LUA_EVENT", "resolver_exception", "warning", event=event_name, error=e)
        return _LuaEventBase(error=_lua_error(500, "Account resolver failed", event=event_name))
    identity = resolution.identity
    identity_name = _identity_name(identity)
    if not identity_name and not identity.user_id:
        return _LuaEventBase(error=_lua_error(400, "Missing Lua identity"))
    if resolution.ambiguous:
        return _LuaEventBase(
            error=_lua_error(
                409,
                "Lua identity matched multiple accounts",
                event=event_name,
                account=identity_name,
                candidates=list(resolution.candidates),
            )
        )
    if not resolution.account:
        return _LuaEventBase(
            error=_lua_error(404, "Account not found", event=event_name, account=identity_name)
        )
    acc = resolution.account
    finished = _finished_lua_result(farm, acc, event_name, resolution)
    if finished:
        return _LuaEventBase(error=finished)
    return _LuaEventBase(event_name, acc, resolution, identity, identity_name, None)


def _lua_reason(payload: dict[str, Any], event_name: str) -> str:
    default = f"lua_{event_name}"
    return str(payload.get("reason_key") or default).strip() or default


def _event_text(payload: dict[str, Any], key: str, default: str = "") -> str:
    return str(payload.get(key) or default)


def _lua_event_payload(
    payload: dict[str, Any],
    event_name: str,
    reason: str,
    identity: Any,
    resolution: Any,
) -> dict[str, Any]:
    return {
        "trigger": event_name,
        "reason_key": reason,
        "detail": str(payload.get("detail") or payload.get("message") or reason),
        "popup_code": _event_text(payload, "error_code"),
        "error_code": _event_text(payload, "error_code"),
        "place_id": _event_text(payload, "place_id"),
        "job_id": _event_text(payload, "job_id"),
        "evidence_source": _event_text(payload, "evidence_source", "lua_helper"),
        "visual_disconnect": _event_text(payload, "visual_disconnect").lower() == "true",
        "lua_username": identity.username,
        "lua_user_id": identity.user_id,
        "lua_account": identity.account,
        "configured_account": identity.configured_account,
        "lua_pid": identity.pid or "",
        "matched_pid": resolution.bound_pid or "",
        "identity_match": resolution.match_reason,
    }


def _apply_server_detection(
    farm: Any,
    acc: Any,
    payload: dict[str, Any],
    event_name: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    try:
        return farm._apply_lua_server_detection(acc, payload), None
    except Exception as e:
        return {}, farm._lua_event_handler_error(acc, event_name, e)


def _handle_pidless(
    farm: Any,
    acc: Any,
    payload: dict[str, Any],
    event_name: str,
    resolution: Any,
    log: Any,
) -> tuple[dict[str, Any] | None, str]:
    if _lua_session_binding_matches(acc, payload):
        log(
            "LUA",
            "pid_missing_session_bound_accepted",
            account=acc.display_name,
            lua_event=event_name,
            identity_match=resolution.match_reason,
        )
        return None, "session_bound_no_pid"
    farm._push_event(
        "lua",
        f"Lua helper ignored missing PID - {acc.display_name}",
        account=acc,
        severity="warning",
        reason="lua_pid_missing",
        lua_event=event_name,
        lua_pid="",
        matched_pid=resolution.bound_pid or "",
        identity_match=resolution.match_reason,
        accepted=False,
    )
    return (
        _lua_ignored(
            event_name,
            acc,
            resolution,
            "Lua event ignored because PID is required for the bound Cronus process",
            lua_pid="",
        ),
        "",
    )


def _apply_pidless_guard(
    farm: Any,
    acc: Any,
    payload: dict[str, Any],
    event_name: str,
    resolution: Any,
    identity: Any,
    event_payload: dict[str, Any],
    log: Any,
) -> dict[str, Any] | None:
    if not (resolution.bound_pid and identity.pid is None):
        return None
    detection, error = _apply_server_detection(farm, acc, payload, event_name)
    if error:
        return error
    event_payload.update(detection)
    ignored, pid_proof = _handle_pidless(farm, acc, payload, event_name, resolution, log)
    if pid_proof:
        event_payload["pid_proof"] = pid_proof
    return ignored


def _pid_mismatch_result(
    farm: Any,
    acc: Any,
    event_name: str,
    identity: Any,
    resolution: Any,
) -> dict[str, Any] | None:
    if not (identity.pid and resolution.bound_pid and not resolution.pid_match):
        return None
    farm._push_event(
        "lua",
        f"Lua helper ignored PID mismatch - {acc.display_name}",
        account=acc,
        severity="warning",
        reason="lua_pid_mismatch",
        lua_event=event_name,
        lua_pid=identity.pid,
        matched_pid=resolution.bound_pid or "",
        identity_match=resolution.match_reason,
        accepted=False,
    )
    return _lua_ignored(
        event_name,
        acc,
        resolution,
        "Lua event ignored because PID does not match Cronus binding",
        lua_pid=identity.pid,
    )


def _guard_lua_event(
    farm: Any,
    acc: Any,
    payload: dict[str, Any],
    event_name: str,
    resolution: Any,
    identity: Any,
    event_payload: dict[str, Any],
    requires_pid_guard: bool,
    log: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
    if requires_pid_guard:
        error = _apply_pidless_guard(
            farm, acc, payload, event_name, resolution, identity, event_payload, log
        )
        if error:
            return error, event_payload, {}
        mismatch = _pid_mismatch_result(farm, acc, event_name, identity, resolution)
        if mismatch:
            return mismatch, event_payload, {}
    detection, error = _apply_server_detection(farm, acc, payload, event_name)
    if error:
        return error, event_payload, {}
    event_payload.update(detection)
    return None, event_payload, detection


def _record_lua_activity(acc: Any, event_name: str, now: float) -> None:
    with acc._lock:
        acc.last_activity_at = now
        acc.last_activity_reason = f"lua:{event_name}"
        acc.lua_last_event = event_name
        acc.lua_last_event_at = now
        acc.lua_session_id = acc.session_id
        acc.lua_launch_nonce = acc.launch_nonce
        if event_name == "teleport_state":
            # Teleport handoff starting: old client will exit, queued new
            # client takes seconds to bind. Until arrival, the missing pid
            # is expected — not a crash (no fault rejoin) and not a
            # rotation target (no cycle kill mid-teleport).
            acc.teleport_suppress_until = now + 180.0
        acc.sync_runtime(f"lua:{event_name}")


def _handle_description_event(
    farm: Any,
    acc: Any,
    payload: dict[str, Any],
    event_name: str,
    reason: str,
    resolution: Any,
) -> dict[str, Any]:
    persisted, description = farm._set_lua_account_description(
        acc,
        str(payload.get("description") or payload.get("text") or payload.get("detail") or ""),
    )
    farm._bump_status_revision()
    farm._push_event(
        "lua",
        f"Lua helper: description - {acc.display_name}",
        account=acc,
        severity="success",
        reason=reason,
        lua_event=event_name,
        signal="description_updated",
        accepted=True,
        persisted=persisted,
    )
    return {
        "ok": True,
        "accepted": True,
        "event": event_name,
        "account": acc._config_username,
        "matched_pid": resolution.bound_pid,
        "identity_match": resolution.match_reason,
        "signal": "description_updated",
        "persisted": persisted,
        "description": description,
        "msg": "Description updated",
    }


def _handle_finished_event(
    farm: Any,
    acc: Any,
    event_name: str,
    reason: str,
    resolution: Any,
) -> dict[str, Any]:
    # The "Finished" mechanism is removed: accounts never terminate. Accept
    # the event as a no-op so the Lua helper call still succeeds, but do not
    # kill the process and do not mark the account finished.
    farm._bump_status_revision()
    farm._push_event(
        "lua",
        f"Lua helper: finished ignored - {acc.display_name}",
        account=acc,
        severity="info",
        reason=reason,
        lua_event=event_name,
        signal="finished_ignored",
        accepted=True,
    )
    return {
        "ok": True,
        "accepted": True,
        "event": event_name,
        "account": acc._config_username,
        "matched_pid": resolution.bound_pid,
        "identity_match": resolution.match_reason,
        "signal": "finished_ignored",
        "msg": "Finished events are disabled; account keeps running",
    }


def _handle_simple_lua_event(
    farm: Any,
    acc: Any,
    payload: dict[str, Any],
    event_name: str,
    reason: str,
    resolution: Any,
) -> dict[str, Any] | None:
    if event_name in _ACTIVITY_EVENTS:
        _record_lua_activity(acc, event_name, time.time())
    if event_name in _DESCRIPTION_EVENTS:
        return _handle_description_event(farm, acc, payload, event_name, reason, resolution)
    if event_name in _FINISHED_EVENTS:
        return _handle_finished_event(farm, acc, event_name, reason, resolution)
    return None


def _server_job_state(acc: Any, event_name: str, has_server_job: bool) -> tuple[str, bool, str] | None:
    if event_name == "loaded" and not has_server_job:
        severity = "warning" if acc.state in {AccountState.LAUNCHING, AccountState.VERIFY, AccountState.IN_GAME} else "info"
        with acc._lock:
            acc.last_watchdog_classification = "lua_loaded_waiting_server"
            acc.last_activity_reason = "lua:loaded_waiting_server"
            acc.sync_runtime("lua_loaded_waiting_server")
        return "", True, severity
    if event_name == "in_game" and not has_server_job:
        with acc._lock:
            acc.last_watchdog_classification = "lua_in_game_missing_server_evidence"
            acc.last_activity_reason = "lua:in_game_missing_server_evidence"
            acc.sync_runtime("lua_in_game_missing_server_evidence")
        return "", False, "warning"
    return None


def _mark_lua_process_proof(
    acc: Any,
    event_name: str,
    resolution: Any,
    process_service: Any,
) -> None:
    identity = resolution.identity
    if identity.pid and resolution.pid_match and resolution.bound_pid:
        process_service.mark_account_process_proof(
            acc,
            "strong",
            reason=f"lua_{event_name}_pid_session",
            confidence=max(float(getattr(acc, "process_binding_confidence", 0.0) or 0.0), 100.0),
            status="verified",
        )


def _handle_loaded_in_game(
    farm: Any,
    acc: Any,
    event_name: str,
    reason: str,
    event_payload: dict[str, Any],
    resolution: Any,
    process_service: Any,
) -> tuple[dict[str, Any] | None, str, bool, str]:
    has_server_job = _lua_has_server_job(event_payload)
    state = _server_job_state(acc, event_name, has_server_job)
    if state is not None:
        signal, accepted, severity = state
        return None, signal, accepted, severity
    # Arrived with server evidence — any teleport handoff is done.
    with acc._lock:
        acc.teleport_suppress_until = 0.0
    _mark_lua_process_proof(acc, event_name, resolution, process_service)
    signal = RuntimeSignal.LAUNCH_SUCCESS.value
    try:
        accepted = farm._runtime_orchestrator.handle_runtime_signal(
            acc,
            signal,
            reason,
            payload={**event_payload, "count_rejoin": None},
        )
    except Exception as e:
        return farm._lua_event_handler_error(acc, event_name, e), "", False, ""
    if accepted and event_name == "in_game":
        with acc._lock:
            now = time.time()
            acc.lua_in_game_at = now
            acc.lua_last_event = event_name
            acc.lua_last_event_at = now
            acc.lua_session_id = acc.session_id
            acc.lua_launch_nonce = acc.launch_nonce
            acc.sync_runtime("lua_in_game_confirmed")
    return None, signal, accepted, "info"


def _is_moderation_signal(code: str, low: str) -> bool:
    return code in {"267", "268", "273"} or "bann" in low or "suspend" in low or "terminat" in low


def _request_moderation_check(farm: Any, acc: Any, event_payload: dict[str, Any], reason: str) -> None:
    try:
        code = _event_text(event_payload, "error_code")
        low = f"{event_payload.get('detail', '')} {reason}".lower()
        if not _is_moderation_signal(code, low):
            return
        maint = getattr(farm, "_maintenance", None)
        prio = getattr(maint, "request_moderation_check", None) if maint is not None else None
        if callable(prio):
            try:
                prio(acc._config_username)
            except Exception:
                "intentional: ignore exception"
    except Exception:
        "intentional: ignore exception"


def _wake_worker(farm: Any, acc: Any) -> None:
    worker = farm._workers.get(acc._config_username)
    if worker:
        worker.wake()


def _dispatch_runtime_signal(
    farm: Any,
    acc: Any,
    event_name: str,
    reason: str,
    event_payload: dict[str, Any],
    signal: str,
    severity: str,
    *,
    wake: bool,
) -> tuple[dict[str, Any] | None, str, bool, str]:
    try:
        accepted = farm._runtime_orchestrator.handle_runtime_signal(
            acc,
            signal,
            reason,
            payload=event_payload,
        )
    except Exception as e:
        return farm._lua_event_handler_error(acc, event_name, e), "", False, ""
    if wake:
        _wake_worker(farm, acc)
    return None, signal, accepted, severity


def _handle_disconnect(
    farm: Any,
    acc: Any,
    event_name: str,
    reason: str,
    event_payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, str, bool, str]:
    error, signal, accepted, severity = _dispatch_runtime_signal(
        farm,
        acc,
        event_name,
        reason,
        event_payload,
        RuntimeSignal.DISCONNECT_DETECTED.value,
        "critical",
        wake=False,
    )
    if error:
        return error, signal, accepted, severity
    _request_moderation_check(farm, acc, event_payload, reason)
    _wake_worker(farm, acc)
    return None, signal, accepted, severity


def _handle_teleport_error(
    farm: Any,
    acc: Any,
    event_name: str,
    reason: str,
    event_payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, str, bool, str]:
    # Teleport failed — handoff is over, normal fault recovery may proceed.
    with acc._lock:
        acc.teleport_suppress_until = 0.0
    return _dispatch_runtime_signal(
        farm,
        acc,
        event_name,
        reason,
        event_payload,
        RuntimeSignal.FAULT.value,
        "warning",
        wake=True,
    )


def _handle_rejoin_requested(
    farm: Any,
    acc: Any,
    event_name: str,
    reason: str,
    event_payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, str, bool, str]:
    return _dispatch_runtime_signal(
        farm,
        acc,
        event_name,
        reason,
        event_payload,
        RuntimeSignal.REJOIN_REQUESTED.value,
        "warning",
        wake=False,
    )


def _log_teleport(log: Any, acc: Any, payload: dict[str, Any], resolution: Any, identity: Any) -> None:
    log(
        "LUA",
        "teleport_detected",
        account=acc.display_name,
        pid=resolution.bound_pid or identity.pid or "",
        teleport_state=str(payload.get("teleport_state") or payload.get("detail") or ""),
        place_id=str(payload.get("teleport_place_id") or payload.get("place_id") or ""),
        job_id=str(payload.get("job_id") or ""),
    )


def _route_lua_signal(
    farm: Any,
    acc: Any,
    payload: dict[str, Any],
    event_name: str,
    reason: str,
    event_payload: dict[str, Any],
    resolution: Any,
    identity: Any,
    identity_name: str,
    process_service: Any,
    log: Any,
) -> tuple[dict[str, Any] | None, str, bool, str]:
    if event_name in _LOADED_EVENTS:
        return _handle_loaded_in_game(
            farm, acc, event_name, reason, event_payload, resolution, process_service
        )
    if event_name in _DISCONNECT_EVENTS:
        return _handle_disconnect(farm, acc, event_name, reason, event_payload)
    if event_name == "teleport_error":
        return _handle_teleport_error(farm, acc, event_name, reason, event_payload)
    if event_name == "rejoin_requested":
        return _handle_rejoin_requested(farm, acc, event_name, reason, event_payload)
    if event_name in _HEARTBEAT_EVENTS:
        if event_name == "teleport_state":
            _log_teleport(log, acc, payload, resolution, identity)
        return None, "", True, "info"
    return (
        _lua_error(400, "Unsupported Lua event", event=event_name, account=identity_name),
        "",
        False,
        "",
    )


def handle_lua_rejoin_event(
    farm: Any,
    payload: dict[str, Any],
    *,
    validate_payload=validate_lua_event_payload,
    resolve_account=resolve_lua_account,
    log=flog_kv,
    process_service=ProcessService,
) -> dict[str, Any]:
    base = _validate_and_resolve(farm, payload, validate_payload, resolve_account, log)
    if base.error:
        return base.error

    event_name = base.event_name
    acc = base.acc
    resolution = base.resolution
    identity = base.identity
    reason = _lua_reason(payload, event_name)
    event_payload = _lua_event_payload(payload, event_name, reason, identity, resolution)
    requires_pid_guard = lua_event_requires_pid_guard(event_name)

    error, event_payload, server_detection = _guard_lua_event(
        farm,
        acc,
        payload,
        event_name,
        resolution,
        identity,
        event_payload,
        requires_pid_guard,
        log,
    )
    if error:
        return error

    simple = _handle_simple_lua_event(farm, acc, payload, event_name, reason, resolution)
    if simple is not None:
        return simple

    error, signal, accepted, severity = _route_lua_signal(
        farm,
        acc,
        payload,
        event_name,
        reason,
        event_payload,
        resolution,
        identity,
        base.identity_name,
        process_service,
        log,
    )
    if error:
        return error

    if event_name == "heartbeat":
        return {
            "ok": True,
            "accepted": True,
            "event": event_name,
            "account": acc._config_username,
            "matched_pid": resolution.bound_pid,
            "identity_match": resolution.match_reason,
            "signal": signal,
            "observed_server_type": server_detection.get("observed_server_type", ""),
            "observed_is_vip": bool(server_detection.get("observed_is_vip", False)),
            "msg": "Lua heartbeat accepted",
        }

    farm._push_event(
        "lua",
        f"Lua helper: {event_name} - {acc.display_name}",
        account=acc,
        severity=severity,
        reason=reason,
        lua_event=event_name,
        signal=signal,
        error_code=event_payload.get("error_code", ""),
        accepted=accepted,
    )
    return {
        "ok": True,
        "accepted": bool(accepted),
        "event": event_name,
        "account": acc._config_username,
        "matched_pid": resolution.bound_pid,
        "identity_match": resolution.match_reason,
        "signal": signal,
        "observed_server_type": server_detection.get("observed_server_type", ""),
        "observed_is_vip": bool(server_detection.get("observed_is_vip", False)),
        "msg": "Lua event accepted" if accepted else "Lua event routed but not accepted",
    }
