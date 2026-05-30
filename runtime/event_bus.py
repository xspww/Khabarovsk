from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable, Dict, List, Set, Tuple

from core_logging import flog_kv


class EventName:
    STATE_CHANGE         = "state_change"
    INVALID_TRANSITION   = "invalid_transition"
    ACCOUNT_CRASH        = "account_crash"
    ACCOUNT_FAILED       = "account_failed"
    RECOVERY_REQUESTED   = "recovery_requested"
    REJOIN_SUCCESS       = "rejoin_success"
    LAUNCH_SUCCESS       = "launch_success"
    LAUNCH_FAILED        = "launch_failed"
    NETWORK_STATE_CHANGE = "network_state_change"
    NETWORK_LOST_ACCOUNT = "network_lost_account"

EVENT_CONTRACTS: Dict[str, Tuple[str, ...]] = {
    EventName.STATE_CHANGE: ("account", "old_state", "new_state"),
    EventName.INVALID_TRANSITION: ("account", "old_state", "new_state"),
    EventName.ACCOUNT_CRASH: ("account", "reason", "reason_msg"),
    EventName.ACCOUNT_FAILED: ("account", "reason", "reason_msg"),
    EventName.RECOVERY_REQUESTED: ("account", "reason"),
    EventName.REJOIN_SUCCESS: ("account",),
    EventName.LAUNCH_SUCCESS: ("account", "pid"),
    EventName.LAUNCH_FAILED: ("account", "reason"),
    EventName.NETWORK_STATE_CHANGE: ("old", "new"),
    EventName.NETWORK_LOST_ACCOUNT: ("account",),
}

CRITICAL_EVENT_NAMES: Set[str] = {
    EventName.STATE_CHANGE,
    EventName.INVALID_TRANSITION,
    EventName.ACCOUNT_CRASH,
    EventName.ACCOUNT_FAILED,
    EventName.RECOVERY_REQUESTED,
    EventName.REJOIN_SUCCESS,
    EventName.LAUNCH_SUCCESS,
    EventName.LAUNCH_FAILED,
    EventName.NETWORK_STATE_CHANGE,
    EventName.NETWORK_LOST_ACCOUNT,
}


# ─────────────────────────────────────────────────────────────────────────────
#  ACCOUNT DATA MODEL
# ─────────────────────────────────────────────────────────────────────────────

class EventBus:
    def __init__(self, workers: int = 4, max_pending: int = 128):
        self._handlers: Dict[str, List[Callable]] = {}
        self._lock = threading.Lock()
        self._tasks: queue.Queue = queue.Queue(maxsize=max(1, int(max_pending or 128)))
        self._slow_handler_sec = 2.0
        self._workers: List[threading.Thread] = []
        for idx in range(max(1, int(workers or 4))):
            thread = threading.Thread(
                target=self._run_worker,
                daemon=True,
                name=f"EventBus-{idx + 1}",
            )
            thread.start()
            self._workers.append(thread)

    def on(self, event: str, handler: Callable):
        with self._lock:
            self._handlers.setdefault(event, []).append(handler)

    def off(self, event: str, handler: Callable):
        with self._lock:
            if event in self._handlers:
                self._handlers[event] = [h for h in self._handlers[event] if h is not handler]

    def _run_worker(self):
        while True:
            event, handler, kwargs = self._tasks.get()
            try:
                self._invoke_handler(event, handler, kwargs)
            finally:
                self._tasks.task_done()

    def _invoke_handler(self, event: str, handler: Callable, kwargs: Dict[str, Any], inline: bool = False):
        started = time.time()
        try:
            handler(**kwargs)
        except Exception as e:
            flog_kv("BUS", "handler_error", "warning", bus_event=event, error=e, inline=inline)
        finally:
            elapsed = time.time() - started
            if elapsed >= self._slow_handler_sec:
                flog_kv("BUS", "slow_handler", "warning", bus_event=event, seconds=f"{elapsed:.2f}", inline=inline)

    def emit(self, event: str, **kwargs):
        required = EVENT_CONTRACTS.get(event)
        if required:
            missing = [key for key in required if key not in kwargs]
            if missing:
                flog_kv("BUS", "contract_violation", "warning", bus_event=event, missing=",".join(missing))
        with self._lock:
            handlers = list(self._handlers.get(event, []))
        for h in handlers:
            payload = dict(kwargs)
            try:
                self._tasks.put_nowait((event, h, payload))
            except queue.Full:
                if event in CRITICAL_EVENT_NAMES:
                    flog_kv("BUS", "queue_full_inline", "warning", bus_event=event, pending=self._tasks.qsize())
                    self._invoke_handler(event, h, payload, inline=True)
                else:
                    flog_kv("BUS", "queue_full_drop", "warning", bus_event=event, pending=self._tasks.qsize())


# ─────────────────────────────────────────────────────────────────────────────
#  STATE MANAGER
# ─────────────────────────────────────────────────────────────────────────────
