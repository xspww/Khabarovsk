from __future__ import annotations

import threading
import time
import urllib.request
from typing import Optional

from core import EventBus, flog

NET_ONLINE   = "ONLINE"
NET_DEGRADED = "DEGRADED"
NET_OFFLINE  = "OFFLINE"

class NetworkState:
    ONLINE   = NET_ONLINE
    DEGRADED = NET_DEGRADED
    OFFLINE  = NET_OFFLINE

CHECK_TARGETS = [
    "http://connectivitycheck.gstatic.com/generate_204",
    "http://www.msftncsi.com/ncsi.txt",
]
CHECK_ROBLOX = "https://www.roblox.com"

class NetworkMonitor:
    def __init__(self, bus: EventBus, interval: int = 5, debounce: int = 3,
                 stop: Optional[threading.Event] = None):
        self._bus         = bus
        self._interval    = interval
        self._debounce    = debounce
        self._stop        = stop or threading.Event()
        self._state       = NET_ONLINE
        self._state_since = time.time()
        self._lock        = threading.Lock()
        self._online_ev   = threading.Event()
        self._online_ev.set()
        self._thread      = threading.Thread(target=self._run, daemon=True, name="NetworkMonitor")

    def start(self):
        if not self._thread.is_alive():
            self._thread.start()

    def join(self, timeout: Optional[float] = None):
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def get_state(self) -> str:
        with self._lock:
            return self._state

    def is_online(self) -> bool:
        return self.get_state() == NET_ONLINE

    def wait_until_online(self, timeout: Optional[float] = None) -> bool:
        return self._online_ev.wait(timeout=timeout)

    def _check(self) -> str:
        internet_ok = False
        for url in CHECK_TARGETS:
            if self._ping(url, timeout=3):
                internet_ok = True
                break
        if not internet_ok:
            return NET_OFFLINE
        roblox_ok = self._ping(CHECK_ROBLOX, timeout=4)
        return NET_ONLINE if roblox_ok else NET_DEGRADED

    @staticmethod
    def _ping(url: str, timeout: int = 3) -> bool:
        try:
            req = urllib.request.Request(url, method="GET",
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                r.read(64)
            return True
        except Exception:
            return False

    def _run(self):
        first_run = True
        pending_state: Optional[str] = None
        pending_since: float = 0.0

        while not self._stop.is_set():
            new_state = self._check()
            with self._lock:
                current = self._state

            if new_state != current:
                if first_run:
                    with self._lock:
                        self._state = new_state
                        self._state_since = time.time()
                        if new_state == NET_ONLINE:
                            self._online_ev.set()
                        else:
                            self._online_ev.clear()
                    pending_state = None
                elif pending_state != new_state:
                    pending_state = new_state
                    pending_since = time.time()
                elif time.time() - pending_since >= self._debounce:
                    with self._lock:
                        self._state = new_state
                        self._state_since = time.time()
                        if new_state == NET_ONLINE:
                            self._online_ev.set()
                        else:
                            self._online_ev.clear()
                    flog(f"[NET] {current} → {new_state}")
                    self._bus.emit("network_state_change", old=current, new=new_state)
                    pending_state = None
            else:
                pending_state = None
                if first_run:
                    flog(f"[NET] Initial state confirmed: {new_state}")

            first_run = False
            self._stop.wait(timeout=self._interval)

    def status_dict(self) -> dict:
        with self._lock:
            return {"state": self._state, "since": self._state_since}

__all__ = ["NET_ONLINE", "NET_DEGRADED", "NET_OFFLINE", "NetworkState", "NetworkMonitor"]
