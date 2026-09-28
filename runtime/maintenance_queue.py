from __future__ import annotations

import time

from core import AccountState, flog_kv
from services.process_service import ProcessService


class MaintenanceQueueMixin:
    def _enforce_auto_close(self):
        if not bool(self._cfg.get("auto_close_enabled", False)):
            self._last_auto_close_at = time.time()
            return
        try:
            minutes = max(0.0, float(self._cfg.get("auto_close_minutes", 0) or 0))
        except Exception:
            minutes = 0.0
        seconds = minutes * 60.0
        if seconds <= 0:
            self._last_auto_close_at = time.time()
            return
        now = time.time()
        if (now - self._last_auto_close_at) < seconds:
            return
        self._last_auto_close_at = now
        killed = ProcessService.kill_all_roblox_clients(wait_seconds=4.0, reason="auto_close_cycle")
        flog_kv("QUEUE", "auto_close_cycle", killed=killed, minutes=f"{minutes:.1f}", seconds=f"{seconds:.1f}")
        for acc in self._accounts:
            with acc._lock:
                if acc.pid:
                    self._state_mgr.clear_process_binding(acc, reason="auto_close_cycle", increment_generation=True)
            self._state_mgr.transition(acc, AccountState.READY, reason="auto_close_cycle", force=True)
            self._runtime_evaluate(acc, trigger="auto_close_cycle")
            worker = self._workers.get(acc._config_username)
            if worker:
                worker.wake()
