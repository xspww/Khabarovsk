from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from core import account_launch_block_reason, flog_kv
from infra.app_paths import APP_DATA_DIR
from runtime.account_selection import runtime_account_filter_reason
from runtime.diagnostic_bundle import build_runtime_diagnostic_bundle
from runtime.runtime_health import (
    account_health_flags,
    build_detailed_farm_health,
    build_public_farm_health,
    build_runtime_health,
)
from runtime.runtime_view_model import RuntimeViewModelBuilder
from runtime.telemetry_view import build_runtime_telemetry
from services.account_hybrid import redact_secret

WATCHDOG_STATUS_CACHE = "watchdog_task_last.json"


def _read_cached_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_cached_operator_health(data_dir: str | Path = APP_DATA_DIR) -> dict[str, Any]:
    root = Path(data_dir)
    return {
        "watchdog_task": _read_cached_json(root / WATCHDOG_STATUS_CACHE),
    }


def build_farm_status(farm: Any, include_diagnostics: bool = False) -> dict:
    return RuntimeViewModelBuilder(farm, include_diagnostics=include_diagnostics).build_status()


def _text(value: Any) -> str:
    return str(value or "")


def _cfg_snapshot(farm: Any) -> dict[str, Any]:
    try:
        return farm.cfg_mgr.snapshot()
    except Exception:
        return {}


def _account_key(acc: Any) -> str:
    return _text(getattr(acc, "_config_username", "") or getattr(acc, "username", ""))


def _public_state(snapshot: dict[str, Any], key: str, acc: Any, attr: str, default: str) -> str:
    return str(snapshot.get(key) or getattr(getattr(acc, attr, None), "name", default))


def _snapshot_text(
    snapshot: dict[str, Any],
    key: str,
    acc: Any,
    attr: str,
    default: str = "",
) -> str:
    return _text(snapshot.get(key) or getattr(acc, attr, default))


def _snapshot_float(
    snapshot: dict[str, Any],
    key: str,
    acc: Any,
    attr: str,
    default: float = 0.0,
) -> float:
    return float(snapshot.get(key, getattr(acc, attr, default)) or 0.0)


def _snapshot_int(snapshot: dict[str, Any], key: str, acc: Any, attr: str) -> int:
    return int(snapshot.get(key, getattr(acc, attr, 0)) or 0)


def _blocked_reason(acc: Any, cfg_snapshot: dict[str, Any]) -> str:
    return account_launch_block_reason(acc) or runtime_account_filter_reason(acc, cfg_snapshot)


def _cooldown_left(cooldown_until: float, now: float) -> int:
    if not cooldown_until:
        return 0
    return max(0, int(cooldown_until - now))


def _binding_status(snapshot: dict[str, Any], acc: Any) -> str:
    return _text(
        snapshot.get("binding_status")
        or snapshot.get("bind_status")
        or getattr(acc, "process_binding_status", "")
    )


def _proof_level(snapshot: dict[str, Any], acc: Any) -> str:
    return str(
        snapshot.get("process_proof_level")
        or getattr(acc, "process_proof_level", "untrusted")
        or "untrusted"
    )


def _watchdog_classification(acc: Any) -> str:
    return _text(getattr(acc, "last_watchdog_classification", ""))


def _account_health_row(
    acc: Any,
    snapshot: dict[str, Any],
    cfg_snapshot: dict[str, Any],
    now: float,
) -> dict[str, Any]:
    cooldown_until = float(
        snapshot.get("cooldown_until", getattr(acc, "cooldown_until", 0.0)) or 0.0
    )
    state = _public_state(snapshot, "public_state", acc, "state", "IDLE")
    desired_state = _public_state(
        snapshot,
        "desired_public_state",
        acc,
        "desired_state",
        "IN_GAME",
    )
    row: dict[str, Any] = {
        "account_id": _account_key(acc),
        "state": state,
        "public_state": state,
        "desired_state": desired_state,
        "blocked_reason": _text(_blocked_reason(acc, cfg_snapshot)),
        "pid_bound": bool(snapshot.get("pid")),
        "recovery_active": bool(snapshot.get("recovery_active", False)),
        "recovery_inflight": bool(snapshot.get("recovery_inflight", False)),
        "recovery_status": _snapshot_text(snapshot, "recovery_status", acc, "recovery_status"),
        "recovery_reason": _snapshot_text(
            snapshot,
            "recovery_reason",
            acc,
            "last_recovery_reason",
        ),
        "cooldown_left": _cooldown_left(cooldown_until, now),
        "retry_count": _snapshot_int(snapshot, "retry_count", acc, "retry_count"),
        "fail_count": _snapshot_int(snapshot, "fail_count", acc, "fail_count"),
        "last_heartbeat": _snapshot_float(snapshot, "last_heartbeat", acc, "last_heartbeat"),
        "last_transition_at": _snapshot_float(
            snapshot,
            "last_transition_at",
            acc,
            "last_state_change_at",
        ),
        "last_transition_reason": _snapshot_text(
            snapshot,
            "last_transition_reason",
            acc,
            "last_state_reason",
        ),
        "current_command": _snapshot_text(snapshot, "current_command", acc, "current_command"),
        "command_inflight": snapshot.get("command_inflight"),
        "process_binding_status": _binding_status(snapshot, acc),
        "process_proof_level": _proof_level(snapshot, acc),
        "process_reject_reason": _snapshot_text(
            snapshot,
            "process_reject_reason",
            acc,
            "process_reject_reason",
        ),
        "liveness_state": _snapshot_text(snapshot, "liveness_state", acc, "liveness_state"),
        "liveness_score": _snapshot_float(snapshot, "liveness_score", acc, "liveness_score"),
        "watchdog_classification": _watchdog_classification(acc),
    }
    row["health_flags"] = account_health_flags(
        {**row, "process_alive": bool(row["pid_bound"])},
        now=now,
    )
    return row


def build_farm_health_account_rows(farm: Any, now: float) -> list[dict[str, Any]]:
    cfg_snapshot = _cfg_snapshot(farm)
    rows: list[dict[str, Any]] = []
    for acc in farm._accounts:
        runtime_snapshot = farm._runtime_state.snapshot(acc)
        rows.append(_account_health_row(acc, runtime_snapshot, cfg_snapshot, now))
    return rows


def build_thread_health(farm: Any, thread_obj: Any, now: float) -> dict[str, Any]:
    configured = thread_obj is not None
    alive = bool(configured and thread_obj.is_alive())
    dead_for = max(0.0, now - float(farm.start_ts or now)) if farm.running and configured and not alive else 0.0
    return {
        "configured": configured,
        "alive": alive,
        "dead_for_seconds": round(dead_for, 1),
        "name": str(getattr(thread_obj, "name", "") or "") if configured else "",
    }


def build_worker_health(farm: Any, now: float) -> dict[str, Any]:
    items = []
    for account_id, worker in sorted(farm._workers.items()):
        acc = getattr(worker, "acc", None)
        state = getattr(getattr(acc, "state", None), "name", "") if acc is not None else ""
        heartbeat = float(getattr(acc, "last_activity_at", 0.0) or 0.0) if acc is not None else 0.0
        items.append({
            "account_id": str(account_id or ""),
            "alive": bool(worker.is_alive()),
            "state": state,
            "last_heartbeat_age_seconds": round(max(0.0, now - heartbeat), 1) if heartbeat else 0.0,
        })
    return {
        "total": len(items),
        "alive": sum(1 for item in items if item["alive"]),
        "dead": sum(1 for item in items if not item["alive"]),
        "items": items,
    }


def _queue_snapshot(farm: Any) -> dict[str, Any]:
    if farm._queue:
        return farm._queue.snapshot()
    return {
        "size": 0,
        "pending": 0,
        "busy": False,
        "closed": not farm.running,
        "stale_rejections": 0,
        "oldest_age_seconds": 0.0,
        "entries": [],
    }


def _recovery_storm(farm: Any) -> dict[str, Any]:
    recovery = getattr(farm, "_recovery", None)
    if not getattr(recovery, "_storm", None):
        return {}
    return recovery._storm.snapshot()


def _status_revision(farm: Any) -> int:
    with farm._status_lock:
        return int(farm._status_revision)


def _event_ages(events: list[Any], now: float) -> list[float]:
    ages: list[float] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        try:
            ts = float(event.get("ts") or 0.0)
        except Exception:
            ts = 0.0
        if ts > 0.0:
            ages.append(max(0.0, now - ts))
    return ages


def _last_event_age(ages: list[float]) -> float:
    return round(min(ages), 1) if ages else 0.0


def build_farm_health_snapshot(farm: Any) -> dict[str, Any]:
    now = time.time()
    queue_snapshot = _queue_snapshot(farm)
    scheduler_snapshot = farm._runtime_scheduler.snapshot(now=now) if farm._runtime_scheduler else {}
    with farm._event_lock:
        recent_events = list(farm._event_log)[-100:]
    accounts = build_farm_health_account_rows(farm, now)
    runtime_health = build_runtime_health(
        accounts,
        queue_snapshot,
        recent_events,
        scheduler_snapshot=scheduler_snapshot,
        now=now,
    )
    recovery_storm = _recovery_storm(farm)
    status_revision = _status_revision(farm)
    operator_health = load_cached_operator_health()
    event_ages = _event_ages(recent_events, now)
    return {
        "running": bool(farm.running),
        "status_revision": status_revision,
        "status_updated_at": now,
        "accounts": accounts,
        "queue_snapshot": queue_snapshot,
        "scheduler_health": scheduler_snapshot,
        "runtime_health": runtime_health,
        "recovery_storm": recovery_storm,
        "recent_runtime_events": recent_events,
        "last_runtime_event_age_seconds": _last_event_age(event_ages),
        "workers": build_worker_health(farm, now),
        "dispatcher": build_thread_health(farm, farm._dispatcher, now),
        "maintenance": build_thread_health(farm, farm._maintenance, now),
        "last_control_plane_restart_at": float(farm._last_control_plane_restart_at or 0.0),
        "watchdog_task": operator_health.get("watchdog_task") or {},
    }


def get_public_farm_health(farm: Any) -> dict:
    return build_public_farm_health(build_farm_health_snapshot(farm))


def get_detailed_farm_health(farm: Any) -> dict:
    return build_detailed_farm_health(build_farm_health_snapshot(farm))


def get_runtime_health(farm: Any) -> dict:
    status = build_farm_health_snapshot(farm)
    return {
        "ok": True,
        "runtime_health": status.get("runtime_health", {}),
        "queue_snapshot": status.get("queue_snapshot", {}),
        "status_revision": status.get("status_revision", 0),
        "status_updated_at": status.get("status_updated_at", 0.0),
    }


def get_runtime_telemetry(farm: Any) -> dict:
    return build_runtime_telemetry(build_farm_status(farm, include_diagnostics=True))


def get_runtime_events(
    farm: Any,
    account_id: str = "",
    limit: int = 100,
    event_type: str = "",
    severity: str = "",
) -> dict:
    safe_limit = max(1, min(int(limit or 100), 500))
    try:
        events = farm._runtime_store.list_recent_events(
            account_id=account_id,
            limit=safe_limit,
            event_type=event_type,
            severity=severity,
        )
    except Exception as exc:
        flog_kv("RUNTIME", "runtime_events_query_failed", "warning", account=account_id, error=exc)
        events = []
    return {
        "ok": True,
        "account_id": account_id or "",
        "event_type": event_type or "",
        "severity": severity or "",
        "limit": safe_limit,
        "events": events,
    }


def get_runtime_diagnostics(
    farm: Any,
    account_id: str = "",
    limit: int = 200,
    event_type: str = "",
    severity: str = "",
) -> dict:
    safe_limit = max(1, min(int(limit or 200), 500))
    status = build_farm_status(farm, include_diagnostics=True)
    try:
        events = farm._runtime_store.list_recent_events(
            account_id=account_id,
            limit=safe_limit,
            event_type=event_type,
            severity=severity,
        )
    except Exception as exc:
        flog_kv("RUNTIME", "runtime_diagnostics_events_failed", "warning", account=account_id, error=exc)
        events = []
    try:
        cfg = farm.cfg_mgr.snapshot()
    except Exception:
        cfg = {}
    return build_runtime_diagnostic_bundle(
        status,
        events,
        cfg,
        account_id=account_id,
        event_type=event_type,
        severity=severity,
        limit=safe_limit,
    )


def get_account(farm: Any, username: str) -> dict | None:
    status = build_farm_status(farm, include_diagnostics=True)
    for item in status["accounts"]:
        if item["username"] == username:
            acc = next((x for x in farm._accounts if x.username == username), None)
            if acc:
                item["retry_history"] = acc.retry_history[-20:]
                item["vip_links"] = [redact_secret(link) for link in list(acc.vip_links or [])]
                item["place_id"] = acc.place_id
                item["cookie_present"] = bool(acc.cookie)
            return item
    return None
