from __future__ import annotations

from enum import Enum
from typing import Dict, Set

from .account_state import AccountState, RuntimeState


class RuntimeLifecycleState(str, Enum):
    IDLE = "IDLE"
    QUEUED = "QUEUED"
    STARTING = "STARTING"
    JOINING = "JOINING"
    IN_GAME = "IN_GAME"
    CHECKING_DISCONNECT = "CHECKING_DISCONNECT"
    RECOVERING = "RECOVERING"
    COOLDOWN = "COOLDOWN"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


LIFECYCLE_ALLOWED_TRANSITIONS: Dict[RuntimeLifecycleState, Set[RuntimeLifecycleState]] = {
    RuntimeLifecycleState.STOPPED: {
        RuntimeLifecycleState.IDLE,
        RuntimeLifecycleState.QUEUED,
        RuntimeLifecycleState.STARTING,
        RuntimeLifecycleState.FAILED,
    },
    RuntimeLifecycleState.IDLE: {
        RuntimeLifecycleState.QUEUED,
        RuntimeLifecycleState.STARTING,
        RuntimeLifecycleState.STOPPED,
        RuntimeLifecycleState.FAILED,
    },
    RuntimeLifecycleState.QUEUED: {
        RuntimeLifecycleState.STARTING,
        RuntimeLifecycleState.STOPPED,
        RuntimeLifecycleState.FAILED,
    },
    RuntimeLifecycleState.STARTING: {
        RuntimeLifecycleState.JOINING,
        RuntimeLifecycleState.RECOVERING,
        RuntimeLifecycleState.STOPPED,
        RuntimeLifecycleState.FAILED,
    },
    RuntimeLifecycleState.JOINING: {
        RuntimeLifecycleState.IN_GAME,
        RuntimeLifecycleState.CHECKING_DISCONNECT,
        RuntimeLifecycleState.RECOVERING,
        RuntimeLifecycleState.STOPPED,
        RuntimeLifecycleState.FAILED,
    },
    RuntimeLifecycleState.IN_GAME: {
        RuntimeLifecycleState.CHECKING_DISCONNECT,
        RuntimeLifecycleState.RECOVERING,
        RuntimeLifecycleState.COOLDOWN,
        RuntimeLifecycleState.STOPPED,
        RuntimeLifecycleState.FAILED,
    },
    RuntimeLifecycleState.CHECKING_DISCONNECT: {
        RuntimeLifecycleState.IN_GAME,
        RuntimeLifecycleState.RECOVERING,
        RuntimeLifecycleState.COOLDOWN,
        RuntimeLifecycleState.STOPPED,
        RuntimeLifecycleState.FAILED,
    },
    RuntimeLifecycleState.RECOVERING: {
        RuntimeLifecycleState.STARTING,
        RuntimeLifecycleState.COOLDOWN,
        RuntimeLifecycleState.STOPPED,
        RuntimeLifecycleState.FAILED,
    },
    RuntimeLifecycleState.COOLDOWN: {
        RuntimeLifecycleState.QUEUED,
        RuntimeLifecycleState.STARTING,
        RuntimeLifecycleState.STOPPED,
        RuntimeLifecycleState.FAILED,
    },
    RuntimeLifecycleState.FAILED: {
        RuntimeLifecycleState.STOPPED,
    },
}


PUBLIC_TO_LIFECYCLE: Dict[AccountState, RuntimeLifecycleState] = {
    AccountState.IDLE: RuntimeLifecycleState.IDLE,
    AccountState.READY: RuntimeLifecycleState.IDLE,
    AccountState.QUEUED: RuntimeLifecycleState.QUEUED,
    AccountState.LAUNCHING: RuntimeLifecycleState.STARTING,
    AccountState.VERIFY: RuntimeLifecycleState.JOINING,
    AccountState.IN_GAME: RuntimeLifecycleState.IN_GAME,
    AccountState.CRASH: RuntimeLifecycleState.RECOVERING,
    AccountState.FAILED: RuntimeLifecycleState.FAILED,
    AccountState.NETWORK_LOST: RuntimeLifecycleState.CHECKING_DISCONNECT,
    AccountState.COOLDOWN: RuntimeLifecycleState.COOLDOWN,
}


LEGACY_RUNTIME_TO_LIFECYCLE: Dict[RuntimeState, RuntimeLifecycleState] = {
    RuntimeState.STOPPED: RuntimeLifecycleState.STOPPED,
    RuntimeState.STARTING: RuntimeLifecycleState.STARTING,
    RuntimeState.JOINING: RuntimeLifecycleState.JOINING,
    RuntimeState.RUNNING: RuntimeLifecycleState.IN_GAME,
    RuntimeState.RECOVERING: RuntimeLifecycleState.RECOVERING,
    RuntimeState.BACKOFF: RuntimeLifecycleState.COOLDOWN,
    RuntimeState.FAILED: RuntimeLifecycleState.FAILED,
}


def lifecycle_for_public(public_state: AccountState) -> RuntimeLifecycleState:
    return PUBLIC_TO_LIFECYCLE.get(public_state, RuntimeLifecycleState.IDLE)


def lifecycle_for_legacy_runtime(runtime_state: RuntimeState) -> RuntimeLifecycleState:
    return LEGACY_RUNTIME_TO_LIFECYCLE.get(runtime_state, RuntimeLifecycleState.STOPPED)


def is_valid_lifecycle_transition(
    old: RuntimeLifecycleState,
    new: RuntimeLifecycleState,
    *,
    force_stop: bool = False,
) -> bool:
    if old == new:
        return True
    if force_stop and new == RuntimeLifecycleState.STOPPED:
        return True
    return new in LIFECYCLE_ALLOWED_TRANSITIONS.get(old, set())
