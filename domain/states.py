"""Runtime states, transitions, and signals in one module.

Previously split across account_state / state_transitions /
public_state_mapper / runtime_signals — four shallow modules whose
interfaces were nearly as large as their implementations. Merged so a
state lookup costs one import and one hop.
"""

from __future__ import annotations

from enum import Enum, auto
from typing import Dict, Optional, Set


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


RUNTIME_ALLOWED_TRANSITIONS: Dict[RuntimeState, Set[RuntimeState]] = {
    RuntimeState.STOPPED:    {RuntimeState.STARTING, RuntimeState.FAILED},
    RuntimeState.STARTING:   {RuntimeState.JOINING, RuntimeState.FAILED},
    RuntimeState.JOINING:    {RuntimeState.RUNNING, RuntimeState.FAILED},
    RuntimeState.RUNNING:    {RuntimeState.RECOVERING, RuntimeState.FAILED},
    RuntimeState.RECOVERING: {RuntimeState.BACKOFF, RuntimeState.FAILED},
    RuntimeState.BACKOFF:    {RuntimeState.STARTING, RuntimeState.FAILED},
    RuntimeState.FAILED:     set(),
}

LIFECYCLE_ALLOWED_TRANSITIONS = {
    state.value: {target.value for target in targets}
    for state, targets in RUNTIME_ALLOWED_TRANSITIONS.items()
}


def is_valid_runtime_transition(old: RuntimeState, new: RuntimeState, force: bool = False) -> bool:
    if old == new:
        return True
    if new == RuntimeState.FAILED:
        return True
    if force and new == RuntimeState.STOPPED:
        return True
    return new in RUNTIME_ALLOWED_TRANSITIONS.get(old, set())


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


class RuntimeSignal(str, Enum):
    LAUNCH_REQUESTED = "launch_requested"
    LAUNCH_BOUND = "launch_bound"
    IN_GAME_VERIFIED = "in_game_verified"
    DISCONNECT_DETECTED = "disconnect_detected"
    PROCESS_DEAD = "process_dead"
    REJOIN_REQUESTED = "rejoin_requested"
    RECOVERY_FAILED = "recovery_failed"

    FAULT = "fault"
    CRASH = "crash"
    WATCHDOG_TIMEOUT = "watchdog_timeout"
    PROCESS_LOST = "process_lost"
    LOADING_FREEZE = "loading_freeze"
    NETWORK_LOST = "network_lost"
    NETWORK_DROP = "network_drop"
    FATAL = "fatal"
    AUTH_FAILURE = "auth_failure"
    SESSION_FAILURE = "session_failure"
    LAUNCH_FAILURE = "launch_failure"
    LAUNCH_FAILED = "launch_failed"
    LAUNCH_SUCCESS = "launch_success"
    EVALUATE = "evaluate"


_SIGNAL_ALIASES = {
    RuntimeSignal.DISCONNECT_DETECTED.value: RuntimeSignal.FAULT.value,
    RuntimeSignal.PROCESS_DEAD.value: RuntimeSignal.PROCESS_LOST.value,
    RuntimeSignal.RECOVERY_FAILED.value: RuntimeSignal.FAULT.value,
    RuntimeSignal.LAUNCH_BOUND.value: RuntimeSignal.LAUNCH_SUCCESS.value,
    RuntimeSignal.IN_GAME_VERIFIED.value: RuntimeSignal.LAUNCH_SUCCESS.value,
}


def normalize_runtime_signal(value: object) -> str:
    raw = str(value or "").strip().lower()
    return _SIGNAL_ALIASES.get(raw, raw)


def is_recovery_signal(value: object) -> bool:
    return normalize_runtime_signal(value) in {
        RuntimeSignal.FAULT.value,
        RuntimeSignal.CRASH.value,
        RuntimeSignal.WATCHDOG_TIMEOUT.value,
        RuntimeSignal.PROCESS_LOST.value,
        RuntimeSignal.LOADING_FREEZE.value,
        RuntimeSignal.NETWORK_LOST.value,
        RuntimeSignal.NETWORK_DROP.value,
        RuntimeSignal.FATAL.value,
        RuntimeSignal.AUTH_FAILURE.value,
        RuntimeSignal.SESSION_FAILURE.value,
        RuntimeSignal.REJOIN_REQUESTED.value,
    }
