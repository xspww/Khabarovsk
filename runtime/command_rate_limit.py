from __future__ import annotations

import threading
import time
from typing import Dict, Tuple

FORCE_REJOIN_INTERVAL_SECONDS = 10.0


class PerAccountRateLimiter:
    def __init__(self, interval_seconds: float):
        self._interval_seconds = max(0.0, float(interval_seconds or 0.0))
        self._lock = threading.Lock()
        self._last_at: Dict[str, float] = {}

    def check(self, account_key: str) -> Tuple[bool, str]:
        now = time.time()
        with self._lock:
            last_at = float(self._last_at.get(account_key) or 0.0)
            wait = self._interval_seconds - (now - last_at) if last_at else 0.0
            if last_at and wait > 0:
                retry_after = max(1, int(wait + 0.999))
                return False, f"Force rejoin rate limited; retry in {retry_after}s"
            self._last_at[account_key] = now
        return True, ""
