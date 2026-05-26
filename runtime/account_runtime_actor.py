from __future__ import annotations

import heapq
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass(frozen=True)
class AccountRuntimeSignal:
    name: str
    reason: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    priority: int = 50
    deadline_at: float = 0.0


@dataclass(frozen=True)
class AccountActorDecision:
    accepted: bool
    action: str
    reason: str = ""
    queue_depth: int = 0
    shadow_only: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accepted": self.accepted,
            "action": self.action,
            "reason": self.reason,
            "queue_depth": self.queue_depth,
            "shadow_only": self.shadow_only,
        }


class AccountRuntimeActor:
    """Bounded per-account signal facade for controlled migration.

    The first migration step uses this actor in shadow mode. It proves ordering,
    backpressure, and decision logging before it owns runtime mutation.
    """

    def __init__(
        self,
        account: Any,
        *,
        controller: Optional[Any] = None,
        max_mailbox: int = 128,
        shadow_only: bool = True,
        clock: Callable[[], float] = time.time,
    ):
        self._account = account
        self._controller = controller
        self._max_mailbox = max(1, int(max_mailbox or 128))
        self._shadow_only = bool(shadow_only)
        self._clock = clock
        self._mailbox: List[tuple[int, int, AccountRuntimeSignal]] = []
        self._sequence = 0
        self._last_heartbeat_at = float(clock())
        self._lock = threading.RLock()
        self.decisions: List[AccountActorDecision] = []

    @property
    def queue_depth(self) -> int:
        with self._lock:
            return len(self._mailbox)

    @property
    def last_heartbeat_at(self) -> float:
        with self._lock:
            return self._last_heartbeat_at

    def submit(self, signal: AccountRuntimeSignal) -> AccountActorDecision:
        with self._lock:
            if len(self._mailbox) >= self._max_mailbox:
                decision = AccountActorDecision(False, "reject", "mailbox_full", len(self._mailbox), self._shadow_only)
                self.decisions.append(decision)
                return decision
            self._sequence += 1
            heapq.heappush(self._mailbox, (int(signal.priority), self._sequence, signal))
            decision = AccountActorDecision(True, "queued", signal.reason or signal.name, len(self._mailbox), self._shadow_only)
            self.decisions.append(decision)
            return decision

    def drain(self, max_items: int = 1) -> List[AccountActorDecision]:
        decisions: List[AccountActorDecision] = []
        for _ in range(max(0, int(max_items or 0))):
            with self._lock:
                if not self._mailbox:
                    break
                _priority, _seq, signal = heapq.heappop(self._mailbox)
            decisions.append(self._handle(signal))
        with self._lock:
            self._last_heartbeat_at = float(self._clock())
        return decisions

    def heartbeat_stale(self, now: Optional[float] = None, timeout_seconds: float = 30.0) -> bool:
        current = self._clock() if now is None else float(now)
        with self._lock:
            last_heartbeat_at = float(self._last_heartbeat_at or 0.0)
        return (current - last_heartbeat_at) > max(1.0, float(timeout_seconds or 30.0))

    def _handle(self, signal: AccountRuntimeSignal) -> AccountActorDecision:
        action = str(signal.name or "").strip().lower()
        if self._shadow_only:
            decision = AccountActorDecision(True, f"shadow_{action}", signal.reason or action, self.queue_depth, True)
            self._record_decision(decision)
            return decision
        if not self._controller:
            decision = AccountActorDecision(False, action, "missing_controller", self.queue_depth, False)
            self._record_decision(decision)
            return decision
        if action == "evaluate":
            ok = bool(self._controller.request_evaluate(self._account, signal.reason or "actor_evaluate"))
        elif action == "rejoin":
            ok = bool(self._controller.request_rejoin(self._account, signal.reason or "actor_rejoin"))
        else:
            ok = False
        decision = AccountActorDecision(ok, action, signal.reason or action, self.queue_depth, False)
        self._record_decision(decision)
        return decision

    def _record_decision(self, decision: AccountActorDecision) -> None:
        with self._lock:
            self.decisions.append(decision)
