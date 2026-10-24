"""Recovery reporters: crash, launch, fail, revive, force-rejoin."""
from __future__ import annotations

import time
from typing import Any, NamedTuple

from core import Account, AccountState, EventName, flog, flog_kv
from runtime.lua_liveness_policy import lua_liveness_required, mark_waiting_for_lua
from runtime.recovery_context import RecoveryAttemptContext
from runtime.recovery_policy import (
    canonical_launch_failure_reason,
    canonical_reason,
    is_transient_recovery_mark,
    kill_local_duplicate_for_session_conflict,
    policy_for,
)
from runtime.recovery_policy import canonical_recovery_reason as _canonical_recovery_reason
from runtime.recovery_policy import display_recovery_reason as _display_recovery_reason
from runtime.recovery_policy import is_game_unavailable_detail as _is_game_unavailable_detail
from runtime.recovery_support import RECOVERY_REASON_MESSAGES
from services.auth_gate import evaluate_account_auth_gate, mark_account_auth_quarantined
from services.captcha_guard import (
    CAPTCHA_BLOCK_REASON,
    CAPTCHA_REASON,
    is_captcha_status_text,
    set_account_captcha_hold,
)
from services.process_service import ProcessManager, ProcessService


class _CrashPlan(NamedTuple):
    canonical: str
    policy: dict[str, Any]
    bucket: str
    is_network_recovery: bool
    is_game_down: bool
    display_reason: str


class _ReviveState(NamedTuple):
    now: float
    crash_reason: str
    recovery_reason: str
    age: float


def _crash_detail_text(reason_key: str, reason_msg: str, context: RecoveryAttemptContext | None) -> str:
    return " ".join(
        part
        for part in (
            str(reason_msg or ""),
            str(getattr(context, "detail", "") or "") if context else "",
            str(reason_key or ""),
        )
        if part
    )


def _is_network_recovery(bucket: str, canonical: str) -> bool:
    return bucket == "network" or canonical in {"connection_error", "network_drop"}


def _crash_count_flags(plan: _CrashPlan) -> tuple[bool, bool]:
    count = not plan.is_network_recovery and not plan.is_game_down
    return count, count


def _recent_network_loss(acc: Account, now: float, window: float) -> bool:
    last = acc.last_network_lost_at
    if last is None:
        return False
    return (now - last) <= max(window, 30.0)


def _mark_vip_crash(acc: Account, active_vip: str, is_game_down: bool) -> None:
    if active_vip and acc._vip_tracker and not is_game_down:
        acc._vip_tracker.mark_crash(active_vip)


def _log_public_fallback(
    acc: Account,
    is_game_down: bool,
    active_vip: str,
    launch_fail_count: int,
    threshold: int,
) -> None:
    if is_game_down or not acc.place_id or not active_vip or launch_fail_count < threshold:
        return
    flog(
        f"[RECOVERY] {acc.display_name} switching launch strategy to public fallback "
        f"after {launch_fail_count} launch failures",
        "warning",
    )


def _launch_strategy(acc: Account, is_game_down: bool, launch_fail_count: int, threshold: int) -> str:
    active_vip = bool(acc.active_vip)
    if is_game_down:
        return "vip_preferred" if active_vip else "public_only"
    if active_vip and acc.place_id and launch_fail_count >= threshold:
        return "public_fallback"
    if active_vip:
        return "vip_preferred"
    return "public_only"


def _counts_as_rejoin(previous_trigger: str) -> bool:
    if not previous_trigger:
        return False
    if previous_trigger in {"farm_start", "initial_boot", "initial_probe"}:
        return False
    return (
        previous_trigger.startswith("recover:")
        or "force_rejoin" in previous_trigger
        or "network_restored" in previous_trigger
        or "backoff" in previous_trigger
        or "session_retry" in previous_trigger
    )


def _reset_launch_success_counters(acc: Account) -> None:
    acc.retry_count = 0
    acc.fail_count = 0
    acc.launch_fail_count = 0
    acc.crash_retry_count = 0
    acc.network_retry_count = 0
    acc.session_retry_count = 0
    acc.recovery_budget_attempts.clear()
    acc.session_wait_started_at = 0.0
    acc.last_network_lost_at = None
    acc.last_crash_reason = ""
    acc.last_rejoin_trigger = ""
    acc.last_watchdog_classification = "alive"
    acc.liveness_state = "alive"
    acc.liveness_suspect_since = 0.0
    acc.last_activity_reason = "launch_success"


def _revive_auth_blocked(acc: Account) -> bool:
    try:
        return evaluate_account_auth_gate(acc).blocked
    except Exception:
        return False


def _clamp_revive_launch_failures(acc: Account) -> None:
    try:
        if int(acc.launch_fail_count or 0) > 20:
            acc.launch_fail_count = 10
    except Exception:
        "intentional: ignore exception"


class RecoveryReportsMixin:
    """Crash/launch/fail reporters. Owns the report_* entries the router
    invokes through the RouterRecoveryActions seam; launch classification
    and revive markers live in recovery_policy."""

    def _kill_local_duplicate_for_session_conflict(self, acc: Account, ctx: RecoveryAttemptContext) -> int:
        try:
            return kill_local_duplicate_for_session_conflict(
                acc,
                ctx,
                lambda: ProcessManager.list_live_game_processes(launched_after=None),
                ProcessManager.kill_pid,
                lambda event, **fields: self._log_recovery_decision(event, acc, "session_conflict", **fields),
            )
        except Exception as exc:
            flog_kv("RECOVERY", "session_conflict_duplicate_check_failed", "warning", account=acc.display_name, error=exc)
            return 0


    def _relaunch_loop_window(self) -> tuple[float, int]:
        window = max(10.0, float(self._cfg.get("relaunch_loop_window", 45) or 45))
        limit = max(1, int(self._cfg.get("relaunch_loop_limit", 3) or 3))
        return window, limit

    def _detect_relaunch_loop(self, acc: Account, reason_key: str) -> str | None:
        canonical = canonical_reason(reason_key)
        fast_crash_reasons = {"process_crash", "watchdog_timeout", "loading_freeze"}
        if canonical not in fast_crash_reasons:
            with acc._lock:
                acc.rapid_relaunch_count = 0
            return None

        window, limit = self._relaunch_loop_window()
        now = time.time()
        with acc._lock:
            runtime = (now - acc.in_game_since) if acc.in_game_since else None
            recent_network_loss = _recent_network_loss(acc, now, window)
            if runtime is None or runtime > window:
                acc.rapid_relaunch_count = 0
                return None
            if recent_network_loss or not self._net.is_online():
                acc.rapid_relaunch_count = 0
                flog(
                    f"[RECOVERY] {acc.display_name} rapid crash ignored "
                    f"(reason={canonical}, network_context=true)",
                    "warning",
                )
                return None
            acc.rapid_relaunch_count += 1
            rapid_count = acc.rapid_relaunch_count

        flog(
            f"[RECOVERY] {acc.display_name} rapid crash #{rapid_count}/{limit} "
            f"(reason={canonical}, runtime={runtime:.1f}s)",
            "warning",
        )
        if rapid_count >= limit:
            return (
                f"Stopped auto rejoin after {rapid_count} rapid crashes "
                f"within {window:.0f}s"
            )
        return None


    def _record_session_conflict(
        self,
        acc: Account,
        context: RecoveryAttemptContext | None,
        reason_msg: str,
    ) -> tuple[str, bool]:
        attempt = self._session_conflicts.record(
            acc._config_username,
            float(self._cfg.get("session_conflict_window_seconds", 90) or 90),
        )
        if context:
            self._kill_local_duplicate_for_session_conflict(acc, context)
        return f"{reason_msg} [session conflict attempt {attempt}/3]", attempt >= 3

    def _kill_crash_process(
        self,
        acc: Account,
        canonical: str,
        pid: int,
        ctx: dict[str, Any],
        context: RecoveryAttemptContext | None,
    ) -> None:
        ProcessService.evict_pid_cache(pid, reason=f"recover:{canonical}", account=acc)
        kill_result = ProcessService.safe_kill_bound_process(
            acc,
            self._state_mgr,
            reason=f"recover:{canonical}",
            expected_runtime_generation=int(ctx.get("runtime_generation") or 0),
            increment_generation=False,
        )
        self._log_recovery_decision(
            "process_killed",
            acc,
            canonical,
            killed=kill_result.get("killed", False),
            kill_reason=kill_result.get("reason", ""),
            **(context.to_dict() if context else {}),
        )

    def _crash_plan(
        self,
        reason_key: str,
        reason_msg: str,
        context: RecoveryAttemptContext | None,
    ) -> _CrashPlan:
        canonical = _canonical_recovery_reason(reason_key, context)
        detail_text = _crash_detail_text(reason_key, reason_msg, context)
        if _is_game_unavailable_detail(detail_text):
            canonical = "game_unavailable"
        display_reason = _display_recovery_reason(reason_key, canonical, reason_msg, context)
        policy = policy_for(canonical)
        bucket = str(policy.get("bucket") or "crash")
        is_network_recovery = _is_network_recovery(bucket, canonical)
        is_game_down = canonical == "game_unavailable"
        return _CrashPlan(canonical, policy, bucket, is_network_recovery, is_game_down, display_reason)

    def _begin_crash_recovery(
        self,
        acc: Account,
        plan: _CrashPlan,
        reason_msg: str,
        context: RecoveryAttemptContext | None,
    ):
        count_crash, count_fail = _crash_count_flags(plan)
        return self._begin_recovery(
            acc,
            plan.canonical,
            status="recovering",
            bucket=plan.bucket,
            reason_msg=reason_msg,
            count_crash=count_crash,
            count_fail=count_fail,
            context=context,
        )

    def _handle_relaunch_loop(self, acc: Account, loop_reason: str | None) -> bool:
        if not loop_reason:
            return False
        if bool(self._cfg.get("relaunch_loop_fatal", False)):
            self.fail_account(acc, "relaunch_loop", loop_reason)
            return True
        try:
            loop_cooldown = max(10.0, float(self._cfg.get("relaunch_loop_cooldown_seconds", 300.0) or 300.0))
        except Exception:
            loop_cooldown = 300.0
        with acc._lock:
            acc.rapid_relaunch_count = 0
        self._log_recovery_decision(
            "relaunch_loop_cooldown",
            acc,
            "relaunch_loop",
            reason_msg=loop_reason,
            delay=f"{loop_cooldown:.1f}",
        )
        self._schedule_cooldown(acc, loop_cooldown, "relaunch_loop", "recover:relaunch_loop")
        return True

    def _reset_fail_counters_keep_active(self, acc: Account, max_fail: int) -> None:
        with acc._lock:
            acc.fail_count = 0
            acc.retry_count = 0
            acc.crash_retry_count = 0
            acc.launch_fail_count = 0
            acc.network_retry_count = 0
            acc.session_retry_count = 0
        self._log_recovery_decision(
            "max_fail_reset_keep_active",
            acc,
            "max_fail",
            reason_msg=f"fail limit {max_fail} reached - counters reset, account stays active",
        )

    def _stop_auto_rejoin(self, acc: Account, canonical: str, message: str) -> None:
        self._clear_recovery(acc, status="disabled", reason=canonical, inflight=False)
        flog(message, "warning")
        self._persist_runtime()

    def _schedule_crash_recovery(
        self,
        acc: Account,
        canonical: str,
        display_reason: str,
        cooldown: float | None,
    ) -> None:
        wait_for = self._adaptive_recovery_delay(acc, canonical, cooldown=cooldown)
        flog_kv(
            "RECOVERY",
            "scheduled",
            account=acc.display_name,
            reason=canonical,
            delay=f"{wait_for:.1f}",
            generation=acc.recovery_generation,
        )
        self._schedule_cooldown(acc, wait_for, canonical, f"recover:{canonical}", display_reason=display_reason)
        self._persist_runtime()

    def report_crash(
        self,
        acc: Account,
        reason_key: str,
        reason_msg: str,
        cooldown: float | None = None,
        context: RecoveryAttemptContext | None = None,
    ):
        plan = self._crash_plan(reason_key, reason_msg, context)
        if plan.canonical == "session_conflict":
            reason_msg, exceeded = self._record_session_conflict(acc, context, reason_msg)
            if exceeded:
                self.fail_account(acc, "session_conflict", "Repeated Error 273 session conflict")
                return
        ctx = self._begin_crash_recovery(acc, plan, reason_msg, context)
        if not ctx:
            return
        pid = ctx.get("pid")
        active_vip = ctx.get("active_vip")
        raw_fail_count = ctx.get("fail_count")
        fail_count = int(raw_fail_count) if raw_fail_count else 0
        loop_reason = self._detect_relaunch_loop(acc, plan.canonical)
        max_fail = int(self._cfg.get("max_fail_count", 5))

        if pid:
            self._kill_crash_process(acc, plan.canonical, pid, ctx, context)
        _mark_vip_crash(acc, active_vip, plan.is_game_down)

        self._state_mgr.transition(acc, AccountState.CRASH, reason=plan.canonical, force=True)
        self._bus.emit(
            EventName.ACCOUNT_CRASH,
            account=acc,
            reason=plan.canonical,
            reason_msg=reason_msg,
            **(context.to_dict() if context else {}),
        )

        if bool(plan.policy.get("fatal")):
            self.fail_account(acc, plan.canonical, reason_msg)
            return
        if self._handle_relaunch_loop(acc, loop_reason):
            return
        if fail_count >= max_fail:
            self._reset_fail_counters_keep_active(acc, max_fail)
        if not self._cfg.get("auto_rejoin", True):
            self._stop_auto_rejoin(
                acc,
                plan.canonical,
                f"[RECOVERY] Auto rejoin disabled - not scheduling recovery for {acc.display_name}",
            )
            return
        if self._executor_held():
            self._park_for_executor_hold(acc, plan.canonical)
            return
        self._schedule_crash_recovery(acc, plan.canonical, plan.display_reason, cooldown)


    def _public_fallback_threshold(self) -> int:
        return int(self._cfg.get("launch_public_fallback_threshold", 2) or 2)

    def _apply_launch_failure_strategy(
        self,
        acc: Account,
        is_game_down: bool,
        launch_fail_count: int,
        threshold: int,
    ) -> None:
        acc.launch_strategy = _launch_strategy(acc, is_game_down, launch_fail_count, threshold)

    def report_launch_failure(self, acc: Account, reason: str):
        canonical = canonical_launch_failure_reason(reason)
        if canonical == "captcha_required":
            set_account_captcha_hold(acc, reason, source="launch_failure", runtime_writer=self._runtime_state)
            self.fail_account(acc, CAPTCHA_REASON, CAPTCHA_BLOCK_REASON)
            return
        policy = policy_for(canonical)
        bucket = str(policy.get("bucket") or "launch")
        is_game_down = canonical == "game_unavailable"
        ctx = self._begin_recovery(
            acc,
            canonical,
            status="launch_backoff",
            bucket=bucket,
            reason_msg=reason,
            count_crash=False,
            count_fail=not is_game_down,
        )
        if not ctx:
            self._persist_runtime(force=True)
            return
        threshold = self._public_fallback_threshold()
        with acc._lock:
            launch_fail_count = acc.launch_fail_count
            active_vip = acc.active_vip
            self._apply_launch_failure_strategy(acc, is_game_down, launch_fail_count, threshold)

        delay = self._adaptive_recovery_delay(acc, canonical)
        self._state_mgr.transition(acc, AccountState.CRASH, reason=canonical, force=True)
        self._bus.emit(
            EventName.ACCOUNT_CRASH,
            account=acc,
            reason=canonical,
            reason_msg=RECOVERY_REASON_MESSAGES.get(canonical, RECOVERY_REASON_MESSAGES["launch_fail"]),
        )
        if bool(policy.get("fatal")):
            self.fail_account(acc, canonical, RECOVERY_REASON_MESSAGES.get(canonical, canonical))
            return
        if not self._cfg.get("auto_rejoin", True):
            self._clear_recovery(acc, status="disabled", reason=canonical, inflight=False)
            flog(f"[RECOVERY] Auto rejoin disabled - not scheduling launch retry for {acc.display_name}", "warning")
            self._persist_runtime()
            return
        if self._executor_held():
            self._park_for_executor_hold(acc, canonical)
            return
        _mark_vip_crash(acc, active_vip, is_game_down)
        _log_public_fallback(acc, is_game_down, active_vip, launch_fail_count, threshold)
        with acc._lock:
            acc.active_vip = ""
        self._schedule_cooldown(acc, delay, canonical, f"{canonical}_backoff")
        self._persist_runtime()


    def report_lua_waiting(self, acc: Account, trigger: str = "lua_required"):
        with acc._lock:
            runtime_generation = int(acc.runtime_generation or 0)
            recovery_generation = int(acc.recovery_generation or 0)
        mark_waiting_for_lua(acc, self._runtime_state, self._state_mgr, trigger)
        self._release_recovery_owner(acc._config_username, runtime_generation, recovery_generation, "waiting_for_lua")
        self._persist_runtime()


    def report_launch_success(self, acc: Account, trigger: str = "launch_success", count_rejoin: bool | None = None, lua_confirmed: bool = False):
        auth_gate = evaluate_account_auth_gate(acc)
        if auth_gate.blocked:
            mark_account_auth_quarantined(acc, auth_gate, source="launch_success", runtime_writer=self._runtime_state)
            self.fail_account(acc, auth_gate.reason_key, auth_gate.reason)
            return
        if lua_liveness_required(self._cfg) and not lua_confirmed:
            self.report_lua_waiting(acc, trigger)
            return
        with acc._lock:
            runtime_generation = int(acc.runtime_generation or 0)
            recovery_generation = int(acc.recovery_generation or 0)
            previous_trigger = trigger or acc.last_rejoin_trigger
            if count_rejoin is None:
                count_rejoin = _counts_as_rejoin(previous_trigger)
            _reset_launch_success_counters(acc)
            self._runtime_state.set_cooldown(acc, 0.0, reason="launch_success")
            self._runtime_state.set_recovery(acc, status="in_game", reason="launch_success", inflight=False)
            acc.recovery_scheduled_at = 0.0
        self._release_recovery_owner(acc._config_username, runtime_generation, recovery_generation, "launch_success")
        self._session_conflicts.clear(acc._config_username)
        self._state_mgr.transition(acc, AccountState.IN_GAME, reason="launch_success", force=True)
        if count_rejoin:
            self._bus.emit(EventName.REJOIN_SUCCESS, account=acc)
        self._persist_runtime()


    def fail_account(self, acc: Account, reason: str, reason_msg: str):
        if is_captcha_status_text(reason, reason_msg):
            set_account_captcha_hold(acc, reason_msg or reason, source="fail_account", runtime_writer=self._runtime_state)
            reason = CAPTCHA_REASON
            reason_msg = CAPTCHA_BLOCK_REASON
        if canonical_reason(reason) == "multi_roblox_guard_failed":
            if self._schedule_multi_roblox_guard_recovery(acc, reason_msg):
                return
        with acc._lock:
            if acc.state == AccountState.FAILED and acc.recovery_status == "failed":
                self._log_recovery_decision("fail_suppressed", acc, reason, reason_msg=reason_msg)
                return
            runtime_generation = int(acc.runtime_generation or 0)
            recovery_generation = int(acc.recovery_generation or 0)
            acc.last_crash_reason = reason
            acc.fail_count += 1
            self._runtime_state.set_cooldown(acc, 0.0, reason=reason)
            self._runtime_state.set_recovery(acc, status="failed", reason=reason, inflight=False)
            acc.recovery_scheduled_at = 0.0
        self._release_recovery_owner(acc._config_username, runtime_generation, recovery_generation, reason)
        self._state_mgr.transition(acc, AccountState.FAILED, reason=reason, force=True)
        self._bus.emit(
            EventName.ACCOUNT_FAILED,
            account=acc,
            reason=reason,
            reason_msg=reason_msg,
        )
        self._persist_runtime(force=True)


    def _revive_min_age(self) -> float:
        try:
            min_age = max(30.0, float(self._cfg.get("failed_transient_revive_seconds", 90) or 90))
        except Exception:
            min_age = 90.0
        return min(600.0, max(30.0, min_age))

    def _revive_state(self, acc: Account) -> _ReviveState | None:
        now = time.time()
        with acc._lock:
            if acc.desired_state != AccountState.IN_GAME:
                return None
            if acc.state != AccountState.FAILED:
                return None
            crash_reason = str(acc.last_crash_reason or "").strip().lower()
            recovery_reason = str(acc.last_recovery_reason or "").strip().lower()
            last_at = max(float(acc.last_recovery_at or 0.0), float(acc.last_state_change_at or 0.0))
            age = (now - last_at) if last_at else 1e9
        return _ReviveState(now, crash_reason, recovery_reason, age)

    def _reset_revive_budget(self, acc: Account, now: float) -> bool:
        try:
            window_seconds = max(1.0, float(self._cfg.get("recovery_budget_window_seconds", 300) or 300))
            with acc._lock:
                acc.recovery_budget_attempts = [
                    float(ts)
                    for ts in list(getattr(acc, "recovery_budget_attempts", []) or [])
                    if (now - float(ts)) <= window_seconds
                ]
                acc.fail_count = 0
                acc.retry_count = 0
                acc.crash_retry_count = 0
                acc.network_retry_count = 0
                acc.session_retry_count = 0
                _clamp_revive_launch_failures(acc)
                self._runtime_state.set_cooldown(acc, 0.0, reason="transient_failed_revive")
                self._runtime_state.set_recovery(acc, status="queued", reason="transient_failed_revive", inflight=True)
        except Exception:
            return False
        return True

    def _finish_transient_revive(self, acc: Account, trigger: str) -> bool:
        try:
            self._state_mgr.transition(acc, AccountState.READY, reason="transient_failed_revive", force=True)
        except Exception:
            return False
        try:
            self._queue_account(acc, f"{trigger}:transient_failed_revive")
        except Exception:
            return False
        try:
            self._persist_runtime(force=True)
        except Exception:
            "intentional: ignore exception"
        return True

    def maybe_revive_transient_failed(self, acc: Account, trigger: str = "periodic_reconcile") -> bool:
        """Auto-revive FAILED accounts stuck by transient game-down budget.

        Before this fix, 8 quick "VIP invite did not expose accessCode"
        launch failures inside 300s tripped recovery_budget_exceeded ->
        FAILED forever (evaluate holds "already_failed"). The map reopening
        later never rejoined — the card just sat on "Rejoining" (frontend
        maps FAILED to Rejoining) with zero retries.

        Revive only transient failures (budget/launch/game_unavailable),
        only when the failure is old enough that an immediate requeue will
        not hot-loop, and never for auth/captcha/ban blocks.
        Returns True when the account was revived and requeued.
        """
        state = self._revive_state(acc)
        if state is None:
            return False
        if not is_transient_recovery_mark(state.crash_reason, state.recovery_reason):
            return False
        if _revive_auth_blocked(acc):
            return False
        if state.age < self._revive_min_age():
            return False
        if not self._reset_revive_budget(acc, state.now):
            return False
        self._log_recovery_decision(
            "transient_failed_revived",
            acc,
            "recovery_budget_exceeded",
            trigger=trigger,
            age=f"{state.age:.1f}",
        )
        if not self._finish_transient_revive(acc, trigger):
            return False
        return True


    def force_rejoin(self, acc: Account):
        if self._executor_held():
            self._park_for_executor_hold(acc, "force_rejoin")
            return
        with acc._lock:
            acc.retry_count = 0
            acc.fail_count = 0
            acc.launch_fail_count = 0
            acc.crash_retry_count = 0
            acc.network_retry_count = 0
            acc.session_retry_count = 0
            acc.recovery_budget_attempts.clear()
            acc.session_wait_started_at = 0.0
            acc.last_network_lost_at = None
            acc.pid_missing_since = 0.0
            self._runtime_state.set_cooldown(acc, 0.0, reason="force_rejoin")
            acc.last_crash_reason = ""
            acc.last_rejoin_trigger = "force_rejoin"
            acc.session_checked = True
            acc.session_valid = True
            pid = acc.pid
            runtime_generation = acc.runtime_generation
        ctx = self._begin_recovery(
            acc,
            "force_rejoin",
            status="manual",
            bucket="manual",
            force=True,
            count_retry=False,
            count_crash=False,
            count_fail=False,
        )
        if not ctx:
            return
        if pid:
            kill_result = ProcessService.safe_kill_bound_process(
                acc,
                self._state_mgr,
                reason="force_rejoin_kill",
                expected_runtime_generation=runtime_generation,
            )
            if kill_result.get("reason") == "stale_runtime_generation":
                return
        self._state_mgr.transition(acc, AccountState.READY, reason="force_rejoin_reset", force=True)
        self.request_evaluate(acc, trigger="force_rejoin", force_restart=False)
        self._persist_runtime(force=True)
