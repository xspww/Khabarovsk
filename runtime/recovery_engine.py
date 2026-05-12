from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from core import Account, AccountState, EventBus, EventName, SmartQueue, StateManager, flog, flog_kv
from domain.runtime_signals import RuntimeSignal, is_recovery_signal, normalize_runtime_signal
from services.network_monitor import NetworkMonitor
from services.process_service import ProcessManager
from runtime.account_runtime_controller import AccountRuntimeController
from runtime.runtime_state_manager import RuntimeStateManager
from runtime.recovery_context import RecoveryAttemptContext, SESSION_CONFLICT, reason_for_category
from runtime.recovery_policy import (
    RecoveryDedupeTracker,
    SessionConflictTracker,
    active_recovery_block_reason,
    adaptive_recovery_delay,
    build_recovery_log_payload,
    canonical_reason,
    context_from_signal,
    kill_local_duplicate_for_session_conflict,
    policy_for,
)
from runtime.recovery_support import (
    RECOVERY_REASON_MESSAGES,
    _enrich_visual_disconnect_payload_with_log,
    compute_backoff,
)


class RecoveryCoordinator:
    """
    Central recovery/rejoin controller.
    Every path that wants recovery reports here.
    """

    def __init__(
        self,
        queue: SmartQueue,
        state_mgr: StateManager,
        bus: EventBus,
        net: NetworkMonitor,
        stop: threading.Event,
        cfg: dict,
        accounts: Optional[List[Account]] = None,
        persist_callback=None,
    ):
        self._queue = queue
        self._state_mgr = state_mgr
        self._bus = bus
        self._net = net
        self._stop = stop
        self._cfg = cfg
        self._accounts = accounts or []
        self._persist_callback = persist_callback
        self._last_persist = 0.0
        self._pending: Dict[str, Tuple[float, Account, str, int, int]] = {}
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._closed = False
        self._active_recoveries: Dict[str, Dict[str, Any]] = {}
        self._runtime_state = RuntimeStateManager(logger=flog_kv)
        self._account_runtime = AccountRuntimeController(self._runtime_state, recovery=self, logger=flog_kv)
        self._duplicate_window = max(1.0, float(cfg.get("recovery_duplicate_window", 8) or 8))
        self._recent_signals: Dict[Tuple[str, str, str, int], float] = {}
        self._recovery_dedupe = RecoveryDedupeTracker(float(cfg.get("recovery_dedupe_window_seconds", 3) or 3))
        self._session_conflicts = SessionConflictTracker()
        self._scheduler = threading.Thread(
            target=self._scheduler_loop,
            daemon=True,
            name="RecoveryScheduler",
        )
        self._scheduler.start()
    def _persist_runtime(self, force: bool = False):
        if not self._persist_callback:
            return
        now = time.time()
        if not force and (now - self._last_persist) < 2.0:
            return
        self._last_persist = now
        try:
            self._persist_callback()
        except Exception as e:
            flog_kv("RECOVERY", "persist_failed", "warning", error=e)

    def stop(self):
        with self._cond:
            self._closed = True
            pending = len(self._pending)
            active = len(self._active_recoveries)
            self._pending.clear()
            self._active_recoveries.clear()
            self._cond.notify_all()
        try:
            self._queue.cancel_all("recovery_stop")
        except Exception as exc:
            flog_kv("RECOVERY", "queue_cancel_failed", "warning", error=exc)
        flog_kv("RECOVERY", "coordinator_stopped", pending_cancelled=pending, active_cancelled=active)

    def _dedupe_recovery_context(self, ctx: RecoveryAttemptContext, acc: Account, reason_key: str) -> bool:
        result = self._recovery_dedupe.check_and_mark(ctx)
        if not result.get("ignore"):
            return False
        self._log_recovery_decision("recovery_ignored", acc, reason_key, **result, **ctx.to_dict())
        return True

    def _active_recovery_blocks(self, acc: Account, ctx: RecoveryAttemptContext, reason_key: str) -> bool:
        with self._lock:
            owner = self._active_recoveries.get(acc._config_username)
            result = active_recovery_block_reason(owner, ctx)
        if not result.get("blocked"):
            return False
        self._log_recovery_decision("recovery_ignored", acc, reason_key, **result, **ctx.to_dict())
        return True

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

    def _log_recovery_decision(self, event: str, acc: Account, reason: str, **fields):
        flog_kv("RECOVERY", event, **build_recovery_log_payload(event, acc, reason, fields))

    def _max_concurrent_accounts(self) -> int:
        try:
            return max(1, int(float(self._cfg.get("max_concurrent_accounts", 40) or 40)))
        except Exception:
            return 40

    def _queue_delay_seconds(self) -> float:
        try:
            return max(1.0, float(self._cfg.get("queue_delay_seconds", self._cfg.get("launch_rate_interval", 15)) or 15))
        except Exception:
            return 15.0

    def _active_slot_count(self, excluding: Optional[Account] = None) -> int:
        active_states = {AccountState.QUEUED, AccountState.LAUNCHING, AccountState.VERIFY, AccountState.IN_GAME}
        count = 0
        for item in self._accounts:
            if item is excluding:
                continue
            with item._lock:
                if item.desired_state == AccountState.IN_GAME and item.state in active_states:
                    count += 1
        return count

    def _queue_slot_available(self, acc: Account) -> bool:
        return self._active_slot_count(excluding=acc) < self._max_concurrent_accounts()

    def handle_runtime_signal(
        self,
        acc: Account,
        signal: str,
        reason: str = "",
        payload: Optional[Dict[str, Any]] = None,
        expected_runtime_generation: Optional[int] = None,
        expected_session_id: str = "",
        expected_launch_nonce: str = "",
        expected_transaction_id: str = "",
    ) -> bool:
        """Single boundary for worker/watchdog/maintenance recovery signals."""
        payload = dict(payload or {})
        payload = _enrich_visual_disconnect_payload_with_log(payload)
        raw_signal = str(signal or "").strip().lower()
        signal_name = RuntimeSignal.REJOIN_REQUESTED.value if raw_signal == RuntimeSignal.REJOIN_REQUESTED.value else normalize_runtime_signal(signal)
        reason_key = str(payload.get("reason_key") or reason or signal_name or "runtime_signal")
        context = context_from_signal(acc, signal_name, reason_key, payload)
        if context.category == SESSION_CONFLICT:
            reason_key = "session_conflict"
            payload.setdefault("reason_key", reason_key)
            payload.setdefault("disconnect_category", SESSION_CONFLICT)
        with self._lock:
            if self._closed or self._stop.is_set():
                self._log_recovery_decision(
                    "runtime_signal_rejected",
                    acc,
                    reason_key,
                    signal=signal_name,
                    reject="coordinator_closed",
                    **context.to_dict(),
                )
                return False
        with acc._lock:
            if not self._runtime_state.guard_session_identity(
                acc,
                expected_generation=expected_runtime_generation,
                expected_session_id=expected_session_id,
                expected_launch_nonce=expected_launch_nonce,
                expected_transaction_id=expected_transaction_id,
                reason=f"runtime_signal:{signal_name}:{reason_key}",
            ):
                self._log_recovery_decision(
                    "runtime_signal_rejected",
                    acc,
                    reason_key,
                    signal=signal_name,
                    reject="stale_identity",
                    expected_runtime_generation=expected_runtime_generation,
                    expected_session_id=expected_session_id,
                    expected_transaction_id=expected_transaction_id,
                    **context.to_dict(),
                )
                return False
            current_recovery_generation = int(acc.recovery_generation or 0)

        if is_recovery_signal(signal_name):
            if self._active_recovery_blocks(acc, context, reason_key):
                return True
            if self._dedupe_recovery_context(context, acc, reason_key):
                return True

        signal_key = (acc._config_username, signal_name, canonical_reason(reason_key), current_recovery_generation)
        now = time.time()
        if is_recovery_signal(signal_name):
            with self._lock:
                last_seen = float(self._recent_signals.get(signal_key, 0.0) or 0.0)
                if last_seen and (now - last_seen) < self._duplicate_window:
                    self._log_recovery_decision(
                        "recovery_duplicate_suppressed",
                        acc,
                        reason_key,
                        signal=signal_name,
                        recovery_generation=current_recovery_generation,
                        age=f"{now - last_seen:.2f}",
                        **context.to_dict(),
                    )
                    return True
                self._recent_signals[signal_key] = now
                if len(self._recent_signals) > 512:
                    cutoff = now - max(self._duplicate_window * 4, 60.0)
                    self._recent_signals = {key: ts for key, ts in self._recent_signals.items() if ts >= cutoff}

        self._log_recovery_decision(
            "runtime_signal_received",
            acc,
            reason_key,
            signal=signal_name,
            payload_keys=",".join(sorted(str(k) for k in payload.keys())),
            **context.to_dict(),
        )

        if signal_name in {RuntimeSignal.FAULT.value, RuntimeSignal.CRASH.value, RuntimeSignal.WATCHDOG_TIMEOUT.value, RuntimeSignal.PROCESS_LOST.value, RuntimeSignal.LOADING_FREEZE.value}:
            reason_msg = str(payload.get("reason_msg") or payload.get("detail") or reason_key)
            self.report_crash(acc, reason_key, reason_msg, cooldown=payload.get("cooldown"), context=context)
        elif signal_name in {RuntimeSignal.LAUNCH_FAILURE.value, RuntimeSignal.LAUNCH_FAILED.value}:
            self.report_launch_failure(acc, str(payload.get("detail") or reason_key or "launch_failed"))
        elif signal_name == RuntimeSignal.LAUNCH_SUCCESS.value:
            count_rejoin = payload.get("count_rejoin") if "count_rejoin" in payload else None
            self.report_launch_success(acc, trigger=str(payload.get("trigger") or reason_key or "launch_success"), count_rejoin=count_rejoin)
        elif signal_name in {RuntimeSignal.FATAL.value, RuntimeSignal.AUTH_FAILURE.value, RuntimeSignal.SESSION_FAILURE.value}:
            reason_msg = str(payload.get("reason_msg") or payload.get("detail") or reason_key)
            self.fail_account(acc, reason_key, reason_msg)
        elif signal_name in {RuntimeSignal.NETWORK_LOST.value, RuntimeSignal.NETWORK_DROP.value}:
            self.mark_network_lost(acc, trigger=str(payload.get("trigger") or reason_key or "network_lost"))
        elif signal_name == RuntimeSignal.EVALUATE.value:
            self.evaluate(
                acc,
                trigger=str(payload.get("trigger") or reason_key or "runtime_signal"),
                force_restart=bool(payload.get("force_restart", False)),
                expected_runtime_generation=expected_runtime_generation,
                expected_session_id=expected_session_id,
                expected_launch_nonce=expected_launch_nonce,
                expected_transaction_id=expected_transaction_id,
            )
        elif signal_name == RuntimeSignal.REJOIN_REQUESTED.value:
            self.force_rejoin(acc)
        else:
            self._log_recovery_decision(
                "runtime_signal_rejected",
                acc,
                reason_key,
                signal=signal_name,
                reject="unsupported_signal",
            )
            return False

        self._log_recovery_decision("runtime_signal_routed", acc, reason_key, signal=signal_name, **context.to_dict())
        return True

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
        context: Optional[RecoveryAttemptContext] = None,
    ) -> Optional[Dict[str, Any]]:
        now = time.time()
        with acc._lock:
            if self._stop.is_set() or self._closed or acc.desired_state != AccountState.IN_GAME:
                self._log_recovery_decision(
                    "ignored",
                    acc,
                    canonical,
                    desired=getattr(acc.desired_state, "name", acc.desired_state),
                    stopped=self._stop.is_set(),
                    closed=self._closed,
                )
                return None
            if acc.state == AccountState.FAILED:
                self._log_recovery_decision("ignored", acc, canonical, reason_detail="already_failed")
                return None
            duplicate = (
                acc.recovery_inflight and
                not force and
                acc.last_recovery_reason == canonical and
                (now - float(acc.last_recovery_at or 0.0)) < self._duplicate_window
            )
            if duplicate:
                self._log_recovery_decision(
                    "recovery_duplicate_suppressed",
                    acc,
                    canonical,
                    age=f"{now - float(acc.last_recovery_at or now):.2f}",
                    window=f"{self._duplicate_window:.1f}",
                )
                return None
            account_key = acc._config_username
            with self._lock:
                owner = self._active_recoveries.get(account_key)
                if owner and not force:
                    same_runtime = int(owner.get("runtime_generation", -1)) == int(acc.runtime_generation or 0)
                    same_recovery = int(owner.get("recovery_generation", -1)) == int(acc.recovery_generation or 0)
                    if same_runtime and same_recovery:
                        current_state = acc.state
                        if canonical in {"launch_fail", "watchdog_timeout"} and current_state in (AccountState.LAUNCHING, AccountState.VERIFY):
                            self._active_recoveries.pop(account_key, None)
                            self._log_recovery_decision(
                                "recovery_owner_replaced",
                                acc,
                                canonical,
                                generation=owner.get("recovery_generation", 0),
                                runtime_generation=owner.get("runtime_generation", 0),
                                owner_reason=owner.get("reason", ""),
                                state=current_state.name,
                            )
                        else:
                            self._log_recovery_decision(
                                "recovery_duplicate_suppressed",
                                acc,
                                canonical,
                                generation=owner.get("recovery_generation", 0),
                                runtime_generation=owner.get("runtime_generation", 0),
                                owner_reason=owner.get("reason", ""),
                            )
                            return None
                    else:
                        self._log_recovery_decision(
                            "recovery_duplicate_suppressed",
                            acc,
                            canonical,
                            reject="active_recovery_owner_exists",
                            owner_runtime_generation=owner.get("runtime_generation", 0),
                            owner_recovery_generation=owner.get("recovery_generation", 0),
                            owner_reason=owner.get("reason", ""),
                        )
                        return None

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
            with self._lock:
                self._active_recoveries[account_key] = {
                    "account_id": account_key,
                    "runtime_generation": int(acc.runtime_generation or 0),
                    "recovery_generation": int(acc.recovery_generation or 0),
                    "reason": canonical,
                    "status": status,
                    "started_at": now,
                    "bucket": bucket,
                    "priority": int(context.priority if context else 0),
                    "token": context.token if context else "",
                }

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
        runtime_generation: Optional[int],
        recovery_generation: Optional[int],
        reason: str,
    ) -> None:
        with self._lock:
            owner = self._active_recoveries.get(account_key)
            if not owner:
                return
            if runtime_generation is not None and int(owner.get("runtime_generation", -1)) != int(runtime_generation):
                return
            if recovery_generation is not None and int(owner.get("recovery_generation", -1)) != int(recovery_generation):
                return
            self._active_recoveries.pop(account_key, None)
        flog_kv(
            "RECOVERY",
            "recovery_owner_released",
            account=account_key,
            runtime_generation=runtime_generation,
            recovery_generation=recovery_generation,
            reason=reason,
        )

    def _schedule_cooldown(self, acc: Account, delay: float, reason: str, transition_reason: str):
        until = time.time() + max(0.0, float(delay or 0.0))
        self._state_mgr.set_cooldown(acc, until, reason=transition_reason)
        self._state_mgr.set_recovery(acc, status="cooldown", reason=reason, inflight=True)
        self._state_mgr.transition(acc, AccountState.COOLDOWN, reason=transition_reason)
        self._log_recovery_decision(
            "cooldown",
            acc,
            reason,
            delay=f"{max(0.0, float(delay or 0.0)):.1f}",
            until=f"{until:.3f}",
        )
        self._schedule(acc, delay, transition_reason)

    def _scheduler_loop(self):
        while not self._stop.is_set():
            with self._cond:
                if self._closed:
                    break
                if not self._pending:
                    self._cond.wait(timeout=1.0)
                    continue
                key, item = min(self._pending.items(), key=lambda pair: pair[1][0])
                due, acc, reason, generation, runtime_generation = item
                wait_for = due - time.time()
                if wait_for > 0:
                    self._cond.wait(timeout=min(wait_for, 5.0))
                    continue
                self._pending.pop(key, None)
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
                    continue
                if not self._runtime_state.guard_runtime_generation(
                    acc,
                    runtime_generation,
                    reason=f"scheduler:{reason}",
                ):
                    continue
                self._runtime_state.set_recovery(acc, status="due", reason="", inflight=True)
                acc.recovery_scheduled_at = 0.0
            self._persist_runtime()
            self.evaluate(
                acc,
                trigger=reason,
                expected_runtime_generation=runtime_generation,
                expected_recovery_generation=generation,
            )

    def _detect_relaunch_loop(self, acc: Account, reason_key: str) -> Optional[str]:
        canonical = canonical_reason(reason_key)
        fast_crash_reasons = {"process_crash", "watchdog_timeout", "loading_freeze"}
        if canonical not in fast_crash_reasons:
            with acc._lock:
                acc.rapid_relaunch_count = 0
            return None

        window = max(10.0, float(self._cfg.get("relaunch_loop_window", 45) or 45))
        limit = max(1, int(self._cfg.get("relaunch_loop_limit", 3) or 3))
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

    def set_desired(self, acc: Account, desired: AccountState):
        with acc._lock:
            self._runtime_state.set_desired(acc, desired, reason="recovery_set_desired")

    def _log_hold(self, acc: Account, trigger: str, reason: str):
        flog(f"[RECOVERY] hold {acc.display_name}: trigger={trigger} reason={reason}")

    def request_evaluate(self, acc: Account, trigger: str, force_restart: bool = False) -> bool:
        return self._account_runtime.request_evaluate(acc, trigger=trigger, force_restart=force_restart)

    def request_rejoin(self, acc: Account, reason: str = "force_rejoin") -> bool:
        return self._account_runtime.request_rejoin(acc, reason=reason, bump_runtime_generation=True)

    def _retry_bucket_exceeded(self, acc: Account) -> Optional[str]:
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

    def _adaptive_recovery_delay(self, acc: Account, reason_key: str, cooldown: Optional[float] = None) -> float:
        attempts = self._session_conflicts.count(
            acc._config_username,
            float(self._cfg.get("session_conflict_window_seconds", 90) or 90),
        )
        return adaptive_recovery_delay(self._cfg, acc, reason_key, cooldown, attempts, compute_backoff)

    def evaluate(
        self,
        acc: Account,
        trigger: str = "evaluate",
        force_restart: bool = False,
        expected_runtime_generation: Optional[int] = None,
        expected_recovery_generation: Optional[int] = None,
        expected_session_id: str = "",
        expected_launch_nonce: str = "",
        expected_transaction_id: str = "",
    ):
        with acc._lock:
            if not self._runtime_state.guard_session_identity(
                acc,
                expected_generation=expected_runtime_generation,
                expected_session_id=expected_session_id,
                expected_launch_nonce=expected_launch_nonce,
                expected_transaction_id=expected_transaction_id,
                reason=f"evaluate:{trigger}",
            ):
                self._log_recovery_decision(
                    "evaluate_rejected",
                    acc,
                    trigger,
                    reject="stale_identity",
                    expected_runtime_generation=expected_runtime_generation,
                    expected_session_id=expected_session_id,
                    expected_transaction_id=expected_transaction_id,
                )
                return
            if not self._runtime_state.guard_recovery_generation(
                acc,
                expected_recovery_generation,
                reason=f"evaluate:{trigger}",
            ):
                self._log_recovery_decision(
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
            retry_count = acc.retry_count
            cooldown_until = acc.cooldown_until
            pid = acc.pid
            runtime_generation = acc.runtime_generation
            session_valid = acc.session_valid
            session_checked = acc.session_checked
            has_cookie = bool(acc.cookie)
            session_wait_started_at = acc.session_wait_started_at

        if self._stop.is_set() or desired != AccountState.IN_GAME:
            self._log_hold(acc, trigger, "stopped_or_not_desired")
            return
        if current == AccountState.FAILED:
            self._log_hold(acc, trigger, "already_failed")
            return

        max_fail = int(self._cfg.get("max_fail_count", 5))
        if fail_count >= max_fail:
            self.fail_account(acc, "max_fail", RECOVERY_REASON_MESSAGES["max_fail"])
            return
        retry_exceeded = self._retry_bucket_exceeded(acc)
        if retry_exceeded:
            self.fail_account(acc, "max_retry", retry_exceeded)
            return

        if has_cookie and not session_checked:
            now = time.time()
            with acc._lock:
                if not acc.session_wait_started_at:
                    acc.session_wait_started_at = now
                    session_wait_started_at = now
            wait_age = max(0.0, now - (session_wait_started_at or now))
            self._log_hold(acc, trigger, f"waiting_session_check age={wait_age:.1f}s")
            self._schedule(acc, min(5.0, max(2.0, float(self._cfg.get('network_check_interval', 5) or 5))), "wait_session_check")
            return

        if not session_valid:
            with acc._lock:
                acc.session_retry_count += 1
                acc.retry_count += 1
                if not acc.recovery_inflight:
                    self._runtime_state.bump_recovery_generation(acc, "session_retry", now=time.time())
                self._runtime_state.set_recovery(acc, status="session_retry", reason="session_retry", inflight=True)
                session_retry_count = acc.session_retry_count
            hard_invalid = acc.last_crash_reason == "cookie_invalid"
            if hard_invalid:
                self.fail_account(acc, "cookie_invalid", RECOVERY_REASON_MESSAGES["cookie_invalid"])
                return
            delay = compute_backoff(session_retry_count, base=3, cap=30)
            self._log_hold(acc, trigger, f"session_unverified_retry delay={delay:.1f}s")
            self._schedule_cooldown(acc, delay, "session_retry", "session_retry")
            return

        if not self._net.is_online():
            self.mark_network_lost(acc, trigger)
            return

        if force_restart and pid:
            kill_result = ProcessManager.safe_kill_bound_process(
                acc,
                self._state_mgr,
                reason=f"{trigger}:force_restart",
                expected_runtime_generation=runtime_generation,
            )
            if kill_result.get("reason") == "stale_runtime_generation":
                return
            self._state_mgr.transition(acc, AccountState.READY, reason=f"{trigger}:force_restart", force=True)
            current = AccountState.READY

        if current in (AccountState.IN_GAME, AccountState.LAUNCHING, AccountState.VERIFY, AccountState.QUEUED):
            self._log_hold(acc, trigger, f"active_state={current.name}")
            return

        remaining = cooldown_until - time.time()
        if remaining > 0:
            self._state_mgr.transition(acc, AccountState.COOLDOWN, reason=trigger)
            self._log_hold(acc, trigger, f"cooldown remaining={remaining:.1f}s")
            self._schedule(acc, remaining, f"{trigger}:cooldown")
            return

        if not self._queue_slot_available(acc):
            delay = self._queue_delay_seconds()
            self._log_hold(
                acc,
                trigger,
                f"queue_slot_full active={self._active_slot_count(excluding=acc)} max={self._max_concurrent_accounts()}",
            )
            self._schedule(acc, delay, "queue_slot_wait")
            return

        self._queue_account(acc, trigger)

    def reconcile_all(self, accounts: List[Account], trigger: str = "reconcile_all", force_restart: bool = False):
        for acc in accounts:
            self.evaluate(acc, trigger=trigger, force_restart=force_restart)

    def report_crash(
        self,
        acc: Account,
        reason_key: str,
        reason_msg: str,
        cooldown: Optional[float] = None,
        context: Optional[RecoveryAttemptContext] = None,
    ):
        canonical = canonical_reason(reason_key)
        if context and context.category:
            canonical = reason_for_category(context.category, canonical)
        policy = policy_for(canonical)
        bucket = str(policy.get("bucket") or "crash")
        is_network_recovery = bucket == "network" or canonical in {"connection_error", "network_drop"}
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
            count_crash=not is_network_recovery,
            count_fail=not is_network_recovery,
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
            ProcessManager.evict_pid_cache(pid)
            kill_result = ProcessManager.safe_kill_bound_process(
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
        if active_vip and acc._vip_tracker:
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
            self.fail_account(acc, "relaunch_loop", loop_reason)
            return
        if fail_count >= max_fail:
            self.fail_account(acc, "max_fail", RECOVERY_REASON_MESSAGES["max_fail"])
            return

        if not self._cfg.get("auto_rejoin", True):
            self._clear_recovery(acc, status="disabled", reason=canonical, inflight=False)
            flog(f"[RECOVERY] Auto rejoin disabled - not scheduling recovery for {acc.display_name}", "warning")
            self._persist_runtime()
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
        self._schedule_cooldown(acc, wait_for, canonical, f"recover:{canonical}")
        self._persist_runtime()

    def report_launch_failure(self, acc: Account, reason: str):
        reason_l = str(reason or "").lower()
        if "server full" in reason_l or "experience is full" in reason_l:
            canonical = "server_full"
        elif "cookie" in reason_l or "auth" in reason_l or "login" in reason_l:
            canonical = "auth_failure"
        elif "verify_timeout" in reason_l or "pid not detected" in reason_l:
            canonical = "watchdog_timeout"
        else:
            canonical = "launch_fail"
        policy = policy_for(canonical)
        bucket = str(policy.get("bucket") or "launch")
        ctx = self._begin_recovery(
            acc,
            canonical,
            status="launch_backoff",
            bucket=bucket,
            reason_msg=reason,
            count_crash=False,
            count_fail=True,
        )
        if not ctx:
            self._persist_runtime(force=True)
            return
        with acc._lock:
            launch_fail_count = acc.launch_fail_count
            active_vip = acc.active_vip
            if (
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
            reason_msg=RECOVERY_REASON_MESSAGES["launch_fail"],
        )
        if bool(policy.get("fatal")):
            self.fail_account(acc, canonical, RECOVERY_REASON_MESSAGES.get(canonical, canonical))
            return
        if not self._cfg.get("auto_rejoin", True):
            self._clear_recovery(acc, status="disabled", reason=canonical, inflight=False)
            flog(f"[RECOVERY] Auto rejoin disabled - not scheduling launch retry for {acc.display_name}", "warning")
            self._persist_runtime()
            return
        if active_vip and acc._vip_tracker:
            acc._vip_tracker.mark_crash(active_vip)
        if acc.place_id and active_vip and launch_fail_count >= int(self._cfg.get("launch_public_fallback_threshold", 2) or 2):
            flog(
                f"[RECOVERY] {acc.display_name} switching launch strategy to public fallback "
                f"after {launch_fail_count} launch failures",
                "warning",
            )
        with acc._lock:
            acc.active_vip = ""
        self._schedule_cooldown(acc, delay, canonical, f"{canonical}_backoff")
        self._persist_runtime()

    def report_launch_success(self, acc: Account, trigger: str = "launch_success", count_rejoin: Optional[bool] = None):
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

    def mark_network_lost(self, acc: Account, trigger: str = "network_lost"):
        if acc.desired_state != AccountState.IN_GAME or acc.state == AccountState.FAILED:
            return
        now = time.time()
        with acc._lock:
            should_count = (
                acc.state != AccountState.NETWORK_LOST or
                acc.last_network_lost_at is None or
                (now - acc.last_network_lost_at) >= 20.0
            )
            acc.last_network_lost_at = now
            if should_count:
                acc.network_retry_count += 1
            if not acc.recovery_inflight:
                self._runtime_state.bump_recovery_generation(acc, "network_drop", now=now)
            self._runtime_state.set_recovery(acc, status="network_lost", reason="network_drop", inflight=True)
            network_generation = acc.recovery_generation
        changed = self._state_mgr.transition(acc, AccountState.NETWORK_LOST, reason=trigger, force=True)
        if changed:
            self._bus.emit(EventName.NETWORK_LOST_ACCOUNT, account=acc)
        self._log_recovery_decision(
            "network_lost",
            acc,
            "network_drop",
            trigger=trigger,
            generation=network_generation,
            counted=should_count,
        )
        self._schedule(
            acc,
            min(10.0, max(3.0, float(self._cfg.get("network_check_interval", 5) or 5))),
            "network_poll",
        )
        self._persist_runtime()

    def on_network_restored(self, accounts: List[Account]):
        if not self._cfg.get("auto_rejoin", True):
            flog("[RECOVERY] Auto rejoin disabled - skip reconcile on network restore", "warning")
            return
        for acc in accounts:
            if acc.desired_state != AccountState.IN_GAME or acc.state == AccountState.FAILED:
                continue
            with acc._lock:
                acc.network_retry_count = 0
                if acc.recovery_status == "network_lost":
                    self._runtime_state.set_recovery(acc, status="network_restored", reason="network_restored", inflight=True)
                    acc.sync_runtime("network_restored")
            self._log_recovery_decision("network_restored", acc, "network_restored")
            self.request_evaluate(acc, trigger="network_restored", force_restart=True)

    def force_rejoin(self, acc: Account):
        with acc._lock:
            acc.retry_count = 0
            acc.fail_count = 0
            acc.launch_fail_count = 0
            acc.crash_retry_count = 0
            acc.network_retry_count = 0
            acc.session_retry_count = 0
            acc.session_wait_started_at = 0.0
            acc.last_network_lost_at = None
            acc.pid_missing_since = 0.0
            self._runtime_state.set_cooldown(acc, 0.0, reason="force_rejoin")
            acc.last_crash_reason = ""
            acc.last_rejoin_trigger = "force_rejoin"
            if acc.cookie or not self._cfg.get("use_ram_account_manager", False):
                acc.session_checked = True
                acc.session_valid = True
            pid = acc.pid
            identity = acc.bound_process_identity
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
            kill_result = ProcessManager.safe_kill_bound_process(
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
        self._queue.push(
            acc,
            reason=reason,
            runtime_generation=runtime_generation,
            recovery_generation=recovery_generation,
        )
        self._release_recovery_owner(acc._config_username, runtime_generation, recovery_generation, f"queued:{reason}")
        self._log_recovery_decision("queued", acc, reason, generation=acc.recovery_generation)
        self._bus.emit(EventName.RECOVERY_REQUESTED, account=acc, reason=reason)
        self._persist_runtime()

    def _schedule(self, acc: Account, delay: float, reason: str):
        key = acc._config_username
        due = time.time() + max(0.0, delay)
        with acc._lock:
            if self._closed or self._stop.is_set():
                self._log_recovery_decision("schedule_rejected", acc, reason, reject="coordinator_closed")
                return
            generation = acc.recovery_generation
            runtime_generation = acc.runtime_generation
            acc.recovery_scheduled_at = due
            if acc.recovery_status not in {"manual", "network_lost"}:
                self._runtime_state.set_recovery(acc, status="scheduled", reason="", inflight=True)
            else:
                self._runtime_state.set_recovery(acc, reason="", inflight=True)
        with self._cond:
            if self._closed:
                self._log_recovery_decision("schedule_rejected", acc, reason, reject="coordinator_closed")
                return
            current = self._pending.get(key)
            if current and current[0] <= due and current[3] == generation and current[4] == runtime_generation:
                self._log_recovery_decision(
                    "schedule_suppressed",
                    acc,
                    reason,
                    existing_due=f"{current[0]:.3f}",
                    new_due=f"{due:.3f}",
                    generation=generation,
                    runtime_generation=runtime_generation,
                )
                return
            self._pending[key] = (due, acc, reason, generation, runtime_generation)
            self._cond.notify_all()
        flog_kv(
            "RECOVERY",
            "schedule_timer",
            account=acc.display_name,
            reason=reason,
            delay=f"{max(0.0, delay):.1f}",
            generation=generation,
            runtime_generation=runtime_generation,
        )
        self._persist_runtime()


RecoveryEngine = RecoveryCoordinator
