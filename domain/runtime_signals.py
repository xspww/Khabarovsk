from __future__ import annotations

from enum import Enum


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
