from __future__ import annotations

import time
from typing import Any, Dict, Optional


SUPERVISOR_TREE = {
    "GlobalSupervisor": ("SchedulerSupervisor", "RuntimeSupervisor", "TelemetrySupervisor", "AccountSupervisor"),
    "AccountSupervisor": ("ProcessSupervisor", "JoinSupervisor", "RecoverySupervisor", "WatchdogSupervisor", "HeartbeatSupervisor"),
}


class SupervisorRuntime:
    """Incremental supervisor facade.

    This records authority decisions and child signals without replacing the
    existing threads yet. Child components can emit events here; recovery and
    process decisions still remain with RecoveryCoordinator/ProcessService.
    """

    def __init__(self, store: Any = None, logger: Any = None):
        self._store = store
        self._logger = logger

    def emit(
        self,
        supervisor: str,
        event_type: str,
        account: Optional[Any] = None,
        severity: str = "info",
        reason: str = "",
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        snapshot: Dict[str, Any] = {}
        account_id = ""
        if account is not None:
            try:
                snapshot = account.runtime_snapshot()
                account_id = str(getattr(account, "_config_username", "") or getattr(account, "username", "") or "")
            except Exception:
                snapshot = {}
        event = {
            "ts": time.time(),
            "severity": severity or "info",
            "account": account_id,
            "event_type": event_type,
            "reason": reason or "",
            "supervisor": supervisor,
            "session_id": snapshot.get("session_id", ""),
            "launch_nonce": snapshot.get("launch_nonce", ""),
            "account_runtime_id": snapshot.get("account_runtime_id", ""),
            "rejoin_transaction_id": snapshot.get("rejoin_transaction_id", ""),
            "runtime_generation": snapshot.get("runtime_generation", 0),
            "recovery_generation": snapshot.get("recovery_generation", 0),
            "command_generation": snapshot.get("command_generation", 0),
            "pid": snapshot.get("pid", None),
            "runtime_state": snapshot.get("runtime_state", ""),
            "public_state": snapshot.get("public_state", ""),
            "payload": dict(payload or {}),
        }
        if self._store is not None:
            try:
                self._store.record_event(event)
            except Exception:
                pass
        if self._logger is not None:
            try:
                self._logger(
                    "SUPERVISOR",
                    event_type,
                    severity,
                    supervisor=supervisor,
                    account=account_id,
                    reason=reason,
                    session_id=event["session_id"],
                    transaction_id=event["rejoin_transaction_id"],
                    runtime_generation=event["runtime_generation"],
                    recovery_generation=event["recovery_generation"],
                    command_generation=event["command_generation"],
                    pid=event["pid"] or "",
                )
            except Exception:
                pass
        return event

    def snapshot(self) -> Dict[str, Any]:
        return {
            "type": "supervisor_tree",
            "tree": {key: list(value) for key, value in SUPERVISOR_TREE.items()},
        }
