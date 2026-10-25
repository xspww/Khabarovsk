from __future__ import annotations

import time
from collections import Counter
from collections.abc import Iterable
from typing import Any

from domain._util import contains_any as _has_marker
from domain._util import event_type as _event_type

RUNTIME_HEALTH_EVENT_WINDOW_SECONDS = 600.0
FARM_HEALTH_STUCK_STATE_SECONDS = 180.0
CONTROL_PLANE_RESTART_THRESHOLD_SECONDS = 180.0
CONTROL_PLANE_RESTART_BACKOFF_SECONDS = 300.0
FARM_HEALTH_RECOVERY_STATES = {"CRASH", "NETWORK_LOST", "COOLDOWN", "QUEUED", "LAUNCHING", "VERIFY"}

_NORMAL_LAUNCH_REASONS = frozenset(
    {
        "dispatcher_launch",
        "farm_start",
        "cookie_validated",
        "launch_sent",
        "launch_success",
    }
)
_RELAUNCH_MARKERS = ("relaunch_loop", "force_rejoin", "manual_rejoin")
_REJOIN_FAILURE_MARKERS = (
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
_LAUNCH_FAILURE_MARKERS = ("fail", "crash", "retry", "recovery", "backoff", "stale")
_WATCHDOG_ISSUE_STATES = frozenset({"CRASH", "NETWORK_LOST", "COOLDOWN"})
_WATCHDOG_ISSUE_FLAGS = (
    "heartbeat_stale",
    "running_without_live_process",
    "process_binding_warning",
    "high_retry_count",
    "high_fail_count",
)


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


def _runtime_event_age(events: Iterable[dict[str, Any]], now: float) -> float:
    ages = []
    for event in events or []:
        ts = _safe_float(event.get("ts"), 0.0)
        if ts > 0.0:
            ages.append(max(0.0, now - ts))
    return round(min(ages), 1) if ages else 0.0


def _account_state(row: dict[str, Any]) -> str:
    return str(row.get("state") or row.get("public_state") or "").upper()


def _account_flags(row: dict[str, Any]) -> list[str]:
    return [str(flag) for flag in (row.get("health_flags") or []) if str(flag)]


def _queue_public(queue_snapshot: dict[str, Any]) -> dict[str, Any]:
    size = _safe_int(queue_snapshot.get("size", queue_snapshot.get("pending", 0)), 0)
    return {
        "size": size,
        "pending": _safe_int(queue_snapshot.get("pending", size), size),
        "busy": bool(queue_snapshot.get("busy", False)),
        "oldest_age_seconds": round(_safe_float(queue_snapshot.get("oldest_age_seconds"), 0.0), 1),
    }


def _queue_detailed(queue_snapshot: dict[str, Any]) -> dict[str, Any]:
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


def _is_recent_event(event: dict[str, Any], now: float, window_seconds: float = RUNTIME_HEALTH_EVENT_WINDOW_SECONDS) -> bool:
    try:
        ts = float(event.get("ts") or 0.0)
    except Exception:
        ts = 0.0
    if ts <= 0.0:
        return True
    return 0.0 <= (now - ts) <= max(1.0, float(window_seconds or RUNTIME_HEALTH_EVENT_WINDOW_SECONDS))


def _is_relaunch_pressure_event(event: dict[str, Any]) -> bool:
    event_type = _event_type(event)
    reason = str(event.get("reason") or "").lower()
    severity = str(event.get("severity") or "").lower()
    joined = f"{event_type} {reason}"
    if reason in _NORMAL_LAUNCH_REASONS and severity in {"", "info", "success"}:
        return False
    if _has_marker(joined, _RELAUNCH_MARKERS):
        return True
    if "rejoin" in event_type and _has_marker(joined, _REJOIN_FAILURE_MARKERS):
        return True
    if "launch" in event_type and _has_marker(joined, _LAUNCH_FAILURE_MARKERS):
        return True
    return False


def _recovery_active(account: dict[str, Any]) -> bool:
    recovery_status = str(account.get("recovery_status") or "").strip().lower()
    return bool(
        account.get("recovery_inflight")
        or (account.get("recovery_active") and recovery_status not in {"", "in_game"})
    )


def _heartbeat_stale(account: dict[str, Any], current: float) -> bool:
    last_heartbeat = float(account.get("last_heartbeat") or 0.0)
    return bool(
        account.get("process_alive")
        and last_heartbeat
        and (current - last_heartbeat) > 120
    )


def _process_binding_mismatch(account: dict[str, Any]) -> bool:
    if account.get("state") != "IN_GAME":
        return False
    if not account.get("process_alive"):
        return False
    proof_level = str(account.get("process_proof_level") or "").strip().lower()
    return proof_level != "strong"


def _process_reject_reason(account: dict[str, Any]) -> str:
    return str(account.get("process_reject_reason") or "").strip()


def account_health_flags(account: dict[str, Any], now: float | None = None) -> list[str]:
    current = float(now if now is not None else time.time())
    checks = (
        ("blocked", bool(account.get("blocked_reason"))),
        ("command_inflight", bool(account.get("command_inflight"))),
        ("recovery_active", _recovery_active(account)),
        ("cooldown", int(account.get("cooldown_left") or 0) > 0),
        (
            "running_without_live_process",
            account.get("state") == "IN_GAME" and not account.get("process_alive"),
        ),
        ("heartbeat_stale", _heartbeat_stale(account, current)),
        ("high_retry_count", int(account.get("retry_count") or 0) >= 3),
        ("high_fail_count", int(account.get("fail_count") or 0) >= 3),
        ("process_binding_warning", _process_binding_mismatch(account)),
    )
    flags = [flag for flag, active in checks if active]
    if _process_reject_reason(account) and "process_binding_warning" not in flags:
        flags.append("process_binding_warning")
    return flags


def _stuck_reason(
    state: str, age: float, flags: list[str], threshold: float
) -> str:
    if state in {"LAUNCHING", "VERIFY", "QUEUED"} and age >= threshold:
        return f"{state.lower()}_stuck"
    if "heartbeat_stale" in flags:
        return "heartbeat_stale"
    if "running_without_live_process" in flags:
        return "running_without_live_process"
    if "process_binding_warning" in flags:
        return "process_binding_warning"
    return ""


def _stuck_row(
    row: dict[str, Any],
    state: str,
    age: float,
    reason: str,
    flags: list[str],
) -> dict[str, Any]:
    return {
        "account_id": str(row.get("account_id") or ""),
        "state": state,
        "age_seconds": round(age, 1),
        "reason": reason,
        "last_transition_reason": str(row.get("last_transition_reason") or ""),
        "health_flags": flags,
    }


def _stuck_entry(
    row: dict[str, Any], current: float, threshold: float
) -> dict[str, Any] | None:
    state = _account_state(row)
    flags = _account_flags(row)
    transition_at = _safe_float(row.get("last_transition_at"), 0.0)
    age = max(0.0, current - transition_at) if transition_at else 0.0
    reason = _stuck_reason(state, age, flags, threshold)
    if not reason:
        return None
    return _stuck_row(row, state, age, reason, flags)


def find_stuck_farm_states(
    accounts: Iterable[dict[str, Any]],
    now: float | None = None,
    stuck_after_seconds: float = FARM_HEALTH_STUCK_STATE_SECONDS,
) -> list[dict[str, Any]]:
    current = float(now if now is not None else time.time())
    threshold = max(1.0, float(stuck_after_seconds or FARM_HEALTH_STUCK_STATE_SECONDS))
    stuck: list[dict[str, Any]] = []
    for row in accounts or []:
        if not isinstance(row, dict):
            continue
        entry = _stuck_entry(row, current, threshold)
        if entry is not None:
            stuck.append(entry)
    return stuck


def _ambiguous_process_count(accounts: Iterable[dict[str, Any]]) -> int:
    count = 0
    for row in accounts or []:
        if not isinstance(row, dict):
            continue
        flags = set(_account_flags(row))
        reject_reason = str(row.get("process_reject_reason") or "").strip()
        if "process_binding_warning" in flags or reject_reason:
            count += 1
    return count


def _watchdog_unknown(message: str) -> dict[str, Any]:
    return {
        "status": "unknown",
        "task_installed": False,
        "project_root_matches": False,
        "watchdog_script_exists": False,
        "message": message,
    }


def _watchdog_status(
    task_installed: bool, script_exists: bool, project_root_matches: bool
) -> str:
    if not task_installed:
        return "missing"
    if not script_exists:
        return "missing_script"
    if not project_root_matches:
        return "stale"
    return "installed"


def _watchdog_installed_health(raw: dict[str, Any]) -> dict[str, Any]:
    task_installed = bool(raw.get("TaskInstalled", raw.get("task_installed", False)))
    project_root_matches = bool(
        raw.get("ProjectRootMatches", raw.get("project_root_matches", False))
    )
    script_exists = bool(
        raw.get("WatchdogScriptExists", raw.get("watchdog_script_exists", False))
    )
    return {
        "status": _watchdog_status(task_installed, script_exists, project_root_matches),
        "task_installed": task_installed,
        "task_state": str(raw.get("TaskState") or raw.get("task_state") or ""),
        "project_root_matches": project_root_matches,
        "watchdog_script_exists": script_exists,
        "expected_project_root": str(
            raw.get("ExpectedProjectRoot") or raw.get("expected_project_root") or ""
        ),
        "task_working_directory": str(
            raw.get("TaskWorkingDirectory") or raw.get("task_working_directory") or ""
        ),
        "last_task_result": raw.get(
            "LastTaskResult", raw.get("last_task_result")
        ),
        "backend_healthy": bool(
            raw.get("BackendHealthy", raw.get("backend_healthy", False))
        ),
    }


def _watchdog_task_health(snapshot: dict[str, Any]) -> dict[str, Any]:
    raw = dict(snapshot.get("watchdog_task") or {})
    if not raw:
        return _watchdog_unknown("Watchdog task status has not been inspected.")
    if raw.get("_inspection_error"):
        message = str(raw.get("_inspection_error") or "watchdog inspection failed")[:300]
        return _watchdog_unknown(message)
    return _watchdog_installed_health(raw)


def _public_warnings(
    runtime_health: dict[str, Any],
    control_plane: dict[str, Any],
    running: bool,
) -> list[str]:
    warnings = [str(item) for item in (runtime_health.get("warnings") or []) if str(item)]
    if control_plane.get("stuck_reasons"):
        warnings.append("control_plane_stuck")
    if not running:
        warnings.append("farm_stopped")
    return list(dict.fromkeys(warnings))


def _blocked_count(accounts: list[dict[str, Any]]) -> int:
    return sum(
        1
        for row in accounts
        if row.get("blocked_reason") or "blocked" in _account_flags(row)
    )


def _failed_count(accounts: list[dict[str, Any]]) -> int:
    return sum(
        1
        for row in accounts
        if _account_state(row) == "FAILED"
        or row.get("blocked_reason")
        or "blocked" in _account_flags(row)
    )


def _recovering_count(accounts: list[dict[str, Any]]) -> int:
    return sum(
        1
        for row in accounts
        if row.get("recovery_active")
        or row.get("recovery_inflight")
        or _account_state(row) in FARM_HEALTH_RECOVERY_STATES
    )


def _public_state(running: bool, warnings: list[str]) -> str:
    if not running:
        return "stopped"
    return "degraded" if warnings else "ok"


def _last_event_age(snapshot: dict[str, Any], current: float) -> Any:
    last_event_age = snapshot.get("last_runtime_event_age_seconds")
    if last_event_age is None:
        return _runtime_event_age(snapshot.get("recent_runtime_events") or [], current)
    return last_event_age


def build_public_farm_health(snapshot: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    current = float(now if now is not None else time.time())
    accounts = [row for row in (snapshot.get("accounts") or []) if isinstance(row, dict)]
    runtime_health = dict(snapshot.get("runtime_health") or {})
    queue_snapshot = dict(snapshot.get("queue_snapshot") or {})
    control_plane = dict(snapshot.get("control_plane") or {})
    running = bool(snapshot.get("running", False))
    warnings = _public_warnings(runtime_health, control_plane, running)

    states = Counter(_account_state(row) for row in accounts)
    status_updated_at = _safe_float(snapshot.get("status_updated_at"), 0.0)
    last_event_age = _last_event_age(snapshot, current)
    state = _public_state(running, warnings)
    return {
        "ok": state == "ok",
        "state": state,
        "running": running,
        "account_count": len(accounts),
        "in_game": states.get("IN_GAME", 0),
        "launching": states.get("LAUNCHING", 0) + states.get("VERIFY", 0),
        "queued": states.get("QUEUED", 0),
        "recovering": _recovering_count(accounts),
        "blocked": _blocked_count(accounts),
        "failed": _failed_count(accounts),
        "cooldown": states.get("COOLDOWN", 0),
        "bound_process_count": sum(1 for row in accounts if bool(row.get("pid_bound"))),
        "queue": _queue_public(queue_snapshot),
        "warnings": warnings,
        "last_runtime_event_age_seconds": round(_safe_float(last_event_age), 1),
        "status_revision": _safe_int(snapshot.get("status_revision"), 0),
        "status_age_seconds": round(max(0.0, current - status_updated_at), 1) if status_updated_at else 0.0,
        "checked_at": current,
    }


def _control_plane_component_reasons(
    dispatcher: dict[str, Any],
    maintenance: dict[str, Any],
    reasons: list[str],
    ages: list[float],
) -> None:
    if dispatcher.get("configured") and not dispatcher.get("alive"):
        reasons.append("dispatcher_dead")
        ages.append(_safe_float(dispatcher.get("dead_for_seconds"), 0.0))
    if maintenance.get("configured") and not maintenance.get("alive"):
        reasons.append("maintenance_dead")
        ages.append(_safe_float(maintenance.get("dead_for_seconds"), 0.0))


def _scheduler_age(scheduler: dict[str, Any]) -> float:
    return max(
        _safe_float(scheduler.get("last_loop_age_seconds"), 0.0),
        _safe_float(scheduler.get("max_overdue_seconds"), 0.0),
        _safe_float(scheduler.get("last_dispatch_age_seconds"), 0.0),
    )


def _scheduler_health_reasons(
    scheduler: dict[str, Any], reasons: list[str], ages: list[float]
) -> None:
    scheduler_age = _scheduler_age(scheduler)
    if scheduler.get("closed") and not scheduler.get("external_stop"):
        reasons.append("scheduler_closed")
        ages.append(scheduler_age)
    if not scheduler.get("thread_alive", True) and not scheduler.get("external_stop"):
        reasons.append("scheduler_dead")
        ages.append(scheduler_age)
    if (
        _safe_int(scheduler.get("overdue_count"), 0) > 0
        and _safe_float(scheduler.get("max_overdue_seconds"), 0.0) >= 30.0
    ):
        reasons.append("scheduler_overdue")
        ages.append(_safe_float(scheduler.get("max_overdue_seconds"), 0.0))


def build_control_plane_health(snapshot: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    current = float(now if now is not None else time.time())
    running = bool(snapshot.get("running", False))
    scheduler = dict(snapshot.get("scheduler_health") or {})
    dispatcher = dict(snapshot.get("dispatcher") or {})
    maintenance = dict(snapshot.get("maintenance") or {})
    reasons: list[str] = []
    ages: list[float] = []
    if running:
        _control_plane_component_reasons(dispatcher, maintenance, reasons, ages)
        if scheduler:
            _scheduler_health_reasons(scheduler, reasons, ages)
    reasons = list(dict.fromkeys(reasons))
    return {
        "ok": not reasons,
        "stuck_reasons": reasons,
        "max_stuck_age_seconds": round(max(ages), 1) if ages else 0.0,
        "last_restart_at": _safe_float(snapshot.get("last_control_plane_restart_at"), 0.0),
        "checked_at": current,
    }


def _watchdog_none(current: float, reason: str) -> dict[str, Any]:
    return {
        "action": "none",
        "scope": "farm",
        "reason": reason,
        "checked_at": current,
    }


def _watchdog_context(
    health: dict[str, Any],
    threshold_seconds: float,
    backoff_seconds: float,
) -> tuple[dict[str, Any], list[str], float, float]:
    control_plane = dict(health.get("control_plane") or {})
    control_reasons = [
        str(item) for item in (control_plane.get("stuck_reasons") or []) if str(item)
    ]
    threshold = max(
        1.0, float(threshold_seconds or CONTROL_PLANE_RESTART_THRESHOLD_SECONDS)
    )
    backoff = max(
        1.0, float(backoff_seconds or CONTROL_PLANE_RESTART_BACKOFF_SECONDS)
    )
    return control_plane, control_reasons, threshold, backoff


def _control_plane_decision(
    control_plane: dict[str, Any],
    current: float,
    threshold: float,
    backoff: float,
    control_reasons: list[str],
) -> dict[str, Any] | None:
    control_age = _safe_float(control_plane.get("max_stuck_age_seconds"), 0.0)
    if not control_reasons or control_age < threshold:
        return None
    last_restart_at = _safe_float(control_plane.get("last_restart_at"), 0.0)
    since_restart = (
        max(0.0, current - last_restart_at) if last_restart_at else backoff
    )
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


def _account_has_issue(row: dict[str, Any]) -> bool:
    if _account_state(row) in _WATCHDOG_ISSUE_STATES:
        return True
    flags = _account_flags(row)
    return any(flag in flags for flag in _WATCHDOG_ISSUE_FLAGS)


def _account_recovery_rows(
    health: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    stuck_states = [
        row for row in (health.get("stuck_states") or []) if isinstance(row, dict)
    ]
    account_issue_rows = [
        row
        for row in (health.get("accounts") or [])
        if isinstance(row, dict) and _account_has_issue(row)
    ]
    return stuck_states, account_issue_rows


def _recovery_reasons(
    stuck_states: list[dict[str, Any]],
    account_issue_rows: list[dict[str, Any]],
) -> list[str]:
    stuck_reasons = [
        str(row.get("reason") or "") for row in stuck_states if row.get("reason")
    ]
    issue_flags = [flag for row in account_issue_rows for flag in _account_flags(row)]
    return list(dict.fromkeys(stuck_reasons + issue_flags))


def _runtime_warnings(health: dict[str, Any]) -> list[str]:
    runtime_health = dict(health.get("runtime_health") or {})
    return [
        str(item)
        for item in (runtime_health.get("warnings") or health.get("warnings") or [])
        if str(item)
    ]


def decide_farm_watchdog_action(
    health: dict[str, Any],
    now: float | None = None,
    control_plane_restart_threshold_seconds: float = CONTROL_PLANE_RESTART_THRESHOLD_SECONDS,
    control_plane_restart_backoff_seconds: float = CONTROL_PLANE_RESTART_BACKOFF_SECONDS,
) -> dict[str, Any]:
    current = float(now if now is not None else time.time())
    if not bool(health.get("running", True)):
        return _watchdog_none(current, "farm_stopped")

    control_plane, control_reasons, threshold, backoff = _watchdog_context(
        health,
        control_plane_restart_threshold_seconds,
        control_plane_restart_backoff_seconds,
    )
    decision = _control_plane_decision(
        control_plane, current, threshold, backoff, control_reasons
    )
    if decision is not None:
        return decision

    stuck_states, account_issue_rows = _account_recovery_rows(health)
    if stuck_states or account_issue_rows:
        return {
            "action": "targeted_recovery",
            "scope": "account",
            "account_count": len(stuck_states) or len(account_issue_rows),
            "reasons": _recovery_reasons(stuck_states, account_issue_rows),
            "checked_at": current,
        }

    warnings = _runtime_warnings(health)
    if control_reasons or warnings:
        return {
            "action": "log_degraded",
            "scope": "farm",
            "reasons": list(dict.fromkeys(control_reasons + warnings)),
            "backoff_active": False,
            "checked_at": current,
        }
    return _watchdog_none(current, "healthy")


def _string_or_empty(value: Any) -> str:
    return str(value or "")


def _detailed_account_row(row: dict[str, Any], current: float) -> dict[str, Any]:
    return {
        "account_id": _string_or_empty(row.get("account_id")),
        "state": _account_state(row),
        "desired_state": _string_or_empty(row.get("desired_state")),
        "recovery_status": _string_or_empty(row.get("recovery_status")),
        "recovery_reason": _string_or_empty(row.get("recovery_reason")),
        "recovery_active": bool(
            row.get("recovery_active") or row.get("recovery_inflight")
        ),
        "cooldown_left": _safe_int(row.get("cooldown_left"), 0),
        "retry_count": _safe_int(row.get("retry_count"), 0),
        "fail_count": _safe_int(row.get("fail_count"), 0),
        "pid_bound": bool(row.get("pid_bound")),
        "process_proof_level": _string_or_empty(row.get("process_proof_level")),
        "process_binding_status": _string_or_empty(row.get("process_binding_status")),
        "process_reject_reason": _string_or_empty(row.get("process_reject_reason")),
        "liveness_state": _string_or_empty(row.get("liveness_state")),
        "watchdog_classification": _string_or_empty(
            row.get("watchdog_classification")
        ),
        "last_transition_age_seconds": round(
            max(
                0.0,
                current - _safe_float(row.get("last_transition_at"), current),
            ),
            1,
        ),
        "health_flags": _account_flags(row),
    }


def _detailed_accounts(rows: Iterable[Any], current: float) -> list[dict[str, Any]]:
    return [
        _detailed_account_row(row, current)
        for row in rows
        if isinstance(row, dict)
    ]


def build_detailed_farm_health(snapshot: dict[str, Any], now: float | None = None) -> dict[str, Any]:
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
    accounts = _detailed_accounts(
        detail_snapshot.get("accounts") or [], current
    )
    detail = {
        **public,
        "runtime": {
            "last_event_age_seconds": public["last_runtime_event_age_seconds"],
            "stuck_accounts": len(stuck_states),
            "ambiguous_process_count": _ambiguous_process_count(accounts),
        },
        "watchdog_task": _watchdog_task_health(detail_snapshot),
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


def _pressure_counts(events: Iterable[dict[str, Any]]) -> tuple[int, int, int]:
    event_counts = Counter(_event_type(item) for item in events)
    stale_work = sum(
        count for event, count in event_counts.items() if "stale_work_rejected" in event
    )
    invariant = sum(
        count for event, count in event_counts.items() if "invariant" in event
    )
    recovery = sum(
        count for event, count in event_counts.items() if "recovery" in event
    )
    return stale_work, invariant, recovery


def _heartbeat_ages(
    rows: Iterable[dict[str, Any]], current: float
) -> list[float]:
    return [
        current - float(row.get("last_heartbeat") or 0.0)
        for row in rows
        if row.get("process_alive")
        and float(row.get("last_heartbeat") or 0.0) > 0.0
    ]


def _runtime_warning_flags(
    account_count: int,
    stale_work_count: int,
    invariant_count: int,
    recovery_count: int,
    relaunch_count: int,
    queue_depth: int,
) -> list[str]:
    warnings: list[str] = []
    if stale_work_count >= 3:
        warnings.append("stale_work_pressure")
    if invariant_count:
        warnings.append("runtime_invariant_violations")
    if recovery_count >= max(3, account_count):
        warnings.append("recovery_pressure")
    if relaunch_count >= max(3, account_count):
        warnings.append("relaunch_pressure")
    if queue_depth >= max(5, account_count):
        warnings.append("queue_pressure")
    return warnings


def _scheduler_latency(scheduler: dict[str, Any]) -> float:
    return max(
        float(scheduler.get("last_dispatch_latency_seconds") or 0.0),
        float(scheduler.get("max_overdue_seconds") or 0.0),
    )


def _scheduler_warnings(
    scheduler: dict[str, Any], scheduler_latency: float
) -> list[str]:
    warnings: list[str] = []
    if scheduler.get("callback_failure_count"):
        warnings.append("scheduler_callback_failures")
    if int(scheduler.get("overdue_count") or 0) > 0 and scheduler_latency >= 10.0:
        warnings.append("scheduler_overdue")
    if scheduler.get("closed") and not scheduler.get("external_stop"):
        warnings.append("scheduler_closed")
    return warnings


def build_runtime_health(
    accounts: Iterable[dict[str, Any]],
    queue_snapshot: dict[str, Any],
    recent_events: Iterable[dict[str, Any]],
    scheduler_snapshot: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    current = float(now if now is not None else time.time())
    account_rows = list(accounts or [])
    event_rows = list(recent_events or [])
    pressure_events = [item for item in event_rows if _is_recent_event(item, current)]
    stale_work_count, invariant_count, recovery_count = _pressure_counts(
        pressure_events
    )
    relaunch_count = sum(
        1 for event in pressure_events if _is_relaunch_pressure_event(event)
    )

    heartbeat_ages = _heartbeat_ages(account_rows, current)
    queue_depth = int(
        queue_snapshot.get("size") or queue_snapshot.get("pending") or 0
    )
    warnings = _runtime_warning_flags(
        len(account_rows),
        stale_work_count,
        invariant_count,
        recovery_count,
        relaunch_count,
        queue_depth,
    )
    if heartbeat_ages and max(heartbeat_ages) > 180:
        warnings.append("heartbeat_stale")
    scheduler = dict(scheduler_snapshot or {})
    scheduler_latency = _scheduler_latency(scheduler)
    warnings.extend(_scheduler_warnings(scheduler, scheduler_latency))

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
