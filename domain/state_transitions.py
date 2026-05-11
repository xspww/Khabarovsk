from __future__ import annotations

from typing import Dict, Set

from .account_state import RuntimeState


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
