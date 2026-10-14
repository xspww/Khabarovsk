from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable
from typing import Any

LogFunction = Callable[..., None]


class LogRateLimiter:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.RLock()
        self._last_emit_at: dict[tuple[Hashable, ...], float] = {}

    def clear(self) -> None:
        with self._lock:
            self._last_emit_at.clear()

    def should_emit(self, key: tuple[Hashable, ...], interval_seconds: float) -> bool:
        now = self._clock()
        interval = max(0.0, float(interval_seconds or 0.0))
        with self._lock:
            last = self._last_emit_at.get(tuple(key))
            if last is not None and (now - last) < interval:
                return False
            self._last_emit_at[tuple(key)] = now
            return True

    def emit(
        self,
        key: tuple[Hashable, ...],
        interval_seconds: float,
        log_func: LogFunction,
        *args: Any,
        **kwargs: Any,
    ) -> bool:
        if not self.should_emit(key, interval_seconds):
            return False
        log_func(*args, **kwargs)
        return True

