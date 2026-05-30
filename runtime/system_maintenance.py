from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

from core import Account, StateManager, flog
from runtime.invariant_monitor import RuntimeInvariantMonitor
from runtime.orphan_sweeper import RuntimeOrphanSweeper
from runtime.runtime_scheduler import RuntimeScheduledJob, RuntimeScheduler
from runtime.supervisor_runtime import SupervisorRuntime
from services.process_service import ProcessManager
from runtime.maintenance_liveness import MaintenanceLivenessMixin
from runtime.maintenance_performance import MaintenancePerformanceMixin
from runtime.maintenance_queue import MaintenanceQueueMixin


class SystemMaintenance(
    MaintenanceLivenessMixin,
    MaintenanceQueueMixin,
    MaintenancePerformanceMixin,
    threading.Thread,
):
    def __init__(
        self,
        accounts: List[Account],
        workers: Dict[str, AccountWorker],
        recovery: RecoveryEngine,
        state_mgr: StateManager,
        cfg: dict,
        stop: threading.Event,
        supervisor: Optional[SupervisorRuntime] = None,
        scheduler: Optional[RuntimeScheduler] = None,
        record_runtime_event: Optional[Callable[..., None]] = None,
    ):
        super().__init__(daemon=True, name="Maintenance")
        self._accounts = accounts
        self._workers = workers
        self._recovery = recovery
        self._runtime_owner = getattr(recovery, "runtime_orchestrator", recovery)
        self._runtime_state = getattr(recovery, "_runtime_state", None)
        self._state_mgr = state_mgr
        self._cfg = cfg
        self._stop = stop
        self._supervisor = supervisor
        self._last_auto_close_at = time.time()
        self._last_priority_apply_at = 0.0
        self._last_cpu_limiter_apply_at = 0.0
        self._cpu_limiter_released = False
        self._last_window_resize_at = 0.0
        self._last_popup_scan_at: Dict[str, float] = {}
        self._last_popup_batch_at = 0.0
        self._popup_scan_cursor = 0
        self._owns_scheduler = scheduler is None
        self._scheduler = scheduler or RuntimeScheduler(
            stop=stop,
            state_manager=self._runtime_state,
            name="MaintenanceScheduler",
        )
        self._maintenance_job_keys: List[str] = []
        self._invariant_monitor = RuntimeInvariantMonitor(
            accounts,
            pid_validator=self._runtime_pid_validator,
            record_event=record_runtime_event,
            suppress_seconds=float(cfg.get("runtime_invariant_suppress_seconds", 60) or 60),
        )
        self._orphan_sweeper = RuntimeOrphanSweeper(
            accounts,
            runtime_state=self._runtime_state,
            record_event=record_runtime_event,
        )

    def update_config(self, cfg: dict) -> None:
        self._cfg = cfg

    def run(self):
        flog("[MAINT] started")
        self._register_periodic_jobs()
        self._stop.wait()
        for key in self._maintenance_job_keys:
            self._scheduler.cancel(key, reason="maintenance_stop")
        if self._owns_scheduler:
            self._scheduler.stop()
        flog("[MAINT] stopped")

    def _base_interval(self) -> float:
        try:
            return max(1.0, min(5.0, float(self._cfg.get("periodic_reconcile_interval", 15) or 15)))
        except Exception:
            return 5.0

    def _maintenance_interval(self, key: str, default: float, minimum: float, maximum: float) -> float:
        try:
            value = float(self._cfg.get(key, default) or default)
        except Exception:
            value = default
        return max(minimum, min(maximum, value))

    def _reconcile_interval(self) -> float:
        try:
            return max(self._base_interval(), float(self._cfg.get("periodic_reconcile_interval", 15) or 15))
        except Exception:
            return max(self._base_interval(), 15.0)

    def _register_periodic_jobs(self) -> None:
        base = self._base_interval()
        liveness_interval = self._maintenance_interval("maintenance_liveness_interval_seconds", base, 3.0, 30.0)
        queue_interval = self._maintenance_interval("maintenance_queue_interval_seconds", 10.0, 5.0, 60.0)
        performance_interval = self._maintenance_interval("maintenance_performance_interval_seconds", 15.0, 10.0, 60.0)
        housekeeping_interval = self._maintenance_interval("maintenance_housekeeping_interval_seconds", 20.0, 15.0, 120.0)
        jobs = [
            ("maintenance:liveness", liveness_interval, self._run_liveness, "maintenance_liveness"),
            ("maintenance:queue", queue_interval, self._run_queue, "maintenance_queue"),
            ("maintenance:performance", performance_interval, self._run_performance, "maintenance_performance"),
            ("maintenance:housekeeping", housekeeping_interval, self._run_housekeeping, "maintenance_housekeeping"),
            ("maintenance:reconcile", self._reconcile_interval(), self._run_reconcile, "periodic_reconcile"),
        ]
        self._maintenance_job_keys = [key for key, _interval, _callback, _reason in jobs]
        stagger = min(2.0, max(0.5, base / max(1, len(jobs))))
        for index, (key, interval, callback, reason) in enumerate(jobs):
            self._scheduler.schedule_periodic(
                key,
                interval,
                callback,
                reason=reason,
                initial_delay=interval + (index * stagger),
                payload={"maintenance_job": key},
            )

    def _run_housekeeping(self, job: RuntimeScheduledJob) -> None:
        ProcessManager.cleanup_stale_pid_claims()
        self._reconcile_duplicate_pid_claims()
        if bool(self._cfg.get("runtime_invariant_monitor_enabled", True)):
            self._invariant_monitor.scan()
        self._orphan_sweeper.sweep(self._cfg)
        self._recover_stale_joining_states()
        self._recover_failed_live_sessions()

    def _runtime_pid_validator(self, account: Any, pid: int) -> bool:
        return bool(ProcessManager.is_bound_game_alive(
            pid,
            owner_key=getattr(account, "_config_username", ""),
            expected_identity=getattr(account, "bound_process_identity", ""),
            expected_browser_tracker_id=getattr(account, "browser_tracker_id", ""),
        ))

    def _run_liveness(self, job: RuntimeScheduledJob) -> None:
        self._scan_liveness_watchdog()

    def _run_queue(self, job: RuntimeScheduledJob) -> None:
        self._enforce_queue_duration()
        self._enforce_auto_close()

    def _run_performance(self, job: RuntimeScheduledJob) -> None:
        self._apply_auto_process_priority()
        self._apply_cpu_limiter()
        self._enforce_window_resize()

    def _run_reconcile(self, job: RuntimeScheduledJob) -> None:
        self._runtime_reconcile_all(trigger=job.reason or "periodic_reconcile", force_restart=False)

    def _runtime_signal(self, acc: Account, signal: str, reason: str = "", **kwargs):
        owner = getattr(self, "_runtime_owner", None) or self._recovery
        return owner.handle_runtime_signal(acc, signal, reason, **kwargs)

    def _runtime_evaluate(self, acc: Account, trigger: str, force_restart: bool = False):
        owner = getattr(self, "_runtime_owner", None) or self._recovery
        return owner.request_evaluate(acc, trigger=trigger, force_restart=force_restart)

    def _runtime_reconcile_all(self, trigger: str, force_restart: bool = False):
        owner = getattr(self, "_runtime_owner", None) or self._recovery
        if hasattr(owner, "reconcile_all"):
            return owner.reconcile_all(self._accounts, trigger=trigger, force_restart=force_restart)
        return self._recovery.reconcile_all(self._accounts, trigger=trigger, force_restart=force_restart)

    def _set_recovery_status(self, acc: Account, status: str = "", reason: str = "", inflight: Optional[bool] = None):
        owner = getattr(self, "_runtime_owner", None)
        if owner and hasattr(owner, "set_recovery_status"):
            return owner.set_recovery_status(acc, status=status, reason=reason, inflight=inflight)
        if self._runtime_state:
            return self._runtime_state.set_recovery(acc, status=status, reason=reason, inflight=inflight)
        return None
