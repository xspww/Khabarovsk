from __future__ import annotations

from typing import Any, Iterable, Optional

from core import AccountState


ACTIVE_SLOT_STATES = {
    AccountState.QUEUED,
    AccountState.LAUNCHING,
    AccountState.VERIFY,
    AccountState.IN_GAME,
}


def max_concurrent_accounts(cfg: dict) -> int:
    try:
        return max(1, int(float(cfg.get("max_concurrent_accounts", 40) or 40)))
    except Exception:
        return 40


def queue_delay_seconds(cfg: dict) -> float:
    try:
        return max(1.0, float(cfg.get("queue_delay_seconds", cfg.get("launch_rate_interval", 15)) or 15))
    except Exception:
        return 15.0


def active_slot_count(accounts: Iterable[Any], excluding: Optional[Any] = None) -> int:
    count = 0
    for item in accounts:
        if item is excluding:
            continue
        with item._lock:
            if item.desired_state == AccountState.IN_GAME and item.state in ACTIVE_SLOT_STATES:
                count += 1
    return count


def queue_slot_available(accounts: Iterable[Any], cfg: dict, acc: Any) -> bool:
    return active_slot_count(accounts, excluding=acc) < max_concurrent_accounts(cfg)
