"""Slot policy for the recovery module.

Counts how many accounts currently hold an active farm slot and decides
whether another account may queue. Previously these four helpers lived on
the coordinator mixin and the evaluator reached into all of them; now the
decision leverages this one collaborator.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core import Account, AccountState

ACTIVE_SLOT_STATES = {
    AccountState.QUEUED,
    AccountState.LAUNCHING,
    AccountState.VERIFY,
    AccountState.IN_GAME,
}


class RecoverySlotPolicy:
    """Owns slot counting so the evaluator needs one seam, not four."""

    def __init__(
        self,
        cfg_getter: Callable[[], dict],
        accounts_getter: Callable[[], list[Account]],
    ):
        self._cfg_getter = cfg_getter
        self._accounts_getter = accounts_getter

    def max_concurrent_accounts(self) -> int:
        try:
            cfg = self._cfg_getter()
            return max(1, int(float(cfg.get("max_concurrent_accounts", 40) or 40)))
        except Exception:
            return 40

    def queue_delay_seconds(self) -> float:
        try:
            cfg = self._cfg_getter()
            return max(1.0, float(cfg.get("queue_delay_seconds", cfg.get("launch_rate_interval", 15)) or 15))
        except Exception:
            return 15.0

    def active_slot_count(self, excluding: Account | None = None) -> int:
        count = 0
        for item in self._accounts_getter():
            if item is excluding:
                continue
            with item._lock:
                if item.desired_state == AccountState.IN_GAME and item.state in ACTIVE_SLOT_STATES:
                    count += 1
        return count

    def slot_available(self, acc: Account) -> bool:
        return self.active_slot_count(excluding=acc) < self.max_concurrent_accounts()


def describe_slot_full(policy: RecoverySlotPolicy, acc: Any) -> str:
    return (
        f"queue_slot_full active={policy.active_slot_count(excluding=acc)}"
        f" max={policy.max_concurrent_accounts()}"
    )
