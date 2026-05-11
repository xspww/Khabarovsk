from __future__ import annotations

import threading
import time
from typing import Optional

from core import Account, AccountState, flog_kv
from services.process_service import ProcessManager
from services.resource_monitor import get_rt_monitor


class RobloxWatchdog(threading.Thread):
    CHECK_INTERVAL = 5.0

    def __init__(self, acc: Account, worker: "AccountWorker", cfg: dict, stop: threading.Event):
        super().__init__(daemon=True, name=f"Watchdog-{acc.username}")
        self.acc = acc
        self.worker = worker
        self.cfg = cfg
        self._stop = stop
        self._mon = get_rt_monitor()

    def run(self):
        acc = self.acc
        flog_kv("WATCHDOG", "started", account=acc.display_name)
        abnormal_since: Optional[float] = None

        while not self._stop.wait(timeout=self.CHECK_INTERVAL):
            if not self.cfg.get("watchdog_enabled", True):
                abnormal_since = None
                continue

            if acc.state != AccountState.IN_GAME:
                abnormal_since = None
                continue

            pid = acc.pid
            if not pid or not ProcessManager.is_bound_game_alive(
                pid,
                owner_key=acc._config_username,
                expected_identity=acc.bound_process_identity,
            ):
                abnormal_since = None
                self.worker.handle_missing_bound_process("watchdog_pid_missing")
                continue

            now = time.time()
            runtime = now - (acc.in_game_since or now)
            loading_grace = max(30.0, float(self.cfg.get("watchdog_loading_grace", 90) or 90))
            if runtime < loading_grace:
                abnormal_since = None
                with acc._lock:
                    acc.last_activity_at = now
                    acc.last_activity_reason = "loading_grace"
                continue

            cpu_low = float(self.cfg.get("watchdog_cpu_low", 0.9))
            ram_low = float(self.cfg.get("watchdog_ram_low", 90.0))
            hold_sec = float(self.cfg.get("watchdog_hold_time", 60))
            activity_timeout = max(hold_sec, float(self.cfg.get("watchdog_activity_timeout", 180) or 180))
            activity = ProcessManager.get_game_activity(pid)
            cpu = float(activity.get("cpu") or 0.0)
            ram = float(activity.get("ram_mb") or 0.0)
            windows = int(activity.get("windows") or 0)
            if cpu <= 0.0 and ram <= 0.0:
                abnormal_since = None
                continue

            responsive_window = windows > 0 and not ProcessManager.is_not_responding(pid)
            resource_active = cpu >= cpu_low
            memory_present = ram >= ram_low
            if responsive_window or resource_active:
                if abnormal_since is not None:
                    flog_kv("WATCHDOG", "resource_recovered", account=acc.display_name, pid=pid)
                abnormal_since = None
                with acc._lock:
                    acc.last_activity_at = now
                    acc.last_activity_reason = "window" if responsive_window else "resource"
                    acc.last_activity_cpu = cpu
                    acc.last_activity_ram_mb = ram
                    acc.last_watchdog_classification = "active"
                continue

            is_abnormal = (cpu < cpu_low) and (memory_present or windows > 0)
            if not is_abnormal:
                abnormal_since = None
                with acc._lock:
                    acc.last_watchdog_classification = "loading"
                continue

            if self.worker.connection_recovery_active():
                abnormal_since = None
                with acc._lock:
                    acc.last_watchdog_classification = "reconnecting"
                continue

            with acc._lock:
                last_activity = acc.last_activity_at or acc.in_game_since or now
            inactive_for = max(0.0, now - last_activity)
            if inactive_for < activity_timeout:
                classification = "frozen_hold" if windows > 0 else "loading"
                with acc._lock:
                    acc.last_watchdog_classification = classification
                if abnormal_since is None:
                    abnormal_since = now
                continue

            if abnormal_since is None:
                abnormal_since = now
                flog_kv(
                    "WATCHDOG",
                    "abnormal_hold_started",
                    "warning",
                    account=acc.display_name,
                    pid=pid,
                    cpu=f"{cpu:.2f}",
                    ram=f"{ram:.1f}",
                    windows=windows,
                    inactive=f"{inactive_for:.1f}",
                    hold=f"{hold_sec:.1f}",
                )
            elif now - abnormal_since >= hold_sec:
                reason_key = "loading_freeze" if windows <= 0 else "watchdog_timeout"
                flog_kv(
                    "WATCHDOG",
                    "frozen_recovery_signal",
                    "warning",
                    account=acc.display_name,
                    pid=pid,
                    reason=reason_key,
                    cpu=f"{cpu:.2f}",
                    ram=f"{ram:.1f}",
                    windows=windows,
                    inactive=f"{inactive_for:.1f}",
                )
                pid_was = pid
                with acc._lock:
                    runtime_generation = acc.runtime_generation
                    session_id = acc.session_id
                    launch_nonce = acc.launch_nonce
                    transaction_id = acc.rejoin_transaction_id
                    acc.last_watchdog_classification = reason_key
                    acc.last_activity_reason = f"watchdog:{reason_key}"
                flog_kv(
                    "WATCHDOG",
                    "verified_kill_deferred",
                    "warning",
                    account=acc.display_name,
                    pid=pid_was,
                    reason=reason_key,
                    runtime_generation=runtime_generation,
                    session_id=session_id,
                    transaction_id=transaction_id,
                )
                with acc._lock:
                    signal_generation = acc.runtime_generation
                abnormal_since = None
                self.worker.report_fault(
                    reason_key,
                    f"PID={pid_was} CPU={cpu:.2f}% RAM={ram:.1f}MB windows={windows} inactive={inactive_for:.1f}s",
                    expected_runtime_generation=signal_generation,
                    expected_session_id=session_id,
                    expected_launch_nonce=launch_nonce,
                    expected_transaction_id=transaction_id,
                )

        flog_kv("WATCHDOG", "stopped", account=acc.display_name)
