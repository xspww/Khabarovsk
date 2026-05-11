from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from domain.account_state import AccountState, RuntimeState
from domain.public_state_mapper import runtime_state_for_public


PidValidator = Callable[[Any, int], bool]


def _account_id(acc: Any) -> str:
    return str(getattr(acc, "_config_username", "") or getattr(acc, "username", "") or "")


def _public_state(acc: Any) -> str:
    state = getattr(acc, "state", AccountState.IDLE)
    return str(getattr(state, "name", state) or "")


def _runtime_state(acc: Any, override: Optional[RuntimeState] = None) -> RuntimeState:
    if override is not None:
        return override
    state = getattr(acc, "state", AccountState.IDLE)
    if isinstance(state, AccountState):
        return runtime_state_for_public(state)
    return RuntimeState.STOPPED


def invariant_snapshot(acc: Any, runtime_state: Optional[RuntimeState] = None) -> Dict[str, Any]:
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


def check_runtime_invariants(
    acc: Any,
    runtime_state: Optional[RuntimeState] = None,
    pid_validator: Optional[PidValidator] = None,
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    state = _runtime_state(acc, runtime_state)
    current_time = float(now if now is not None else time.time())
    violations: List[Dict[str, Any]] = []
    pid = getattr(acc, "pid", None)
    binding_status = str(getattr(acc, "process_binding_status", "") or "")
    recovery_inflight = bool(getattr(acc, "recovery_inflight", False))
    recovery_generation = int(getattr(acc, "recovery_generation", 0) or 0)
    cooldown_until = float(getattr(acc, "cooldown_until", 0.0) or 0.0)
    command_id = str(getattr(acc, "current_command_id", "") or "")
    command_name = str(getattr(acc, "current_command", "") or "")
    command_started_at = float(getattr(acc, "command_inflight_started_at", 0.0) or 0.0)

    if state == RuntimeState.RUNNING:
        if not pid:
            violations.append({"code": "running_without_pid", "severity": "critical"})
        elif binding_status in {"", "unbound", "released"}:
            violations.append({"code": "running_without_verified_binding", "severity": "critical", "binding_status": binding_status})
        elif pid_validator is not None:
            try:
                if not pid_validator(acc, int(pid)):
                    violations.append({"code": "running_pid_not_live", "severity": "critical", "pid": pid})
            except Exception as exc:
                violations.append({"code": "pid_validator_failed", "severity": "warning", "error": str(exc)})

    if state == RuntimeState.STOPPED:
        if pid:
            violations.append({"code": "stopped_with_pid", "severity": "warning", "pid": pid})
        if recovery_inflight:
            violations.append({"code": "stopped_with_recovery_inflight", "severity": "warning"})
        if cooldown_until > current_time:
            violations.append({"code": "stopped_with_cooldown", "severity": "warning", "cooldown_until": cooldown_until})

    if state == RuntimeState.RECOVERING and (not recovery_inflight or recovery_generation <= 0):
        violations.append({"code": "recovering_without_recovery_owner", "severity": "critical"})

    if state == RuntimeState.BACKOFF and cooldown_until <= current_time:
        violations.append({"code": "backoff_without_cooldown", "severity": "critical", "cooldown_until": cooldown_until})

    if command_id and (not command_name or command_started_at <= 0.0):
        violations.append({"code": "command_owner_incomplete", "severity": "warning", "command_id": command_id})
    if command_name and not command_id:
        violations.append({"code": "command_name_without_owner", "severity": "warning", "command": command_name})

    return violations

