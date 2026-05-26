from __future__ import annotations

import queue
import threading
from typing import Any, Callable


class SchedulerDispatchPool:
    def __init__(self, name: str, worker_count: int, stop_event: threading.Event, dispatch: Callable[..., None]):
        self._stop = stop_event
        self._dispatch = dispatch
        self._queue: queue.Queue = queue.Queue()
        self._workers = []
        self._lock = threading.Lock()
        self._active_count = 0
        for idx in range(max(1, int(worker_count or 1))):
            worker = threading.Thread(
                target=self._loop,
                daemon=True,
                name=f"{name}-Dispatch-{idx + 1}",
            )
            worker.start()
            self._workers.append(worker)

    def submit(self, job: Any, callback: Any, current: float) -> None:
        self._queue.put((job, callback, current))

    def stop(self, timeout: float = 2.0) -> None:
        for _worker in self._workers:
            self._queue.put(None)
        deadline = None if timeout is None else max(0.0, float(timeout or 0.0)) / max(1, len(self._workers))
        for worker in self._workers:
            worker.join(timeout=deadline)

    def snapshot(self) -> dict:
        with self._lock:
            active_count = int(self._active_count)
        return {
            "queued_count": int(self._queue.qsize()),
            "active_count": active_count,
            "worker_count": len(self._workers),
            "worker_alive_count": sum(1 for worker in self._workers if worker.is_alive()),
        }

    def _loop(self) -> None:
        while not self._stop.is_set():
            item = self._queue.get()
            try:
                if item is None:
                    return
                job, callback, current = item
                with self._lock:
                    self._active_count += 1
                self._dispatch(job, callback, current)
            finally:
                if item is not None:
                    with self._lock:
                        self._active_count = max(0, self._active_count - 1)
                self._queue.task_done()
