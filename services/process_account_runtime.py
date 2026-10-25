"""Account runtime helpers for process ownership decisions."""

from __future__ import annotations

import threading
from typing import Any

from core import flog_kv
from domain._util import account_key
from runtime.runtime_state_manager import RuntimeStateManager
from services.hold_clear import run_under_account_lock as _with_account_lock
from services.process_proof_policy import (
    PROOF_STRONG,
    PROOF_UNTRUSTED,
    normalize_process_proof_level,
)

_RUNTIME_STATE = RuntimeStateManager(logger=flog_kv)


def _text(value: Any) -> str:
    return str(value or "")


def _number(value: Any) -> float:
    return float(value or 0.0)


def _fallback_text(value: Any, default: str) -> str:
    return str(value or default)


def _first_text(first: Any, second: Any, default: str) -> str:
    return str(first or second or default)


def _sync_runtime_quietly(account: Any, reason: str) -> None:
    try:
        account.sync_runtime(reason)
    except Exception:
        "intentional: ignore exception"


def _write_process_diagnostics(
    account: Any,
    decision: str,
    confidence: float,
    reject_reason: str,
    owner_claim: str,
    proof_level: str,
    proof_reason: str,
    reason: str,
) -> None:
    account.binding_decision = _text(decision)
    account.process_binding_confidence = _number(confidence)
    account.process_reject_reason = _text(reject_reason)
    account.process_owner_claim = _text(owner_claim)
    if proof_level:
        account.process_proof_level = normalize_process_proof_level(proof_level)
    if proof_reason and reject_reason:
        account.process_reject_reason = str(reject_reason or proof_reason)
    _sync_runtime_quietly(account, _first_text(reason, decision, "process_diagnostics"))


def _write_quarantine(account: Any, reason: str, validation: dict[str, Any], proof: dict[str, Any]) -> None:
    _RUNTIME_STATE.set_binding_status(
        account,
        "process_proof_quarantine",
        reason=_fallback_text(reason, "process_proof_quarantine"),
    )
    account.binding_decision = "quarantined"
    account.process_binding_confidence = _number(validation.get("confidence"))
    account.process_proof_level = _fallback_text(proof.get("process_proof_level"), PROOF_UNTRUSTED)
    account.process_reject_reason = "process_proof_insufficient"
    account.process_owner_claim = _text(validation.get("owner"))
    _sync_runtime_quietly(account, _fallback_text(reason, "process_proof_quarantine"))


def _write_adopt_diagnostics(account: Any, pids: list[int], candidate_pid: int | None, reject_reason: str) -> None:
    account.unmanaged_live_process_count = len(pids)
    account.unmanaged_live_pids = pids
    candidate = int(candidate_pid or 0)
    account.adopt_candidate_pid = candidate or None
    account.adopt_reject_reason = _text(reject_reason)
    _sync_runtime_quietly(account, _fallback_text(reject_reason, "adopt_diagnostics"))


def _write_process_proof(account: Any, proof: str, confidence: float | None, status: str, reason: str) -> None:
    account.process_proof_level = proof
    if confidence is not None:
        value = _number(confidence)
        account.process_binding_confidence = value
        account.ownership_confidence = value
        account.last_signal_confidence = value
    if status:
        _RUNTIME_STATE.set_binding_status(
            account,
            str(status),
            reason=_fallback_text(reason, "process_proof"),
        )
    if proof == PROOF_STRONG:
        account.binding_decision = "verified"
        account.process_reject_reason = ""
    _sync_runtime_quietly(account, _fallback_text(reason, "process_proof"))


def account_name(acc: Any) -> str:
    return str(getattr(acc, "display_name", "") or getattr(acc, "username", "") or account_key(acc))


def account_browser_tracker_id(acc: Any) -> str:
    return str(getattr(acc, "browser_tracker_id", "") or "")


def process_log_fields(account: Any, include_session: bool = True) -> dict[str, Any]:
    fields = {
        "runtime_generation": getattr(account, "runtime_generation", 0),
        "recovery_generation": getattr(account, "recovery_generation", 0),
        "command_generation": getattr(account, "command_generation", 0),
    }
    if include_session:
        fields.update(
            {
                "session_id": getattr(account, "session_id", ""),
                "transaction_id": getattr(account, "rejoin_transaction_id", ""),
            }
        )
    return fields


def runtime_generation_matches(account: Any, expected_generation: int | None, reason: str) -> bool:
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
    _with_account_lock(
        account,
        lambda: _write_process_diagnostics(
            account,
            decision,
            confidence,
            reject_reason,
            owner_claim,
            proof_level,
            proof_reason,
            reason,
        ),
    )


def quarantine_process_match(account: Any, reason: str, validation: dict[str, Any], proof: dict[str, Any]) -> None:
    _with_account_lock(
        account,
        lambda: _write_quarantine(account, reason, validation, proof),
    )


def set_adopt_diagnostics(
    account: Any,
    live: list[dict[str, Any]],
    candidate_pid: int | None = None,
    reject_reason: str = "",
) -> None:
    pids = [int(item.get("pid") or 0) for item in live if item.get("pid")]
    _with_account_lock(
        account,
        lambda: _write_adopt_diagnostics(account, pids, candidate_pid, reject_reason),
    )


def mark_account_process_proof(
    account: Any,
    proof_level: str,
    reason: str = "",
    confidence: float | None = None,
    status: str = "",
) -> None:
    proof = normalize_process_proof_level(proof_level)
    _with_account_lock(
        account,
        lambda: _write_process_proof(account, proof, confidence, status, reason),
    )
    flog_kv(
        "PROC",
        "process_proof_marked",
        account=account_name(account),
        pid=_text(getattr(account, "pid", None)),
        reason=reason,
        process_proof_level=proof,
        confidence=getattr(account, "process_binding_confidence", 0.0),
        status=getattr(account, "process_binding_status", ""),
    )
