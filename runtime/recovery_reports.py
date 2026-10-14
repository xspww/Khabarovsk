"""Recovery reporters: crash, launch, fail, revive, force-rejoin."""
from __future__ import annotations

import time

from core import Account, AccountState, EventName, flog, flog_kv
from runtime.lua_liveness_policy import lua_liveness_required, mark_waiting_for_lua
from runtime.recovery_context import RecoveryAttemptContext
from runtime.recovery_policy import (
    canonical_reason,
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


class RecoveryReportsMixin:
    """Central recovery/rejoin controller. Every path that wants recovery reports here."""

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


    def _detect_relaunch_loop(self, acc: Account, reason_key: str) -> str | None:
        cfg = self._cfg
        canonical = canonical_reason(reason_key)
        fast_crash_reasons = {"process_crash", "watchdog_timeout", "loading_freeze"}
        if canonical not in fast_crash_reasons:
            with acc._lock:
                acc.rapid_relaunch_count = 0
            return None

        window = max(10.0, float(cfg.get("relaunch_loop_window", 45) or 45))
        limit = max(1, int(cfg.get("relaunch_loop_limit", 3) or 3))
        now = time.time()
        with acc._lock:
            runtime = (now - acc.in_game_since) if acc.in_game_since else None
            recent_network_loss = (
                acc.last_network_lost_at is not None and
                (now - acc.last_network_lost_at) <= max(window, 30.0)
            )
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


    def report_crash(
        self,
        acc: Account,
        reason_key: str,
        reason_msg: str,
        cooldown: float | None = None,
        context: RecoveryAttemptContext | None = None,
    ):
        canonical = _canonical_recovery_reason(reason_key, context)
        detail_text = " ".join(
            part for part in (
                str(reason_msg or ""),
                str(getattr(context, "detail", "") or "") if context else "",
                str(reason_key or ""),
            )
            if part
        )
        if _is_game_unavailable_detail(detail_text):
            # Kicked because the map shut down for an update (e.g. Teleport
            # Unauthorized). The client is dead so normal crash cleanup still
            # runs, but the retry must be slow/indefinite and budget-exempt —
            # otherwise 8 quick launch failures trip FAILED and the map
            # reopening never rejoins.
            canonical = "game_unavailable"
        display_reason = _display_recovery_reason(reason_key, canonical, reason_msg, context)
        policy = policy_for(canonical)
        bucket = str(policy.get("bucket") or "crash")
        is_network_recovery = bucket == "network" or canonical in {"connection_error", "network_drop"}
        is_game_down = canonical == "game_unavailable"
        if canonical == "session_conflict":
            attempt = self._session_conflicts.record(
                acc._config_username,
                float(self._cfg.get("session_conflict_window_seconds", 90) or 90),
            )
            reason_msg = f"{reason_msg} [session conflict attempt {attempt}/3]"
            if context:
                self._kill_local_duplicate_for_session_conflict(acc, context)
            if attempt >= 3:
                self.fail_account(acc, "session_conflict", "Repeated Error 273 session conflict")
                return
        ctx = self._begin_recovery(
            acc,
            canonical,
            status="recovering",
            bucket=bucket,
            reason_msg=reason_msg,
            count_crash=not is_network_recovery and not is_game_down,
            count_fail=not is_network_recovery and not is_game_down,
            context=context,
        )
        if not ctx:
            return
        pid = ctx.get("pid")
        active_vip = ctx.get("active_vip")
        fail_count = int(ctx.get("fail_count") or 0)

        loop_reason = self._detect_relaunch_loop(acc, canonical)
        max_fail = int(self._cfg.get("max_fail_count", 5))

        if pid:
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
        if active_vip and acc._vip_tracker and not is_game_down:
            # Game-down is not the link's fault — do not blacklist it.
            acc._vip_tracker.mark_crash(active_vip)

        self._state_mgr.transition(acc, AccountState.CRASH, reason=canonical, force=True)
        self._bus.emit(
            EventName.ACCOUNT_CRASH,
            account=acc,
            reason=canonical,
            reason_msg=reason_msg,
            **(context.to_dict() if context else {}),
        )

        if bool(policy.get("fatal")):
            self.fail_account(acc, canonical, reason_msg)
            return
        if loop_reason:
            if bool(self._cfg.get("relaunch_loop_fatal", False)):
                self.fail_account(acc, "relaunch_loop", loop_reason)
                return
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
            return
        if fail_count >= max_fail:
            # The "Finished" mechanism is removed: exceeding fail limits never
            # terminates an account. Reset the counters and keep rejoining.
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

        if not self._cfg.get("auto_rejoin", True):
            self._clear_recovery(acc, status="disabled", reason=canonical, inflight=False)
            flog(f"[RECOVERY] Auto rejoin disabled - not scheduling recovery for {acc.display_name}", "warning")
            self._persist_runtime()
            return

        if self._executor_held():
            self._park_for_executor_hold(acc, canonical)
            return

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


    def report_launch_failure(self, acc: Account, reason: str):
        reason_l = str(reason or "").lower()
        if is_captcha_status_text(reason_l):
            set_account_captcha_hold(acc, reason, source="launch_failure", runtime_writer=self._runtime_state)
            self.fail_account(acc, CAPTCHA_REASON, CAPTCHA_BLOCK_REASON)
            return
        if _is_game_unavailable_detail(reason):
            canonical = "game_unavailable"
        elif "server full" in reason_l or "experience is full" in reason_l:
            canonical = "server_full"
        elif "cookie" in reason_l or "auth" in reason_l or "login" in reason_l:
            canonical = "auth_failure"
        elif "verify_timeout" in reason_l or "pid not detected" in reason_l:
            canonical = "watchdog_timeout"
        else:
            canonical = "launch_fail"
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
        with acc._lock:
            launch_fail_count = acc.launch_fail_count
            active_vip = acc.active_vip
            if is_game_down:
                # Map down, not a bad link: keep VIP affinity so the reopen
                # rejoins the same private server instead of drifting public.
                if active_vip:
                    acc.launch_strategy = "vip_preferred"
                else:
                    acc.launch_strategy = "public_only"
            elif (
                active_vip and acc.place_id and
                launch_fail_count >= int(self._cfg.get("launch_public_fallback_threshold", 2) or 2)
            ):
                acc.launch_strategy = "public_fallback"
            elif active_vip:
                acc.launch_strategy = "vip_preferred"
            else:
                acc.launch_strategy = "public_only"

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
        if active_vip and acc._vip_tracker and not is_game_down:
            # Game-down is not the link's fault — do not blacklist it.
            acc._vip_tracker.mark_crash(active_vip)
        if not is_game_down and acc.place_id and active_vip and launch_fail_count >= int(self._cfg.get("launch_public_fallback_threshold", 2) or 2):
            flog(
                f"[RECOVERY] {acc.display_name} switching launch strategy to public fallback "
                f"after {launch_fail_count} launch failures",
                "warning",
            )
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
                count_rejoin = bool(
                    previous_trigger and
                    previous_trigger not in {"farm_start", "initial_boot", "initial_probe"} and
                    (
                        previous_trigger.startswith("recover:") or
                        "force_rejoin" in previous_trigger or
                        "network_restored" in previous_trigger or
                        "backoff" in previous_trigger or
                        "session_retry" in previous_trigger
                    )
                )
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
        try:
            min_age = max(
                30.0,
                float(self._cfg.get("failed_transient_revive_seconds", 90) or 90),
            )
        except Exception:
            min_age = 90.0
        min_age = min(600.0, max(30.0, min_age))
        now = time.time()
        with acc._lock:
            if acc.desired_state != AccountState.IN_GAME:
                return False
            if acc.state != AccountState.FAILED:
                return False
            crash_reason = str(acc.last_crash_reason or "").strip().lower()
            recovery_reason = str(acc.last_recovery_reason or "").strip().lower()
            last_at = max(
                float(acc.last_recovery_at or 0.0),
                float(acc.last_state_change_at or 0.0),
            )
            age = (now - last_at) if last_at else 1e9
        transient_markers = (
            "recovery_budget_exceeded",
            "launch_fail",
            "game_unavailable",
            "launch_backoff",
            "vip invite did not expose accesscode",
            "joinprivategame",
            "unauthorized",
            "teleport failed",
        )
        haystack = f"{crash_reason} {recovery_reason}"
        if not any(marker in haystack for marker in transient_markers):
            return False
        # Auth blocks are terminal — never auto-revive (evaluator re-fails them).
        try:
            gate = evaluate_account_auth_gate(acc)
            if gate.blocked:
                return False
        except Exception:
            pass
        if age < min_age:
            return False
        # Budget window has slid past: drop stale attempts so the revived
        # account does not instantly re-trip FAILED on the next failure.
        try:
            window_seconds = max(
                1.0,
                float(self._cfg.get("recovery_budget_window_seconds", 300) or 300),
            )
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
                # Keep launch_fail_count for game_unavailable backoff growth;
                # reset only a runaway counter so delay restarts from ~30s.
                try:
                    if int(acc.launch_fail_count or 0) > 20:
                        acc.launch_fail_count = 10
                except Exception:
                    pass
                self._runtime_state.set_cooldown(acc, 0.0, reason="transient_failed_revive")
                self._runtime_state.set_recovery(
                    acc, status="queued", reason="transient_failed_revive", inflight=True,
                )
        except Exception:
            return False
        self._log_recovery_decision(
            "transient_failed_revived",
            acc,
            "recovery_budget_exceeded",
            trigger=trigger,
            age=f"{age:.1f}",
        )
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
            pass
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
