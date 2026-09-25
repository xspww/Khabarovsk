from __future__ import annotations

import os
import threading
from typing import Dict, Optional, Tuple

from core import flog

class RealtimeResourceMonitor:
    """
    Background thread อัปเดต CPU% และ RAM MB ของทุก Roblox PID ที่ลงทะเบียนไว้
    ทุก 1 วินาที (ไม่ block API thread)

    การใช้งาน:
      monitor = RealtimeResourceMonitor()
      monitor.start()
      monitor.register(pid)          # เพิ่ม PID ที่ต้องการติดตาม
      monitor.unregister(pid)        # ลบ PID ออก
      cpu, ram = monitor.get(pid)    # ดึงค่าล่าสุด (non-blocking)
    """
    INTERVAL = 1.0  # วินาที

    def __init__(self):
        self._lock    = threading.Lock()
        self._pids:   Dict[int, object]  = {}  # pid → psutil.Process
        self._cpu:    Dict[int, float]   = {}  # pid → cpu%
        self._ram:    Dict[int, float]   = {}  # pid → MB
        self._stop    = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._logical_cpus = max(1, int(os.cpu_count() or 1))

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="RT-ResourceMonitor"
            )
            self._thread.start()

    def stop(self):
        with self._lock:
            thread = self._thread
            if not thread:
                return
            self._stop.set()
        thread.join(timeout=self.INTERVAL + 1.0)
        with self._lock:
            if self._thread is thread:
                self._thread = None

    def register(self, pid: int):
        if pid is None:
            return
        try:
            import psutil
            with self._lock:
                if pid not in self._pids:
                    p = psutil.Process(pid)
                    p.cpu_percent(interval=None)  # prime — ค่าแรกเสมอ 0
                    self._pids[pid] = p
                    self._cpu[pid]  = 0.0
                    self._ram[pid]  = 0.0
                    flog(f"[RT_MON] register PID {pid}")
        except Exception as e:
            flog(f"[RT_MON] register error PID {pid}: {e}", "warning")

    def unregister(self, pid: int):
        if pid is None:
            return
        with self._lock:
            self._pids.pop(pid, None)
            self._cpu.pop(pid, None)
            self._ram.pop(pid, None)
        flog(f"[RT_MON] unregister PID {pid}")

    def get(self, pid: int) -> Tuple[float, float]:
        """คืน (cpu%, ram_mb) ล่าสุด"""
        with self._lock:
            return self._cpu.get(pid, 0.0), self._ram.get(pid, 0.0)

    def get_cpu(self, pid: int) -> float:
        with self._lock:
            return self._cpu.get(pid, 0.0)

    def get_ram(self, pid: int) -> float:
        with self._lock:
            return self._ram.get(pid, 0.0)

    def _run(self):
        flog("[RT_MON] started")
        while not self._stop.wait(timeout=self.INTERVAL):
            with self._lock:
                pids = dict(self._pids)

            dead = []
            for pid, proc in pids.items():
                try:
                    # Normalize by logical CPU count so the reported value tracks
                    # Windows Task Manager's per-process CPU percentage more closely.
                    cpu_raw = proc.cpu_percent(interval=None)
                    cpu = max(0.0, cpu_raw / self._logical_cpus)
                    ram = proc.memory_info().rss / (1024 * 1024)
                    with self._lock:
                        self._cpu[pid] = round(cpu, 2)
                        self._ram[pid] = round(ram, 1)
                except Exception:
                    dead.append(pid)

            for pid in dead:
                self.unregister(pid)

        flog("[RT_MON] stopped")


# Singleton
_rt_monitor = RealtimeResourceMonitor()


def get_rt_monitor() -> RealtimeResourceMonitor:
    return _rt_monitor

__all__ = ["RealtimeResourceMonitor", "get_rt_monitor"]
