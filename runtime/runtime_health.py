from __future__ import annotations

import time
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional

RUNTIME_HEALTH_EVENT_WINDOW_SECONDS = 600.0
FARM_HEALTH_STUCK_STATE_SECONDS = 180.0
CONTROL_PLANE_RESTART_THRESHOLD_SECONDS = 180.0
CONTROL_PLANE_RESTART_BACKOFF_SECONDS = 300.0
FARM_HEALTH_RECOVERY_STATES = {"CRASH", "NETWORK_LOST", "COOLDOWN", "QUEUED", "LAUNCHING", "VERIFY"}


def _event_type(event: Dict[str, Any]) -> str:
    return str(event.get("event_type") or event.get("kind") or "").lower()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _runtime_event_age(events: Iterable[Dict[str, Any]], now: float) -> float:
    ages = []
    for event in events or []:
        ts = _safe_float(event.get("ts"), 0.0)
        if ts > 0.0:
            ages.append(max(0.0, now - ts))
    return round(min(ages), 1) if ages else 0.0


def _account_state(row: Dict[str, Any]) -> str:
    return str(row.get("state") or row.get("public_state") or "").upper()


def _account_flags(row: Dict[str, Any]) -> List[str]:
    return [str(flag) for flag in (row.get("health_flags") or []) if str(flag)]


def _queue_public(queue_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    size = _safe_int(queue_snapshot.get("size", queue_snapshot.get("pending", 0)), 0)
    return {
        "size": size,
        "pending": _safe_int(queue_snapshot.get("pending", size), size),
        "busy": bool(queue_snapshot.get("busy", False)),
        "oldest_age_seconds": round(_safe_float(queue_snapshot.get("oldest_age_seconds"), 0.0), 1),
    }


def _queue_detailed(queue_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    queue = _queue_public(queue_snapshot)
    queue.update({
        "closed": bool(queue_snapshot.get("closed", False)),
        "stale_rejections": _safe_int(queue_snapshot.get("stale_rejections"), 0),
        "entries": [
            {
                "account_id": str(item.get("account") or ""),
                "reason": str(item.get("reason") or ""),
                "queue_position": _safe_int(item.get("queue_position"), 0),
                "queue_sequence": _safe_int(item.get("queue_sequence"), 0),
                "priority": _safe_int(item.get("priority"), 50),
                "score": round(_safe_float(item.get("score"), 0.0), 2),
                "ready": bool(item.get("ready", False)),
                "age_seconds": round(_safe_float(item.get("age_seconds"), 0.0), 1),
                "due_in_seconds": round(_safe_float(item.get("due_in_seconds"), 0.0), 1),
                "runtime_generation": _safe_int(item.get("runtime_generation"), 0),
                "recovery_generation": _safe_int(item.get("recovery_generation"), 0),
                "boosted": bool(item.get("boosted", False)),
            }
            for item in (queue_snapshot.get("entries") or [])
            if isinstance(item, dict)
        ],
    })
    return queue


def _is_recent_event(event: Dict[str, Any], now: float, window_seconds: float = RUNTIME_HEALTH_EVENT_WINDOW_SECONDS) -> bool:
    try:
        ts = float(event.get("ts") or 0.0)
    except Exception:
        ts = 0.0
    if ts <= 0.0:
        return True
    return 0.0 <= (now - ts) <= max(1.0, float(window_seconds or RUNTIME_HEALTH_EVENT_WINDOW_SECONDS))


def _is_relaunch_pressure_event(event: Dict[str, Any]) -> bool:
    event_type = _event_type(event)
    reason = str(event.get("reason") or "").lower()
    severity = str(event.get("severity") or "").lower()
    joined = f"{event_type} {reason}"
    normal_launch_reasons = {
        "dispatcher_launch",
        "farm_start",
        "cookie_validated",
        "launch_sent",
        "launch_success",
    }
    if reason in normal_launch_reasons and severity in {"", "info", "success"}:
        return False
    if "relaunch_loop" in joined or "force_rejoin" in joined or "manual_rejoin" in joined:
        return True
    if "rejoin" in event_type and any(
        marker in joined
        for marker in (
            "recovery",
            "network",
            "disconnect",
            "watchdog",
            "crash",
            "process_lost",
            "timeout",
            "failure",
            "failed",
        )
    ):
        return True
    if "launch" in event_type and any(
        marker in joined
        for marker in ("fail", "crash", "retry", "recovery", "backoff", "stale")
    ):
        return True
    return False


def account_health_flags(account: Dict[str, Any], now: Optional[float] = None) -> List[str]:
    current = float(now if now is not None else time.time())
    flags: List[str] = []
    if account.get("blocked_reason"):
        flags.append("blocked")
    if account.get("command_inflight"):
        flags.append("command_inflight")
    recovery_status = str(account.get("recovery_status") or "").strip().lower()
    if account.get("recovery_inflight") or (
        account.get("recovery_active") and recovery_status not in {"", "in_game"}
    ):
        flags.append("recovery_active")
    if int(account.get("cooldown_left") or 0) > 0:
        flags.append("cooldown")
    if account.get("state") == "IN_GAME" and not account.get("process_alive"):
        flags.append("running_without_live_process")
    last_heartbeat = float(account.get("last_heartbeat") or 0.0)
    if account.get("process_alive") and last_heartbeat and (current - last_heartbeat) > 120:
        flags.append("heartbeat_stale")
    if int(account.get("retry_count") or 0) >= 3:
        flags.append("high_retry_count")
    if int(account.get("fail_count") or 0) >= 3:
        flags.append("high_fail_count")
    proof_level = str(account.get("process_proof_level") or "").strip().lower()
    if account.get("state") == "IN_GAME" and account.get("process_alive") and proof_level != "strong":
        flags.append("process_binding_warning")
    if account.get("process_reject_reason") and "process_binding_warning" not in flags:
        flags.append("process_binding_warning")
    return flags


def find_stuck_farm_states(
    accounts: Iterable[Dict[str, Any]],
    now: Optional[float] = None,
    stuck_after_seconds: float = FARM_HEALTH_STUCK_STATE_SECONDS,
) -> List[Dict[str, Any]]:
    current = float(now if now is not None else time.time())
    threshold = max(1.0, float(stuck_after_seconds or FARM_HEALTH_STUCK_STATE_SECONDS))
    stuck: List[Dict[str, Any]] = []
    for row in accounts or []:
        if not isinstance(row, dict):
            continue
        state = _account_state(row)
        flags = _account_flags(row)
        transition_at = _safe_float(row.get("last_transition_at"), 0.0)
        age = max(0.0, current - transition_at) if transition_at else 0.0
        reason = ""
        if state in {"LAUNCHING", "VERIFY", "QUEUED"} and age >= threshold:
            reason = f"{state.lower()}_stuck"
        elif "heartbeat_stale" in flags:
            reason = "heartbeat_stale"
        elif "running_without_live_process" in flags:
            reason = "running_without_live_process"
        elif "process_binding_warning" in flags:
            reason = "process_binding_warning"
        if not reason:
            continue
        stuck.append({
            "account_id": str(row.get("account_id") or ""),
            "state": state,
            "age_seconds": round(age, 1),
            "reason": reason,
            "last_transition_reason": str(row.get("last_transition_reason") or ""),
            "health_flags": flags,
        })
    return stuck


def _ambiguous_process_count(accounts: Iterable[Dict[str, Any]]) -> int:
    count = 0
    for row in accounts or []:
        if not isinstance(row, dict):
            continue
        flags = set(_account_flags(row))
        reject_reason = str(row.get("process_reject_reason") or "").strip()
        if "process_binding_warning" in flags or reject_reason:
            count += 1
    return count


def _watchdog_task_health(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    raw = dict(snapshot.get("watchdog_task") or {})
    if not raw:
        return {
            "status": "unknown",
            "task_installed": False,
            "project_root_matches": False,
            "watchdog_script_exists": False,
            "message": "Watchdog task status has not been inspected.",
        }
    if raw.get("_inspection_error"):
        return {
            "status": "unknown",
            "task_installed": False,
            "project_root_matches": False,
            "watchdog_script_exists": False,
            "message": str(raw.get("_inspection_error") or "watchdog inspection failed")[:300],
        }

    task_installed = bool(raw.get("TaskInstalled", raw.get("task_installed", False)))
    project_root_matches = bool(raw.get("ProjectRootMatches", raw.get("project_root_matches", False)))
    script_exists = bool(raw.get("WatchdogScriptExists", raw.get("watchdog_script_exists", False)))
    if not task_installed:
        status = "missing"
    elif not script_exists:
        status = "missing_script"
    elif not project_root_matches:
        status = "stale"
    else:
        status = "installed"
    return {
        "status": status,
        "task_installed": task_installed,
        "task_state": str(raw.get("TaskState") or raw.get("task_state") or ""),
        "project_root_matches": project_root_matches,
        "watchdog_script_exists": script_exists,
        "expected_project_root": str(raw.get("ExpectedProjectRoot") or raw.get("expected_project_root") or ""),
        "task_working_directory": str(raw.get("TaskWorkingDirectory") or raw.get("task_working_directory") or ""),
        "last_task_result": raw.get("LastTaskResult", raw.get("last_task_result")),
        "backend_healthy": bool(raw.get("BackendHealthy", raw.get("backend_healthy", False))),
    }


def _release_gate_health(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    raw = dict(snapshot.get("release_gate") or {})
    if not raw:
        return {
            "last_result": "not_run",
            "last_run_at": 0.0,
            "fail_count": 0,
            "warn_count": 0,
        }
    if "last_result" in raw:
        result = str(raw.get("last_result") or "not_run")
    else:
        result = "pass" if bool(raw.get("ok", False)) else "fail"
    return {
        "last_result": result,
        "last_run_at": _safe_float(raw.get("last_run_at", raw.get("generated_at", 0.0)), 0.0),
        "fail_count": _safe_int(raw.get("fail_count"), 0),
        "warn_count": _safe_int(raw.get("warn_count"), 0),
    }


def build_public_farm_health(snapshot: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    current = float(now if now is not None else time.time())
    accounts = [row for row in (snapshot.get("accounts") or []) if isinstance(row, dict)]
    runtime_health = dict(snapshot.get("runtime_health") or {})
    queue_snapshot = dict(snapshot.get("queue_snapshot") or {})
    control_plane = dict(snapshot.get("control_plane") or {})
    warnings = [str(item) for item in (runtime_health.get("warnings") or []) if str(item)]
    if control_plane.get("stuck_reasons"):
        warnings.append("control_plane_stuck")
    running = bool(snapshot.get("running", False))
    if not running:
        warnings.append("farm_stopped")
    warnings = list(dict.fromkeys(warnings))

    states = Counter(_account_state(row) for row in accounts)
    blocked_count = sum(1 for row in accounts if row.get("blocked_reason") or "blocked" in _account_flags(row))
    failed_count = sum(1 for row in accounts if _account_state(row) == "FAILED" or row.get("blocked_reason") or "blocked" in _account_flags(row))
    recovering_count = sum(
        1
        for row in accounts
        if row.get("recovery_active") or row.get("recovery_inflight") or _account_state(row) in FARM_HEALTH_RECOVERY_STATES
    )
    status_updated_at = _safe_float(snapshot.get("status_updated_at"), 0.0)
    last_event_age = snapshot.get("last_runtime_event_age_seconds")
    if last_event_age is None:
        last_event_age = _runtime_event_age(snapshot.get("recent_runtime_events") or [], current)
    state = "stopped" if not running else ("degraded" if warnings else "ok")
    return {
        "ok": state == "ok",
        "state": state,
        "running": running,
        "account_count": len(accounts),
        "in_game": states.get("IN_GAME", 0),
        "launching": states.get("LAUNCHING", 0) + states.get("VERIFY", 0),
        "queued": states.get("QUEUED", 0),
        "recovering": recovering_count,
        "blocked": blocked_count,
        "failed": failed_count,
        "cooldown": states.get("COOLDOWN", 0),
        "bound_process_count": sum(1 for row in accounts if bool(row.get("pid_bound"))),
        "queue": _queue_public(queue_snapshot),
        "warnings": warnings,
        "last_runtime_event_age_seconds": round(_safe_float(last_event_age), 1),
        "status_revision": _safe_int(snapshot.get("status_revision"), 0),
        "status_age_seconds": round(max(0.0, current - status_updated_at), 1) if status_updated_at else 0.0,
        "checked_at": current,
    }


def build_control_plane_health(snapshot: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    current = float(now if now is not None else time.time())
    running = bool(snapshot.get("running", False))
    scheduler = dict(snapshot.get("scheduler_health") or {})
    dispatcher = dict(snapshot.get("dispatcher") or {})
    maintenance = dict(snapshot.get("maintenance") or {})
    reasons: List[str] = []
    ages: List[float] = []
    if running:
        if dispatcher.get("configured") and not dispatcher.get("alive"):
            reasons.append("dispatcher_dead")
            ages.append(_safe_float(dispatcher.get("dead_for_seconds"), 0.0))
        if maintenance.get("configured") and not maintenance.get("alive"):
            reasons.append("maintenance_dead")
            ages.append(_safe_float(maintenance.get("dead_for_seconds"), 0.0))
        if scheduler:
            scheduler_age = max(
                _safe_float(scheduler.get("last_loop_age_seconds"), 0.0),
                _safe_float(scheduler.get("max_overdue_seconds"), 0.0),
                _safe_float(scheduler.get("last_dispatch_age_seconds"), 0.0),
            )
            if scheduler.get("closed") and not scheduler.get("external_stop"):
                reasons.append("scheduler_closed")
                ages.append(scheduler_age)
            if not scheduler.get("thread_alive", True) and not scheduler.get("external_stop"):
                reasons.append("scheduler_dead")
                ages.append(scheduler_age)
            if _safe_int(scheduler.get("overdue_count"), 0) > 0 and _safe_float(scheduler.get("max_overdue_seconds"), 0.0) >= 30.0:
                reasons.append("scheduler_overdue")
                ages.append(_safe_float(scheduler.get("max_overdue_seconds"), 0.0))
    reasons = list(dict.fromkeys(reasons))
    return {
        "ok": not reasons,
        "stuck_reasons": reasons,
        "max_stuck_age_seconds": round(max(ages), 1) if ages else 0.0,
        "last_restart_at": _safe_float(snapshot.get("last_control_plane_restart_at"), 0.0),
        "checked_at": current,
    }


def decide_farm_watchdog_action(
    health: Dict[str, Any],
    now: Optional[float] = None,
    control_plane_restart_threshold_seconds: float = CONTROL_PLANE_RESTART_THRESHOLD_SECONDS,
    control_plane_restart_backoff_seconds: float = CONTROL_PLANE_RESTART_BACKOFF_SECONDS,
) -> Dict[str, Any]:
    current = float(now if now is not None else time.time())
    if not bool(health.get("running", True)):
        return {
            "action": "none",
            "scope": "farm",
            "reason": "farm_stopped",
            "checked_at": current,
        }

    control_plane = dict(health.get("control_plane") or {})
    control_reasons = [str(item) for item in (control_plane.get("stuck_reasons") or []) if str(item)]
    control_age = _safe_float(control_plane.get("max_stuck_age_seconds"), 0.0)
    threshold = max(1.0, float(control_plane_restart_threshold_seconds or CONTROL_PLANE_RESTART_THRESHOLD_SECONDS))
    backoff = max(1.0, float(control_plane_restart_backoff_seconds or CONTROL_PLANE_RESTART_BACKOFF_SECONDS))
    if control_reasons and control_age >= threshold:
        last_restart_at = _safe_float(control_plane.get("last_restart_at"), 0.0)
        since_restart = max(0.0, current - last_restart_at) if last_restart_at else backoff
        if last_restart_at and since_restart < backoff:
            return {
                "action": "log_degraded",
                "scope": "control_plane",
                "reasons": control_reasons,
                "backoff_active": True,
                "next_restart_after_seconds": round(max(0.0, backoff - since_restart), 1),
                "checked_at": current,
            }
        return {
            "action": "restart_control_plane",
            "scope": "control_plane",
            "reasons": control_reasons,
            "stuck_for_seconds": round(control_age, 1),
            "backoff_active": False,
            "checked_at": current,
        }

    stuck_states = [row for row in (health.get("stuck_states") or []) if isinstance(row, dict)]
    account_issue_rows = [
        row for row in (health.get("accounts") or [])
        if isinstance(row, dict)
        and (
            _account_state(row) in {"CRASH", "NETWORK_LOST", "COOLDOWN"}
            or any(
                flag in _account_flags(row)
                for flag in ("heartbeat_stale", "running_without_live_process", "process_binding_warning", "high_retry_count", "high_fail_count")
            )
        )
    ]
    if stuck_states or account_issue_rows:
        return {
            "action": "targeted_recovery",
            "scope": "account",
            "account_count": len(stuck_states) or len(account_issue_rows),
            "reasons": list(dict.fromkeys(
                [str(row.get("reason") or "") for row in stuck_states if row.get("reason")]
                + [flag for row in account_issue_rows for flag in _account_flags(row)]
            )),
            "checked_at": current,
        }

    runtime_health = dict(health.get("runtime_health") or {})
    warnings = [str(item) for item in (runtime_health.get("warnings") or health.get("warnings") or []) if str(item)]
    if control_reasons or warnings:
        return {
            "action": "log_degraded",
            "scope": "farm",
            "reasons": list(dict.fromkeys(control_reasons + warnings)),
            "backoff_active": False,
            "checked_at": current,
        }
    return {
        "action": "none",
        "scope": "farm",
        "reason": "healthy",
        "checked_at": current,
    }


def build_detailed_farm_health(snapshot: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    current = float(now if now is not None else time.time())
    control_plane = build_control_plane_health(snapshot, now=current)
    detail_snapshot = dict(snapshot)
    detail_snapshot["control_plane"] = control_plane
    stuck_states = find_stuck_farm_states(
        detail_snapshot.get("accounts") or [],
        now=current,
        stuck_after_seconds=_safe_float(
            detail_snapshot.get("stuck_state_threshold_seconds"),
            FARM_HEALTH_STUCK_STATE_SECONDS,
        ),
    )
    detail_snapshot["stuck_states"] = stuck_states
    public = build_public_farm_health(detail_snapshot, now=current)
    accounts = []
    for row in detail_snapshot.get("accounts") or []:
        if not isinstance(row, dict):
            continue
        accounts.append({
            "account_id": str(row.get("account_id") or ""),
            "state": _account_state(row),
            "desired_state": str(row.get("desired_state") or ""),
            "recovery_status": str(row.get("recovery_status") or ""),
            "recovery_reason": str(row.get("recovery_reason") or ""),
            "recovery_active": bool(row.get("recovery_active") or row.get("recovery_inflight")),
            "cooldown_left": _safe_int(row.get("cooldown_left"), 0),
            "retry_count": _safe_int(row.get("retry_count"), 0),
            "fail_count": _safe_int(row.get("fail_count"), 0),
            "pid_bound": bool(row.get("pid_bound")),
            "process_proof_level": str(row.get("process_proof_level") or ""),
            "process_binding_status": str(row.get("process_binding_status") or ""),
            "process_reject_reason": str(row.get("process_reject_reason") or ""),
            "liveness_state": str(row.get("liveness_state") or ""),
            "watchdog_classification": str(row.get("watchdog_classification") or ""),
            "last_transition_age_seconds": round(max(0.0, current - _safe_float(row.get("last_transition_at"), current)), 1),
            "health_flags": _account_flags(row),
        })
    detail = {
        **public,
        "runtime": {
            "last_event_age_seconds": public["last_runtime_event_age_seconds"],
            "stuck_accounts": len(stuck_states),
            "ambiguous_process_count": _ambiguous_process_count(accounts),
        },
        "watchdog_task": _watchdog_task_health(detail_snapshot),
        "release_gate": _release_gate_health(detail_snapshot),
        "runtime_health": dict(detail_snapshot.get("runtime_health") or {}),
        "workers": dict(detail_snapshot.get("workers") or {}),
        "dispatcher": dict(detail_snapshot.get("dispatcher") or {}),
        "maintenance": dict(detail_snapshot.get("maintenance") or {}),
        "scheduler": dict(detail_snapshot.get("scheduler_health") or {}),
        "queue": _queue_detailed(dict(detail_snapshot.get("queue_snapshot") or {})),
        "recovery_storm": dict(detail_snapshot.get("recovery_storm") or {}),
        "stuck_states": stuck_states,
        "control_plane": control_plane,
        "accounts": accounts,
    }
    detail["watchdog_decision"] = decide_farm_watchdog_action(detail, now=current)
    return detail


def build_runtime_health(
    accounts: Iterable[Dict[str, Any]],
    queue_snapshot: Dict[str, Any],
    recent_events: Iterable[Dict[str, Any]],
    scheduler_snapshot: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    current = float(now if now is not None else time.time())
    account_rows = list(accounts or [])
    event_rows = list(recent_events or [])
    pressure_events = [item for item in event_rows if _is_recent_event(item, current)]
    event_counts = Counter(_event_type(item) for item in pressure_events)
    stale_work_count = sum(count for event, count in event_counts.items() if "stale_work_rejected" in event)
    invariant_count = sum(count for event, count in event_counts.items() if "invariant" in event)
    recovery_count = sum(count for event, count in event_counts.items() if "recovery" in event)
    relaunch_count = sum(1 for event in pressure_events if _is_relaunch_pressure_event(event))

    heartbeat_ages = [
        current - float(row.get("last_heartbeat") or 0.0)
        for row in account_rows
        if row.get("process_alive") and float(row.get("last_heartbeat") or 0.0) > 0.0
    ]
    queue_depth = int(queue_snapshot.get("size") or queue_snapshot.get("pending") or 0)
    warnings: List[str] = []
    if stale_work_count >= 3:
        warnings.append("stale_work_pressure")
    if invariant_count:
        warnings.append("runtime_invariant_violations")
    if recovery_count >= max(3, len(account_rows)):
        warnings.append("recovery_pressure")
    if relaunch_count >= max(3, len(account_rows)):
        warnings.append("relaunch_pressure")
    if queue_depth >= max(5, len(account_rows)):
        warnings.append("queue_pressure")
    if heartbeat_ages and max(heartbeat_ages) > 180:
        warnings.append("heartbeat_stale")
    scheduler = dict(scheduler_snapshot or {})
    scheduler_latency = max(
        float(scheduler.get("last_dispatch_latency_seconds") or 0.0),
        float(scheduler.get("max_overdue_seconds") or 0.0),
    )
    if scheduler.get("callback_failure_count"):
        warnings.append("scheduler_callback_failures")
    if int(scheduler.get("overdue_count") or 0) > 0 and scheduler_latency >= 10.0:
        warnings.append("scheduler_overdue")
    if scheduler.get("closed") and not scheduler.get("external_stop"):
        warnings.append("scheduler_closed")

    return {
        "ok": not warnings,
        "warnings": warnings,
        "watchdog_latency_seconds": round(scheduler_latency, 1),
        "max_heartbeat_age_seconds": round(max(heartbeat_ages), 1) if heartbeat_ages else 0,
        "scheduler": scheduler,
        "queue_pressure": {
            "size": queue_depth,
            "busy": bool(queue_snapshot.get("busy", False)),
            "oldest_age_seconds": round(float(queue_snapshot.get("oldest_age_seconds") or 0.0), 1),
        },
        "stale_work_count": stale_work_count,
        "invariant_violation_count": invariant_count,
        "recovery_event_count": recovery_count,
        "relaunch_event_count": relaunch_count,
        "account_count": len(account_rows),
        "checked_at": current,
    }
