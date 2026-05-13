from __future__ import annotations

import time

from core import Account, AccountState, flog_kv
from services.process_service import ProcessService


class MaintenanceQueueMixin:
    def _queue_delay_seconds(self) -> float:
        try:
            return max(1.0, float(self._cfg.get("queue_delay_seconds", self._cfg.get("launch_rate_interval", 15)) or 15))
        except Exception:
            return 15.0

    def _queue_duration_seconds(self) -> float:
        if bool(self._cfg.get("multi_roblox_enabled", True)) and not bool(self._cfg.get("rt_rotation_enabled", False)):
            return 0.0
        try:
            return max(0.0, float(self._cfg.get("queue_duration_seconds", 0) or 0))
        except Exception:
            return 0.0

    def _cycle_account(self, acc: Account, reason: str):
        with acc._lock:
            pid = acc.pid
            runtime_generation = acc.runtime_generation
        if pid:
            ProcessService.safe_kill_bound_process(
                acc,
                self._state_mgr,
                reason=reason,
                expected_runtime_generation=runtime_generation,
            )
        delay = self._queue_delay_seconds()
        with acc._lock:
            self._state_mgr.set_cooldown(acc, time.time() + delay, reason=reason)
        self._state_mgr.transition(acc, AccountState.READY, reason=reason, force=True)
        flog_kv("QUEUE", "cycle_account", account=acc.display_name, reason=reason, delay=f"{delay:.1f}")
        self._runtime_evaluate(acc, trigger=reason)
        worker = self._workers.get(acc._config_username)
        if worker:
            worker.wake()

    def _enforce_queue_duration(self):
        duration = self._queue_duration_seconds()
        if duration <= 0:
            return
        now = time.time()
        for acc in self._accounts:
            with acc._lock:
                if acc.desired_state != AccountState.IN_GAME or acc.state != AccountState.IN_GAME:
                    continue
                started = float(acc.in_game_since or 0.0)
            if started and (now - started) >= duration:
                self._cycle_account(acc, "queue_duration_elapsed")

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
