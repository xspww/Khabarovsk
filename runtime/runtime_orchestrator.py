from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Optional

from core import AccountState
from domain.runtime_signals import RuntimeSignal
from runtime.account_runtime_controller import AccountRuntimeController
from runtime.runtime_state_manager import RuntimeStateManager
from services.process_service import ProcessService


Logger = Callable[..., None]


@dataclass(frozen=True)
class RuntimeCommand:
    action: str
    account_id: str = ""
    reason: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    runtime_generation: int = 0
    recovery_generation: int = 0
    command_generation: int = 0
    session_id: str = ""
    launch_nonce: str = ""
    transaction_id: str = ""
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class RuntimeEvent:
    event_type: str
    account_id: str = ""
    reason: str = ""
    severity: str = "info"
    payload: Dict[str, Any] = field(default_factory=dict)
    runtime_generation: int = 0
    recovery_generation: int = 0
    command_generation: int = 0
    session_id: str = ""
    launch_nonce: str = ""
    transaction_id: str = ""
    created_at: float = field(default_factory=time.time)


class RuntimeOrchestrator:
    """Central command/event boundary for runtime mutations.

    RuntimeStateManager remains the only field-level writer. This class owns the
    command/event contract and delegates existing behavior to recovery so callers
    no longer need to couple themselves directly to the recovery engine.
    """

    def __init__(
        self,
        state_manager: RuntimeStateManager,
        recovery: Optional[Any] = None,
        timeline: Optional[Any] = None,
        logger: Optional[Logger] = None,
    ):
        self._state_manager = state_manager
        self._timeline = timeline
        self._log = logger
        self._account_runtime = AccountRuntimeController(state_manager, recovery=recovery, logger=logger)

    @property
    def account_runtime(self) -> AccountRuntimeController:
        return self._account_runtime

    def bind_recovery(self, recovery: Any) -> None:
        self._account_runtime.bind_recovery(recovery)

    def _account_key(self, acc: Any) -> str:
        return str(getattr(acc, "_config_username", "") or getattr(acc, "username", "") or "")

    def capture_owner(self, acc: Any) -> Dict[str, Any]:
        return self._account_runtime.capture_owner(acc)

    def _snapshot(self, acc: Any) -> Dict[str, Any]:
        try:
            return dict(acc.runtime_snapshot())
        except Exception:
            return {}

    def _emit_log(self, name: str, level: str = "info", **fields: Any) -> None:
        fields.setdefault("thread_name", threading.current_thread().name)
        fields.setdefault("lifecycle_owner", "runtime_orchestrator")
        if self._log:
            try:
                self._log("RUNTIME", name, level, **fields)
            except TypeError:
                self._log("RUNTIME", name, **fields)

    def emit_event(self, event: RuntimeEvent, acc: Optional[Any] = None) -> Dict[str, Any]:
        item = asdict(event)
        payload = item.pop("payload", {}) or {}
        item.update(payload)
        item["ts"] = item.pop("created_at", time.time())
        item["account"] = event.account_id
        item["lifecycle_owner"] = "runtime_orchestrator"
        snapshot = self._snapshot(acc) if acc is not None else {}
        if self._timeline:
            try:
                return self._timeline.record(item, account_snapshot=snapshot, account_id=event.account_id)
            except Exception as exc:
                self._emit_log("orchestrator_timeline_failed", "warning", account=event.account_id, error=exc)
        self._emit_log(event.event_type, event.severity, account=event.account_id, reason=event.reason, **payload)
        return item

    def _command(self, acc: Any, action: str, reason: str = "", payload: Optional[Dict[str, Any]] = None) -> RuntimeCommand:
        owner = self.capture_owner(acc)
        return RuntimeCommand(
            action=action,
            account_id=self._account_key(acc),
            reason=reason,
            payload=dict(payload or {}),
            runtime_generation=int(owner.get("runtime_generation") or 0),
            recovery_generation=int(owner.get("recovery_generation") or 0),
            command_generation=int(owner.get("command_generation") or 0),
            session_id=str(owner.get("session_id") or ""),
            launch_nonce=str(owner.get("launch_nonce") or ""),
            transaction_id=str(owner.get("transaction_id") or ""),
        )

    def _emit_command(self, command: RuntimeCommand, accepted: bool, acc: Any, event_type: str = "runtime_command") -> None:
        self.emit_event(
            RuntimeEvent(
                event_type=event_type,
                account_id=command.account_id,
                reason=command.reason,
                severity="info" if accepted else "warning",
                payload={"action": command.action, "accepted": accepted, "command_id": command.command_id},
                runtime_generation=command.runtime_generation,
                recovery_generation=command.recovery_generation,
                command_generation=command.command_generation,
                session_id=command.session_id,
                launch_nonce=command.launch_nonce,
                transaction_id=command.transaction_id,
            ),
            acc=acc,
        )

    def _signal_event_type(self, signal: str, reason: str, accepted: bool) -> str:
        if not accepted:
            return "runtime_signal_rejected"
        signal_name = str(signal or "").strip().lower()
        reason_key = str(reason or "").strip().lower()
        if signal_name in {"disconnect_detected", "fault", "crash", "watchdog_timeout", "process_lost", "loading_freeze"}:
            if reason_key in {"pid_dead", "process_crash", "not_responding"}:
                return "process_lost"
            return "disconnect_detected"
        if signal_name in {"network_lost", "network_drop"}:
            return "network_lost"
        if signal_name == "rejoin_requested":
            return "rejoin_requested"
        if signal_name == "launch_success":
            return "launch_success"
        if signal_name in {"fatal", "auth_failure", "session_failure"}:
            return "failed"
        return "runtime_signal"

    def request_evaluate(self, acc: Any, trigger: str, force_restart: bool = False) -> bool:
        command = self._command(acc, RuntimeSignal.EVALUATE.value, trigger, {"force_restart": force_restart})
        accepted = self._account_runtime.request_evaluate(acc, trigger=trigger, force_restart=force_restart)
        self._emit_command(command, accepted, acc, event_type="runtime_evaluate_requested")
        return accepted

    def request_rejoin(self, acc: Any, reason: str = "force_rejoin", bump_runtime_generation: bool = True) -> bool:
        command = self._command(acc, RuntimeSignal.REJOIN_REQUESTED.value, reason)
        accepted = self._account_runtime.request_rejoin(acc, reason=reason, bump_runtime_generation=bump_runtime_generation)
        self._emit_command(command, accepted, acc, event_type="runtime_rejoin_requested")
        return accepted

    def request_start_epoch(self, acc: Any, reason: str = "farm_start_epoch") -> int:
        with acc._lock:
            generation = self._state_manager.bump_runtime_generation(acc, reason)
        self.emit_event(
            RuntimeEvent(
                event_type="runtime_start_epoch",
                account_id=self._account_key(acc),
                reason=reason,
                **self.capture_owner(acc),
            ),
            acc=acc,
        )
        return int(generation)

    def request_network_lost(self, acc: Any, reason: str = "network_drop", payload: Optional[Dict[str, Any]] = None) -> bool:
        return self.handle_runtime_signal(
            acc,
            "network_lost",
            reason,
            payload=payload or {"trigger": "network"},
        )

    def handle_runtime_signal(
        self,
        acc: Any,
        signal: str,
        reason: str = "",
        payload: Optional[Dict[str, Any]] = None,
        expected_runtime_generation: Optional[int] = None,
        expected_session_id: str = "",
        expected_launch_nonce: str = "",
        expected_transaction_id: str = "",
    ) -> bool:
        recovery = self._account_runtime.recovery
        if not recovery:
            self._emit_log("runtime_signal_rejected", "warning", account=self._account_key(acc), reason="missing_recovery")
            return False
        if expected_runtime_generation is None:
            owner = self.capture_owner(acc)
            expected_runtime_generation = int(owner.get("runtime_generation") or 0)
            expected_session_id = expected_session_id or str(owner.get("session_id") or "")
            expected_launch_nonce = expected_launch_nonce or str(owner.get("launch_nonce") or "")
            expected_transaction_id = expected_transaction_id or str(owner.get("transaction_id") or "")
        accepted = bool(
            recovery.handle_runtime_signal(
                acc,
                signal,
                reason,
                payload=payload,
                expected_runtime_generation=expected_runtime_generation,
                expected_session_id=expected_session_id,
                expected_launch_nonce=expected_launch_nonce,
                expected_transaction_id=expected_transaction_id,
            )
        )
        self.emit_event(
            RuntimeEvent(
                event_type=self._signal_event_type(signal, reason, accepted),
                account_id=self._account_key(acc),
                reason=reason or signal,
                severity="info" if accepted else "warning",
                payload={"signal": signal, "accepted": accepted},
                runtime_generation=int(expected_runtime_generation or 0),
                session_id=expected_session_id,
                launch_nonce=expected_launch_nonce,
                transaction_id=expected_transaction_id,
            ),
            acc=acc,
        )
        return accepted

    def set_recovery_status(self, acc: Any, status: str = "", reason: str = "", inflight: Optional[bool] = None) -> None:
        with acc._lock:
            self._state_manager.set_recovery(acc, status=status, reason=reason, inflight=inflight)
            acc.sync_runtime(reason or status or "orchestrator_recovery_status")
        self.emit_event(
            RuntimeEvent(
                event_type="runtime_recovery_status_set",
                account_id=self._account_key(acc),
                reason=reason or status,
                payload={"status": status, "inflight": inflight},
                **self.capture_owner(acc),
            ),
            acc=acc,
        )

    def request_reconcile_all(self, accounts, trigger: str, force_restart: bool = False):
        recovery = self._account_runtime.recovery
        if not recovery:
            return None
        self._emit_log("runtime_reconcile_requested", account="*", trigger=trigger, force_restart=force_restart)
        return recovery.reconcile_all(accounts, trigger=trigger, force_restart=force_restart)

    def reconcile_all(self, accounts, trigger: str, force_restart: bool = False):
        return self.request_reconcile_all(accounts, trigger=trigger, force_restart=force_restart)

    def request_kill_account_pid(self, acc: Any, state_manager: Any = None, reason: str = "api_kill_pid") -> Dict[str, Any]:
        with acc._lock:
            pid = acc.pid
            runtime_generation = acc.runtime_generation
        if not pid:
            return {"ok": False, "killed": False, "pid": None, "reason": "missing_pid"}
        command = self._command(acc, "kill_pid", reason, {"pid": pid})
        result = ProcessService.safe_kill_bound_process(
            acc,
            state_manager or self._state_manager,
            reason=reason,
            expected_runtime_generation=runtime_generation,
        )
        self.emit_event(
            RuntimeEvent(
                event_type="runtime_process_action",
                account_id=command.account_id,
                reason=reason,
                severity="info" if result.get("ok") else "warning",
                payload={
                    "action": command.action,
                    "accepted": bool(result.get("ok")),
                    "command_id": command.command_id,
                    "process_action": "kill_account_pid",
                    "pid": pid,
                    "killed": bool(result.get("killed")),
                    "process_reason": result.get("reason", ""),
                },
                runtime_generation=command.runtime_generation,
                recovery_generation=command.recovery_generation,
                command_generation=command.command_generation,
                session_id=command.session_id,
                launch_nonce=command.launch_nonce,
                transaction_id=command.transaction_id,
            ),
            acc=acc,
        )
        return result

    def request_verify_finished(self, acc: Any, state_manager: Any = None, reason: str = "manual_verify_finished") -> Dict[str, Any]:
        now = time.time()
        with acc._lock:
            pid = acc.pid
            runtime_generation = acc.runtime_generation
            acc.manual_status = "finished"
            acc.finished_at = now
            acc.last_state_reason = reason
            acc.last_state_change_at = now
            self._state_manager.set_desired(acc, AccountState.IDLE, reason=reason)
        command = self._command(acc, "verify_finished", reason, {"pid": pid})
        killed = False
        kill_result: Dict[str, Any] = {}
        if pid:
            kill_result = ProcessService.safe_kill_bound_process(
                acc,
                state_manager or self._state_manager,
                reason=reason,
                expected_runtime_generation=runtime_generation,
            )
            killed = bool(kill_result.get("killed"))
        if state_manager:
            state_manager.transition(acc, AccountState.IDLE, reason=reason, force=True)
        else:
            with acc._lock:
                self._state_manager.forced_reset(acc, desired=AccountState.IDLE, reason=reason)
        self.emit_event(
            RuntimeEvent(
                event_type="runtime_process_action",
                account_id=command.account_id,
                reason=reason,
                payload={
                    "action": command.action,
                    "accepted": True,
                    "command_id": command.command_id,
                    "process_action": "verify_finished",
                    "pid": pid or "",
                    "killed": killed,
                    "process_reason": kill_result.get("reason", ""),
                },
                runtime_generation=command.runtime_generation,
                recovery_generation=command.recovery_generation,
                command_generation=command.command_generation,
                session_id=command.session_id,
                launch_nonce=command.launch_nonce,
                transaction_id=command.transaction_id,
            ),
            acc=acc,
        )
        return {"ok": True, "killed": killed, "pid": pid, "finished_at": now, "kill_result": kill_result}

    def request_close_all_roblox(
        self,
        wait_seconds: float = 4.0,
        exclude_pids: Optional[list[int]] = None,
        reason: str = "api_close_all_roblox",
        idempotency_key: str = "",
        command_id: str = "",
    ) -> int:
        closed = ProcessService.kill_all_roblox_clients(
            wait_seconds=wait_seconds,
            exclude_pids=exclude_pids,
            reason=reason,
            idempotency_key=idempotency_key,
            command_id=command_id,
        )
        self.emit_event(
            RuntimeEvent(
                event_type="runtime_process_action",
                account_id="*",
                reason=reason,
                payload={
                    "action": "close_all_roblox",
                    "accepted": True,
                    "command_id": command_id,
                    "process_action": "kill_all_roblox_clients",
                    "closed": closed,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return closed
