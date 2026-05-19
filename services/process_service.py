"""
Roblox process ownership boundary.

Phase 1 keeps the heavy Windows/process implementation in process_net.py, but
all account-level bind/kill decisions should pass through this module so PID
identity, owner claims, and stale PID reuse are checked consistently.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from core import flog_kv
from services.browser_tracker import tracker_matches
from process_net import ProcessManager as _LegacyProcessManager
from services.process_ownership import validate_process_ownership
from services.process_proof_policy import (
    PROOF_STRONG,
    PROOF_UNTRUSTED,
    classify_process_proof,
    normalize_process_proof_level,
    process_proof_allowed_for_state,
    required_process_proof_for_state,
)
from services.process_window_ops import (
    arrange_roblox_windows as _arrange_roblox_windows,
    resize_roblox_windows as _resize_roblox_windows,
    restore_roblox_window_styles as _restore_roblox_window_styles,
)
from services.resource_monitor import get_rt_monitor
from runtime.runtime_state_manager import RuntimeStateManager


_RUNTIME_STATE = RuntimeStateManager(logger=flog_kv)


def _account_key(acc: Any) -> str:
    return str(getattr(acc, "_config_username", "") or getattr(acc, "username", "") or "")


def _account_name(acc: Any) -> str:
    return str(getattr(acc, "display_name", "") or getattr(acc, "username", "") or _account_key(acc))


def _account_browser_tracker_id(acc: Any) -> str:
    return str(getattr(acc, "browser_tracker_id", "") or "")


def _runtime_generation_matches(account: Any, expected_generation: Optional[int], reason: str) -> bool:
    if expected_generation is None:
        return True
    current = int(getattr(account, "runtime_generation", 0) or 0)
    if int(expected_generation) == current:
        return True
    flog_kv(
        "RUNTIME",
        "stale_work_rejected",
        "warning",
        account=_account_name(account),
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


def _set_process_diagnostics(
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


def _quarantine_process_match(account: Any, reason: str, validation: Dict[str, Any], proof: Dict[str, Any]) -> None:
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


def _has_binding_evidence(
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


def _set_adopt_diagnostics(account: Any, live: List[Dict[str, Any]], candidate_pid: Optional[int] = None, reject_reason: str = "") -> None:
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


class ProcessService:
    """Account-aware process validation and ownership helpers."""

    @staticmethod
    def validate_binding(
        account: Any,
        pid: Optional[int],
        expected_identity: Optional[str] = None,
        require_window: bool = False,
        reason: str = "",
        launched_after: Optional[float] = None,
        min_ram_mb: float = 20.0,
        log_success: bool = True,
        log_failure: bool = True,
    ) -> Dict[str, Any]:
        owner_key = _account_key(account)
        expected_browser_tracker_id = _account_browser_tracker_id(account)
        if expected_identity is None:
            try:
                current_pid = int(getattr(account, "pid", 0) or 0)
                expected_identity = (
                    str(getattr(account, "bound_process_identity", "") or "")
                    if pid and current_pid == int(pid)
                    else ""
                )
            except Exception:
                expected_identity = ""

        validation = _LegacyProcessManager.validate_game_process(
            pid,
            owner_key=owner_key,
            expected_identity=str(expected_identity or ""),
            launched_after=launched_after,
            min_ram_mb=min_ram_mb,
            expected_browser_tracker_id=expected_browser_tracker_id,
        )
        windows = int(validation.get("windows") or 0)
        if validation.get("ok") and require_window and windows <= 0:
            validation = dict(validation)
            validation["ok"] = False
            validation["reason"] = "window_required"
        if validation.get("ok") and not _has_binding_evidence(
            validation,
            owner_key,
            str(expected_identity or ""),
            launched_after,
            expected_browser_tracker_id,
        ):
            validation = dict(validation)
            validation["ok"] = False
            validation["reason"] = "unclaimed_process"
        if validation.get("ok"):
            validation = validate_process_ownership(
                validation,
                pid=pid,
                owner_key=owner_key,
                expected_identity=str(expected_identity or ""),
                launched_after=launched_after,
                current_runtime_generation=int(getattr(account, "runtime_generation", 0) or 0),
            )

        proof = classify_process_proof(
            validation,
            owner_key=owner_key,
            expected_identity=str(expected_identity or ""),
            expected_browser_tracker_id=expected_browser_tracker_id,
            launched_after=launched_after,
            current_process_proof_level=str(getattr(account, "process_proof_level", "") or ""),
        )
        validation.update(proof)
        required_proof = required_process_proof_for_state(getattr(account, "state", ""), destructive=False)
        validation["required_process_proof"] = required_proof
        if validation.get("ok") and not process_proof_allowed_for_state(
            validation.get("process_proof_level"),
            getattr(account, "state", ""),
            destructive=False,
        ):
            validation = dict(validation)
            validation["ok"] = False
            validation["reason"] = "process_proof_insufficient"
            validation["process_reject_reason"] = "process_proof_insufficient"
            _quarantine_process_match(account, reason or "validate_binding", validation, proof)

        validation["binding_decision"] = "verified" if validation.get("ok") else "rejected"
        validation["binding_confidence"] = float(validation.get("confidence") or 0.0)
        validation["process_reject_reason"] = "" if validation.get("ok") else str(validation.get("reason", "") or "")
        validation["process_owner_claim"] = str(validation.get("owner") or _LegacyProcessManager.get_pid_owner(pid) or "")
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

        should_log = (validation.get("ok") and log_success) or ((not validation.get("ok")) and log_failure)
        if should_log:
            event = "process_bind_verified" if validation.get("ok") else "process_bind_rejected"
            flog_kv(
                "PROC",
                event,
                "info" if validation.get("ok") else "warning",
                account=_account_name(account),
                pid=pid or "",
            reason=reason,
            process_action="validate_binding",
            reject="" if validation.get("ok") else validation.get("reason", ""),
                identity=validation.get("identity", ""),
                expected_identity=str(expected_identity or ""),
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
                runtime_generation=getattr(account, "runtime_generation", 0),
                recovery_generation=getattr(account, "recovery_generation", 0),
                command_generation=getattr(account, "command_generation", 0),
                session_id=getattr(account, "session_id", ""),
                transaction_id=getattr(account, "rejoin_transaction_id", ""),
                thread=threading.current_thread().name,
            )
        return validation

    @staticmethod
    def bind_account_process(
        account: Any,
        pid: Optional[int],
        state_manager: Any,
        reason: str = "",
        require_window: bool = False,
        expected_identity: Optional[str] = None,
        launched_after: Optional[float] = None,
        process_name: str = "",
        min_ram_mb: float = 20.0,
        increment_generation: bool = True,
        expected_runtime_generation: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not _runtime_generation_matches(account, expected_runtime_generation, reason or "bind_account_process"):
            return {"ok": False, "pid": pid, "reason": "stale_runtime_generation", "validation": {}}
        if state_manager is None or not hasattr(state_manager, "bind_process"):
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
        flog_kv(
            "PROC",
            "process_bind_attempt",
            account=_account_name(account),
            pid=pid or "",
            reason=reason,
            process_action="bind_account_process",
            expected_identity=expected_identity or "",
            expected_browser_tracker_id=_account_browser_tracker_id(account),
            runtime_generation=getattr(account, "runtime_generation", 0),
            recovery_generation=getattr(account, "recovery_generation", 0),
            command_generation=getattr(account, "command_generation", 0),
            session_id=getattr(account, "session_id", ""),
            transaction_id=getattr(account, "rejoin_transaction_id", ""),
        )
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

        old_pid = getattr(account, "pid", None)
        identity = str(validation.get("identity") or _LegacyProcessManager.get_process_identity(pid))
        name = process_name or str(validation.get("name") or "RobloxPlayerBeta.exe")
        confidence = float(validation.get("confidence") or 100.0)
        proof_level = str(validation.get("process_proof_level") or PROOF_UNTRUSTED)

        if old_pid and int(old_pid) != int(pid):
            _LegacyProcessManager.evict_pid_cache(old_pid)

        state_manager.bind_process(
            account,
            int(pid),
            identity,
            status="verified",
            process_name=name,
            confidence=confidence,
            process_proof_level=proof_level,
            reason=reason or "bind_account_process",
            increment_generation=increment_generation,
        )
        _LegacyProcessManager.claim_pid_owner(pid, _account_key(account))
        get_rt_monitor().register(int(pid))
        _set_process_diagnostics(
            account,
            "verified",
            confidence,
            "",
            _account_key(account),
            proof_level=proof_level,
            proof_reason=str(validation.get("process_proof_reason") or ""),
            reason=reason or "bind_account_process",
        )

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
            runtime_generation=getattr(account, "runtime_generation", 0),
            recovery_generation=getattr(account, "recovery_generation", 0),
            command_generation=getattr(account, "command_generation", 0),
            session_id=getattr(account, "session_id", ""),
            transaction_id=getattr(account, "rejoin_transaction_id", ""),
        )
        return {"ok": True, "pid": int(pid), "identity": identity, "name": name, "validation": validation}

    @staticmethod
    def safe_adopt_visible_process(
        account: Any,
        state_manager: Any,
        accounts: Optional[List[Any]] = None,
        reason: str = "",
        expected_runtime_generation: Optional[int] = None,
        launched_after: Optional[float] = None,
        min_ram_mb: float = 100.0,
    ) -> Dict[str, Any]:
        if not _runtime_generation_matches(account, expected_runtime_generation, reason or "safe_adopt_visible_process"):
            return {"ok": False, "reason": "stale_runtime_generation", "pid": None}

        all_accounts = list(accounts or [account])
        desired = getattr(account, "desired_state", "")
        desired_name = str(getattr(desired, "name", desired) or "").upper()
        if desired_name and desired_name not in {"IN_GAME", "RUNNING"}:
            _set_adopt_diagnostics(account, [], reject_reason="desired_state_not_in_game")
            return {"ok": False, "reason": "desired_state_not_in_game", "pid": None, "live": []}

        live = _LegacyProcessManager.list_live_game_processes(launched_after=None)
        visible = [
            item for item in live
            if int(item.get("windows") or 0) > 0 or float(item.get("rss_mb") or 0.0) >= float(min_ram_mb or 100.0)
        ]
        _set_adopt_diagnostics(account, visible, reject_reason="")

        if not visible:
            _set_adopt_diagnostics(account, live, reject_reason="no_visible_candidate")
            flog_kv("PROC", "adopt_visible_rejected", "warning", account=_account_name(account), reason="no_visible_candidate", live=len(live))
            return {"ok": False, "reason": "no_visible_candidate", "pid": None, "live": live}
        if len(visible) != 1:
            _set_adopt_diagnostics(account, visible, reject_reason="visible_process_ambiguous")
            flog_kv("PROC", "adopt_visible_rejected", "warning", account=_account_name(account), reason="visible_process_ambiguous", candidates=len(visible))
            return {"ok": False, "reason": "visible_process_ambiguous", "pid": None, "live": visible}

        candidate = visible[0]
        pid = int(candidate.get("pid") or 0)
        _set_adopt_diagnostics(account, visible, candidate_pid=pid)
        if not pid:
            _set_adopt_diagnostics(account, visible, reject_reason="missing_pid")
            return {"ok": False, "reason": "missing_pid", "pid": None, "live": visible}

        owner = str(candidate.get("owner") or _LegacyProcessManager.get_pid_owner(pid) or "")
        account_key = _account_key(account)
        ambiguous_accounts: List[str] = []
        for other in all_accounts:
            if other is account:
                continue
            other_desired = getattr(other, "desired_state", "")
            other_desired_name = str(getattr(other_desired, "name", other_desired) or "").upper()
            try:
                other_pid = int(getattr(other, "pid", 0) or 0)
            except Exception:
                other_pid = 0
            other_bound_alive = False
            if other_pid:
                try:
                    other_bound_alive = bool(_LegacyProcessManager.is_bound_game_alive(
                        other_pid,
                        owner_key=_account_key(other),
                        expected_identity=str(getattr(other, "bound_process_identity", "") or ""),
                        expected_browser_tracker_id=_account_browser_tracker_id(other),
                    ))
                except Exception:
                    other_bound_alive = False
            if other_desired_name in {"IN_GAME", "RUNNING"} and not other_bound_alive:
                ambiguous_accounts.append(_account_name(other))
        if ambiguous_accounts:
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

        if owner and owner != account_key:
            _set_adopt_diagnostics(account, visible, candidate_pid=pid, reject_reason=f"owner_mismatch:{owner}")
            flog_kv("PROC", "adopt_visible_rejected", "warning", account=_account_name(account), pid=pid, reason="owner_mismatch", owner=owner)
            return {"ok": False, "reason": f"owner_mismatch:{owner}", "pid": pid, "live": visible}

        for other in all_accounts:
            if other is account:
                continue
            try:
                other_pid = int(getattr(other, "pid", 0) or 0)
            except Exception:
                other_pid = 0
            if other_pid and other_pid == pid:
                _set_adopt_diagnostics(account, visible, candidate_pid=pid, reject_reason="pid_claimed_by_other_account")
                return {"ok": False, "reason": "pid_claimed_by_other_account", "pid": pid, "live": visible}
            other_owner = _LegacyProcessManager.get_pid_owner(pid)
            if other_owner and other_owner != account_key:
                _set_adopt_diagnostics(account, visible, candidate_pid=pid, reject_reason=f"owner_mismatch:{other_owner}")
                return {"ok": False, "reason": f"owner_mismatch:{other_owner}", "pid": pid, "live": visible}

        validation = ProcessService.validate_binding(
            account,
            pid,
            expected_identity=str(candidate.get("identity") or ""),
            require_window=int(candidate.get("windows") or 0) > 0,
            reason=reason or "safe_adopt_visible_process",
            launched_after=None,
            min_ram_mb=0.0,
        )
        if not validation.get("ok"):
            _set_adopt_diagnostics(account, visible, candidate_pid=pid, reject_reason=str(validation.get("reason") or "validation_failed"))
            return {"ok": False, "reason": validation.get("reason", ""), "pid": pid, "validation": validation, "live": visible}

        bind_result = ProcessService.bind_account_process(
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
        if not bind_result.get("ok"):
            _set_adopt_diagnostics(account, visible, candidate_pid=pid, reject_reason=str(bind_result.get("reason") or "bind_failed"))
            return {"ok": False, "reason": bind_result.get("reason", ""), "pid": pid, "validation": validation, "live": visible}

        if hasattr(state_manager, "set_binding_status"):
            state_manager.set_binding_status(account, "adopted_visible_singleton", reason=reason or "safe_adopt_visible_process")
        _set_process_diagnostics(
            account,
            "adopted_visible_singleton",
            float(validation.get("confidence") or 0.0),
            "",
            account_key,
            proof_level=str(validation.get("process_proof_level") or ""),
            proof_reason=str(validation.get("process_proof_reason") or ""),
            reason=reason or "safe_adopt_visible_process",
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
            runtime_generation=getattr(account, "runtime_generation", 0),
            recovery_generation=getattr(account, "recovery_generation", 0),
            command_generation=getattr(account, "command_generation", 0),
            session_id=getattr(account, "session_id", ""),
            transaction_id=getattr(account, "rejoin_transaction_id", ""),
            thread=threading.current_thread().name,
        )
        return {"ok": True, "pid": pid, "reason": "adopted_visible_singleton", "validation": validation, "live": visible}

    @staticmethod
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
            account=_account_name(account),
            pid=getattr(account, "pid", None) or "",
            reason=reason,
            process_proof_level=proof,
            confidence=getattr(account, "process_binding_confidence", 0.0),
            status=getattr(account, "process_binding_status", ""),
        )

    @staticmethod
    def release_account_process(account: Any, pid: Optional[int] = None, reason: str = "") -> None:
        target_pid = pid or getattr(account, "pid", None)
        if not target_pid:
            return
        _LegacyProcessManager.evict_pid_cache(target_pid)
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
            runtime_generation=getattr(account, "runtime_generation", 0),
            recovery_generation=getattr(account, "recovery_generation", 0),
            command_generation=getattr(account, "command_generation", 0),
        )

    @staticmethod
    def safe_kill_owned_orphan(
        account: Any,
        pid: Optional[int],
        runtime_state: Any = None,
        expected_identity: str = "",
        reason: str = "",
        min_confidence: float = 45.0,
    ) -> Dict[str, Any]:
        try:
            target_pid = int(pid or 0)
        except Exception:
            target_pid = 0
        if not target_pid:
            return {"ok": False, "killed": False, "pid": None, "reason": "missing_pid"}

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
        confidence = float(validation.get("confidence") or 0.0)
        proof_level = str(validation.get("process_proof_level") or PROOF_UNTRUSTED)
        if validation.get("ok") and not process_proof_allowed_for_state(proof_level, getattr(account, "state", ""), destructive=True):
            validation = dict(validation)
            validation["ok"] = False
            validation["reason"] = "process_proof_insufficient"
            validation["required_process_proof"] = required_process_proof_for_state(getattr(account, "state", ""), destructive=True)
        if not validation.get("ok") or confidence < float(min_confidence or 45.0):
            reject = str(validation.get("reason") or "low_confidence")
            flog_kv(
                "PROC",
                "orphan_process_kill_rejected",
                "warning",
                account=_account_name(account),
                pid=target_pid,
                reason=reason,
                reject=reject,
                confidence=confidence,
                owner=validation.get("owner", ""),
                identity=validation.get("identity", ""),
                expected_identity=expected_identity or "",
            )
            return {
                "ok": False,
                "killed": False,
                "pid": target_pid,
                "reason": reject,
                "validation": validation,
            }

        try:
            current_pid = int(getattr(account, "pid", 0) or 0)
        except Exception:
            current_pid = 0
        killed = _LegacyProcessManager.kill_pid(target_pid)
        _LegacyProcessManager.release_pid_owner(target_pid, _account_key(account))
        if killed:
            if runtime_state is not None and current_pid == target_pid and hasattr(runtime_state, "clear_process_binding"):
                runtime_state.clear_process_binding(account, reason=reason or "safe_kill_owned_orphan")
            elif runtime_state is not None and hasattr(runtime_state, "clear_orphan_diagnostics"):
                runtime_state.clear_orphan_diagnostics(account, reason=reason or "safe_kill_owned_orphan")
            _set_process_diagnostics(
                account,
                "released",
                0.0,
                "",
                "",
                reason=reason or "safe_kill_owned_orphan",
            )
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
            runtime_generation=getattr(account, "runtime_generation", 0),
            recovery_generation=getattr(account, "recovery_generation", 0),
            command_generation=getattr(account, "command_generation", 0),
        )
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
        expected_runtime_generation: Optional[int] = None,
    ) -> Dict[str, Any]:
        with getattr(account, "_lock"):
            pid = getattr(account, "pid", None)
            identity = str(getattr(account, "bound_process_identity", "") or "")
        if not _runtime_generation_matches(account, expected_runtime_generation, reason or "safe_kill_bound_process"):
            return {"ok": False, "killed": False, "pid": pid, "reason": "stale_runtime_generation"}
        if not pid:
            return {"ok": False, "killed": False, "pid": None, "reason": "missing_pid"}

        validation = ProcessService.validate_binding(
            account,
            pid,
            expected_identity=identity,
            require_window=False,
            reason=reason or "safe_kill_bound_process",
            min_ram_mb=0.0,
            log_success=False,
            log_failure=False,
        )
        if not validation.get("ok"):
            reject_reason = str(validation.get("reason") or "")
            if reject_reason != "process_proof_insufficient":
                ProcessService.release_account_process(account, pid, reason=f"{reason}:validation_failed")
            else:
                _quarantine_process_match(
                    account,
                    reason or "safe_kill_bound_process",
                    validation,
                    {
                        "process_proof_level": validation.get("process_proof_level", PROOF_UNTRUSTED),
                        "process_proof_reason": validation.get("process_proof_reason", ""),
                    },
                )
            if state_manager is not None and reject_reason != "process_proof_insufficient":
                try:
                    state_manager.clear_process_binding(
                        account,
                        reason=reason or "safe_kill_rejected",
                        increment_generation=increment_generation,
                    )
                except TypeError:
                    state_manager.clear_process_binding(account, reason=reason or "safe_kill_rejected")
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
                runtime_generation=getattr(account, "runtime_generation", 0),
                recovery_generation=getattr(account, "recovery_generation", 0),
                command_generation=getattr(account, "command_generation", 0),
                session_id=getattr(account, "session_id", ""),
                transaction_id=getattr(account, "rejoin_transaction_id", ""),
            )
            return {"ok": False, "killed": False, "pid": pid, "reason": reject_reason, "validation": validation}

        proof_level = str(validation.get("process_proof_level") or PROOF_UNTRUSTED)
        if not process_proof_allowed_for_state(proof_level, getattr(account, "state", ""), destructive=True):
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

        killed = _LegacyProcessManager.kill_pid(pid)
        if state_manager is not None:
            try:
                state_manager.clear_process_binding(
                    account,
                    reason=reason or "safe_kill_bound_process",
                    increment_generation=increment_generation,
                )
            except TypeError:
                state_manager.clear_process_binding(account, reason=reason or "safe_kill_bound_process")
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
            runtime_generation=getattr(account, "runtime_generation", 0),
            recovery_generation=getattr(account, "recovery_generation", 0),
            command_generation=getattr(account, "command_generation", 0),
            session_id=getattr(account, "session_id", ""),
            transaction_id=getattr(account, "rejoin_transaction_id", ""),
        )
        return {"ok": True, "killed": bool(killed), "pid": pid, "reason": "killed" if killed else "kill_failed", "validation": validation}

    @staticmethod
    def evict_pid_cache(pid: Optional[int], reason: str = "", account: Any = None) -> None:
        if not pid:
            return
        _LegacyProcessManager.evict_pid_cache(pid)
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
        exclude_pids: Optional[List[int]] = None,
        reason: str = "",
        idempotency_key: str = "",
        command_id: str = "",
    ) -> int:
        killed = _LegacyProcessManager.kill_all_roblox_clients(
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
        keep_pids: Optional[List[int]] = None,
        launched_after: Optional[float] = None,
        wait_seconds: float = 2.0,
        reason: str = "",
        account: Any = None,
    ) -> int:
        killed = _LegacyProcessManager.cleanup_extra_launch_processes(
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

    resize_roblox_windows = staticmethod(_resize_roblox_windows)
    arrange_roblox_windows = staticmethod(_arrange_roblox_windows)
    restore_roblox_window_styles = staticmethod(_restore_roblox_window_styles)


class ProcessManager(_LegacyProcessManager):
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
        result = _LegacyProcessManager.staged_orphan_reconcile(*args, **kwargs)
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
                runtime_generation=getattr(account, "runtime_generation", 0),
                recovery_generation=getattr(account, "recovery_generation", 0),
                command_generation=getattr(account, "command_generation", 0),
            )
        return result


__all__ = ["ProcessManager", "ProcessService"]
