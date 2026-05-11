from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional

from domain.runtime_signals import RuntimeSignal
from runtime.runtime_state_manager import RuntimeStateManager


Logger = Callable[..., None]


class AccountRuntimeController:
    """Narrow account runtime boundary for command/recovery requests.

    RuntimeStateManager remains the field-level writer. This controller is the
    request boundary that captures generation/session ownership before handing
    work to the recovery coordinator.
    """

    def __init__(
        self,
        state_manager: RuntimeStateManager,
        recovery: Optional[Any] = None,
        logger: Optional[Logger] = None,
    ):
        self._state_manager = state_manager
        self._recovery = recovery
        self._log = logger

    def bind_recovery(self, recovery: Any) -> None:
        self._recovery = recovery

    def _emit(self, event: str, level: str = "info", **fields: Any) -> None:
        if not self._log:
            return
        fields.setdefault("event_type", event)
        fields.setdefault("thread_name", threading.current_thread().name)
        fields.setdefault("lifecycle_owner", "account_runtime_controller")
        try:
            self._log("RUNTIME", event, level, **fields)
        except TypeError:
            self._log("RUNTIME", event, **fields)

    def _account_key(self, acc: Any) -> str:
        return str(getattr(acc, "_config_username", "") or getattr(acc, "username", "") or "")

    def capture_owner(self, acc: Any) -> Dict[str, Any]:
        lock = getattr(acc, "_lock", None)

        def _capture() -> Dict[str, Any]:
            return {
                "runtime_generation": int(getattr(acc, "runtime_generation", 0) or 0),
                "recovery_generation": int(getattr(acc, "recovery_generation", 0) or 0),
                "session_id": str(getattr(acc, "session_id", "") or ""),
                "launch_nonce": str(getattr(acc, "launch_nonce", "") or ""),
                "transaction_id": str(getattr(acc, "rejoin_transaction_id", "") or ""),
            }

        if lock:
            with lock:
                return _capture()
        return _capture()

    def request_evaluate(self, acc: Any, trigger: str, force_restart: bool = False) -> bool:
        if not self._recovery:
            self._emit(
                "runtime_request_rejected",
                "warning",
                account_id=self._account_key(acc),
                reason="missing_recovery_coordinator",
                action="evaluate",
                trigger=trigger,
            )
            return False
        owner = self.capture_owner(acc)
        return bool(
            self._recovery.handle_runtime_signal(
                acc,
                RuntimeSignal.EVALUATE.value,
                trigger,
                payload={"trigger": trigger, "force_restart": force_restart},
                expected_runtime_generation=owner["runtime_generation"],
                expected_session_id=owner["session_id"],
                expected_launch_nonce=owner["launch_nonce"],
                expected_transaction_id=owner["transaction_id"],
            )
        )

    def request_rejoin(self, acc: Any, reason: str = "force_rejoin", bump_runtime_generation: bool = True) -> bool:
        if not self._recovery:
            self._emit(
                "runtime_request_rejected",
                "warning",
                account_id=self._account_key(acc),
                reason="missing_recovery_coordinator",
                action="rejoin",
            )
            return False
        lock = getattr(acc, "_lock", None)
        if lock:
            with lock:
                if bump_runtime_generation:
                    self._state_manager.bump_runtime_generation(acc, f"{reason}_command")
                owner = self.capture_owner(acc)
        else:
            if bump_runtime_generation:
                self._state_manager.bump_runtime_generation(acc, f"{reason}_command")
            owner = self.capture_owner(acc)
        return bool(
            self._recovery.handle_runtime_signal(
                acc,
                RuntimeSignal.REJOIN_REQUESTED.value,
                reason,
                payload={"trigger": reason},
                expected_runtime_generation=owner["runtime_generation"],
                expected_session_id=owner["session_id"],
                expected_launch_nonce=owner["launch_nonce"],
                expected_transaction_id=owner["transaction_id"],
            )
        )
