"""Rejoin transaction lifecycle helpers.

RuntimeStateManager remains the single public writer boundary; this module keeps
transaction snapshot construction and audit events out of the broader state
transition manager.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional

from domain.session_identity import create_rejoin_transaction, create_session_identity


Emit = Callable[..., None]


def _display_name(acc: Any) -> str:
    return str(getattr(acc, "display_name", getattr(acc, "username", "")))


def begin_rejoin_transaction(
    acc: Any,
    reason: str,
    launch_intent: Optional[Dict[str, Any]] = None,
    emit: Optional[Emit] = None,
) -> Dict[str, Any]:
    account_id = str(getattr(acc, "_config_username", "") or getattr(acc, "username", "") or "")
    identity = create_session_identity(
        account_id=account_id,
        runtime_generation=int(getattr(acc, "runtime_generation", 0) or 0),
        account_runtime_id="",
        reason=reason,
        launch_intent=launch_intent or {},
    )
    tx = create_rejoin_transaction(
        identity,
        recovery_generation=int(getattr(acc, "recovery_generation", 0) or 0),
        command_generation=int(getattr(acc, "command_generation", 0) or 0),
        reason=reason,
    )
    acc.account_runtime_id = identity.account_runtime_id
    acc.session_id = identity.session_id
    acc.launch_nonce = identity.launch_nonce
    acc.rejoin_transaction_id = tx.transaction_id
    acc.launch_intent = dict(launch_intent or {})
    acc.server_validation = "intent_recorded"
    acc.scheduler_slot = "reserved"
    acc.supervisor_state = "transaction_pending"
    acc.last_transaction_status = tx.status
    acc.last_transaction_step = tx.step
    acc.last_transaction_reason = reason
    acc.last_transaction_started_at = tx.created_at
    acc.last_transaction_completed_at = 0.0
    acc.last_transaction_failure_reason = ""
    acc.session_started_at = identity.created_at
    acc.last_transaction_at = tx.created_at
    acc.sync_runtime(reason or "begin_rejoin_transaction")
    snapshot = tx.snapshot()
    snapshot["session"] = identity.snapshot()
    if emit:
        emit(
            "RUNTIME",
            "rejoin_transaction_begin",
            account=_display_name(acc),
            transaction_id=tx.transaction_id,
            session_id=identity.session_id,
            launch_nonce=identity.launch_nonce,
            account_runtime_id=identity.account_runtime_id,
            runtime_generation=getattr(acc, "runtime_generation", 0),
            recovery_generation=getattr(acc, "recovery_generation", 0),
            command_generation=getattr(acc, "command_generation", 0),
            reason=reason,
        )
        emit(
            "RUNTIME",
            "transaction_begin",
            account=_display_name(acc),
            transaction_id=tx.transaction_id,
            session_id=identity.session_id,
            runtime_generation=getattr(acc, "runtime_generation", 0),
            recovery_generation=getattr(acc, "recovery_generation", 0),
            command_generation=getattr(acc, "command_generation", 0),
            reason=reason,
            thread=threading.current_thread().name,
        )
    return snapshot


def update_rejoin_transaction(
    acc: Any,
    status: str = "",
    step: str = "",
    reason: str = "",
    server_validation: str = "",
    scheduler_slot: str = "",
    emit: Optional[Emit] = None,
) -> Dict[str, Any]:
    if status:
        acc.last_transaction_status = status
    if step:
        acc.last_transaction_step = step
    if reason:
        acc.last_transaction_reason = reason
    if server_validation:
        acc.server_validation = server_validation
        acc.destination_validation = server_validation
    if scheduler_slot:
        acc.scheduler_slot = scheduler_slot
    if step:
        acc.supervisor_state = step
    acc.last_transaction_at = time.time()
    if status in {"committed", "rolled_back", "failed"}:
        acc.last_transaction_completed_at = acc.last_transaction_at
    if status in {"rolled_back", "failed"} and reason:
        acc.last_transaction_failure_reason = reason
    acc.sync_runtime(reason or step or status or "transaction_update")
    snapshot = {
        "transaction_id": getattr(acc, "rejoin_transaction_id", ""),
        "account_id": getattr(acc, "_config_username", getattr(acc, "username", "")),
        "runtime_generation": getattr(acc, "runtime_generation", 0),
        "recovery_generation": getattr(acc, "recovery_generation", 0),
        "command_generation": getattr(acc, "command_generation", 0),
        "account_runtime_id": getattr(acc, "account_runtime_id", ""),
        "session_id": getattr(acc, "session_id", ""),
        "launch_nonce": getattr(acc, "launch_nonce", ""),
        "status": status or getattr(acc, "last_transaction_status", ""),
        "step": step or getattr(acc, "last_transaction_step", "") or getattr(acc, "supervisor_state", ""),
        "reason": reason or getattr(acc, "last_transaction_reason", ""),
        "failure_reason": getattr(acc, "last_transaction_failure_reason", "") if (status in {"rolled_back", "failed"} or getattr(acc, "last_transaction_status", "") in {"rolled_back", "failed"}) else "",
        "launch_intent": getattr(acc, "launch_intent", {}) or {},
        "destination_evidence": {},
        "created_at": getattr(acc, "session_started_at", 0.0) or time.time(),
        "updated_at": getattr(acc, "last_transaction_at", 0.0) or time.time(),
        "completed_at": getattr(acc, "last_transaction_at", 0.0) if status in {"committed", "rolled_back", "failed"} else 0.0,
    }
    if emit:
        emit(
            "RUNTIME",
            "rejoin_transaction_update",
            account=_display_name(acc),
            transaction_id=snapshot["transaction_id"],
            session_id=snapshot["session_id"],
            status=snapshot["status"],
            step=snapshot["step"],
            reason=snapshot["reason"],
            runtime_generation=snapshot["runtime_generation"],
            recovery_generation=snapshot["recovery_generation"],
            command_generation=snapshot["command_generation"],
        )
        emit(
            "RUNTIME",
            "transaction_step",
            account=_display_name(acc),
            transaction_id=snapshot["transaction_id"],
            session_id=snapshot["session_id"],
            status=snapshot["status"],
            step=snapshot["step"],
            reason=snapshot["reason"],
            server_validation=getattr(acc, "server_validation", ""),
            runtime_generation=snapshot["runtime_generation"],
            recovery_generation=snapshot["recovery_generation"],
            command_generation=snapshot["command_generation"],
            thread=threading.current_thread().name,
        )
    return snapshot


def finish_rejoin_transaction(
    acc: Any,
    status: str,
    reason: str,
    destination_evidence: Optional[Dict[str, Any]] = None,
    server_validation: str = "",
    emit: Optional[Emit] = None,
) -> Dict[str, Any]:
    final_status = status or "failed"
    acc.last_transaction_status = final_status
    acc.last_transaction_reason = reason
    acc.last_transaction_at = time.time()
    acc.last_transaction_completed_at = acc.last_transaction_at
    if final_status == "committed":
        acc.scheduler_slot = ""
        acc.supervisor_state = "running"
        acc.last_transaction_step = "committed"
        acc.last_transaction_failure_reason = ""
        acc.server_validation = server_validation or "intent_verified_no_destination_signal"
        acc.destination_validation = acc.server_validation
    elif final_status == "rolled_back":
        acc.scheduler_slot = ""
        acc.supervisor_state = "rolled_back"
        acc.last_transaction_step = "rolled_back"
        acc.last_transaction_failure_reason = reason
        acc.server_validation = server_validation or "unverified_rollback"
        acc.destination_validation = acc.server_validation
    else:
        acc.scheduler_slot = ""
        acc.supervisor_state = "failed"
        acc.last_transaction_step = "failed"
        acc.last_transaction_failure_reason = reason
        acc.server_validation = server_validation or "failed"
        acc.destination_validation = acc.server_validation
    acc.sync_runtime(reason or final_status)
    snapshot = {
        "transaction_id": getattr(acc, "rejoin_transaction_id", ""),
        "account_id": getattr(acc, "_config_username", getattr(acc, "username", "")),
        "runtime_generation": getattr(acc, "runtime_generation", 0),
        "recovery_generation": getattr(acc, "recovery_generation", 0),
        "command_generation": getattr(acc, "command_generation", 0),
        "account_runtime_id": getattr(acc, "account_runtime_id", ""),
        "session_id": getattr(acc, "session_id", ""),
        "launch_nonce": getattr(acc, "launch_nonce", ""),
        "status": final_status,
        "step": acc.last_transaction_step,
        "reason": reason,
        "failure_reason": acc.last_transaction_failure_reason,
        "launch_intent": getattr(acc, "launch_intent", {}) or {},
        "destination_evidence": dict(destination_evidence or {}),
        "created_at": getattr(acc, "session_started_at", 0.0) or time.time(),
        "updated_at": acc.last_transaction_at,
        "completed_at": acc.last_transaction_at,
    }
    if emit:
        emit(
            "RUNTIME",
            "rejoin_transaction_finish",
            account=_display_name(acc),
            transaction_id=snapshot["transaction_id"],
            session_id=snapshot["session_id"],
            status=final_status,
            reason=reason,
            server_validation=acc.server_validation,
            runtime_generation=snapshot["runtime_generation"],
            recovery_generation=snapshot["recovery_generation"],
            command_generation=snapshot["command_generation"],
        )
        emit(
            "RUNTIME",
            "transaction_commit" if final_status == "committed" else "transaction_rollback",
            account=_display_name(acc),
            transaction_id=snapshot["transaction_id"],
            session_id=snapshot["session_id"],
            status=final_status,
            step=snapshot["step"],
            reason=reason,
            failure_reason=snapshot["failure_reason"],
            server_validation=acc.server_validation,
            runtime_generation=snapshot["runtime_generation"],
            recovery_generation=snapshot["recovery_generation"],
            command_generation=snapshot["command_generation"],
            thread=threading.current_thread().name,
        )
    return snapshot
