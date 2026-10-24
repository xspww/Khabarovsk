"""Recovery wiring, lifecycle, slot helpers, and public entry points."""
from __future__ import annotations

import threading
import time
from typing import Any, NamedTuple

from core import Account, AccountState, EventBus, SmartQueue, StateManager, flog_kv
from runtime.recovery_evaluator import RecoveryEvaluator
from runtime.recovery_owner import RecoveryOwnerRegistry
from runtime.recovery_policy import (
    RecoveryDedupeTracker,
    RecoveryGate,
    SessionConflictTracker,
)
from runtime.recovery_signal_router import RecoverySignalRouter
from runtime.recovery_slots import RecoverySlotPolicy
from runtime.recovery_storm import RecoveryStormController
from runtime.recovery_support import should_suppress_recovery_schedule
from runtime.runtime_orchestrator import RuntimeOrchestrator
from runtime.runtime_scheduler import RuntimeScheduler
from runtime.runtime_state_manager import RuntimeStateManager
from services.network_monitor import NetworkMonitor

ACTIVE_SLOT_STATES = {
    AccountState.QUEUED,
    AccountState.LAUNCHING,
    AccountState.VERIFY,
    AccountState.IN_GAME,
}


class _ExecutorPark(NamedTuple):
    key: str
    due: float
    generation: int
    runtime_generation: int
    command_generation: int


class RecoveryCoreMixin:
    """Wiring and lifecycle: owns construction, config fan-out, slots via
    slot_policy, executor-hold park, and the public signal/evaluate entries
    that delegate into the router and evaluator collaborator objects."""

    def __init__(
        self,
        queue: SmartQueue,
        state_mgr: StateManager,
        bus: EventBus,
        net: NetworkMonitor,
        stop: threading.Event,
        cfg: dict,
        accounts: list[Account] | None = None,
        persist_callback=None,
        timeline=None,
        runtime_state: RuntimeStateManager | None = None,
        runtime_orchestrator: RuntimeOrchestrator | None = None,
        scheduler: RuntimeScheduler | None = None,
    ):
        self._queue = queue
        self._state_mgr = state_mgr
        self._bus = bus
        self._net = net
        self._stop = stop
        self._cfg = cfg
        self._accounts = accounts or []
        self._persist_callback = persist_callback
        self._last_persist = 0.0
        self._lock = threading.Lock()
        self._closed = False
        self._owner_registry = RecoveryOwnerRegistry()
        self._runtime_state = runtime_state or RuntimeStateManager(logger=flog_kv)
        self._runtime_orchestrator = runtime_orchestrator or RuntimeOrchestrator(
            self._runtime_state,
            timeline=timeline,
            logger=flog_kv,
        )
        self._runtime_orchestrator.bind_recovery(self)
        self._account_runtime = self._runtime_orchestrator.account_runtime
        self._duplicate_window = max(1.0, float(cfg.get("recovery_duplicate_window", 8) or 8))
        self._recovery_dedupe = RecoveryDedupeTracker(float(cfg.get("recovery_dedupe_window_seconds", 3) or 3))
        self._recovery_gate = RecoveryGate(
            owner=self._owner_registry,
            dedupe=self._recovery_dedupe,
            duplicate_window=self._duplicate_window,
        )
        self._signal_router = RecoverySignalRouter(
            self._runtime_state,
            is_closed=self._is_closed,
            log_decision=self._log_recovery_decision,
            recovery_gate=self._recovery_gate,
        )
        self._session_conflicts = SessionConflictTracker()
        self._owns_scheduler = scheduler is None
        self._scheduler = scheduler or RuntimeScheduler(
            stop=stop,
            state_manager=self._runtime_state,
            timeline=timeline,
            logger=flog_kv,
            name="RecoveryScheduler",
        )
        self.slot_policy = RecoverySlotPolicy(
            cfg_getter=lambda: self._cfg,
            accounts_getter=lambda: self._accounts,
        )
        self._evaluator = RecoveryEvaluator(self)
        self._storm = RecoveryStormController(cfg, self._accounts)
        self._executor_hold_check = lambda: False
        self._guard_recovery_lock = threading.Lock()
        self._guard_recovery_scheduled = False
        self._guard_recovery_attempt = 0
        self._guard_recovery_last_at = 0.0


    def update_config(self, cfg: dict, accounts: list[Account] | None = None) -> None:
        with self._lock:
            self._cfg = cfg
            if accounts is not None:
                self._accounts = accounts
        self._storm.update_config(cfg)
        if accounts is not None:
            self._storm.set_accounts(accounts)


    @property
    def runtime_orchestrator(self) -> RuntimeOrchestrator:
        return self._runtime_orchestrator


    def set_executor_hold_check(self, fn) -> None:
        """Live predicate polled before any new launch/rejoin is scheduled."""
        try:
            self._executor_hold_check = fn if callable(fn) else (lambda: False)
        except Exception:
            self._executor_hold_check = lambda: False


    def _executor_held(self) -> bool:
        try:
            check = getattr(self, "_executor_hold_check", None)
            return bool(check() if callable(check) else False)
        except Exception:
            return False


    def _executor_hold_remaining(self, acc: Account, now: float) -> float:
        try:
            if str(getattr(acc, "recovery_status", "") or "") != "wait_exploit_patch":
                return 0.0
            return float(getattr(acc, "cooldown_until", 0.0) or 0.0) - now
        except Exception:
            return 0.0

    def _park_account(self, acc: Account, trigger: str, due: float) -> _ExecutorPark | None:
        key = f"recovery:{acc._config_username}"
        try:
            with acc._lock:
                if self._closed or self._stop.is_set():
                    return None
                park = _ExecutorPark(
                    key=key,
                    due=due,
                    generation=acc.recovery_generation,
                    runtime_generation=acc.runtime_generation,
                    command_generation=acc.command_generation,
                )
                self._state_mgr.set_cooldown(acc, due, reason=f"executor_hold:{trigger}")
                self._state_mgr.set_recovery(acc, status="wait_exploit_patch", reason=trigger, inflight=True)
                try:
                    self._state_mgr.transition(acc, AccountState.COOLDOWN, reason=f"executor_hold:{trigger}")
                except Exception:
                    "intentional: ignore exception"
                acc.recovery_scheduled_at = due
                acc.scheduler_slot = key
        except Exception:
            return None
        return park

    def _schedule_parked_recovery(self, acc: Account, trigger: str, park: _ExecutorPark) -> None:
        try:
            current = self._scheduler.get(park.key)
            if should_suppress_recovery_schedule(
                current, park.due, park.generation, park.runtime_generation, park.command_generation
            ):
                return
        except Exception:
            "intentional: ignore exception"
        try:
            self._scheduler.schedule_once(
                park.key,
                self._run_scheduled_recovery,
                due_at=park.due,
                reason=f"executor_hold:{trigger}",
                account=acc,
                runtime_generation=park.runtime_generation,
                recovery_generation=park.generation,
                command_generation=park.command_generation,
                payload={"scheduler_slot": "recovery", "allow_runtime_generation_drift": True},
            )
        except Exception:
            "intentional: ignore exception"
        try:
            self._persist_runtime()
        except Exception:
            "intentional: ignore exception"

    def _park_for_executor_hold(self, acc: Account, trigger: str) -> None:
        """Park an account as Wait-for-patch. Dashboard-only, no terminal spam.

        Sets recovery_status=wait_exploit_patch plus a 60s recheck timer.
        Re-parking is skipped while a fresh timer is pending so repeated
        evaluate ticks stay quiet.
        """
        now = time.time()
        if self._executor_hold_remaining(acc, now) > 30.0:
            return
        due = now + 60.0
        park = self._park_account(acc, trigger, due)
        if park is None:
            return
        self._schedule_parked_recovery(acc, trigger, park)


    def _is_closed(self) -> bool:
        with self._lock:
            return self._closed or self._stop.is_set()


    def _persist_runtime(self, force: bool = False):
        if not self._persist_callback:
            return
        now = time.time()
        if not force and (now - self._last_persist) < 2.0:
            return
        self._last_persist = now
        try:
            self._persist_callback()
        except Exception as e:
            flog_kv("RECOVERY", "persist_failed", "warning", error=e)


    def stop(self):
        with self._lock:
            self._closed = True
        active = self._owner_registry.clear()
        self._signal_router.clear()
        pending = sum(self._scheduler.cancel(f"recovery:{acc._config_username}", reason="recovery_stop") for acc in self._accounts)
        if self._owns_scheduler:
            pending += self._scheduler.cancel_all(reason="recovery_stop")
            self._scheduler.stop()
        try:
            self._queue.cancel_all("recovery_stop")
        except Exception as exc:
            flog_kv("RECOVERY", "queue_cancel_failed", "warning", error=exc)
        flog_kv("RECOVERY", "coordinator_stopped", pending_cancelled=pending, active_cancelled=active)


    def _log_recovery_decision(self, event: str, acc: Account, reason: str, **fields):
        from runtime.recovery_support import log_recovery_decision

        log_recovery_decision(event, acc, reason, **fields)



    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self._cfg.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
        return bool(value)


    def _cfg_float(self, key: str, default: float, minimum: float = 0.0, maximum: float = 3600.0) -> float:
        try:
            value = float(self._cfg.get(key, default) or default)
        except Exception:
            value = default
        return max(minimum, min(maximum, value))


    def handle_runtime_signal(
        self,
        acc: Account,
        signal: str,
        reason: str = "",
        payload: dict[str, Any] | None = None,
        expected_runtime_generation: int | None = None,
        expected_session_id: str = "",
        expected_launch_nonce: str = "",
        expected_transaction_id: str = "",
    ) -> bool:
        """Single boundary for worker/watchdog/maintenance recovery signals."""
        return self._signal_router.route(
            self,
            acc,
            signal,
            reason,
            payload=payload,
            expected_runtime_generation=expected_runtime_generation,
            expected_session_id=expected_session_id,
            expected_launch_nonce=expected_launch_nonce,
            expected_transaction_id=expected_transaction_id,
        )


    def set_desired(self, acc: Account, desired: AccountState):
        with acc._lock:
            self._runtime_state.set_desired(acc, desired, reason="recovery_set_desired")


    def request_evaluate(self, acc: Account, trigger: str, force_restart: bool = False) -> bool:
        return self._runtime_orchestrator.request_evaluate(acc, trigger=trigger, force_restart=force_restart)


    def request_rejoin(self, acc: Account, reason: str = "force_rejoin") -> bool:
        return self._runtime_orchestrator.request_rejoin(acc, reason=reason, bump_runtime_generation=True)


    def evaluate(
        self,
        acc: Account,
        trigger: str = "evaluate",
        force_restart: bool = False,
        expected_runtime_generation: int | None = None,
        expected_recovery_generation: int | None = None,
        expected_session_id: str = "",
        expected_launch_nonce: str = "",
        expected_transaction_id: str = "",
    ):
        return self._evaluator.evaluate(
            acc,
            trigger=trigger,
            force_restart=force_restart,
            expected_runtime_generation=expected_runtime_generation,
            expected_recovery_generation=expected_recovery_generation,
            expected_session_id=expected_session_id,
            expected_launch_nonce=expected_launch_nonce,
            expected_transaction_id=expected_transaction_id,
        )


    def reconcile_all(self, accounts: list[Account], trigger: str = "reconcile_all", force_restart: bool = False):
        for acc in accounts:
            self.evaluate(acc, trigger=trigger, force_restart=force_restart)
