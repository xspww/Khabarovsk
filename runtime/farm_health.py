from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from app_paths import APP_DATA_DIR
from account_hybrid import redact_secret
from core import account_launch_block_reason, flog_kv
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


WATCHDOG_STATUS_CACHE = "watchdog_task_last.json"
RELEASE_GATE_CACHE = "release_gate_last.json"


def _read_cached_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_cached_operator_health(data_dir: str | Path = APP_DATA_DIR) -> Dict[str, Any]:
    root = Path(data_dir)
    return {
        "watchdog_task": _read_cached_json(root / WATCHDOG_STATUS_CACHE),
        "release_gate": _read_cached_json(root / RELEASE_GATE_CACHE),
    }


def build_farm_status(farm: Any) -> dict:
    return RuntimeViewModelBuilder(farm).build_status()


def build_farm_health_account_rows(farm: Any, now: float) -> List[Dict[str, Any]]:
    try:
        cfg_snapshot = farm.cfg_mgr.snapshot()
    except Exception:
        cfg_snapshot = {}
    rows: List[Dict[str, Any]] = []
    for acc in farm._accounts:
        runtime_snapshot = farm._runtime_state.snapshot(acc)
        state = str(runtime_snapshot.get("public_state") or getattr(getattr(acc, "state", None), "name", "IDLE"))
        desired_state = str(runtime_snapshot.get("desired_public_state") or getattr(getattr(acc, "desired_state", None), "name", "IN_GAME"))
        cooldown_until = float(runtime_snapshot.get("cooldown_until", getattr(acc, "cooldown_until", 0.0)) or 0.0)
        blocked_reason = account_launch_block_reason(acc) or runtime_account_filter_reason(acc, cfg_snapshot)
        row: Dict[str, Any] = {
            "account_id": str(getattr(acc, "_config_username", "") or getattr(acc, "username", "") or ""),
            "state": state,
            "public_state": state,
            "desired_state": desired_state,
            "blocked_reason": blocked_reason or "",
            "pid_bound": bool(runtime_snapshot.get("pid")),
            "recovery_active": bool(runtime_snapshot.get("recovery_active", False)),
            "recovery_inflight": bool(runtime_snapshot.get("recovery_inflight", False)),
            "recovery_status": str(runtime_snapshot.get("recovery_status") or getattr(acc, "recovery_status", "") or ""),
            "recovery_reason": str(runtime_snapshot.get("recovery_reason") or getattr(acc, "last_recovery_reason", "") or ""),
            "cooldown_left": max(0, int(cooldown_until - now)) if cooldown_until else 0,
            "retry_count": int(runtime_snapshot.get("retry_count", getattr(acc, "retry_count", 0)) or 0),
            "fail_count": int(runtime_snapshot.get("fail_count", getattr(acc, "fail_count", 0)) or 0),
            "last_heartbeat": float(runtime_snapshot.get("last_heartbeat", 0.0) or 0.0),
            "last_transition_at": float(runtime_snapshot.get("last_transition_at", getattr(acc, "last_state_change_at", 0.0)) or 0.0),
            "last_transition_reason": str(runtime_snapshot.get("last_transition_reason") or getattr(acc, "last_state_reason", "") or ""),
            "current_command": str(runtime_snapshot.get("current_command") or getattr(acc, "current_command", "") or ""),
            "command_inflight": runtime_snapshot.get("command_inflight"),
            "process_binding_status": str(runtime_snapshot.get("binding_status") or runtime_snapshot.get("bind_status") or getattr(acc, "process_binding_status", "") or ""),
            "process_proof_level": str(runtime_snapshot.get("process_proof_level") or getattr(acc, "process_proof_level", "untrusted") or "untrusted"),
            "process_reject_reason": str(runtime_snapshot.get("process_reject_reason") or getattr(acc, "process_reject_reason", "") or ""),
            "liveness_state": str(runtime_snapshot.get("liveness_state") or getattr(acc, "liveness_state", "") or ""),
            "liveness_score": float(runtime_snapshot.get("liveness_score", getattr(acc, "liveness_score", 0.0)) or 0.0),
            "watchdog_classification": str(getattr(acc, "last_watchdog_classification", "") or ""),
        }
        row["health_flags"] = account_health_flags({**row, "process_alive": bool(row["pid_bound"])}, now=now)
        rows.append(row)
    return rows


def build_thread_health(farm: Any, thread_obj: Any, now: float) -> Dict[str, Any]:
    configured = thread_obj is not None
    alive = bool(configured and thread_obj.is_alive())
    dead_for = max(0.0, now - float(farm.start_ts or now)) if farm.running and configured and not alive else 0.0
    return {
        "configured": configured,
        "alive": alive,
        "dead_for_seconds": round(dead_for, 1),
        "name": str(getattr(thread_obj, "name", "") or "") if configured else "",
    }


def build_worker_health(farm: Any, now: float) -> Dict[str, Any]:
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


def build_farm_health_snapshot(farm: Any) -> Dict[str, Any]:
    now = time.time()
    queue_snapshot = farm._queue.snapshot() if farm._queue else {
        "size": 0,
        "pending": 0,
        "busy": False,
        "closed": not farm.running,
        "stale_rejections": 0,
        "oldest_age_seconds": 0.0,
        "entries": [],
    }
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
    recovery_storm = {}
    if getattr(getattr(farm, "_recovery", None), "_storm", None):
        recovery_storm = farm._recovery._storm.snapshot()
    with farm._status_lock:
        status_revision = int(farm._status_revision)
    operator_health = load_cached_operator_health()
    event_ages = []
    for event in recent_events:
        if not isinstance(event, dict):
            continue
        try:
            ts = float(event.get("ts") or 0.0)
        except Exception:
            ts = 0.0
        if ts > 0.0:
            event_ages.append(max(0.0, now - ts))
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
        "last_runtime_event_age_seconds": round(min(event_ages), 1) if event_ages else 0.0,
        "workers": build_worker_health(farm, now),
        "dispatcher": build_thread_health(farm, farm._dispatcher, now),
        "maintenance": build_thread_health(farm, farm._maintenance, now),
        "last_control_plane_restart_at": float(farm._last_control_plane_restart_at or 0.0),
        "watchdog_task": operator_health.get("watchdog_task") or {},
        "release_gate": operator_health.get("release_gate") or {},
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
    return build_runtime_telemetry(build_farm_status(farm))


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
    status = build_farm_status(farm)
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


def get_account(farm: Any, username: str) -> Optional[dict]:
    status = build_farm_status(farm)
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
