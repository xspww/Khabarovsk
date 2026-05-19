from __future__ import annotations

from typing import Any, Dict

from core import flog_kv
from services.log_rate_limiter import LogRateLimiter


WATCHDOG_LOG_RATE_LIMITER = LogRateLimiter()
_WATCHDOG_HOLD_LOG_INTERVAL_SECONDS = 15.0


def handle_memory_pressure_rejoin(owner: Any, acc: Any, pid: Any, pressure: Dict[str, Any]) -> None:
    ram_mb = float(pressure.get("ram_mb") or 0.0)
    limit_mb = float(pressure.get("limit_mb") or 0.0)
    high_for = float(pressure.get("high_for") or 0.0)
    flog_kv(
        "WATCHDOG",
        "memory_pressure_rejoin_signal",
        "warning",
        account=acc.display_name,
        pid=pid,
        ram=f"{ram_mb:.1f}",
        limit=f"{limit_mb:.1f}",
        high_for=f"{high_for:.1f}",
        runtime_generation=pressure.get("runtime_generation"),
        session_id=pressure.get("session_id"),
        transaction_id=pressure.get("transaction_id"),
    )
    owner._runtime_signal(
        acc,
        "watchdog_timeout",
        "process_memory_pressure",
        payload={
            "trigger": "memory_guard",
            "detail": f"PID={pid} Roblox memory {ram_mb:.1f}MB exceeded {limit_mb:.1f}MB for {high_for:.1f}s",
            "reason_msg": f"PID={pid} Roblox memory pressure",
        },
        expected_runtime_generation=int(pressure.get("runtime_generation") or 0),
        expected_session_id=str(pressure.get("session_id") or ""),
        expected_launch_nonce=str(pressure.get("launch_nonce") or ""),
        expected_transaction_id=str(pressure.get("transaction_id") or ""),
    )


def log_memory_pressure_hold(acc: Any, pid: Any, pressure: Dict[str, Any], hold_seconds: float) -> None:
    WATCHDOG_LOG_RATE_LIMITER.emit(
        (
            "WATCHDOG",
            "memory_pressure_hold",
            str(getattr(acc, "_config_username", "") or getattr(acc, "username", "") or getattr(acc, "display_name", "")),
            int(pid or 0),
            str(pressure.get("reason") or "process_memory_pressure"),
        ),
        _WATCHDOG_HOLD_LOG_INTERVAL_SECONDS,
        flog_kv,
        "WATCHDOG",
        "memory_pressure_hold",
        "warning",
        account=acc.display_name,
        pid=pid,
        ram=f"{float(pressure.get('ram_mb') or 0.0):.1f}",
        limit=f"{float(pressure.get('limit_mb') or 0.0):.1f}",
        high_for=f"{float(pressure.get('high_for') or 0.0):.1f}",
        hold=f"{hold_seconds:.1f}",
    )


def handle_disconnect_dialog_rejoin(owner: Any, acc: Any, pid: Any, dialog: Dict[str, Any]) -> None:
    reason_key = str(dialog.get("reason_key") or "connection_error")
    detail = str(dialog.get("detail") or "")
    error_code = str(dialog.get("error_code") or "")
    flog_kv(
        "WATCHDOG",
        "disconnect_dialog_rejoin_signal",
        "warning",
        account=acc.display_name,
        pid=pid,
        reason=reason_key,
        error_code=error_code,
        confidence=f"{float(dialog.get('popup_confidence') or 0.0):.2f}",
        source=dialog.get("evidence_source", ""),
        visual_source=dialog.get("visual_evidence_source", ""),
        detail=detail,
        reconnecting_for=f"{float(dialog.get('reconnecting_for') or 0.0):.1f}",
        runtime_generation=dialog.get("runtime_generation"),
        session_id=dialog.get("session_id"),
        transaction_id=dialog.get("transaction_id"),
    )
    owner._runtime_signal(
        acc,
        "disconnect_detected",
        reason_key,
        payload={
            "trigger": "watchdog_popup",
            "detail": f"PID={pid} UI={detail}",
            "reason_msg": f"PID={pid} UI={detail}",
            "popup_code": error_code,
            "popup_confidence": dialog.get("popup_confidence", 0.0),
            "disconnect_category": dialog.get("disconnect_category", ""),
            "visual_disconnect": bool(dialog.get("visual_disconnect", False)),
            "evidence_source": dialog.get("evidence_source", ""),
            "visual_evidence_source": dialog.get("visual_evidence_source", ""),
        },
        expected_runtime_generation=int(dialog.get("runtime_generation") or 0),
        expected_session_id=str(dialog.get("session_id") or ""),
        expected_launch_nonce=str(dialog.get("launch_nonce") or ""),
        expected_transaction_id=str(dialog.get("transaction_id") or ""),
    )


def handle_frozen_recovery_signal(
    owner: Any,
    acc: Any,
    worker: Any,
    pid: Any,
    reason_key: str,
    state: str,
    score: float,
    inactive_for: float,
    suspect_for: float,
    cpu: float,
    ram: float,
    windows: int,
) -> None:
    with acc._lock:
        runtime_generation = acc.runtime_generation
        session_id = acc.session_id
        launch_nonce = acc.launch_nonce
        transaction_id = acc.rejoin_transaction_id
    flog_kv(
        "WATCHDOG",
        "frozen_recovery_signal",
        "warning",
        account=acc.display_name,
        pid=pid,
        reason=reason_key,
        state=state,
        score=f"{score:.1f}",
        inactive=f"{inactive_for:.1f}",
        suspect=f"{suspect_for:.1f}",
        cpu=f"{cpu:.2f}",
        ram=f"{ram:.1f}",
        windows=windows,
    )
    if owner._supervisor:
        owner._supervisor.emit(
            "WatchdogSupervisor",
            "WATCHDOG_TIMEOUT",
            account=acc,
            severity="warning",
            reason=reason_key,
            payload={
                "state": state,
                "score": score,
                "inactive_for": inactive_for,
                "suspect_for": suspect_for,
                "cpu": cpu,
                "ram_mb": ram,
                "windows": windows,
            },
        )
    with acc._lock:
        acc.liveness_state = "frozen"
        acc.last_watchdog_classification = "frozen"
        acc.liveness_suspect_since = 0.0
        acc.last_activity_reason = f"watchdog:{reason_key}"
    flog_kv(
        "WATCHDOG",
        "verified_kill_deferred",
        "warning",
        account=acc.display_name,
        pid=pid,
        reason=reason_key,
        runtime_generation=runtime_generation,
        session_id=session_id,
        transaction_id=transaction_id,
    )
    if worker:
        worker.report_fault(
            reason_key,
            f"PID={pid} state={state} score={score:.1f} inactive={inactive_for:.1f}s",
            expected_runtime_generation=runtime_generation,
            expected_session_id=session_id,
            expected_launch_nonce=launch_nonce,
            expected_transaction_id=transaction_id,
        )
