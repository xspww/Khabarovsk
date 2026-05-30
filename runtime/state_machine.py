from __future__ import annotations

import threading
import time
from typing import Dict, Optional, Set

from core_logging import flog_kv
from domain.account_model import Account
from domain.account_state import AccountState
from domain.public_state_mapper import LIFECYCLE_STATE, runtime_state_for_public
from domain.state_transitions import LIFECYCLE_ALLOWED_TRANSITIONS
from runtime.event_bus import EventBus, EventName
from runtime.runtime_state_manager import RuntimeStateManager


ALLOWED_STATE_TRANSITIONS: Dict[AccountState, Set[AccountState]] = {
    AccountState.IDLE: {
        AccountState.READY,
        AccountState.FAILED,
        AccountState.NETWORK_LOST,
    },
    AccountState.READY: {
        AccountState.QUEUED,
        AccountState.CRASH,
        AccountState.FAILED,
        AccountState.NETWORK_LOST,
        AccountState.COOLDOWN,
    },
    AccountState.QUEUED: {
        AccountState.LAUNCHING,
        AccountState.CRASH,
        AccountState.FAILED,
        AccountState.NETWORK_LOST,
        AccountState.COOLDOWN,
        AccountState.READY,
    },
    AccountState.LAUNCHING: {
        AccountState.VERIFY,
        AccountState.IN_GAME,
        AccountState.CRASH,
        AccountState.FAILED,
        AccountState.NETWORK_LOST,
        AccountState.READY,
    },
    AccountState.VERIFY: {
        AccountState.IN_GAME,
        AccountState.CRASH,
        AccountState.FAILED,
        AccountState.NETWORK_LOST,
        AccountState.READY,
    },
    AccountState.IN_GAME: {
        AccountState.CRASH,
        AccountState.FAILED,
        AccountState.NETWORK_LOST,
        AccountState.COOLDOWN,
        AccountState.READY,
    },
    AccountState.CRASH: {
        AccountState.COOLDOWN,
        AccountState.READY,
        AccountState.QUEUED,
        AccountState.FAILED,
        AccountState.NETWORK_LOST,
    },
    AccountState.FAILED: set(),
    AccountState.NETWORK_LOST: {
        AccountState.READY,
        AccountState.QUEUED,
        AccountState.FAILED,
        AccountState.COOLDOWN,
    },
    AccountState.COOLDOWN: {
        AccountState.READY,
        AccountState.QUEUED,
        AccountState.FAILED,
        AccountState.NETWORK_LOST,
    },
}

class StateManager:
    def __init__(self, bus: EventBus):
        self._bus = bus
        self._runtime = RuntimeStateManager(logger=flog_kv)

    def transition(self, acc: Account, new_state: AccountState, force: bool = False, **kwargs) -> bool:
        with acc._lock:
            old = acc.state
            if old == new_state:
                return True

            allowed = ALLOWED_STATE_TRANSITIONS.get(old, set())
            if not force and new_state not in allowed:
                flog_kv(
                    "STATE",
                    "invalid_transition",
                    "warning",
                    event_type="invalid_transition",
                    account=acc.display_name,
                    account_id=acc._config_username,
                    old=old.name,
                    new=new_state.name,
                    reason=kwargs.get("reason", ""),
                    runtime_state=runtime_state_for_public(old).value,
                    public_state=old.name,
                    runtime_generation=acc.runtime_generation,
                    recovery_generation=acc.recovery_generation,
                    command_generation=acc.command_generation,
                    PID=acc.pid,
                    pid=acc.pid,
                    thread_name=threading.current_thread().name,
                    snapshot=acc.runtime_snapshot(),
                )
                self._bus.emit(
                    EventName.INVALID_TRANSITION,
                    account=acc,
                    old_state=old,
                    new_state=new_state,
                    **kwargs,
                )
                return False

            old_lifecycle = LIFECYCLE_STATE.get(old, "INIT")
            new_lifecycle = LIFECYCLE_STATE.get(new_state, "INIT")
            lifecycle_allowed = LIFECYCLE_ALLOWED_TRANSITIONS.get(old_lifecycle, set())
            if old_lifecycle != new_lifecycle and new_lifecycle not in lifecycle_allowed:
                flog_kv(
                    "STATE",
                    "lifecycle_jump",
                    "warning",
                    event_type="lifecycle_jump",
                    account=acc.display_name,
                    account_id=acc._config_username,
                    old=old.name,
                    new=new_state.name,
                    old_lifecycle=old_lifecycle,
                    new_lifecycle=new_lifecycle,
                    force=force,
                    reason=kwargs.get("reason", ""),
                    runtime_state=old_lifecycle,
                    public_state=old.name,
                    runtime_generation=acc.runtime_generation,
                    recovery_generation=acc.recovery_generation,
                    command_generation=acc.command_generation,
                    PID=acc.pid,
                    pid=acc.pid,
                    thread_name=threading.current_thread().name,
                    snapshot=acc.runtime_snapshot(),
                )
                if not force:
                    self._bus.emit(
                        EventName.INVALID_TRANSITION,
                        account=acc,
                        old_state=old,
                        new_state=new_state,
                        **kwargs,
                    )
                    return False

            changed = self._runtime.transition_public(
                acc,
                new_state,
                reason=str(kwargs.get("reason", "")),
                force=force,
                expected_generation=kwargs.get("expected_generation"),
                increment_generation=bool(kwargs.get("increment_generation", False) or new_state == AccountState.FAILED),
            )
            if not changed:
                self._bus.emit(
                    EventName.INVALID_TRANSITION,
                    account=acc,
                    old_state=old,
                    new_state=new_state,
                    **kwargs,
                )
                return False

            if new_state == AccountState.IN_GAME and old != AccountState.IN_GAME:
                acc.in_game_since = time.time()
            elif new_state != AccountState.IN_GAME:
                acc.in_game_since = None
            acc.sync_runtime(acc.last_state_reason)

        flog_kv(
            "STATE",
            "transition",
            event_type="transition",
            account=acc.display_name,
            account_id=acc._config_username,
            old=old.name,
            new=new_state.name,
            old_lifecycle=LIFECYCLE_STATE.get(old, "INIT"),
            new_lifecycle=LIFECYCLE_STATE.get(new_state, "INIT"),
            force=force,
            reason=kwargs.get("reason", ""),
            runtime_state=LIFECYCLE_STATE.get(new_state, "INIT"),
            public_state=new_state.name,
            runtime_generation=acc.runtime_generation,
            recovery_generation=acc.recovery_generation,
            command_generation=acc.command_generation,
            PID=acc.pid,
            pid=acc.pid,
            thread_name=threading.current_thread().name,
        )
        self._bus.emit(
            EventName.STATE_CHANGE,
            account=acc,
            old_state=old,
            new_state=new_state,
            **kwargs,
        )
        return True

    def set_desired(self, acc: Account, desired: AccountState, reason: str = ""):
        with acc._lock:
            old = acc.desired_state
            self._runtime.set_desired(acc, desired, reason or "desired_state")
        flog_kv(
            "STATE",
            "desired_transition",
            account=acc.display_name,
            old=old.name,
            new=desired.name,
            reason=reason,
            runtime_generation=acc.runtime_generation,
            recovery_generation=acc.recovery_generation,
            command_generation=acc.command_generation,
        )

    def set_cooldown(self, acc: Account, until_ts: float, reason: str = ""):
        with acc._lock:
            self._runtime.set_cooldown(acc, until_ts, reason or "cooldown")
        flog_kv(
            "STATE",
            "cooldown_set",
            account=acc.display_name,
            cooldown_left=max(0, int(acc.cooldown_until - time.time())) if acc.cooldown_until else 0,
            reason=reason,
            runtime_generation=acc.runtime_generation,
            recovery_generation=acc.recovery_generation,
            command_generation=acc.command_generation,
        )

    def set_recovery(self, acc: Account, status: str = "", reason: str = "", inflight: Optional[bool] = None):
        with acc._lock:
            self._runtime.set_recovery(acc, status=status, reason=reason, inflight=inflight)
        flog_kv(
            "STATE",
            "recovery_update",
            account=acc.display_name,
            status=acc.recovery_status,
            reason=reason,
            inflight=acc.recovery_inflight,
            generation=acc.recovery_generation,
            runtime_generation=acc.runtime_generation,
            command_generation=acc.command_generation,
        )

    def clear_recovery(self, acc: Account, reason: str = "", inflight: Optional[bool] = False):
        with acc._lock:
            self._runtime.clear_recovery(acc, reason=reason, inflight=inflight)
        flog_kv(
            "STATE",
            "recovery_clear",
            account=acc.display_name,
            reason=reason,
            inflight=acc.recovery_inflight,
            generation=acc.recovery_generation,
            runtime_generation=acc.runtime_generation,
            command_generation=acc.command_generation,
        )

    def set_binding_status(self, acc: Account, status: str, reason: str = ""):
        with acc._lock:
            self._runtime.set_binding_status(acc, status, reason or "binding_status")
        flog_kv(
            "STATE",
            "process_binding_status",
            account=acc.display_name,
            pid=acc.pid or "",
            status=acc.process_binding_status,
            reason=reason,
            runtime_generation=acc.runtime_generation,
            recovery_generation=acc.recovery_generation,
            command_generation=acc.command_generation,
        )

    def clear_process_binding(self, acc: Account, reason: str = "", increment_generation: bool = False):
        with acc._lock:
            old_pid = acc.pid
            self._runtime.clear_process_binding(
                acc,
                reason or "clear_process_binding",
                increment_generation=increment_generation,
            )
        flog_kv(
            "STATE",
            "process_unbound",
            account=acc.display_name,
            pid=old_pid or "",
            reason=reason,
            runtime_generation=acc.runtime_generation,
            recovery_generation=acc.recovery_generation,
            command_generation=acc.command_generation,
        )

    def bind_process(
        self,
        acc: Account,
        pid: int,
        identity: str,
        status: str = "verified",
        process_name: str = "RobloxPlayerBeta.exe",
        confidence: float = 100.0,
        process_proof_level: str = "strong",
        reason: str = "",
        increment_generation: bool = True,
    ):
        with acc._lock:
            old_pid = acc.pid
            self._runtime.bind_process(
                acc,
                int(pid),
                process_name or "RobloxPlayerBeta.exe",
                str(identity or ""),
                reason or "bind_process",
                confidence=float(confidence or 0.0),
                process_proof_level=process_proof_level,
                increment_generation=increment_generation,
            )
            if status and acc.process_binding_status != status:
                self._runtime.set_binding_status(acc, str(status), reason or "bind_process_status")
            acc.last_reconcile_at = time.time()
            if acc.state == AccountState.IN_GAME and not acc.in_game_since:
                acc.in_game_since = time.time()
                acc.sync_runtime(reason or "bind_process_ingame")
        flog_kv(
            "STATE",
            "process_bind_verified",
            account=acc.display_name,
            pid=pid,
            old_pid=old_pid or "",
            identity=identity,
            status=status,
            confidence=f"{float(confidence or 0.0):.1f}",
            reason=reason,
            runtime_generation=acc.runtime_generation,
            recovery_generation=acc.recovery_generation,
            command_generation=acc.command_generation,
        )


# ─────────────────────────────────────────────────────────────────────────────
#  GLOBAL LAUNCH LIMITER
# ─────────────────────────────────────────────────────────────────────────────
