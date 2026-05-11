from __future__ import annotations

import time
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional


def _event_type(event: Dict[str, Any]) -> str:
    return str(event.get("event_type") or event.get("kind") or "").lower()


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
    if account.get("process_reject_reason"):
        flags.append("process_binding_warning")
    return flags


def build_runtime_health(
    accounts: Iterable[Dict[str, Any]],
    queue_snapshot: Dict[str, Any],
    recent_events: Iterable[Dict[str, Any]],
    now: Optional[float] = None,
) -> Dict[str, Any]:
    current = float(now if now is not None else time.time())
    account_rows = list(accounts or [])
    event_rows = list(recent_events or [])
    event_counts = Counter(_event_type(item) for item in event_rows)
    stale_work_count = sum(count for event, count in event_counts.items() if "stale_work_rejected" in event)
    invariant_count = sum(count for event, count in event_counts.items() if "invariant" in event)
    recovery_count = sum(count for event, count in event_counts.items() if "recovery" in event)
    relaunch_count = sum(1 for event in event_rows if _is_relaunch_pressure_event(event))

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

    return {
        "ok": not warnings,
        "warnings": warnings,
        "watchdog_latency_seconds": 0,
        "max_heartbeat_age_seconds": round(max(heartbeat_ages), 1) if heartbeat_ages else 0,
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
