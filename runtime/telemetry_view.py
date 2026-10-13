from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _event_type(event: Dict[str, Any]) -> str:
    return str(event.get("event_type") or event.get("kind") or "").lower()


def _durations(events: Iterable[Dict[str, Any]]) -> List[float]:
    values: List[float] = []
    for event in events:
        for key in ("reconnect_duration_seconds", "duration_seconds", "elapsed_seconds"):
            if key in event:
                duration = _num(event.get(key), -1.0)
                if duration >= 0:
                    values.append(duration)
                break
    return values


def build_runtime_telemetry(status: Dict[str, Any], now: float | None = None) -> Dict[str, Any]:
    current = float(now if now is not None else time.time())
    accounts = list(status.get("accounts") or [])
    events = list(status.get("recent_runtime_events") or [])
    health = dict(status.get("runtime_health") or {})
    total_rejoin = int(_num(status.get("total_rejoin"), 0.0))
    total_crash = int(_num(status.get("total_crash"), 0.0))
    recovery_active = sum(1 for row in accounts if "recovery_active" in (row.get("health_flags") or []))
    stale_workers = sum(
        1
        for row in accounts
        if any(flag in (row.get("health_flags") or []) for flag in ("heartbeat_stale", "running_without_live_process"))
    )
    crash_count = sum(int(_num(row.get("crash_count", row.get("crash")), 0.0)) for row in accounts)
    memory_mb = round(sum(_num(row.get("mem_mb"), 0.0) for row in accounts), 2)
    durations = _durations(events)
    completed = total_rejoin + total_crash
    event_counts: Dict[str, int] = {}
    for event in events:
        key = _event_type(event) or "runtime_event"
        event_counts[key] = event_counts.get(key, 0) + 1

    return {
        "ok": True,
        "checked_at": current,
        "account_count": len(accounts),
        "running_count": sum(1 for row in accounts if str(row.get("state") or "").upper() == "IN_GAME"),
        "recovery_active_count": recovery_active,
        "stale_worker_count": max(stale_workers, int(_num(health.get("stale_work_count"), 0.0))),
        "crash_count": crash_count,
        "total_rejoin": total_rejoin,
        "total_crash": total_crash,
        "recovery_rate": round((total_rejoin / completed), 4) if completed else 0.0,
        "reconnect_duration_seconds": {
            "avg": round(sum(durations) / len(durations), 3) if durations else 0.0,
            "max": round(max(durations), 3) if durations else 0.0,
            "samples": len(durations),
        },
        "memory_usage_mb": memory_mb,
        "runtime_health": health,
        "event_counts": event_counts,
    }
