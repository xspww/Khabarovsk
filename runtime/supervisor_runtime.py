from __future__ import annotations

import time
from typing import Any

SUPERVISOR_TREE = {
    "GlobalSupervisor": ("SchedulerSupervisor", "RuntimeSupervisor", "TelemetrySupervisor", "AccountSupervisor"),
    "AccountSupervisor": ("ProcessSupervisor", "JoinSupervisor", "RecoverySupervisor", "WatchdogSupervisor", "HeartbeatSupervisor"),
}


def _account_event_context(account: Any | None) -> tuple[dict[str, Any], str]:
    if account is None:
        return {}, ""
    try:
        snapshot = account.runtime_snapshot()
        account_id = str(getattr(account, "_config_username", "") or getattr(account, "username", "") or "")
    except Exception:
        return {}, ""
    return snapshot, account_id


def _supervisor_event(
    supervisor: str,
    event_type: str,
    severity: str,
    reason: str,
    payload: dict[str, Any] | None,
    snapshot: dict[str, Any],
    account_id: str,
) -> dict[str, Any]:
    return {
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
        "pid": snapshot.get("pid"),
        "runtime_state": snapshot.get("runtime_state", ""),
        "public_state": snapshot.get("public_state", ""),
        "payload": dict(payload or {}),
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

    def _record_event(self, event: dict[str, Any]) -> None:
        if self._store is None:
            return
        try:
            self._store.record_event(event)
        except Exception:
            "intentional: ignore exception"

    def _log_event(
        self,
        supervisor: str,
        event_type: str,
        severity: str,
        reason: str,
        account_id: str,
        event: dict[str, Any],
    ) -> None:
        if self._logger is None:
            return
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
            "intentional: ignore exception"

    def emit(
        self,
        supervisor: str,
        event_type: str,
        account: Any | None = None,
        severity: str = "info",
        reason: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot, account_id = _account_event_context(account)
        event = _supervisor_event(supervisor, event_type, severity, reason, payload, snapshot, account_id)
        self._record_event(event)
        self._log_event(supervisor, event_type, severity, reason, account_id, event)
        return event

    def snapshot(self) -> dict[str, Any]:
        return {
            "type": "supervisor_tree",
            "tree": {key: list(value) for key, value in SUPERVISOR_TREE.items()},
        }
