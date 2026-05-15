from __future__ import annotations

import time
from typing import Any, Callable, Dict, Iterable, Optional

from services.process_service import ProcessManager, ProcessService


RecordEvent = Callable[..., None]

ACTIVE_PUBLIC_STATES = {"IN_GAME", "LAUNCHING", "VERIFY", "QUEUED"}


def _account_key(account: Any) -> str:
    return str(getattr(account, "_config_username", "") or getattr(account, "username", "") or "")


def _state_name(value: Any) -> str:
    return str(getattr(value, "name", value) or "").upper()


def _int_pid(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


class RuntimeOrphanSweeper:
    """Conservative cleanup for verified account-owned orphan Roblox processes."""

    def __init__(
        self,
        accounts: Iterable[Any],
        runtime_state: Any = None,
        process_service: Any = ProcessService,
        process_manager: Any = ProcessManager,
        record_event: Optional[RecordEvent] = None,
    ):
        self._accounts = accounts
        self._runtime_state = runtime_state
        self._process_service = process_service
        self._process_manager = process_manager
        self._record_event = record_event

    def sweep(self, cfg: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
        if not bool(cfg.get("orphan_sweeper_enabled", True)):
            return {"enabled": False, "candidates": 0, "killed": 0, "skipped": 0}
        current = float(now if now is not None else time.time())
        min_confidence = float(cfg.get("orphan_sweeper_min_confidence", 45.0) or 45.0)
        kill_enabled = bool(cfg.get("orphan_sweeper_kill_enabled", True))
        candidates = self._collect_candidates(current, min_confidence)
        killed = 0
        skipped = 0
        for pid, candidate in candidates.items():
            account = candidate["account"]
            if not kill_enabled:
                skipped += 1
                self._emit("orphan_process_detected", account, "warning", "kill_disabled", candidate)
                continue
            result = self._process_service.safe_kill_owned_orphan(
                account,
                pid,
                runtime_state=self._runtime_state,
                expected_identity=str(candidate.get("identity") or ""),
                reason=str(candidate.get("reason") or "orphan_sweeper"),
                min_confidence=min_confidence,
            )
            if result.get("ok") and result.get("killed"):
                killed += 1
                self._emit("orphan_process_swept", account, "warning", "killed", candidate, result)
            else:
                skipped += 1
                self._emit(
                    "orphan_process_sweep_rejected",
                    account,
                    "warning",
                    str(result.get("reason") or "validation_failed"),
                    candidate,
                    result,
                )
        return {
            "enabled": True,
            "candidates": len(candidates),
            "killed": killed,
            "skipped": skipped,
        }

    def _collect_candidates(self, now: float, min_confidence: float) -> Dict[int, Dict[str, Any]]:
        by_key = {_account_key(account): account for account in list(self._accounts or [])}
        candidates: Dict[int, Dict[str, Any]] = {}
        for account in by_key.values():
            if self._account_is_active(account):
                continue
            candidate = self._account_orphan_candidate(account, now, min_confidence)
            if candidate:
                candidates[int(candidate["pid"])] = candidate
        for entry in self._live_owner_claim_candidates(by_key):
            candidates.setdefault(int(entry["pid"]), entry)
        return candidates

    def _account_orphan_candidate(self, account: Any, now: float, min_confidence: float) -> Dict[str, Any]:
        lock = getattr(account, "_lock", None)
        if lock:
            with lock:
                pid = _int_pid(getattr(account, "orphan_pid", None))
                identity = str(getattr(account, "orphan_identity", "") or "")
                confidence = float(getattr(account, "orphan_confidence", 0.0) or 0.0)
                verify_after = float(getattr(account, "orphan_verify_after", 0.0) or 0.0)
        else:
            pid = _int_pid(getattr(account, "orphan_pid", None))
            identity = str(getattr(account, "orphan_identity", "") or "")
            confidence = float(getattr(account, "orphan_confidence", 0.0) or 0.0)
            verify_after = float(getattr(account, "orphan_verify_after", 0.0) or 0.0)
        if not pid or confidence < min_confidence or (verify_after and now < verify_after):
            return {}
        return {
            "account": account,
            "pid": pid,
            "identity": identity,
            "confidence": confidence,
            "reason": "orphan_quarantine_elapsed",
            "source": "account_orphan_diagnostics",
        }

    def _live_owner_claim_candidates(self, accounts: Dict[str, Any]) -> list:
        try:
            live = self._process_manager.list_live_game_processes(launched_after=None)
        except Exception:
            live = []
        candidates = []
        for entry in live or []:
            pid = _int_pid(entry.get("pid"))
            owner = str(entry.get("owner") or self._process_manager.get_pid_owner(pid) or "")
            account = accounts.get(owner)
            if not pid or account is None or self._account_is_active(account):
                continue
            if _int_pid(getattr(account, "pid", None)) == pid:
                continue
            candidates.append({
                "account": account,
                "pid": pid,
                "identity": str(entry.get("identity") or ""),
                "confidence": float(entry.get("confidence") or 100.0),
                "reason": "inactive_account_owner_claim",
                "source": "process_owner_claim",
            })
        return candidates

    def _account_is_active(self, account: Any) -> bool:
        public = _state_name(getattr(account, "state", ""))
        return public in ACTIVE_PUBLIC_STATES

    def _emit(
        self,
        event_type: str,
        account: Any,
        severity: str,
        reason: str,
        candidate: Dict[str, Any],
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self._record_event:
            return
        try:
            self._record_event(
                event_type,
                _account_key(account),
                severity=severity,
                reason=reason,
                lifecycle_owner="runtime_orphan_sweeper",
                pid=int(candidate.get("pid") or 0),
                source=str(candidate.get("source") or ""),
                confidence=float(candidate.get("confidence") or 0.0),
                sweep_result=dict(result or {}),
            )
        except Exception:
            return
