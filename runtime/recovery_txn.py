"""Recovery transactions: budget, begin/clear, cooldown, scheduling."""
from __future__ import annotations

import time
from typing import Any

from core import Account, AccountState, EventName, flog_kv
from runtime.recovery_context import RecoveryAttemptContext
from runtime.recovery_policy import (
    adaptive_recovery_delay,
    policy_for,
)
from runtime.recovery_support import compute_backoff, should_suppress_recovery_schedule
from runtime.runtime_scheduler import RuntimeScheduledJob


class RecoveryTxnMixin:
    """Budget, begin/clear, cooldown and scheduling. Called by the reports
    mixin on the same coordinator; the schedule-dedupe verdict it shares
    with core lives in recovery_support.should_suppress_recovery_schedule."""

    def _record_recovery_budget_attempt(self, acc: Account, canonical: str, bucket: str, now: float) -> str:
        cfg = self._cfg
        enabled = bool(cfg.get("recovery_budget_enabled", True))
        max_attempts = max(
            1,
            int(cfg.get("recovery_budget_max_attempts", cfg.get("max_retry", 10)) or 10),
        )
        window_seconds = max(1.0, float(cfg.get("recovery_budget_window_seconds", 300) or 300))
        if not enabled or bucket == "manual" or bool(policy_for(canonical).get("fatal")):
            return ""
        if canonical == "game_unavailable":
            # Map down for update: external/transient. Retry slowly forever —
            # burning the circuit-breaker budget here is what stuck accounts
            # on "Rejoining" (FAILED) with no retry when the map reopens.
            # Prune the window so the counter cannot grow unbounded.
            acc.recovery_budget_attempts = [
                float(ts)
                for ts in list(getattr(acc, "recovery_budget_attempts", []) or [])
                if (now - float(ts)) <= window_seconds
            ]
            return ""
        attempts = [
            float(ts)
            for ts in list(getattr(acc, "recovery_budget_attempts", []) or [])
            if (now - float(ts)) <= window_seconds
        ]
        if len(attempts) >= max_attempts:
            acc.recovery_budget_attempts = attempts
            return (
                f"recovery budget exceeded: {len(attempts)}/{max_attempts} attempts "
                f"in {int(window_seconds)}s"
            )
        attempts.append(now)
        acc.recovery_budget_attempts = attempts
        return ""


    def _begin_gate_reason(self, acc: Account, canonical: str) -> bool:
        if self._stop.is_set() or self._closed or acc.desired_state != AccountState.IN_GAME:
            self._log_recovery_decision(
                "ignored",
                acc,
                canonical,
                desired=getattr(acc.desired_state, "name", acc.desired_state),
                stopped=self._stop.is_set(),
                closed=self._closed,
            )
            return True
        if acc.state == AccountState.FAILED:
            self._log_recovery_decision(
                "ignored",
                acc,
                canonical,
                reason_detail="already_failed",
            )
            return True
        return False

    def _duplicate_recovery(
        self,
        acc: Account,
        canonical: str,
        force: bool,
        now: float,
    ) -> bool:
        duplicate = (
            acc.recovery_inflight
            and not force
            and acc.last_recovery_reason == canonical
            and (now - float(acc.last_recovery_at or 0.0)) < self._duplicate_window
        )
        if duplicate:
            self._log_recovery_decision(
                "recovery_duplicate_suppressed",
                acc,
                canonical,
                age=f"{now - float(acc.last_recovery_at or now):.2f}",
                window=f"{self._duplicate_window:.1f}",
            )
            return True
        return False

    def _owner_key(self, acc: Account, canonical: str, force: bool) -> str | None:
        account_key = acc._config_username
        owner_check = self._owner_registry.check_start(
            account_key,
            runtime_generation=int(acc.runtime_generation or 0),
            recovery_generation=int(acc.recovery_generation or 0),
            reason=canonical,
            current_state=acc.state,
            force=force,
        )
        if not owner_check.get("accepted"):
            self._log_recovery_decision(
                "recovery_owner_rejected",
                acc,
                canonical,
                **{
                    key: value
                    for key, value in owner_check.items()
                    if key != "accepted"
                },
            )
            return None
        if owner_check.get("replaced"):
            self._log_recovery_decision(
                "recovery_owner_replaced",
                acc,
                canonical,
                **{
                    key: value
                    for key, value in owner_check.items()
                    if key not in {"accepted", "replaced"}
                },
            )
        return account_key

    def _begin_recovery(
        self,
        acc: Account,
        canonical: str,
        status: str,
        bucket: str,
        reason_msg: str = "",
        force: bool = False,
        count_retry: bool = True,
        count_crash: bool = True,
        count_fail: bool = True,
        context: RecoveryAttemptContext | None = None,
    ) -> dict[str, Any] | None:
        now = time.time()
        budget_reason = ""
        with acc._lock:
            if self._begin_gate_reason(acc, canonical):
                return None
            if self._duplicate_recovery(acc, canonical, force, now):
                return None
            account_key = self._owner_key(acc, canonical, force)
            if account_key is None:
                return None

            budget_reason = self._record_recovery_budget_attempt(
                acc, canonical, bucket, now
            )
            if not budget_reason:
                self._runtime_state.begin_recovery(
                    acc,
                    status=status,
                    reason=canonical,
                    bucket=bucket,
                    now=now,
                    count_retry=count_retry,
                    count_crash=count_crash,
                    count_fail=count_fail,
                )
                ctx = {
                    "generation": acc.recovery_generation,
                    "recovery_generation": acc.recovery_generation,
                    "runtime_generation": acc.runtime_generation,
                    "pid": acc.pid,
                    "active_vip": acc.active_vip,
                    "fail_count": acc.fail_count,
                    "launch_fail_count": acc.launch_fail_count,
                    "bucket": bucket,
                }
                owner = self._owner_registry.acquire(
                    account_key,
                    runtime_generation=int(acc.runtime_generation or 0),
                    recovery_generation=int(acc.recovery_generation or 0),
                    command_generation=int(acc.command_generation or 0),
                    session_id=acc.session_id,
                    transaction_id=acc.rejoin_transaction_id,
                    reason=canonical,
                    status=status,
                    bucket=bucket,
                    priority=int(context.priority if context else 0),
                    token=context.token if context else "",
                    now=now,
                )

        if budget_reason:
            self._log_recovery_decision(
                "recovery_budget_exceeded",
                acc,
                canonical,
                reason_msg=budget_reason,
                bucket=bucket,
            )
            self.fail_account(acc, "recovery_budget_exceeded", budget_reason)
            return None
        self._log_recovery_decision(
            "recovery_owner_acquired",
            acc,
            canonical,
            **{key: value for key, value in owner.items() if key != "reason"},
        )
        self._log_recovery_decision(
            "recovery_policy_applied",
            acc,
            canonical,
            bucket=bucket,
            status=status,
            generation=ctx["generation"],
            reason_msg=reason_msg,
            **(context.to_dict() if context else {}),
        )
        self._log_recovery_decision(
            "started",
            acc,
            canonical,
            bucket=bucket,
            generation=ctx["generation"],
            status=status,
            reason_msg=reason_msg,
        )
        return ctx


    def _clear_recovery(self, acc: Account, status: str, reason: str, inflight: bool = False):
        with acc._lock:
            self._runtime_state.set_recovery(acc, status=status, reason=reason, inflight=inflight)
            acc.recovery_scheduled_at = 0.0
            acc.sync_runtime(reason)
            if not inflight:
                account_key = acc._config_username
                recovery_generation = int(acc.recovery_generation or 0)
                runtime_generation = int(acc.runtime_generation or 0)
            else:
                account_key = ""
                recovery_generation = 0
                runtime_generation = 0
        if account_key:
            self._release_recovery_owner(account_key, runtime_generation, recovery_generation, reason)
        self._log_recovery_decision("cleared", acc, reason, status=status, inflight=inflight)


    def _release_recovery_owner(
        self,
        account_key: str,
        runtime_generation: int | None,
        recovery_generation: int | None,
        reason: str,
    ) -> None:
        result = self._owner_registry.release(account_key, runtime_generation, recovery_generation, reason)
        if not result.get("found"):
            return
        event = "recovery_owner_released" if result.get("released") else "recovery_owner_release_rejected"
        fields = {
            key: value
            for key, value in result.items()
            if key not in {"found", "released", "reason", "release_reason", "runtime_generation", "recovery_generation"}
        }
        flog_kv(
            "RECOVERY",
            event,
            account=account_key,
            runtime_generation=runtime_generation,
            recovery_generation=recovery_generation,
            reason=reason,
            **fields,
        )


    def _schedule_cooldown(self, acc: Account, delay: float, reason: str, transition_reason: str, display_reason: str = ""):
        until = time.time() + max(0.0, float(delay or 0.0))
        self._state_mgr.set_cooldown(acc, until, reason=transition_reason)
        self._state_mgr.set_recovery(acc, status="cooldown", reason=reason, inflight=True)
        self._state_mgr.transition(acc, AccountState.COOLDOWN, reason=transition_reason)
        self._log_recovery_decision(
            "cooldown",
            acc,
            reason,
            display_reason=display_reason or reason,
            delay=f"{max(0.0, float(delay or 0.0)):.1f}",
            until=f"{until:.3f}",
        )
        self._schedule_recovery(acc, delay, transition_reason)


    def _retry_bucket_exceeded(self, acc: Account) -> str | None:
        max_retry = max(1, int(self._cfg.get("max_retry", 10) or 10))
        buckets = {
            "crash_retry": acc.crash_retry_count,
            "launch_retry": acc.launch_fail_count,
            "network_retry": acc.network_retry_count,
            "session_retry": acc.session_retry_count,
        }
        for label, count in buckets.items():
            if count >= max_retry:
                return f"{label} reached max retry ({max_retry})"
        return None


    def _adaptive_recovery_delay(self, acc: Account, reason_key: str, cooldown: float | None = None) -> float:
        attempts = self._session_conflicts.count(
            acc._config_username,
            float(self._cfg.get("session_conflict_window_seconds", 90) or 90),
        )
        return adaptive_recovery_delay(self._cfg, acc, reason_key, cooldown, attempts, compute_backoff)


    def _queue_account(self, acc: Account, reason: str):
        if self._closed or self._stop.is_set():
            self._log_recovery_decision("queue_rejected", acc, reason, reject="coordinator_closed")
            return
        if acc.state != AccountState.READY:
            self._state_mgr.transition(acc, AccountState.READY, reason=reason, force=True)
        self._state_mgr.transition(acc, AccountState.QUEUED, reason=reason)
        with acc._lock:
            self._runtime_state.set_recovery(acc, status="queued", reason=reason, inflight=True)
            acc.last_rejoin_trigger = reason
            acc.recovery_scheduled_at = 0.0
            runtime_generation = int(acc.runtime_generation or 0)
            recovery_generation = int(acc.recovery_generation or 0)
        storm = self._storm.reserve_delay(acc, 0.0, reason, net_online=self._net.is_online())
        if storm.delayed:
            self._log_recovery_decision("recovery_storm_delayed", acc, reason, **storm.to_log_fields())
        self._queue.push(
            acc,
            reason=reason,
            runtime_generation=runtime_generation,
            recovery_generation=recovery_generation,
            delay_seconds=storm.delay_seconds,
        )
        self._release_recovery_owner(acc._config_username, runtime_generation, recovery_generation, f"queued:{reason}")
        self._log_recovery_decision("queued", acc, reason, generation=acc.recovery_generation)
        self._bus.emit(EventName.RECOVERY_REQUESTED, account=acc, reason=reason)
        self._persist_runtime()


    def _schedule(self, acc: Account, delay: float, reason: str):
        return self._schedule_recovery(acc, delay, reason)


    def _schedule_recovery(self, acc: Account, delay: float, reason: str) -> None:
        key = f"recovery:{acc._config_username}"
        storm = self._storm.reserve_delay(acc, delay, reason, net_online=self._net.is_online())
        if storm.delayed:
            self._log_recovery_decision("recovery_storm_delayed", acc, reason, **storm.to_log_fields())
        delay = storm.delay_seconds
        due = time.time() + max(0.0, delay)
        with acc._lock:
            if self._closed or self._stop.is_set():
                self._log_recovery_decision("schedule_rejected", acc, reason, reject="coordinator_closed")
                return
            generation = acc.recovery_generation
            runtime_generation = acc.runtime_generation
            command_generation = acc.command_generation
            acc.recovery_scheduled_at = due
            acc.scheduler_slot = key
            if acc.recovery_status not in {"manual", "network_lost"}:
                self._runtime_state.set_recovery(acc, status="scheduled", reason="", inflight=True)
            else:
                self._runtime_state.set_recovery(acc, reason="", inflight=True)
        current = self._scheduler.get(key)
        if should_suppress_recovery_schedule(
            current, due, generation, runtime_generation, command_generation
        ):
            self._log_recovery_decision(
                "schedule_suppressed",
                acc,
                reason,
                existing_due=f"{current.due_at:.3f}",
                new_due=f"{due:.3f}",
                generation=generation,
                runtime_generation=runtime_generation,
            )
            return
        self._scheduler.schedule_once(
            key,
            self._run_scheduled_recovery,
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
        self._persist_runtime()


    def _run_scheduled_recovery(self, job: RuntimeScheduledJob) -> None:
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
            runtime_generation = self._scheduler.effective_runtime_generation(job)
            if runtime_generation is None or not self._runtime_state.guard_runtime_generation(
                acc,
                runtime_generation,
                reason=f"scheduler:{reason}",
            ):
                return
            self._runtime_state.set_recovery(acc, status="due", reason="", inflight=True)
            acc.recovery_scheduled_at = 0.0
            acc.scheduler_slot = ""
        self._persist_runtime()
        self._evaluator.evaluate(
            acc,
            trigger=reason,
            expected_runtime_generation=runtime_generation,
            expected_recovery_generation=generation,
        )
