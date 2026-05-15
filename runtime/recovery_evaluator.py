from __future__ import annotations

import time
from typing import Any, Optional

from core import AccountState
from services.process_service import ProcessService
from services.auth_gate import evaluate_account_auth_gate, mark_account_auth_quarantined
from runtime.recovery_support import RECOVERY_REASON_MESSAGES, compute_backoff


class RecoveryEvaluator:
    """Evaluates whether an account should queue, wait, fail, or recover."""

    def __init__(self, recovery: Any):
        self._recovery = recovery

    def evaluate(
        self,
        acc: Any,
        trigger: str = "evaluate",
        force_restart: bool = False,
        expected_runtime_generation: Optional[int] = None,
        expected_recovery_generation: Optional[int] = None,
        expected_session_id: str = "",
        expected_launch_nonce: str = "",
        expected_transaction_id: str = "",
    ):
        r = self._recovery
        with acc._lock:
            if not r._runtime_state.guard_session_identity(
                acc,
                expected_generation=expected_runtime_generation,
                expected_session_id=expected_session_id,
                expected_launch_nonce=expected_launch_nonce,
                expected_transaction_id=expected_transaction_id,
                reason=f"evaluate:{trigger}",
            ):
                r._log_recovery_decision(
                    "evaluate_rejected",
                    acc,
                    trigger,
                    reject="stale_identity",
                    expected_runtime_generation=expected_runtime_generation,
                    expected_session_id=expected_session_id,
                    expected_transaction_id=expected_transaction_id,
                )
                return
            if not r._runtime_state.guard_recovery_generation(
                acc,
                expected_recovery_generation,
                reason=f"evaluate:{trigger}",
            ):
                r._log_recovery_decision(
                    "evaluate_rejected",
                    acc,
                    trigger,
                    reject="stale_recovery_generation",
                    expected_recovery_generation=expected_recovery_generation,
                )
                return
            desired = acc.desired_state
            current = acc.state
            fail_count = acc.fail_count
            cooldown_until = acc.cooldown_until
            pid = acc.pid
            runtime_generation = acc.runtime_generation
            session_valid = acc.session_valid
            session_checked = acc.session_checked
            has_cookie = bool(acc.cookie)
            session_wait_started_at = acc.session_wait_started_at

        if r._stop.is_set() or desired != AccountState.IN_GAME:
            r._log_hold(acc, trigger, "stopped_or_not_desired")
            return
        if current == AccountState.FAILED:
            r._log_hold(acc, trigger, "already_failed")
            return
        auth_gate = evaluate_account_auth_gate(acc)
        if auth_gate.blocked:
            mark_account_auth_quarantined(acc, auth_gate, source=f"evaluate:{trigger}", runtime_writer=r._runtime_state)
            r._log_hold(acc, trigger, auth_gate.reason_key)
            r.fail_account(acc, auth_gate.reason_key, auth_gate.reason)
            return

        max_fail = int(r._cfg.get("max_fail_count", 5))
        if fail_count >= max_fail:
            r.fail_account(acc, "max_fail", RECOVERY_REASON_MESSAGES["max_fail"])
            return
        retry_exceeded = r._retry_bucket_exceeded(acc)
        if retry_exceeded:
            r.fail_account(acc, "max_retry", retry_exceeded)
            return

        if has_cookie and not session_checked:
            now = time.time()
            with acc._lock:
                if not acc.session_wait_started_at:
                    acc.session_wait_started_at = now
                    session_wait_started_at = now
            wait_age = max(0.0, now - (session_wait_started_at or now))
            r._log_hold(acc, trigger, f"waiting_session_check age={wait_age:.1f}s")
            delay = min(5.0, max(2.0, float(r._cfg.get("network_check_interval", 5) or 5)))
            r._schedule(acc, delay, "wait_session_check")
            return

        if not session_valid:
            with acc._lock:
                acc.session_retry_count += 1
                acc.retry_count += 1
                if not acc.recovery_inflight:
                    r._runtime_state.bump_recovery_generation(acc, "session_retry", now=time.time())
                r._runtime_state.set_recovery(acc, status="session_retry", reason="session_retry", inflight=True)
                session_retry_count = acc.session_retry_count
            hard_invalid = acc.last_crash_reason == "cookie_invalid"
            if hard_invalid:
                r.fail_account(acc, "cookie_invalid", RECOVERY_REASON_MESSAGES["cookie_invalid"])
                return
            delay = compute_backoff(session_retry_count, base=3, cap=30)
            r._log_hold(acc, trigger, f"session_unverified_retry delay={delay:.1f}s")
            r._schedule_cooldown(acc, delay, "session_retry", "session_retry")
            return

        if not r._net.is_online():
            r.mark_network_lost(acc, trigger)
            return

        if force_restart and pid:
            kill_result = ProcessService.safe_kill_bound_process(
                acc,
                r._state_mgr,
                reason=f"{trigger}:force_restart",
                expected_runtime_generation=runtime_generation,
            )
            if kill_result.get("reason") == "stale_runtime_generation":
                return
            r._state_mgr.transition(acc, AccountState.READY, reason=f"{trigger}:force_restart", force=True)
            current = AccountState.READY

        if current in (AccountState.IN_GAME, AccountState.LAUNCHING, AccountState.VERIFY, AccountState.QUEUED):
            r._log_hold(acc, trigger, f"active_state={current.name}")
            return

        remaining = cooldown_until - time.time()
        if remaining > 0:
            r._state_mgr.transition(acc, AccountState.COOLDOWN, reason=trigger)
            r._log_hold(acc, trigger, f"cooldown remaining={remaining:.1f}s")
            r._schedule(acc, remaining, f"{trigger}:cooldown")
            return

        if not r._queue_slot_available(acc):
            delay = r._queue_delay_seconds()
            r._log_hold(
                acc,
                trigger,
                f"queue_slot_full active={r._active_slot_count(excluding=acc)} max={r._max_concurrent_accounts()}",
            )
            r._schedule(acc, delay, "queue_slot_wait")
            return

        r._queue_account(acc, trigger)
