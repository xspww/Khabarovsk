"""Account runtime helpers for process ownership decisions."""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from core import flog_kv
from runtime.runtime_state_manager import RuntimeStateManager
from services.browser_tracker import tracker_matches
from services.process_proof_policy import (
    PROOF_STRONG,
    PROOF_UNTRUSTED,
    normalize_process_proof_level,
)


_RUNTIME_STATE = RuntimeStateManager(logger=flog_kv)


def account_key(acc: Any) -> str:
    return str(getattr(acc, "_config_username", "") or getattr(acc, "username", "") or "")


def account_name(acc: Any) -> str:
    return str(getattr(acc, "display_name", "") or getattr(acc, "username", "") or account_key(acc))


def account_browser_tracker_id(acc: Any) -> str:
    return str(getattr(acc, "browser_tracker_id", "") or "")


def runtime_generation_matches(account: Any, expected_generation: Optional[int], reason: str) -> bool:
    if expected_generation is None:
        return True
    current = int(getattr(account, "runtime_generation", 0) or 0)
    if int(expected_generation) == current:
        return True
    flog_kv(
        "RUNTIME",
        "stale_work_rejected",
        "warning",
        account=account_name(account),
        expected_generation=expected_generation,
        current_generation=current,
        recovery_generation=getattr(account, "recovery_generation", 0),
        command_generation=getattr(account, "command_generation", 0),
        session_id=getattr(account, "session_id", ""),
        transaction_id=getattr(account, "rejoin_transaction_id", ""),
        reason=reason,
        thread=threading.current_thread().name,
    )
    return False


def set_process_diagnostics(
    account: Any,
    decision: str,
    confidence: float = 0.0,
    reject_reason: str = "",
    owner_claim: str = "",
    proof_level: str = "",
    proof_reason: str = "",
    reason: str = "",
) -> None:
    lock = getattr(account, "_lock", None)

    def _write() -> None:
        account.binding_decision = str(decision or "")
        account.process_binding_confidence = float(confidence or 0.0)
        account.process_reject_reason = str(reject_reason or "")
        account.process_owner_claim = str(owner_claim or "")
        if proof_level:
            account.process_proof_level = normalize_process_proof_level(proof_level)
        if proof_reason and reject_reason:
            account.process_reject_reason = str(reject_reason or proof_reason)
        try:
            account.sync_runtime(reason or decision or "process_diagnostics")
        except Exception:
            pass

    if lock:
        with lock:
            _write()
    else:
        _write()


def quarantine_process_match(account: Any, reason: str, validation: Dict[str, Any], proof: Dict[str, Any]) -> None:
    lock = getattr(account, "_lock", None)

    def _write() -> None:
        _RUNTIME_STATE.set_binding_status(account, "process_proof_quarantine", reason=reason or "process_proof_quarantine")
        account.binding_decision = "quarantined"
        account.process_binding_confidence = float(validation.get("confidence") or 0.0)
        account.process_proof_level = str(proof.get("process_proof_level") or PROOF_UNTRUSTED)
        account.process_reject_reason = "process_proof_insufficient"
        account.process_owner_claim = str(validation.get("owner") or "")
        try:
            account.sync_runtime(reason or "process_proof_quarantine")
        except Exception:
            pass

    if lock:
        with lock:
            _write()
    else:
        _write()


def has_binding_evidence(
    validation: Dict[str, Any],
    owner_key: str,
    expected_identity: str,
    launched_after: Optional[float],
    expected_browser_tracker_id: str = "",
) -> bool:
    owner = str(validation.get("owner") or "")
    identity = str(validation.get("identity") or "")
    created = float(validation.get("created") or 0.0)
    observed_tracker = str(validation.get("browser_tracker_id") or "")
    if tracker_matches(expected_browser_tracker_id, observed_tracker):
        return True
    if expected_identity and identity and identity == str(expected_identity):
        return True
    if owner_key and owner and owner == owner_key:
        return True
    if launched_after is not None and created and created >= (float(launched_after) - 3.0):
        return True
    return False


def set_adopt_diagnostics(
    account: Any,
    live: List[Dict[str, Any]],
    candidate_pid: Optional[int] = None,
    reject_reason: str = "",
) -> None:
    lock = getattr(account, "_lock", None)
    pids = [int(item.get("pid") or 0) for item in live if item.get("pid")]

    def _write() -> None:
        account.unmanaged_live_process_count = len(pids)
        account.unmanaged_live_pids = pids
        account.adopt_candidate_pid = int(candidate_pid or 0) or None
        account.adopt_reject_reason = str(reject_reason or "")
        try:
            account.sync_runtime(reject_reason or "adopt_diagnostics")
        except Exception:
            pass

    if lock:
        with lock:
            _write()
    else:
        _write()


def mark_account_process_proof(
    account: Any,
    proof_level: str,
    reason: str = "",
    confidence: Optional[float] = None,
    status: str = "",
) -> None:
    proof = normalize_process_proof_level(proof_level)
    lock = getattr(account, "_lock", None)

    def _write() -> None:
        account.process_proof_level = proof
        if confidence is not None:
            account.process_binding_confidence = float(confidence or 0.0)
            account.ownership_confidence = float(confidence or 0.0)
            account.last_signal_confidence = float(confidence or 0.0)
        if status:
            _RUNTIME_STATE.set_binding_status(account, str(status), reason=reason or "process_proof")
        if proof == PROOF_STRONG:
            account.binding_decision = "verified"
            account.process_reject_reason = ""
        try:
            account.sync_runtime(reason or "process_proof")
        except Exception:
            pass

    if lock:
        with lock:
            _write()
    else:
        _write()
    flog_kv(
        "PROC",
        "process_proof_marked",
        account=account_name(account),
        pid=getattr(account, "pid", None) or "",
        reason=reason,
        process_proof_level=proof,
        confidence=getattr(account, "process_binding_confidence", 0.0),
        status=getattr(account, "process_binding_status", ""),
    )
