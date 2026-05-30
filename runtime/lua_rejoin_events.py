"""Lua rejoin event handling for FarmController."""
from __future__ import annotations
import time
from typing import Any, Dict
from core import AccountState, flog_kv
from domain.runtime_signals import RuntimeSignal
from runtime.lua_event_guard import validate_lua_event_payload
from runtime.lua_identity import lua_event_requires_pid_guard, resolve_lua_account
from services.process_service import ProcessService
def _lua_has_server_job(payload: Dict[str, Any]) -> bool:
    place_id = str(payload.get("observed_place_id") or payload.get("place_id") or "").strip()
    job_id = str(payload.get("observed_job_id") or payload.get("job_id") or "").strip()
    return bool(place_id and place_id != "0" and job_id)
def handle_lua_rejoin_event(
    farm: Any,
    payload: Dict[str, Any],
    *,
    validate_payload=validate_lua_event_payload,
    resolve_account=resolve_lua_account,
    log=flog_kv,
    process_service=ProcessService,
) -> Dict[str, Any]:
    valid_payload, payload_error = validate_payload(payload)
    if not valid_payload:
        log(
            "LUA_EVENT",
            "invalid_payload",
            "warning",
            error=payload_error,
            payload_size=len(str(payload or "")),
        )
        return {"ok": False, "status_code": 400, "accepted": False, "msg": payload_error}
    event_name = str(payload.get("event") or "").strip().lower()
    if not event_name:
        return {"ok": False, "status_code": 400, "accepted": False, "msg": "Missing Lua event name"}
    try:
        resolution = resolve_account(farm._accounts, payload)
    except Exception as e:
        log("LUA_EVENT", "resolver_exception", "warning", event=event_name, error=e)
        return {
            "ok": False,
            "status_code": 500,
            "accepted": False,
            "event": event_name,
            "msg": "Account resolver failed",
        }
    identity = resolution.identity
    identity_name = identity.username or identity.account or identity.configured_account
    if not identity_name and not identity.user_id:
        return {"ok": False, "status_code": 400, "accepted": False, "msg": "Missing Lua identity"}
    if resolution.ambiguous:
        return {
            "ok": False,
            "status_code": 409,
            "accepted": False,
            "event": event_name,
            "account": identity_name,
            "candidates": list(resolution.candidates),
            "msg": "Lua identity matched multiple accounts",
        }
    if not resolution.account:
        return {
            "ok": False,
            "status_code": 404,
            "accepted": False,
            "event": event_name,
            "account": identity_name,
            "msg": "Account not found",
        }
    acc = resolution.account
    reason = str(payload.get("reason_key") or f"lua_{event_name}").strip() or f"lua_{event_name}"
    event_payload = {
        "trigger": event_name,
        "reason_key": reason,
        "detail": str(payload.get("detail") or payload.get("message") or reason),
        "popup_code": str(payload.get("error_code") or ""),
        "error_code": str(payload.get("error_code") or ""),
        "place_id": str(payload.get("place_id") or ""),
        "job_id": str(payload.get("job_id") or ""),
        "evidence_source": str(payload.get("evidence_source") or "lua_helper"),
        "visual_disconnect": str(payload.get("visual_disconnect") or "").lower() == "true",
        "lua_username": identity.username,
        "lua_user_id": identity.user_id,
        "lua_account": identity.account,
        "configured_account": identity.configured_account,
        "lua_pid": identity.pid or "",
        "matched_pid": resolution.bound_pid or "",
        "identity_match": resolution.match_reason,
    }
    server_detection: Dict[str, Any] = {}
    requires_pid_guard = lua_event_requires_pid_guard(event_name)
    if requires_pid_guard and resolution.bound_pid and identity.pid is None:
        try:
            server_detection = farm._apply_lua_server_detection(acc, payload)
        except Exception as e:
            return farm._lua_event_handler_error(acc, event_name, e)
        event_payload.update(server_detection)
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
        return {
            "ok": True,
            "accepted": False,
            "event": event_name,
            "account": acc._config_username,
            "signal": "",
            "matched_pid": resolution.bound_pid,
            "lua_pid": "",
            "msg": "Lua event ignored because PID is required for the bound Cronus process",
        }
    if identity.pid and resolution.bound_pid and not resolution.pid_match and requires_pid_guard:
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
        return {
            "ok": True,
            "accepted": False,
            "event": event_name,
            "account": acc._config_username,
            "signal": "",
            "matched_pid": resolution.bound_pid,
            "lua_pid": identity.pid,
            "msg": "Lua event ignored because PID does not match Cronus binding",
        }
    try:
        server_detection = farm._apply_lua_server_detection(acc, payload)
    except Exception as e:
        return farm._lua_event_handler_error(acc, event_name, e)
    event_payload.update(server_detection)
    if event_name in {"loaded", "in_game", "heartbeat", "teleport_state"}:
        now = time.time()
        with acc._lock:
            acc.last_activity_at = now
            acc.last_activity_reason = f"lua:{event_name}"
            acc.lua_last_event = event_name
            acc.lua_last_event_at = now
            acc.lua_session_id = acc.session_id
            acc.lua_launch_nonce = acc.launch_nonce
            acc.sync_runtime(f"lua:{event_name}")
    if event_name in {"description", "set_description", "status_note"}:
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
    if event_name in {"finished", "mark_finished"}:
        raw_description = str(payload.get("description") or "").strip()
        description_persisted = False
        if raw_description:
            description_persisted, _ = farm._set_lua_account_description(acc, raw_description)
        try:
            result = farm._runtime_orchestrator.request_verify_finished(
                acc,
                farm._state_mgr or farm._runtime_state,
                reason=reason,
            )
        except Exception as e:
            return farm._lua_event_handler_error(acc, event_name, e)
        try:
            farm.cfg_mgr.save_accounts(farm._accounts)
        except Exception as e:
            log("ACCOUNT_DATA", "lua_finished_save_failed", "warning", account=acc.display_name, error=e)
        farm._bump_status_revision()
        farm._push_event(
            "lua",
            f"Lua helper: finished - {acc.display_name}",
            account=acc,
            severity="success",
            reason=reason,
            lua_event=event_name,
            signal="verify_finished",
            accepted=True,
            killed=bool(result.get("killed")),
            description_persisted=description_persisted,
        )
        return {
            "ok": True,
            "accepted": True,
            "event": event_name,
            "account": acc._config_username,
            "matched_pid": resolution.bound_pid,
            "identity_match": resolution.match_reason,
            "signal": "verify_finished",
            "killed": bool(result.get("killed")),
            "finished_at": result.get("finished_at", 0.0),
            "description_persisted": description_persisted,
            "msg": "Account marked finished",
        }
    signal = ""
    accepted = True
    severity = "info"
    if event_name in {"loaded", "in_game"}:
        has_server_job = _lua_has_server_job(event_payload)
        if event_name == "loaded" and not has_server_job:
            signal = ""
            accepted = True
            severity = "warning" if acc.state in {AccountState.LAUNCHING, AccountState.VERIFY, AccountState.IN_GAME} else "info"
            with acc._lock:
                acc.last_watchdog_classification = "lua_loaded_waiting_server"
                acc.last_activity_reason = "lua:loaded_waiting_server"
                acc.sync_runtime("lua_loaded_waiting_server")
        elif event_name == "in_game" and not has_server_job:
            signal = ""
            accepted = False
            severity = "warning"
            with acc._lock:
                acc.last_watchdog_classification = "lua_in_game_missing_server_evidence"
                acc.last_activity_reason = "lua:in_game_missing_server_evidence"
                acc.sync_runtime("lua_in_game_missing_server_evidence")
        else:
            if identity.pid and resolution.pid_match and resolution.bound_pid:
                process_service.mark_account_process_proof(
                    acc,
                    "strong",
                    reason=f"lua_{event_name}_pid_session",
                    confidence=max(float(getattr(acc, "process_binding_confidence", 0.0) or 0.0), 100.0),
                    status="verified",
                )
            signal = RuntimeSignal.LAUNCH_SUCCESS.value
            try:
                accepted = farm._runtime_orchestrator.handle_runtime_signal(
                    acc,
                    signal,
                    reason,
                    payload={**event_payload, "count_rejoin": None},
                )
            except Exception as e:
                return farm._lua_event_handler_error(acc, event_name, e)
            if accepted and event_name == "in_game":
                with acc._lock:
                    now = time.time()
                    acc.lua_in_game_at = now
                    acc.lua_last_event = event_name
                    acc.lua_last_event_at = now
                    acc.lua_session_id = acc.session_id
                    acc.lua_launch_nonce = acc.launch_nonce
                    acc.sync_runtime("lua_in_game_confirmed")
    elif event_name in {"disconnect", "error_code"}:
        signal = RuntimeSignal.DISCONNECT_DETECTED.value
        severity = "critical"
        try:
            accepted = farm._runtime_orchestrator.handle_runtime_signal(
                acc,
                signal,
                reason,
                payload=event_payload,
            )
        except Exception as e:
            return farm._lua_event_handler_error(acc, event_name, e)
        worker = farm._workers.get(acc._config_username)
        if worker:
            worker.wake()
    elif event_name == "teleport_error":
        signal = RuntimeSignal.FAULT.value
        severity = "warning"
        try:
            accepted = farm._runtime_orchestrator.handle_runtime_signal(
                acc,
                signal,
                reason,
                payload=event_payload,
            )
        except Exception as e:
            return farm._lua_event_handler_error(acc, event_name, e)
        worker = farm._workers.get(acc._config_username)
        if worker:
            worker.wake()
    elif event_name == "rejoin_requested":
        signal = RuntimeSignal.REJOIN_REQUESTED.value
        severity = "warning"
        try:
            accepted = farm._runtime_orchestrator.handle_runtime_signal(acc, signal, reason, payload=event_payload)
        except Exception as e:
            return farm._lua_event_handler_error(acc, event_name, e)
    elif event_name in {"heartbeat", "teleport_state"}:
        accepted = True
        if event_name == "teleport_state":
            log(
                "LUA",
                "teleport_detected",
                account=acc.display_name,
                pid=resolution.bound_pid or identity.pid or "",
                teleport_state=str(payload.get("teleport_state") or payload.get("detail") or ""),
                place_id=str(payload.get("teleport_place_id") or payload.get("place_id") or ""),
                job_id=str(payload.get("job_id") or ""),
            )
    else:
        return {
            "ok": False,
            "status_code": 400,
            "accepted": False,
            "event": event_name,
            "account": identity_name,
            "msg": "Unsupported Lua event",
        }
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
