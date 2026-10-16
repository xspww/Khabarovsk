"""Account runtime helpers for process ownership decisions."""

from __future__ import annotations

import threading
from typing import Any

from core import flog_kv
from services.process_proof_policy import (
    PROOF_STRONG,
    PROOF_UNTRUSTED,
    normalize_process_proof_level,
)

_RUNTIME_STATE: Any = None
_RUNTIME_STATE_LOCK = threading.Lock()


def _runtime_state() -> Any:
    """Lazy RuntimeStateManager without a top-level services->runtime import."""
    global _RUNTIME_STATE
    if _RUNTIME_STATE is None:
        with _RUNTIME_STATE_LOCK:
            if _RUNTIME_STATE is None:
                try:
                    from runtime.runtime_state_manager import RuntimeStateManager

                    _RUNTIME_STATE = RuntimeStateManager(logger=flog_kv)
                except ImportError:  # pragma: no cover - keeps services importable
                    from core import flog_kv as _flog

                    _flog("RUNTIME", "binding_state_noop_fallback", "warning")
                    class _Noop:
                        def set_binding_status(self, *a: Any, **k: Any) -> None:
                            return None

                    _RUNTIME_STATE = _Noop()
    return _RUNTIME_STATE


def account_key(acc: Any) -> str:
    return str(getattr(acc, "_config_username", "") or getattr(acc, "username", "") or "")


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


def quarantine_process_match(account: Any, reason: str, validation: dict[str, Any], proof: dict[str, Any]) -> None:
    lock = getattr(account, "_lock", None)

    def _write() -> None:
        _runtime_state().set_binding_status(account, "process_proof_quarantine", reason=reason or "process_proof_quarantine")
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


def set_adopt_diagnostics(
    account: Any,
    live: list[dict[str, Any]],
    candidate_pid: int | None = None,
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
    confidence: float | None = None,
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
            _runtime_state().set_binding_status(account, str(status), reason=reason or "process_proof")
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
