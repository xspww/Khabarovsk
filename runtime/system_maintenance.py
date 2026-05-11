from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

from core import Account, StateManager, flog
from runtime.supervisor_runtime import SupervisorRuntime
from services.process_service import ProcessManager
from runtime.maintenance_liveness import MaintenanceLivenessMixin
from runtime.maintenance_performance import (
    MaintenancePerformanceMixin,
    _apply_cpu_limiter_for_bound_process,
    _window_arrange_settings_from_config,
    _window_resize_target_from_config,
)
from runtime.maintenance_presence import MaintenancePresenceMixin, _account_presence_user_id
from runtime.maintenance_queue import MaintenanceQueueMixin


class SystemMaintenance(
    MaintenanceLivenessMixin,
    MaintenancePresenceMixin,
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
    ):
        super().__init__(daemon=True, name="Maintenance")
        self._accounts = accounts
        self._workers = workers
        self._recovery = recovery
        self._runtime_state = getattr(recovery, "_runtime_state", None)
        self._state_mgr = state_mgr
        self._cfg = cfg
        self._stop = stop
        self._supervisor = supervisor
        self._last_auto_close_at = time.time()
        self._last_priority_apply_at = 0.0
        self._last_cpu_limiter_apply_at = 0.0
        self._last_window_resize_at = 0.0
        self._last_popup_scan_at: Dict[str, float] = {}
        self._last_popup_batch_at = 0.0
        self._popup_scan_cursor = 0

    def run(self):
        flog("[MAINT] started")
        interval = max(1.0, min(5.0, float(self._cfg.get("periodic_reconcile_interval", 15) or 15)))
        while not self._stop.wait(timeout=interval):
            ProcessManager.cleanup_stale_pid_claims()
            self._reconcile_duplicate_pid_claims()
            self._recover_stale_joining_states()
            self._recover_failed_live_sessions()
            self._scan_liveness_watchdog()
            self._enforce_queue_duration()
            self._enforce_auto_close()
            self._apply_auto_process_priority()
            self._apply_cpu_limiter()
            self._enforce_window_resize()
            self._recovery.reconcile_all(self._accounts, trigger="periodic_reconcile", force_restart=False)
            for worker in self._workers.values():
                worker.wake()
        flog("[MAINT] stopped")
