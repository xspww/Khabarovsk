from __future__ import annotations

import threading
import time


class GlobalLaunchLimiter:
    def __init__(self, interval: float = 6.0):
        self.interval = interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self, stop: threading.Event | None = None):
        with self._lock:
            now   = time.time()
            delta = self.interval - (now - self._last)
            if delta > 0:
                if stop:
                    stop.wait(timeout=delta)
                else:
                    time.sleep(delta)
            self._last = time.time()


# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG MANAGER
# ─────────────────────────────────────────────────────────────────────────────
