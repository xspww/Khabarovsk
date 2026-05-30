from __future__ import annotations

import inspect
import sys
import threading
from typing import Any, Callable, Dict, Optional

from domain.account_state import AccountState, RuntimeState
from domain.public_state_mapper import runtime_state_for_public
from domain.runtime_lifecycle import lifecycle_for_public, lifecycle_for_runtime_state
from runtime.runtime_invariants import check_runtime_invariants, invariant_snapshot


Logger = Callable[..., None]


def emit(logger: Optional[Logger], scope: str, event: str, level: str = "info", **fields: Any) -> None:
    if not logger:
        return
    thread_name = threading.current_thread().name
    fields.setdefault("event_type", event)
    fields.setdefault("thread_name", thread_name)
    fields.setdefault("thread", thread_name)
    if "account_id" not in fields and fields.get("account"):
        fields["account_id"] = fields.get("account")
    if "PID" not in fields and "pid" in fields:
        fields["PID"] = fields.get("pid")
    if "pid" not in fields and "PID" in fields:
        fields["pid"] = fields.get("PID")
    try:
        logger(scope, event, level, **fields)
    except TypeError:
        logger(scope, event, **fields)
    except Exception as exc:
        print(f"[RUNTIME] logger_failed event={event} error={exc}", file=sys.stderr)


def caller() -> str:
    frame = inspect.stack()[2]
    return f"{frame.function}:{frame.lineno}"


def account_name(acc: Any) -> str:
    return str(
        getattr(acc, "_config_username", "")
        or getattr(acc, "display_name", "")
        or getattr(acc, "username", "")
        or ""
    )


def runtime_log_fields(acc: Any, reason: str = "", **fields: Any) -> Dict[str, Any]:
    state = getattr(acc, "state", None)
    runtime_state = ""
    public_state = ""
    if isinstance(state, AccountState):
        runtime_state = runtime_state_for_public(state).value
        public_state = state.name
        canonical_runtime_state = lifecycle_for_public(state).value
    else:
        public_state = str(getattr(state, "name", state or ""))
        try:
            canonical_runtime_state = lifecycle_for_runtime_state(RuntimeState(str(state))).value
        except Exception:
            canonical_runtime_state = ""
    payload = {
        "account": getattr(acc, "display_name", getattr(acc, "username", "")),
        "account_id": account_name(acc),
        "runtime_generation": getattr(acc, "runtime_generation", 0),
        "recovery_generation": getattr(acc, "recovery_generation", 0),
        "command_generation": getattr(acc, "command_generation", 0),
        "runtime_state": runtime_state,
        "canonical_runtime_state": canonical_runtime_state,
        "public_state": public_state,
        "PID": getattr(acc, "pid", None),
        "pid": getattr(acc, "pid", None),
        "reason": reason,
    }
    payload.update(fields)
    return payload


def emit_invariant_violations(
    logger: Optional[Logger],
    acc: Any,
    reason: str,
    runtime_state: Optional[RuntimeState] = None,
) -> bool:
    violations = check_runtime_invariants(acc, runtime_state=runtime_state)
    if not violations:
        return True
    snapshot = invariant_snapshot(acc, runtime_state=runtime_state)
    for violation in violations:
        emit(
            logger,
            "RUNTIME",
            "invariant_violation",
            "warning" if violation.get("severity") != "critical" else "error",
            **runtime_log_fields(
                acc,
                reason=reason,
                lifecycle_owner="runtime_state_manager",
                violation=violation.get("code", ""),
                violation_detail=violation,
                snapshot=snapshot,
            ),
        )
    return False


def transition_invariant_blockers(
    logger: Optional[Logger],
    acc: Any,
    new_runtime: RuntimeState,
    reason: str,
) -> list:
    violations = check_runtime_invariants(acc, runtime_state=new_runtime)
    hard_codes = {
        "running_without_pid",
        "running_without_verified_binding",
        "running_without_strong_process_proof",
        "running_pid_not_live",
        "recovering_without_recovery_owner",
        "backoff_without_cooldown",
        "command_owner_incomplete",
        "command_name_without_owner",
    }
    blockers = [item for item in violations if item.get("code") in hard_codes]
    if blockers:
        emit_invariant_violations(logger, acc, reason or "transition_invariant_rejected", runtime_state=new_runtime)
    return blockers


def snapshot_account_runtime(logger: Optional[Logger], acc: Any) -> Dict[str, Any]:
    lock = getattr(acc, "_lock", None)
    try:
        if lock is not None:
            with lock:
                return dict(acc.runtime_snapshot())
        return dict(acc.runtime_snapshot())
    except Exception as exc:
        emit(
            logger,
            "RUNTIME",
            "snapshot_failed",
            "warning",
            account=getattr(acc, "display_name", getattr(acc, "username", "")),
            account_id=account_name(acc),
            error=str(exc),
        )
        return {
            "account_id": getattr(acc, "_config_username", getattr(acc, "username", "")),
            "state": getattr(getattr(acc, "state", None), "name", getattr(acc, "state", "")),
            "pid": getattr(acc, "pid", None),
            "runtime_generation": getattr(acc, "runtime_generation", 0),
            "recovery_generation": getattr(acc, "recovery_generation", 0),
            "command_generation": getattr(acc, "command_generation", 0),
        }
