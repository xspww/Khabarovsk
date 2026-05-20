from __future__ import annotations

import time
from typing import Any

from core import Account, AccountState, EventName, flog_kv
from runtime.runtime_scheduler import RuntimeScheduledJob


def schedule_cooldown(
    recovery: Any,
    acc: Account,
    delay: float,
    reason: str,
    transition_reason: str,
) -> None:
    until = time.time() + max(0.0, float(delay or 0.0))
    recovery._state_mgr.set_cooldown(acc, until, reason=transition_reason)
    recovery._state_mgr.set_recovery(acc, status="cooldown", reason=reason, inflight=True)
    recovery._state_mgr.transition(acc, AccountState.COOLDOWN, reason=transition_reason)
    recovery._log_recovery_decision(
        "cooldown",
        acc,
        reason,
        delay=f"{max(0.0, float(delay or 0.0)):.1f}",
        until=f"{until:.3f}",
    )
    schedule_recovery(recovery, acc, delay, transition_reason)


def queue_account(recovery: Any, acc: Account, reason: str) -> None:
    if recovery._closed or recovery._stop.is_set():
        recovery._log_recovery_decision("queue_rejected", acc, reason, reject="coordinator_closed")
        return
    if acc.state != AccountState.READY:
        recovery._state_mgr.transition(acc, AccountState.READY, reason=reason, force=True)
    recovery._state_mgr.transition(acc, AccountState.QUEUED, reason=reason)
    with acc._lock:
        recovery._runtime_state.set_recovery(acc, status="queued", reason=reason, inflight=True)
        acc.last_rejoin_trigger = reason
        acc.recovery_scheduled_at = 0.0
        runtime_generation = int(acc.runtime_generation or 0)
        recovery_generation = int(acc.recovery_generation or 0)
    storm = recovery._storm.reserve_delay(acc, 0.0, reason, net_online=recovery._net.is_online())
    if storm.delayed:
        recovery._log_recovery_decision("recovery_storm_delayed", acc, reason, **storm.to_log_fields())
    recovery._queue.push(
        acc,
        reason=reason,
        runtime_generation=runtime_generation,
        recovery_generation=recovery_generation,
        delay_seconds=storm.delay_seconds,
    )
    recovery._release_recovery_owner(acc._config_username, runtime_generation, recovery_generation, f"queued:{reason}")
    recovery._log_recovery_decision("queued", acc, reason, generation=acc.recovery_generation)
    recovery._bus.emit(EventName.RECOVERY_REQUESTED, account=acc, reason=reason)
    recovery._persist_runtime()


def schedule_recovery(recovery: Any, acc: Account, delay: float, reason: str) -> None:
    key = f"recovery:{acc._config_username}"
    storm = recovery._storm.reserve_delay(acc, delay, reason, net_online=recovery._net.is_online())
    if storm.delayed:
        recovery._log_recovery_decision("recovery_storm_delayed", acc, reason, **storm.to_log_fields())
    delay = storm.delay_seconds
    due = time.time() + max(0.0, delay)
    with acc._lock:
        if recovery._closed or recovery._stop.is_set():
            recovery._log_recovery_decision("schedule_rejected", acc, reason, reject="coordinator_closed")
            return
        generation = acc.recovery_generation
        runtime_generation = acc.runtime_generation
        command_generation = acc.command_generation
        acc.recovery_scheduled_at = due
        acc.scheduler_slot = key
        if acc.recovery_status not in {"manual", "network_lost"}:
            recovery._runtime_state.set_recovery(acc, status="scheduled", reason="", inflight=True)
        else:
            recovery._runtime_state.set_recovery(acc, reason="", inflight=True)
    current = recovery._scheduler.get(key)
    if (
        current
        and current.due_at <= due
        and current.recovery_generation == generation
        and current.runtime_generation == runtime_generation
        and current.command_generation == command_generation
    ):
        recovery._log_recovery_decision(
            "schedule_suppressed",
            acc,
            reason,
            existing_due=f"{current.due_at:.3f}",
            new_due=f"{due:.3f}",
            generation=generation,
            runtime_generation=runtime_generation,
        )
        return
    recovery._scheduler.schedule_once(
        key,
        recovery._run_scheduled_recovery,
        due_at=due,
        reason=reason,
        account=acc,
        runtime_generation=runtime_generation,
        recovery_generation=generation,
        command_generation=command_generation,
        payload={"scheduler_slot": "recovery", "allow_runtime_generation_drift": True},
    )
    flog_kv(
        "RECOVERY",
        "schedule_timer",
        account=acc.display_name,
        reason=reason,
        delay=f"{max(0.0, delay):.1f}",
        generation=generation,
        runtime_generation=runtime_generation,
        command_generation=command_generation,
    )
    recovery._persist_runtime()


def run_scheduled_recovery(recovery: Any, job: RuntimeScheduledJob) -> None:
    acc = job.account
    if acc is None:
        return
    reason = job.reason
    generation = int(job.recovery_generation or 0)
    runtime_generation = int(job.runtime_generation or 0)
    with acc._lock:
        if generation != acc.recovery_generation:
            flog_kv(
                "RUNTIME",
                "stale_work_rejected",
                "warning",
                account=acc.display_name,
                expected_generation=generation,
                current_generation=acc.recovery_generation,
                runtime_generation=acc.runtime_generation,
                command_generation=acc.command_generation,
                reason=f"scheduler:{reason}",
            )
            return
        runtime_generation = recovery._scheduler.effective_runtime_generation(job)
        if runtime_generation is None or not recovery._runtime_state.guard_runtime_generation(
            acc,
            runtime_generation,
            reason=f"scheduler:{reason}",
        ):
            return
        recovery._runtime_state.set_recovery(acc, status="due", reason="", inflight=True)
        acc.recovery_scheduled_at = 0.0
        acc.scheduler_slot = ""
    recovery._persist_runtime()
    recovery.evaluate(
        acc,
        trigger=reason,
        expected_runtime_generation=runtime_generation,
        expected_recovery_generation=generation,
    )
