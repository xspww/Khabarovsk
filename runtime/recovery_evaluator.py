from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, NamedTuple

from core import AccountState
from runtime.recovery_seam import EvaluatorRecovery
from runtime.recovery_support import (
    RECOVERY_REASON_MESSAGES,
    compute_backoff,
    log_hold,
    log_recovery_decision,
)
from services.auth_gate import (
    evaluate_account_auth_gate,
    mark_account_auth_quarantined,
)
from services.process_service import ProcessService


class _EvalSnapshot(NamedTuple):
    desired: Any
    current: Any
    fail_count: int
    cooldown_until: float
    pid: Any
    runtime_generation: int
    session_valid: bool
    session_checked: bool
    has_cookie: bool
    session_wait_started_at: float | None


@dataclass
class _EvalContext:
    recovery: EvaluatorRecovery
    acc: Any
    trigger: str
    snapshot: _EvalSnapshot
    current: Any
    force_restart: bool


def _guard_session_identity(
    recovery: EvaluatorRecovery,
    acc: Any,
    trigger: str,
    expected_runtime_generation: int | None,
    expected_session_id: str,
    expected_launch_nonce: str,
    expected_transaction_id: str,
) -> bool:
    if recovery._runtime_state.guard_session_identity(
        acc,
        expected_generation=expected_runtime_generation,
        expected_session_id=expected_session_id,
        expected_launch_nonce=expected_launch_nonce,
        expected_transaction_id=expected_transaction_id,
        reason=f"evaluate:{trigger}",
    ):
        return True
    log_recovery_decision(
        "evaluate_rejected",
        acc,
        trigger,
        reject="stale_identity",
        expected_runtime_generation=expected_runtime_generation,
        expected_session_id=expected_session_id,
        expected_transaction_id=expected_transaction_id,
    )
    return False


def _guard_recovery_generation(
    recovery: EvaluatorRecovery,
    acc: Any,
    trigger: str,
    expected_recovery_generation: int | None,
) -> bool:
    if recovery._runtime_state.guard_recovery_generation(
        acc,
        expected_recovery_generation,
        reason=f"evaluate:{trigger}",
    ):
        return True
    log_recovery_decision(
        "evaluate_rejected",
        acc,
        trigger,
        reject="stale_recovery_generation",
        expected_recovery_generation=expected_recovery_generation,
    )
    return False


def _snapshot_account(acc: Any) -> _EvalSnapshot:
    return _EvalSnapshot(
        desired=acc.desired_state,
        current=acc.state,
        fail_count=acc.fail_count,
        cooldown_until=acc.cooldown_until,
        pid=acc.pid,
        runtime_generation=acc.runtime_generation,
        session_valid=acc.session_valid,
        session_checked=acc.session_checked,
        has_cookie=bool(acc.cookie),
        session_wait_started_at=acc.session_wait_started_at,
    )


def _stage_stopped_or_not_desired(ctx: _EvalContext) -> bool:
    if (
        ctx.recovery._stop.is_set()
        or ctx.snapshot.desired != AccountState.IN_GAME
    ):
        log_hold(ctx.acc, ctx.trigger, "stopped_or_not_desired")
        return True
    return False


def _stage_failed(ctx: _EvalContext) -> bool:
    if ctx.snapshot.current != AccountState.FAILED:
        return False
    try:
        revive = getattr(
            ctx.recovery,
            "maybe_revive_transient_failed",
            None,
        )
        if callable(revive) and bool(
            revive(ctx.acc, ctx.trigger)
        ):
            return True
    except Exception:
        "intentional: ignore exception"
    log_hold(ctx.acc, ctx.trigger, "already_failed")
    return True


def _stage_auth_gate(ctx: _EvalContext) -> bool:
    auth_gate = evaluate_account_auth_gate(ctx.acc)
    if not auth_gate.blocked:
        return False
    mark_account_auth_quarantined(
        ctx.acc,
        auth_gate,
        source=f"evaluate:{ctx.trigger}",
        runtime_writer=ctx.recovery._runtime_state,
    )
    log_hold(ctx.acc, ctx.trigger, auth_gate.reason_key)
    ctx.recovery.fail_account(
        ctx.acc,
        auth_gate.reason_key,
        auth_gate.reason,
    )
    return True


def _stage_retry_limits(ctx: _EvalContext) -> bool:
    max_fail = int(ctx.recovery._cfg.get("max_fail_count", 5))
    if (
        ctx.snapshot.fail_count >= max_fail
        or ctx.recovery._retry_bucket_exceeded(ctx.acc)
    ):
        # The "Finished" mechanism is removed: exceeding retry limits never
        # terminates an account. Reset the counters and keep the farm active.
        with ctx.acc._lock:
            ctx.acc.fail_count = 0
            ctx.acc.retry_count = 0
            ctx.acc.crash_retry_count = 0
            ctx.acc.launch_fail_count = 0
            ctx.acc.network_retry_count = 0
            ctx.acc.session_retry_count = 0
        log_hold(
            ctx.acc,
            ctx.trigger,
            "retry_limits_reset_keep_active",
        )
        return True
    return False


def _stage_executor_held(ctx: _EvalContext) -> bool:
    try:
        held = (
            bool(ctx.recovery._executor_held())
            if hasattr(ctx.recovery, "_executor_held")
            else False
        )
    except Exception:
        held = False
    if not held:
        return False
    # Executor waits for a patch: keep live sessions untouched,
    # park anything needing a new launch as Wait for exploit patch.
    # Stays quiet (no terminal log); the Dashboard reads the status.
    if ctx.current in (
        AccountState.IN_GAME,
        AccountState.LAUNCHING,
        AccountState.VERIFY,
    ):
        return True
    try:
        ctx.recovery._park_for_executor_hold(
            ctx.acc,
            ctx.trigger,
        )
    except Exception:
        "intentional: ignore exception"
    return True


def _stage_session_check_wait(ctx: _EvalContext) -> bool:
    if not (
        ctx.snapshot.has_cookie
        and not ctx.snapshot.session_checked
    ):
        return False
    now = time.time()
    with ctx.acc._lock:
        if not ctx.acc.session_wait_started_at:
            ctx.acc.session_wait_started_at = now
            ctx.snapshot = ctx.snapshot._replace(
                session_wait_started_at=now,
            )
    wait_age = max(
        0.0,
        now - (ctx.snapshot.session_wait_started_at or now),
    )
    log_hold(
        ctx.acc,
        ctx.trigger,
        f"waiting_session_check age={wait_age:.1f}s",
    )
    delay = min(
        5.0,
        max(
            2.0,
            float(
                ctx.recovery._cfg.get(
                    "network_check_interval",
                    5,
                )
                or 5
            ),
        ),
    )
    ctx.recovery._schedule(
        ctx.acc,
        delay,
        "wait_session_check",
    )
    return True


def _stage_session_invalid(ctx: _EvalContext) -> bool:
    if ctx.snapshot.session_valid:
        return False
    with ctx.acc._lock:
        ctx.acc.session_retry_count += 1
        ctx.acc.retry_count += 1
        if not ctx.acc.recovery_inflight:
            ctx.recovery._runtime_state.bump_recovery_generation(
                ctx.acc,
                "session_retry",
                now=time.time(),
            )
        ctx.recovery._runtime_state.set_recovery(
            ctx.acc,
            status="session_retry",
            reason="session_retry",
            inflight=True,
        )
        session_retry_count = ctx.acc.session_retry_count
    hard_invalid = ctx.acc.last_crash_reason == "cookie_invalid"
    if hard_invalid:
        ctx.recovery.fail_account(
            ctx.acc,
            "cookie_invalid",
            RECOVERY_REASON_MESSAGES["cookie_invalid"],
        )
        return True
    delay = compute_backoff(
        session_retry_count,
        base=3,
        cap=30,
    )
    log_hold(
        ctx.acc,
        ctx.trigger,
        f"session_unverified_retry delay={delay:.1f}s",
    )
    ctx.recovery._schedule_cooldown(
        ctx.acc,
        delay,
        "session_retry",
        "session_retry",
    )
    return True


def _stage_network(ctx: _EvalContext) -> bool:
    if ctx.recovery._net.is_online():
        return False
    ctx.recovery.mark_network_lost(ctx.acc, ctx.trigger)
    return True


def _stage_force_restart(ctx: _EvalContext) -> bool:
    if not (ctx.force_restart and ctx.snapshot.pid):
        return False
    kill_result = ProcessService.safe_kill_bound_process(
        ctx.acc,
        ctx.recovery._state_mgr,
        reason=f"{ctx.trigger}:force_restart",
        expected_runtime_generation=(
            ctx.snapshot.runtime_generation
        ),
    )
    if kill_result.get("reason") == "stale_runtime_generation":
        return True
    ctx.recovery._state_mgr.transition(
        ctx.acc,
        AccountState.READY,
        reason=f"{ctx.trigger}:force_restart",
        force=True,
    )
    ctx.current = AccountState.READY
    return False


def _stage_queued(ctx: _EvalContext) -> bool:
    if ctx.current != AccountState.QUEUED:
        return False
    has_fresh_queue_entry = False
    queue_has_fresh_entry = getattr(
        ctx.recovery._queue,
        "has_fresh_entry",
        None,
    )
    if callable(queue_has_fresh_entry):
        has_fresh_queue_entry = bool(
            queue_has_fresh_entry(ctx.acc)
        )
    if has_fresh_queue_entry:
        log_hold(ctx.acc, ctx.trigger, "active_state=QUEUED")
        return True
    log_recovery_decision(
        "queue_repair",
        ctx.acc,
        ctx.trigger,
        reason_detail="queued_state_without_queue_entry",
    )
    ctx.recovery._queue_account(
        ctx.acc,
        f"{ctx.trigger}:queue_repair",
    )
    return True


def _stage_active_state(ctx: _EvalContext) -> bool:
    if ctx.current in (
        AccountState.IN_GAME,
        AccountState.LAUNCHING,
        AccountState.VERIFY,
    ):
        log_hold(
            ctx.acc,
            ctx.trigger,
            f"active_state={ctx.current.name}",
        )
        return True
    return False


def _stage_cooldown(ctx: _EvalContext) -> bool:
    remaining = ctx.snapshot.cooldown_until - time.time()
    if remaining > 0:
        ctx.recovery._state_mgr.transition(
            ctx.acc,
            AccountState.COOLDOWN,
            reason=ctx.trigger,
        )
        log_hold(
            ctx.acc,
            ctx.trigger,
            f"cooldown remaining={remaining:.1f}s",
        )
        ctx.recovery._schedule(
            ctx.acc,
            remaining,
            f"{ctx.trigger}:cooldown",
        )
        return True
    return False


def _stage_slot(ctx: _EvalContext) -> bool:
    if not ctx.recovery.slot_policy.slot_available(ctx.acc):
        delay = ctx.recovery.slot_policy.queue_delay_seconds()
        log_hold(
            ctx.acc,
            ctx.trigger,
            "queue_slot_full active="
            f"{ctx.recovery.slot_policy.active_slot_count(excluding=ctx.acc)}"
            " max="
            f"{ctx.recovery.slot_policy.max_concurrent_accounts()}",
        )
        ctx.recovery._schedule(
            ctx.acc,
            delay,
            "queue_slot_wait",
        )
        return True
    return False


_EVAL_STAGES = (
    _stage_stopped_or_not_desired,
    _stage_failed,
    _stage_auth_gate,
    _stage_retry_limits,
    _stage_executor_held,
    _stage_session_check_wait,
    _stage_session_invalid,
    _stage_network,
    _stage_force_restart,
    _stage_queued,
    _stage_active_state,
    _stage_cooldown,
    _stage_slot,
)


class RecoveryEvaluator:
    """Evaluates whether an account should queue, wait, fail, or recover."""

    def __init__(self, recovery: EvaluatorRecovery):
        self._recovery: EvaluatorRecovery = recovery

    def evaluate(
        self,
        acc: Any,
        trigger: str = "evaluate",
        force_restart: bool = False,
        expected_runtime_generation: int | None = None,
        expected_recovery_generation: int | None = None,
        expected_session_id: str = "",
        expected_launch_nonce: str = "",
        expected_transaction_id: str = "",
    ):
        r = self._recovery
        with acc._lock:
            if not _guard_session_identity(
                r,
                acc,
                trigger,
                expected_runtime_generation,
                expected_session_id,
                expected_launch_nonce,
                expected_transaction_id,
            ):
                return
            if not _guard_recovery_generation(
                r,
                acc,
                trigger,
                expected_recovery_generation,
            ):
                return
            snapshot = _snapshot_account(acc)
        ctx = _EvalContext(
            recovery=r,
            acc=acc,
            trigger=trigger,
            snapshot=snapshot,
            current=snapshot.current,
            force_restart=force_restart,
        )
        for stage in _EVAL_STAGES:
            if stage(ctx):
                return
        r._queue_account(acc, trigger)
