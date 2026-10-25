from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, NamedTuple

from domain._util import account_key as _account_id
from domain.states import AccountState, RuntimeState, runtime_state_for_public
from services.process_proof_policy import PROOF_STRONG, is_at_least_process_proof

PidValidator = Callable[[Any, int], bool]


def _public_state(acc: Any) -> str:
    state = getattr(acc, "state", AccountState.IDLE)
    return str(getattr(state, "name", state) or "")


def _runtime_state(acc: Any, override: RuntimeState | None = None) -> RuntimeState:
    if override is not None:
        return override
    state = getattr(acc, "state", AccountState.IDLE)
    if isinstance(state, AccountState):
        return runtime_state_for_public(state)
    return RuntimeState.STOPPED


def invariant_snapshot(acc: Any, runtime_state: RuntimeState | None = None) -> dict[str, Any]:
    return {
        "account_id": _account_id(acc),
        "runtime_state": (runtime_state or _runtime_state(acc)).value,
        "public_state": _public_state(acc),
        "pid": getattr(acc, "pid", None),
        "process_identity": getattr(acc, "bound_process_identity", ""),
        "process_binding_status": getattr(acc, "process_binding_status", ""),
        "runtime_generation": int(getattr(acc, "runtime_generation", 0) or 0),
        "recovery_generation": int(getattr(acc, "recovery_generation", 0) or 0),
        "command_generation": int(getattr(acc, "command_generation", 0) or 0),
        "recovery_inflight": bool(getattr(acc, "recovery_inflight", False)),
        "recovery_status": str(getattr(acc, "recovery_status", "") or ""),
        "cooldown_until": float(getattr(acc, "cooldown_until", 0.0) or 0.0),
        "current_command_id": str(getattr(acc, "current_command_id", "") or ""),
        "current_command": str(getattr(acc, "current_command", "") or ""),
        "command_inflight_started_at": float(getattr(acc, "command_inflight_started_at", 0.0) or 0.0),
    }


class _InvariantFields(NamedTuple):
    pid: Any
    binding_status: str
    recovery_inflight: bool
    recovery_generation: int
    cooldown_until: float
    command_id: str
    command_name: str
    command_started_at: float


def _invariant_fields(acc: Any) -> _InvariantFields:
    return _InvariantFields(
        pid=getattr(acc, "pid", None),
        binding_status=str(getattr(acc, "process_binding_status", "") or ""),
        recovery_inflight=bool(getattr(acc, "recovery_inflight", False)),
        recovery_generation=int(getattr(acc, "recovery_generation", 0) or 0),
        cooldown_until=float(getattr(acc, "cooldown_until", 0.0) or 0.0),
        command_id=str(getattr(acc, "current_command_id", "") or ""),
        command_name=str(getattr(acc, "current_command", "") or ""),
        command_started_at=float(
            getattr(acc, "command_inflight_started_at", 0.0) or 0.0
        ),
    )


def _running_violations(
    acc: Any,
    state: RuntimeState,
    fields: _InvariantFields,
    pid_validator: PidValidator | None,
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    if state != RuntimeState.RUNNING:
        return violations
    if not fields.pid:
        violations.append({
            "code": "running_without_pid",
            "severity": "critical",
        })
        return violations
    if fields.binding_status in {"", "unbound", "released"}:
        violations.append({
            "code": "running_without_verified_binding",
            "severity": "critical",
            "binding_status": fields.binding_status,
        })
        return violations
    if not is_at_least_process_proof(
        getattr(acc, "process_proof_level", ""),
        PROOF_STRONG,
    ):
        violations.append({
            "code": "running_without_strong_process_proof",
            "severity": "critical",
            "process_proof_level": getattr(acc, "process_proof_level", ""),
        })
        return violations
    if pid_validator is not None:
        try:
            if not pid_validator(acc, int(fields.pid)):
                violations.append({
                    "code": "running_pid_not_live",
                    "severity": "critical",
                    "pid": fields.pid,
                })
        except Exception as exc:
            violations.append({
                "code": "pid_validator_failed",
                "severity": "warning",
                "error": str(exc),
            })
    return violations


def _stopped_violations(
    state: RuntimeState,
    fields: _InvariantFields,
    current_time: float,
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    if state != RuntimeState.STOPPED:
        return violations
    if fields.pid:
        violations.append({
            "code": "stopped_with_pid",
            "severity": "warning",
            "pid": fields.pid,
        })
    if fields.recovery_inflight:
        violations.append({
            "code": "stopped_with_recovery_inflight",
            "severity": "warning",
        })
    if fields.cooldown_until > current_time:
        violations.append({
            "code": "stopped_with_cooldown",
            "severity": "warning",
            "cooldown_until": fields.cooldown_until,
        })
    return violations


def _recovering_violations(
    state: RuntimeState,
    fields: _InvariantFields,
) -> list[dict[str, Any]]:
    if (
        state == RuntimeState.RECOVERING
        and (not fields.recovery_inflight or fields.recovery_generation <= 0)
    ):
        return [{
            "code": "recovering_without_recovery_owner",
            "severity": "critical",
        }]
    return []


def _backoff_violations(
    state: RuntimeState,
    fields: _InvariantFields,
    current_time: float,
) -> list[dict[str, Any]]:
    if state == RuntimeState.BACKOFF and fields.cooldown_until <= current_time:
        return [{
            "code": "backoff_without_cooldown",
            "severity": "critical",
            "cooldown_until": fields.cooldown_until,
        }]
    return []


def _command_violations(fields: _InvariantFields) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    if fields.command_id and (
        not fields.command_name or fields.command_started_at <= 0.0
    ):
        violations.append({
            "code": "command_owner_incomplete",
            "severity": "warning",
            "command_id": fields.command_id,
        })
    if fields.command_name and not fields.command_id:
        violations.append({
            "code": "command_name_without_owner",
            "severity": "warning",
            "command": fields.command_name,
        })
    return violations


def check_runtime_invariants(
    acc: Any,
    runtime_state: RuntimeState | None = None,
    pid_validator: PidValidator | None = None,
    now: float | None = None,
) -> list[dict[str, Any]]:
    state = _runtime_state(acc, runtime_state)
    current_time = float(now if now is not None else time.time())
    fields = _invariant_fields(acc)
    violations = _running_violations(acc, state, fields, pid_validator)
    violations.extend(_stopped_violations(state, fields, current_time))
    violations.extend(_recovering_violations(state, fields))
    violations.extend(_backoff_violations(state, fields, current_time))
    violations.extend(_command_violations(fields))
    return violations
