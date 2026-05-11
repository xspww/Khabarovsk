from __future__ import annotations

from enum import Enum, auto


class AccountState(Enum):
    IDLE         = auto()
    READY        = auto()
    QUEUED       = auto()
    LAUNCHING    = auto()
    VERIFY       = auto()
    IN_GAME      = auto()
    CRASH        = auto()
    FAILED       = auto()
    NETWORK_LOST = auto()
    COOLDOWN     = auto()


class RuntimeState(str, Enum):
    STOPPED    = "STOPPED"
    STARTING   = "STARTING"
    JOINING    = "JOINING"
    RUNNING    = "RUNNING"
    RECOVERING = "RECOVERING"
    BACKOFF    = "BACKOFF"
    FAILED     = "FAILED"

