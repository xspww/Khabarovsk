"""Roblox process ownership boundary."""

from __future__ import annotations

import os
import threading
from typing import Any, NamedTuple

from core import flog_kv
from domain._util import identity_matches as _identity_evidence
from domain._util import int_or_zero as _int_or_zero
from services.browser_tracker import tracker_matches
from services.process_account_runtime import (
    account_browser_tracker_id as _account_browser_tracker_id,
)
from services.process_account_runtime import (
    account_key as _account_key,
)
from services.process_account_runtime import (
    account_name as _account_name,
)
from services.process_account_runtime import (
    mark_account_process_proof as _mark_account_process_proof,
)
from services.process_account_runtime import (
    process_log_fields as _process_log_fields,
)
from services.process_account_runtime import (
    quarantine_process_match as _quarantine_process_match,
)
from services.process_account_runtime import (
    runtime_generation_matches as _runtime_generation_matches,
)
from services.process_account_runtime import (
    set_adopt_diagnostics as _set_adopt_diagnostics,
)
from services.process_account_runtime import (
    set_process_diagnostics as _set_process_diagnostics,
)
from services.process_backend import ProcessManager as _ProcessBackend
from services.process_proof_policy import (
    PROOF_UNTRUSTED,
    allows_destructive_process_action,
    classify_process_proof,
    process_proof_allowed_for_state,
    required_process_proof_for_state,
)
from services.resource_monitor import get_rt_monitor


def _lower_path(value: Any) -> str:
    return os.path.normcase(os.path.abspath(str(value or ""))) if value else ""


def _tracker_evidence(expected_browser_tracker_id: str, observed_tracker: str) -> bool:
    return bool(tracker_matches(expected_browser_tracker_id, observed_tracker))


def _owner_evidence(owner_key: str, owner: str) -> bool:
    return bool(owner_key and owner and owner == owner_key)


def _creation_evidence(created: float, launched_after: float | None) -> bool:
    return bool(launched_after is not None and created and created >= (float(launched_after) - 3.0))


def _has_binding_evidence(
    validation: dict[str, Any],
    owner_key: str,
    expected_identity: str,
    launched_after: float | None,
    expected_browser_tracker_id: str = "",
) -> bool:
    """Single owner for the binding-evidence predicate (binding decision leverages this)."""
    owner = str(validation.get("owner") or "")
    identity = str(validation.get("identity") or "")
    created = float(validation.get("created") or 0.0)
    observed_tracker = str(validation.get("browser_tracker_id") or "")
    if _tracker_evidence(expected_browser_tracker_id, observed_tracker):
        return True
    if _identity_evidence(expected_identity, identity):
        return True
    if _owner_evidence(owner_key, owner):
        return True
    if _creation_evidence(created, launched_after):
        return True
    return False


def _pid_reasons(pid: int | None) -> list[str]:
    return [] if pid else ["missing_pid"]


def _generation_reasons(
    expected_runtime_generation: int | None,
    current_runtime_generation: int | None,
) -> list[str]:
    if expected_runtime_generation is None or current_runtime_generation is None:
        return []
    if int(expected_runtime_generation) != int(current_runtime_generation):
        return ["runtime_generation_mismatch"]
    return []


def _identity_reasons(result: dict[str, Any], expected_identity: str) -> list[str]:
    identity = str(result.get("identity") or "")
    if expected_identity and identity and identity != str(expected_identity):
        return ["identity_mismatch"]
    return []


def _creation_reasons(result: dict[str, Any], launched_after: float | None) -> list[str]:
    created = float(result.get("created") or result.get("create_time") or 0.0)
    if not created:
        return ["missing_create_time"]
    if launched_after is not None and created < (float(launched_after) - 5.0):
        return ["stale_pid_reuse"]
    return []


def _executable_reasons(result: dict[str, Any], expected_executable_hint: str) -> list[str]:
    if not expected_executable_hint:
        return []
    name = str(result.get("name") or result.get("exe") or "")
    exe = _lower_path(result.get("exe") or result.get("path") or "")
    hint = str(expected_executable_hint).lower()
    if hint not in name.lower() and hint not in exe.lower():
        return ["wrong_executable"]
    return []


def _owner_reasons(result: dict[str, Any], owner_key: str) -> list[str]:
    owner = str(result.get("owner") or "")
    if owner and owner_key and owner != owner_key:
        return ["owner_mismatch"]
    return []


def _validate_process_ownership(
    validation: dict[str, Any],
    *,
    pid: int | None,
    owner_key: str,
    expected_identity: str = "",
    launched_after: float | None = None,
    expected_runtime_generation: int | None = None,
    current_runtime_generation: int | None = None,
    expected_executable_hint: str = "RobloxPlayerBeta.exe",
) -> dict[str, Any]:
    """Single owner for the ownership predicate (binding decision leverages this)."""
    result = dict(validation or {})
    reasons = (
        _pid_reasons(pid)
        + _generation_reasons(expected_runtime_generation, current_runtime_generation)
        + _identity_reasons(result, expected_identity)
        + _creation_reasons(result, launched_after)
        + _executable_reasons(result, expected_executable_hint)
        + _owner_reasons(result, owner_key)
    )
    if reasons:
        result["ok"] = False
        result["reason"] = reasons[0]
        result["ownership_reasons"] = reasons
    else:
        result.setdefault("ownership_reasons", [])
    return result


def _resolve_expected_identity(account: Any, pid: int | None, expected_identity: str | None) -> str:
    if expected_identity is not None:
        return str(expected_identity)
    try:
        current_pid = int(getattr(account, "pid", 0) or 0)
        if pid and current_pid == int(pid):
            return str(getattr(account, "bound_process_identity", "") or "")
        return ""
    except Exception:
        return ""


def _require_window(validation: dict[str, Any], require_window: bool) -> dict[str, Any]:
    if not validation.get("ok") or not require_window or int(validation.get("windows") or 0) > 0:
        return validation
    validation = dict(validation)
    validation["ok"] = False
    validation["reason"] = "window_required"
    return validation


def _require_binding_evidence(
    validation: dict[str, Any],
    owner_key: str,
    expected_identity: str,
    launched_after: float | None,
    expected_browser_tracker_id: str,
) -> dict[str, Any]:
    if not validation.get("ok"):
        return validation
    if _has_binding_evidence(validation, owner_key, expected_identity, launched_after, expected_browser_tracker_id):
        return validation
    validation = dict(validation)
    validation["ok"] = False
    validation["reason"] = "unclaimed_process"
    return validation


def _apply_ownership_validation(
    validation: dict[str, Any],
    account: Any,
    pid: int | None,
    owner_key: str,
    expected_identity: str,
    launched_after: float | None,
) -> dict[str, Any]:
    if not validation.get("ok"):
        return validation
    return _validate_process_ownership(
        validation,
        pid=pid,
        owner_key=owner_key,
        expected_identity=expected_identity,
        launched_after=launched_after,
        current_runtime_generation=int(getattr(account, "runtime_generation", 0) or 0),
    )


def _apply_process_proof(
    validation: dict[str, Any],
    account: Any,
    *,
    owner_key: str,
    expected_identity: str,
    launched_after: float | None,
    expected_browser_tracker_id: str,
    reason: str,
) -> dict[str, Any]:
    proof = classify_process_proof(
        validation,
        owner_key=owner_key,
        expected_identity=expected_identity,
        expected_browser_tracker_id=expected_browser_tracker_id,
        launched_after=launched_after,
        current_process_proof_level=str(getattr(account, "process_proof_level", "") or ""),
    )
    validation.update(proof)
    validation["required_process_proof"] = required_process_proof_for_state(getattr(account, "state", ""), destructive=False)
    if not validation.get("ok"):
        return validation
    if process_proof_allowed_for_state(validation.get("process_proof_level"), getattr(account, "state", ""), destructive=False):
        return validation
    validation = dict(validation)
    validation["ok"] = False
    validation["reason"] = "process_proof_insufficient"
    validation["process_reject_reason"] = "process_proof_insufficient"
    _quarantine_process_match(account, reason or "validate_binding", validation, proof)
    return validation


def _finalize_binding_validation(validation: dict[str, Any], account: Any, pid: int | None, reason: str) -> dict[str, Any]:
    validation["binding_decision"] = "verified" if validation.get("ok") else "rejected"
    validation["binding_confidence"] = float(validation.get("confidence") or 0.0)
    validation["process_reject_reason"] = "" if validation.get("ok") else str(validation.get("reason", "") or "")
    validation["process_owner_claim"] = str(validation.get("owner") or _ProcessBackend.get_pid_owner(pid) or "")
    _set_process_diagnostics(
        account,
        validation["binding_decision"],
        validation["binding_confidence"],
        validation["process_reject_reason"],
        validation["process_owner_claim"],
        proof_level=validation.get("process_proof_level", ""),
        proof_reason=validation.get("process_proof_reason", ""),
        reason=reason or "validate_binding",
    )
    return validation


def _should_log_binding(validation: dict[str, Any], log_success: bool, log_failure: bool) -> bool:
    if validation.get("ok"):
        return bool(log_success)
    return bool(log_failure)


def _log_binding_result(
    account: Any,
    pid: int | None,
    reason: str,
    validation: dict[str, Any],
    expected_identity: str,
    expected_browser_tracker_id: str,
    windows: int,
    owner_key: str,
) -> None:
    ok = bool(validation.get("ok"))
    flog_kv(
        "PROC",
        "process_bind_verified" if ok else "process_bind_rejected",
        "info" if ok else "warning",
        account=_account_name(account),
        pid=pid or "",
        reason=reason,
        process_action="validate_binding",
        reject="" if ok else validation.get("reason", ""),
        identity=validation.get("identity", ""),
        expected_identity=expected_identity,
        expected_browser_tracker_id=expected_browser_tracker_id,
        owner=validation.get("owner", ""),
        owner_key=owner_key,
        confidence=validation.get("confidence", 0.0),
        process_proof_level=validation.get("process_proof_level", ""),
        required_process_proof=validation.get("required_process_proof", ""),
        binding_decision=validation.get("binding_decision", ""),
        process_reject_reason=validation.get("process_reject_reason", ""),
        process_owner_claim=validation.get("process_owner_claim", ""),
        browser_tracker_id=validation.get("browser_tracker_id", ""),
        confidence_level=validation.get("confidence_level", ""),
        windows=windows,
        hwnd=validation.get("hwnd", 0),
        **_process_log_fields(account),
        thread=threading.current_thread().name,
    )


def _state_manager_unavailable(account: Any, pid: int | None, reason: str) -> dict[str, Any]:
    flog_kv(
        "PROC",
        "process_bind_rejected",
        "warning",
        account=_account_name(account),
        pid=pid or "",
        reason=reason,
        reject="state_manager_unavailable",
    )
    return {"ok": False, "pid": pid, "reason": "state_manager_unavailable", "validation": {}}


def _log_bind_attempt(account: Any, pid: int | None, reason: str, expected_identity: str | None) -> None:
    flog_kv(
        "PROC",
        "process_bind_attempt",
        account=_account_name(account),
        pid=pid or "",
        reason=reason,
        process_action="bind_account_process",
        expected_identity=expected_identity or "",
        expected_browser_tracker_id=_account_browser_tracker_id(account),
        **_process_log_fields(account),
    )


def _log_process_bound(
    account: Any,
    pid: int | None,
    old_pid: Any,
    reason: str,
    identity: str,
    confidence: float,
    proof_level: str,
) -> None:
    flog_kv(
        "PROC",
        "process_bound",
        account=_account_name(account),
        pid=pid,
        old_pid=old_pid or "",
        reason=reason,
        process_action="bind_account_process",
        identity=identity,
        confidence=confidence,
        process_proof_level=proof_level,
        binding_decision="verified",
        process_owner_claim=_account_key(account),
        **_process_log_fields(account),
    )


class _BindingFacts(NamedTuple):
    identity: str
    name: str
    confidence: float
    proof_level: str


def _binding_facts(validation: dict[str, Any], pid: int | None, process_name: str) -> _BindingFacts:
    identity = str(validation.get("identity") or _ProcessBackend.get_process_identity(pid))
    name = process_name or str(validation.get("name") or "RobloxPlayerBeta.exe")
    confidence = float(validation.get("confidence") or 100.0)
    proof_level = str(validation.get("process_proof_level") or PROOF_UNTRUSTED)
    return _BindingFacts(identity, name, confidence, proof_level)


def _apply_account_binding(
    account: Any,
    pid: int | None,
    state_manager: Any,
    validation: dict[str, Any],
    process_name: str,
    increment_generation: bool,
    reason: str,
) -> dict[str, Any]:
    old_pid = getattr(account, "pid", None)
    facts = _binding_facts(validation, pid, process_name)
    if old_pid and int(old_pid) != int(pid):
        _ProcessBackend.evict_pid_cache(old_pid)
    state_manager.bind_process(
        account,
        int(pid),
        facts.identity,
        status="verified",
        process_name=facts.name,
        confidence=facts.confidence,
        process_proof_level=facts.proof_level,
        reason=reason or "bind_account_process",
        increment_generation=increment_generation,
    )
    _ProcessBackend.claim_pid_owner(pid, _account_key(account))
    get_rt_monitor().register(int(pid))
    _set_process_diagnostics(
        account,
        "verified",
        facts.confidence,
        "",
        _account_key(account),
        proof_level=facts.proof_level,
        proof_reason=str(validation.get("process_proof_reason") or ""),
        reason=reason or "bind_account_process",
    )
    _log_process_bound(account, pid, old_pid, reason, facts.identity, facts.confidence, facts.proof_level)
    return {"ok": True, "pid": int(pid), "identity": facts.identity, "name": facts.name, "validation": validation}


def _desired_state_name(account: Any) -> str:
    desired = getattr(account, "desired_state", "")
    return str(getattr(desired, "name", desired) or "").upper()


def _adopt_desired_state_rejection(account: Any) -> dict[str, Any] | None:
    desired_name = _desired_state_name(account)
    if desired_name and desired_name not in {"IN_GAME", "RUNNING"}:
        _set_adopt_diagnostics(account, [], reject_reason="desired_state_not_in_game")
        return {"ok": False, "reason": "desired_state_not_in_game", "pid": None, "live": []}
    return None


def _visible_candidates(min_ram_mb: float) -> tuple[list[Any], list[Any]]:
    live = _ProcessBackend.list_live_game_processes(launched_after=None)
    visible = [
        item
        for item in live
        if int(item.get("windows") or 0) > 0 or float(item.get("rss_mb") or 0.0) >= float(min_ram_mb or 100.0)
    ]
    return live, visible


def _candidate_count_rejection(account: Any, live: list[Any], visible: list[Any]) -> dict[str, Any] | None:
    if not visible:
        _set_adopt_diagnostics(account, live, reject_reason="no_visible_candidate")
        flog_kv(
            "PROC",
            "adopt_visible_rejected",
            "warning",
            account=_account_name(account),
            reason="no_visible_candidate",
            live=len(live),
        )
        return {"ok": False, "reason": "no_visible_candidate", "pid": None, "live": live}
    if len(visible) != 1:
        _set_adopt_diagnostics(account, visible, reject_reason="visible_process_ambiguous")
        flog_kv(
            "PROC",
            "adopt_visible_rejected",
            "warning",
            account=_account_name(account),
            reason="visible_process_ambiguous",
            candidates=len(visible),
        )
        return {"ok": False, "reason": "visible_process_ambiguous", "pid": None, "live": visible}
    return None


def _other_bound_alive(other: Any) -> bool:
    other_pid = _int_or_zero(getattr(other, "pid", 0))
    if not other_pid:
        return False
    try:
        return bool(
            _ProcessBackend.is_bound_game_alive(
                other_pid,
                owner_key=_account_key(other),
                expected_identity=str(getattr(other, "bound_process_identity", "") or ""),
                expected_browser_tracker_id=_account_browser_tracker_id(other),
            )
        )
    except Exception:
        return False


def _ambiguous_live_accounts(account: Any, all_accounts: list[Any]) -> list[str]:
    ambiguous: list[str] = []
    for other in all_accounts:
        if other is account:
            continue
        other_bound_alive = _other_bound_alive(other)
        if _desired_state_name(other) in {"IN_GAME", "RUNNING"} and not other_bound_alive:
            ambiguous.append(_account_name(other))
    return ambiguous


def _ambiguous_accounts_rejection(
    account: Any,
    visible: list[Any],
    pid: int,
    ambiguous_accounts: list[str],
) -> dict[str, Any]:
    _set_adopt_diagnostics(account, visible, candidate_pid=pid, reject_reason="visible_process_ambiguous_accounts")
    flog_kv(
        "PROC",
        "adopt_visible_rejected",
        "warning",
        account=_account_name(account),
        pid=pid,
        reason="visible_process_ambiguous_accounts",
        candidates=",".join(ambiguous_accounts[:6]),
    )
    return {"ok": False, "reason": "visible_process_ambiguous_accounts", "pid": pid, "live": visible}


def _ownership_rejection(
    account: Any,
    all_accounts: list[Any],
    visible: list[Any],
    pid: int,
    owner: str,
    account_key: str,
) -> dict[str, Any] | None:
    if owner and owner != account_key:
        _set_adopt_diagnostics(account, visible, candidate_pid=pid, reject_reason=f"owner_mismatch:{owner}")
        flog_kv(
            "PROC",
            "adopt_visible_rejected",
            "warning",
            account=_account_name(account),
            pid=pid,
            reason="owner_mismatch",
            owner=owner,
        )
        return {"ok": False, "reason": f"owner_mismatch:{owner}", "pid": pid, "live": visible}
    for other in all_accounts:
        if other is account:
            continue
        if _int_or_zero(getattr(other, "pid", 0)) == pid:
            _set_adopt_diagnostics(account, visible, candidate_pid=pid, reject_reason="pid_claimed_by_other_account")
            return {"ok": False, "reason": "pid_claimed_by_other_account", "pid": pid, "live": visible}
        other_owner = _ProcessBackend.get_pid_owner(pid)
        if other_owner and other_owner != account_key:
            _set_adopt_diagnostics(account, visible, candidate_pid=pid, reject_reason=f"owner_mismatch:{other_owner}")
            return {"ok": False, "reason": f"owner_mismatch:{other_owner}", "pid": pid, "live": visible}
    return None


def _candidate_owner(candidate: dict[str, Any], pid: int) -> str:
    owner = str(candidate.get("owner") or "")
    return owner or str(_ProcessBackend.get_pid_owner(pid) or "")


def _adopt_validation(account: Any, pid: int, candidate: dict[str, Any], reason: str) -> dict[str, Any]:
    return ProcessService.validate_binding(
        account,
        pid,
        expected_identity=str(candidate.get("identity") or ""),
        require_window=int(candidate.get("windows") or 0) > 0,
        reason=reason or "safe_adopt_visible_process",
        launched_after=None,
        min_ram_mb=0.0,
    )


def _adopt_binding(
    account: Any,
    state_manager: Any,
    pid: int,
    candidate: dict[str, Any],
    validation: dict[str, Any],
    reason: str,
    expected_runtime_generation: int | None,
) -> dict[str, Any]:
    return ProcessService.bind_account_process(
        account,
        pid,
        state_manager,
        reason=reason or "safe_adopt_visible_process",
        require_window=int(candidate.get("windows") or 0) > 0,
        expected_identity=str(validation.get("identity") or candidate.get("identity") or ""),
        launched_after=None,
        process_name=str(validation.get("name") or candidate.get("name") or "RobloxPlayerBeta.exe"),
        min_ram_mb=0.0,
        increment_generation=False,
        expected_runtime_generation=expected_runtime_generation,
    )


def _finish_adoption(
    account: Any,
    state_manager: Any,
    pid: int,
    validation: dict[str, Any],
    reason: str,
    account_key: str,
    visible: list[Any],
) -> dict[str, Any]:
    reason_text = reason or "safe_adopt_visible_process"
    if hasattr(state_manager, "set_binding_status"):
        state_manager.set_binding_status(account, "adopted_visible_singleton", reason=reason_text)
    _set_process_diagnostics(
        account,
        "adopted_visible_singleton",
        float(validation.get("confidence") or 0.0),
        "",
        account_key,
        proof_level=str(validation.get("process_proof_level") or ""),
        proof_reason=str(validation.get("process_proof_reason") or ""),
        reason=reason_text,
    )
    _set_adopt_diagnostics(account, [], candidate_pid=pid, reject_reason="")
    flog_kv(
        "PROC",
        "adopt_visible_verified",
        account=_account_name(account),
        pid=pid,
        reason=reason,
        process_action="safe_adopt_visible_process",
        confidence=validation.get("confidence", 0.0),
        **_process_log_fields(account),
        thread=threading.current_thread().name,
    )
    return {"ok": True, "pid": pid, "reason": "adopted_visible_singleton", "validation": validation, "live": visible}


def _adopt_and_bind(
    account: Any,
    state_manager: Any,
    pid: int,
    candidate: dict[str, Any],
    reason: str,
    expected_runtime_generation: int | None,
    visible: list[Any],
    account_key: str,
) -> dict[str, Any]:
    validation = _adopt_validation(account, pid, candidate, reason)
    if not validation.get("ok"):
        reject = str(validation.get("reason") or "validation_failed")
        _set_adopt_diagnostics(account, visible, candidate_pid=pid, reject_reason=reject)
        return {"ok": False, "reason": validation.get("reason", ""), "pid": pid, "validation": validation, "live": visible}
    bind_result = _adopt_binding(account, state_manager, pid, candidate, validation, reason, expected_runtime_generation)
    if not bind_result.get("ok"):
        reject = str(bind_result.get("reason") or "bind_failed")
        _set_adopt_diagnostics(account, visible, candidate_pid=pid, reject_reason=reject)
        return {"ok": False, "reason": bind_result.get("reason", ""), "pid": pid, "validation": validation, "live": visible}
    return _finish_adoption(account, state_manager, pid, validation, reason, account_key, visible)


def _orphan_validation(account: Any, target_pid: int, expected_identity: str, reason: str) -> dict[str, Any]:
    validation = ProcessService.validate_binding(
        account,
        target_pid,
        expected_identity=expected_identity or "",
        require_window=False,
        reason=reason or "safe_kill_owned_orphan",
        min_ram_mb=0.0,
        log_success=False,
        log_failure=False,
    )
    proof_level = str(validation.get("process_proof_level") or PROOF_UNTRUSTED)
    if not validation.get("ok") or allows_destructive_process_action(proof_level):
        return validation
    validation = dict(validation)
    validation["ok"] = False
    validation["reason"] = "process_proof_insufficient"
    validation["required_process_proof"] = required_process_proof_for_state(getattr(account, "state", ""), destructive=True)
    return validation


def _log_orphan_rejected(
    account: Any,
    target_pid: int,
    reason: str,
    expected_identity: str,
    validation: dict[str, Any],
    confidence: float,
) -> None:
    flog_kv(
        "PROC",
        "orphan_process_kill_rejected",
        "warning",
        account=_account_name(account),
        pid=target_pid,
        reason=reason,
        reject=str(validation.get("reason") or "low_confidence"),
        confidence=confidence,
        owner=validation.get("owner", ""),
        identity=validation.get("identity", ""),
        expected_identity=expected_identity or "",
    )


def _release_orphan_binding(
    account: Any,
    runtime_state: Any,
    target_pid: int,
    current_pid: int,
    reason: str,
) -> None:
    reason_text = reason or "safe_kill_owned_orphan"
    if runtime_state is not None and current_pid == target_pid and hasattr(runtime_state, "clear_process_binding"):
        runtime_state.clear_process_binding(account, reason=reason_text)
    elif runtime_state is not None and hasattr(runtime_state, "clear_orphan_diagnostics"):
        runtime_state.clear_orphan_diagnostics(account, reason=reason_text)


def _log_orphan_allowed(
    account: Any,
    target_pid: int,
    killed: bool,
    reason: str,
    confidence: float,
    validation: dict[str, Any],
) -> None:
    flog_kv(
        "PROC",
        "orphan_process_kill_allowed",
        account=_account_name(account),
        pid=target_pid,
        killed=killed,
        reason=reason,
        confidence=confidence,
        owner=validation.get("owner", ""),
        identity=validation.get("identity", ""),
        **_process_log_fields(account, include_session=False),
    )


def _bound_process_snapshot(account: Any) -> tuple[Any, str]:
    with account._lock:
        pid = getattr(account, "pid", None)
        identity = str(getattr(account, "bound_process_identity", "") or "")
    return pid, identity


def _bound_validation(account: Any, pid: Any, identity: str, reason: str) -> dict[str, Any]:
    return ProcessService.validate_binding(
        account,
        pid,
        expected_identity=identity,
        require_window=False,
        reason=reason or "safe_kill_bound_process",
        min_ram_mb=0.0,
        log_success=False,
        log_failure=False,
    )


def _clear_bound_process_binding(state_manager: Any, account: Any, reason: str, increment_generation: bool) -> None:
    if state_manager is None:
        return
    try:
        state_manager.clear_process_binding(account, reason=reason, increment_generation=increment_generation)
    except TypeError:
        state_manager.clear_process_binding(account, reason=reason)


def _log_bound_kill_rejected(
    account: Any,
    pid: Any,
    reason: str,
    identity: str,
    validation: dict[str, Any],
) -> None:
    flog_kv(
        "PROC",
        "process_kill_rejected",
        "warning",
        account=_account_name(account),
        pid=pid,
        reason=reason,
        process_action="safe_kill_bound_process",
        reject=validation.get("reason", ""),
        identity=identity,
        owner=validation.get("owner", ""),
        binding_decision=validation.get("binding_decision", "rejected"),
        process_reject_reason=validation.get("process_reject_reason", validation.get("reason", "")),
        process_owner_claim=validation.get("process_owner_claim", ""),
        **_process_log_fields(account),
    )


def _reject_bound_kill(
    account: Any,
    state_manager: Any,
    pid: Any,
    reason: str,
    identity: str,
    validation: dict[str, Any],
    increment_generation: bool,
) -> dict[str, Any]:
    reject_reason = str(validation.get("reason") or "")
    if reject_reason == "process_proof_insufficient":
        _quarantine_process_match(
            account,
            reason or "safe_kill_bound_process",
            validation,
            {
                "process_proof_level": validation.get("process_proof_level", PROOF_UNTRUSTED),
                "process_proof_reason": validation.get("process_proof_reason", ""),
            },
        )
    else:
        ProcessService.release_account_process(account, pid, reason=f"{reason}:validation_failed")
        _clear_bound_process_binding(state_manager, account, reason or "safe_kill_rejected", increment_generation)
    _log_bound_kill_rejected(account, pid, reason, identity, validation)
    return {"ok": False, "killed": False, "pid": pid, "reason": reject_reason, "validation": validation}


def _bound_proof_rejection(
    account: Any,
    pid: Any,
    reason: str,
    identity: str,
    validation: dict[str, Any],
) -> dict[str, Any] | None:
    proof_level = str(validation.get("process_proof_level") or PROOF_UNTRUSTED)
    if allows_destructive_process_action(proof_level):
        return None
    validation = dict(validation)
    validation["ok"] = False
    validation["reason"] = "process_proof_insufficient"
    validation["required_process_proof"] = required_process_proof_for_state(getattr(account, "state", ""), destructive=True)
    _set_process_diagnostics(
        account,
        "rejected",
        float(validation.get("confidence") or 0.0),
        "process_proof_insufficient",
        str(validation.get("owner") or ""),
        proof_level=proof_level,
        proof_reason=str(validation.get("process_proof_reason") or ""),
        reason=reason or "safe_kill_bound_process",
    )
    flog_kv(
        "PROC",
        "process_kill_rejected",
        "warning",
        account=_account_name(account),
        pid=pid,
        reason=reason,
        process_action="safe_kill_bound_process",
        reject="process_proof_insufficient",
        process_proof_level=proof_level,
        required_process_proof=validation["required_process_proof"],
        identity=identity,
        owner=validation.get("owner", ""),
    )
    return {"ok": False, "killed": False, "pid": pid, "reason": "process_proof_insufficient", "validation": validation}


def _log_bound_kill_allowed(
    account: Any,
    pid: Any,
    reason: str,
    identity: str,
    validation: dict[str, Any],
    killed: Any,
) -> None:
    flog_kv(
        "PROC",
        "process_kill_allowed",
        account=_account_name(account),
        pid=pid,
        killed=killed,
        reason=reason,
        process_action="safe_kill_bound_process",
        identity=identity,
        confidence=validation.get("confidence", 0.0),
        **_process_log_fields(account),
    )


class ProcessService:
    """Account-aware process validation and ownership helpers."""

    @staticmethod
    def validate_binding(
        account: Any,
        pid: int | None,
        expected_identity: str | None = None,
        require_window: bool = False,
        reason: str = "",
        launched_after: float | None = None,
        min_ram_mb: float = 20.0,
        log_success: bool = True,
        log_failure: bool = True,
    ) -> dict[str, Any]:
        owner_key = _account_key(account)
        expected_browser_tracker_id = _account_browser_tracker_id(account)
        expected_identity = _resolve_expected_identity(account, pid, expected_identity)
        validation = _ProcessBackend.validate_game_process(
            pid,
            owner_key=owner_key,
            expected_identity=expected_identity,
            launched_after=launched_after,
            min_ram_mb=min_ram_mb,
            expected_browser_tracker_id=expected_browser_tracker_id,
        )
        windows = int(validation.get("windows") or 0)
        validation = _require_window(validation, require_window)
        validation = _require_binding_evidence(
            validation,
            owner_key,
            expected_identity,
            launched_after,
            expected_browser_tracker_id,
        )
        validation = _apply_ownership_validation(validation, account, pid, owner_key, expected_identity, launched_after)
        validation = _apply_process_proof(
            validation,
            account,
            owner_key=owner_key,
            expected_identity=expected_identity,
            launched_after=launched_after,
            expected_browser_tracker_id=expected_browser_tracker_id,
            reason=reason,
        )
        validation = _finalize_binding_validation(validation, account, pid, reason)
        if _should_log_binding(validation, log_success, log_failure):
            _log_binding_result(
                account,
                pid,
                reason,
                validation,
                expected_identity,
                expected_browser_tracker_id,
                windows,
                owner_key,
            )
        return validation

    @staticmethod
    def bind_account_process(
        account: Any,
        pid: int | None,
        state_manager: Any,
        reason: str = "",
        require_window: bool = False,
        expected_identity: str | None = None,
        launched_after: float | None = None,
        process_name: str = "",
        min_ram_mb: float = 20.0,
        increment_generation: bool = True,
        expected_runtime_generation: int | None = None,
    ) -> dict[str, Any]:
        if not _runtime_generation_matches(account, expected_runtime_generation, reason or "bind_account_process"):
            return {"ok": False, "pid": pid, "reason": "stale_runtime_generation", "validation": {}}
        if state_manager is None or not hasattr(state_manager, "bind_process"):
            return _state_manager_unavailable(account, pid, reason)
        _log_bind_attempt(account, pid, reason, expected_identity)
        validation = ProcessService.validate_binding(
            account,
            pid,
            expected_identity=expected_identity,
            require_window=require_window,
            reason=reason or "bind_account_process",
            launched_after=launched_after,
            min_ram_mb=min_ram_mb,
        )
        if not validation.get("ok"):
            return {"ok": False, "pid": pid, "reason": validation.get("reason", ""), "validation": validation}
        return _apply_account_binding(
            account,
            pid,
            state_manager,
            validation,
            process_name,
            increment_generation,
            reason,
        )

    @staticmethod
    def safe_adopt_visible_process(
        account: Any,
        state_manager: Any,
        accounts: list[Any] | None = None,
        reason: str = "",
        expected_runtime_generation: int | None = None,
        launched_after: float | None = None,
        min_ram_mb: float = 100.0,
    ) -> dict[str, Any]:
        if not _runtime_generation_matches(account, expected_runtime_generation, reason or "safe_adopt_visible_process"):
            return {"ok": False, "reason": "stale_runtime_generation", "pid": None}

        all_accounts = list(accounts) if accounts else [account]
        rejection = _adopt_desired_state_rejection(account)
        if rejection is not None:
            return rejection

        live, visible = _visible_candidates(min_ram_mb)
        _set_adopt_diagnostics(account, visible, reject_reason="")
        rejection = _candidate_count_rejection(account, live, visible)
        if rejection is not None:
            return rejection

        candidate = visible[0]
        raw_pid = candidate.get("pid")
        pid = int(raw_pid) if raw_pid else 0
        _set_adopt_diagnostics(account, visible, candidate_pid=pid)
        if not pid:
            _set_adopt_diagnostics(account, visible, reject_reason="missing_pid")
            return {"ok": False, "reason": "missing_pid", "pid": None, "live": visible}

        owner = _candidate_owner(candidate, pid)
        account_key = _account_key(account)
        ambiguous_accounts = _ambiguous_live_accounts(account, all_accounts)
        if ambiguous_accounts:
            return _ambiguous_accounts_rejection(account, visible, pid, ambiguous_accounts)
        rejection = _ownership_rejection(account, all_accounts, visible, pid, owner, account_key)
        if rejection is not None:
            return rejection
        return _adopt_and_bind(
            account,
            state_manager,
            pid,
            candidate,
            reason,
            expected_runtime_generation,
            visible,
            account_key,
        )

    @staticmethod
    def mark_account_process_proof(
        account: Any,
        proof_level: str,
        reason: str = "",
        confidence: float | None = None,
        status: str = "",
    ) -> None:
        return _mark_account_process_proof(
            account,
            proof_level,
            reason=reason,
            confidence=confidence,
            status=status,
        )

    @staticmethod
    def release_account_process(account: Any, pid: int | None = None, reason: str = "") -> None:
        target_pid = pid or getattr(account, "pid", None)
        if not target_pid:
            return
        _ProcessBackend.evict_pid_cache(target_pid)
        _set_process_diagnostics(
            account,
            "released",
            0.0,
            "",
            "",
            reason=reason or "release_account_process",
        )
        flog_kv(
            "PROC",
            "process_unbound",
            account=_account_name(account),
            pid=target_pid,
            reason=reason,
            process_action="release_account_process",
            **_process_log_fields(account, include_session=False),
        )

    @staticmethod
    def safe_kill_owned_orphan(
        account: Any,
        pid: int | None,
        runtime_state: Any = None,
        expected_identity: str = "",
        reason: str = "",
        min_confidence: float = 45.0,
    ) -> dict[str, Any]:
        target_pid = _int_or_zero(pid)
        if not target_pid:
            return {"ok": False, "killed": False, "pid": None, "reason": "missing_pid"}

        validation = _orphan_validation(account, target_pid, expected_identity, reason)
        confidence = float(validation.get("confidence") or 0.0)
        if not validation.get("ok") or confidence < float(min_confidence or 45.0):
            _log_orphan_rejected(account, target_pid, reason, expected_identity, validation, confidence)
            reject = str(validation.get("reason") or "low_confidence")
            return {
                "ok": False,
                "killed": False,
                "pid": target_pid,
                "reason": reject,
                "validation": validation,
            }

        current_pid = _int_or_zero(getattr(account, "pid", 0))
        killed = _ProcessBackend.kill_pid(target_pid)
        _ProcessBackend.release_pid_owner(target_pid, _account_key(account))
        if killed:
            _release_orphan_binding(account, runtime_state, target_pid, current_pid, reason)
            _set_process_diagnostics(
                account,
                "released",
                0.0,
                "",
                "",
                reason=reason or "safe_kill_owned_orphan",
            )
        _log_orphan_allowed(account, target_pid, killed, reason, confidence, validation)
        return {
            "ok": bool(killed),
            "killed": bool(killed),
            "pid": target_pid,
            "reason": "killed" if killed else "kill_failed",
            "validation": validation,
        }

    @staticmethod
    def safe_kill_bound_process(
        account: Any,
        state_manager: Any = None,
        reason: str = "",
        increment_generation: bool = True,
        expected_runtime_generation: int | None = None,
    ) -> dict[str, Any]:
        pid, identity = _bound_process_snapshot(account)
        if not _runtime_generation_matches(account, expected_runtime_generation, reason or "safe_kill_bound_process"):
            return {"ok": False, "killed": False, "pid": pid, "reason": "stale_runtime_generation"}
        if not pid:
            return {"ok": False, "killed": False, "pid": None, "reason": "missing_pid"}

        validation = _bound_validation(account, pid, identity, reason)
        if not validation.get("ok"):
            return _reject_bound_kill(account, state_manager, pid, reason, identity, validation, increment_generation)

        rejection = _bound_proof_rejection(account, pid, reason, identity, validation)
        if rejection is not None:
            return rejection
        killed = _ProcessBackend.kill_pid(pid)
        if killed:
            _clear_bound_process_binding(state_manager, account, reason or "safe_kill_bound_process", increment_generation)
        # If the kill failed, keep the binding: the process may still be
        # alive, and clearing the PID here would let the launcher start a
        # duplicate client on the next attempt.
        _log_bound_kill_allowed(account, pid, reason, identity, validation, killed)
        return {"ok": True, "killed": bool(killed), "pid": pid, "reason": "killed" if killed else "kill_failed", "validation": validation}

    @staticmethod
    def evict_pid_cache(pid: int | None, reason: str = "", account: Any = None) -> None:
        if not pid:
            return
        _ProcessBackend.evict_pid_cache(pid)
        flog_kv(
            "PROC",
            "process_cache_evicted",
            account=_account_name(account) if account is not None else "",
            pid=pid,
            reason=reason,
            process_action="evict_pid_cache",
        )

    @staticmethod
    def kill_all_roblox_clients(
        wait_seconds: float = 4.0,
        exclude_pids: list[int] | None = None,
        reason: str = "",
        idempotency_key: str = "",
        command_id: str = "",
    ) -> int:
        killed = _ProcessBackend.kill_all_roblox_clients(
            wait_seconds=wait_seconds,
            exclude_pids=exclude_pids,
        )
        flog_kv(
            "PROC",
            "process_bulk_kill",
            account="*",
            killed=killed,
            wait_seconds=f"{float(wait_seconds):.1f}",
            exclude_pids=",".join(str(pid) for pid in (exclude_pids or []) if pid),
            reason=reason,
            process_action="kill_all_roblox_clients",
            idempotency_key=idempotency_key,
            command_id=command_id,
        )
        return int(killed or 0)

    @staticmethod
    def cleanup_extra_launch_processes(
        before: set,
        keep_pids: list[int] | None = None,
        launched_after: float | None = None,
        wait_seconds: float = 2.0,
        reason: str = "",
        account: Any = None,
    ) -> int:
        killed = _ProcessBackend.cleanup_extra_launch_processes(
            before,
            keep_pids=keep_pids,
            launched_after=launched_after,
            wait_seconds=wait_seconds,
        )
        flog_kv(
            "PROC",
            "process_cleanup_extra",
            account=_account_name(account) if account is not None else "",
            killed=killed,
            keep_pids=",".join(str(pid) for pid in (keep_pids or []) if pid),
            launched_after=f"{float(launched_after):.3f}" if launched_after else "",
            reason=reason,
            process_action="cleanup_extra_launch_processes",
        )
        return int(killed or 0)

    @staticmethod
    def resize_roblox_windows(
        width: int,
        height: int,
        unlock_size: bool = True,
        exclude_pids: list[int] | None = None,
        reason: str = "",
        account: Any = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        result = _ProcessBackend.resize_roblox_windows(width, height, unlock_size=unlock_size, exclude_pids=exclude_pids)
        flog_kv(
            "WINDOW",
            "process_window_resize",
            account=_account_name(account) if account is not None else "",
            width=width,
            height=height,
            unlock_size=unlock_size,
            resized=result.get("resized", 0),
            count=result.get("count", 0),
            reason=reason,
            process_action="resize_roblox_windows",
            idempotency_key=idempotency_key,
        )
        return result

    @staticmethod
    def arrange_roblox_windows(
        width: int,
        height: int,
        columns: int = 6,
        gap: int = 2,
        margin: int = 0,
        unlock_size: bool = True,
        resize: bool = True,
        rows: int | None = None,
        exclude_pids: list[int] | None = None,
        reason: str = "",
        account: Any = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        result = _ProcessBackend.arrange_roblox_windows(
            width,
            height,
            columns=columns,
            gap=gap,
            margin=margin,
            unlock_size=unlock_size,
            resize=resize,
            rows=rows,
            exclude_pids=exclude_pids,
        )
        flog_kv(
            "WINDOW",
            "process_window_arrange",
            account=_account_name(account) if account is not None else "",
            width=width,
            height=height,
            columns=columns,
            rows=result.get("rows", rows or ""),
            gap=result.get("gap", gap),
            gap_auto=result.get("gap_auto", False),
            margin=margin,
            unlock_size=unlock_size,
            resize=resize,
            arranged=result.get("arranged", 0),
            count=result.get("count", 0),
            reason=reason,
            process_action="arrange_roblox_windows",
            idempotency_key=idempotency_key,
        )
        return result

    @staticmethod
    def unminimize_roblox_windows(
        exclude_pids: list[int] | None = None,
        reason: str = "",
        account: Any = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        result = _ProcessBackend.unminimize_roblox_windows(exclude_pids=exclude_pids)
        flog_kv(
            "WINDOW",
            "process_window_unminimize",
            account=_account_name(account) if account is not None else "",
            restored=result.get("restored", 0),
            count=result.get("count", 0),
            reason=reason,
            process_action="unminimize_roblox_windows",
            idempotency_key=idempotency_key,
        )
        return result

    @staticmethod
    def hide_roblox_windows(
        exclude_pids: list[int] | None = None,
        reason: str = "",
        account: Any = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        result = _ProcessBackend.hide_roblox_windows(exclude_pids=exclude_pids)
        flog_kv(
            "WINDOW",
            "process_window_hide",
            account=_account_name(account) if account is not None else "",
            hidden=result.get("hidden", 0),
            count=result.get("count", 0),
            reason=reason,
            process_action="hide_roblox_windows",
            idempotency_key=idempotency_key,
        )
        return result

    @staticmethod
    def show_roblox_windows(
        exclude_pids: list[int] | None = None,
        reason: str = "",
        account: Any = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        result = _ProcessBackend.show_roblox_windows(exclude_pids=exclude_pids)
        flog_kv(
            "WINDOW",
            "process_window_show",
            account=_account_name(account) if account is not None else "",
            shown=result.get("shown", 0),
            count=result.get("count", 0),
            reason=reason,
            process_action="show_roblox_windows",
            idempotency_key=idempotency_key,
        )
        return result

    @staticmethod
    def restore_roblox_window_styles(
        reason: str = "",
        account: Any = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        result = _ProcessBackend.restore_roblox_window_styles()
        flog_kv(
            "WINDOW",
            "process_window_restore",
            account=_account_name(account) if account is not None else "",
            restored=result.get("restored", 0),
            count=result.get("count", 0),
            reason=reason,
            process_action="restore_roblox_window_styles",
            idempotency_key=idempotency_key,
        )
        return result


class ProcessManager(_ProcessBackend):
    """Compatibility facade preserving the legacy ProcessManager surface."""

    validate_binding = staticmethod(ProcessService.validate_binding)
    bind_account_process = staticmethod(ProcessService.bind_account_process)
    safe_adopt_visible_process = staticmethod(ProcessService.safe_adopt_visible_process)
    release_account_process = staticmethod(ProcessService.release_account_process)
    safe_kill_owned_orphan = staticmethod(ProcessService.safe_kill_owned_orphan)
    safe_kill_bound_process = staticmethod(ProcessService.safe_kill_bound_process)
    mark_account_process_proof = staticmethod(ProcessService.mark_account_process_proof)

    @classmethod
    def staged_orphan_reconcile(cls, *args, **kwargs):
        result = _ProcessBackend.staged_orphan_reconcile(*args, **kwargs)
        account = args[0] if args else kwargs.get("acc")
        action = str(result.get("action") or "")
        if action == "quarantine":
            flog_kv(
                "PROC",
                "orphan_quarantined",
                "warning",
                account=_account_name(account),
                pid=result.get("pid") or "",
                reason=result.get("reason", ""),
                confidence=result.get("confidence", 0.0),
                confidence_level=result.get("confidence_level", ""),
                identity=result.get("identity", ""),
                **_process_log_fields(account, include_session=False),
            )
        return result


__all__ = ["ProcessManager", "ProcessService"]
