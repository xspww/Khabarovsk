from __future__ import annotations

from typing import Dict, Optional

from .account_state import AccountState, RuntimeState


PUBLIC_TO_RUNTIME_STATE: Dict[AccountState, RuntimeState] = {
    AccountState.IDLE:         RuntimeState.STOPPED,
    AccountState.READY:        RuntimeState.STOPPED,
    AccountState.QUEUED:       RuntimeState.STARTING,
    AccountState.LAUNCHING:    RuntimeState.STARTING,
    AccountState.VERIFY:       RuntimeState.JOINING,
    AccountState.IN_GAME:      RuntimeState.RUNNING,
    AccountState.CRASH:        RuntimeState.RECOVERING,
    AccountState.FAILED:       RuntimeState.FAILED,
    AccountState.NETWORK_LOST: RuntimeState.RECOVERING,
    AccountState.COOLDOWN:     RuntimeState.BACKOFF,
}

RUNTIME_TO_DEFAULT_PUBLIC_STATE: Dict[RuntimeState, AccountState] = {
    RuntimeState.STOPPED:    AccountState.IDLE,
    RuntimeState.STARTING:   AccountState.LAUNCHING,
    RuntimeState.JOINING:    AccountState.VERIFY,
    RuntimeState.RUNNING:    AccountState.IN_GAME,
    RuntimeState.RECOVERING: AccountState.NETWORK_LOST,
    RuntimeState.BACKOFF:    AccountState.COOLDOWN,
    RuntimeState.FAILED:     AccountState.FAILED,
}

LIFECYCLE_STATE: Dict[AccountState, str] = {
    AccountState.IDLE:         "STOPPED",
    AccountState.READY:        "STOPPED",
    AccountState.QUEUED:       "STARTING",
    AccountState.LAUNCHING:    "STARTING",
    AccountState.VERIFY:       "JOINING",
    AccountState.IN_GAME:      "RUNNING",
    AccountState.CRASH:        "RECOVERING",
    AccountState.FAILED:       "FAILED",
    AccountState.NETWORK_LOST: "RECOVERING",
    AccountState.COOLDOWN:     "BACKOFF",
}


def runtime_state_for_public(public_state: AccountState) -> RuntimeState:
    return PUBLIC_TO_RUNTIME_STATE.get(public_state, RuntimeState.STOPPED)


def public_state_for_runtime(runtime_state: RuntimeState, fallback: Optional[AccountState] = None) -> AccountState:
    return fallback or RUNTIME_TO_DEFAULT_PUBLIC_STATE.get(runtime_state, AccountState.IDLE)
