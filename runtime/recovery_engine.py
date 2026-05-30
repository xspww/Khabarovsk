from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from core import Account, AccountState, EventBus, EventName, SmartQueue, StateManager, flog, flog_kv
from services.network_monitor import NetworkMonitor
from services.process_service import ProcessManager, ProcessService
from services.auth_gate import evaluate_account_auth_gate, mark_account_auth_quarantined
from services.captcha_guard import CAPTCHA_BLOCK_REASON, CAPTCHA_REASON, is_captcha_status_text, set_account_captcha_hold
from runtime.runtime_state_manager import RuntimeStateManager
from runtime.runtime_orchestrator import RuntimeOrchestrator
from runtime.runtime_scheduler import RuntimeScheduledJob, RuntimeScheduler
from runtime.recovery_context import RecoveryAttemptContext, reason_for_category
from runtime.recovery_budget import record_recovery_budget_attempt
from runtime.recovery_evaluator import RecoveryEvaluator
from runtime.recovery_owner import RecoveryOwnerRegistry
from runtime.recovery_queue_slots import active_slot_count, max_concurrent_accounts, queue_delay_seconds, queue_slot_available
from runtime.recovery_storm import RecoveryStormController
from runtime.recovery_network import handle_network_restored
from runtime.recovery_policy import RecoveryDedupeTracker, SessionConflictTracker, adaptive_recovery_delay, build_recovery_log_payload, canonical_reason, kill_local_duplicate_for_session_conflict, policy_for
from runtime.recovery_relaunch import detect_relaunch_loop
from runtime.recovery_retry_limits import retry_bucket_exceeded
from runtime.recovery_scheduling import (
    queue_account as _queue_account,
    run_scheduled_recovery as _run_scheduled_recovery,
    schedule_cooldown as _schedule_cooldown,
    schedule_recovery as _schedule_recovery,
)
from runtime.recovery_signal_router import RecoverySignalRouter
from runtime.recovery_support import RECOVERY_REASON_MESSAGES, compute_backoff
from runtime.lua_liveness_policy import lua_liveness_required, mark_waiting_for_lua


_SPECIFIC_PROCESS_RECOVERY_REASONS = {
    "process_crash",
    "watchdog_timeout",
    "loading_freeze",
    "teleport_timeout",
}


def _canonical_recovery_reason(reason_key: str, context: Optional[RecoveryAttemptContext] = None) -> str:
    canonical = canonical_reason(reason_key)
    if not context or not context.category:
        return canonical
    category = str(context.category or "").strip().upper()
    if category == "PROCESS_CRASH" and canonical in _SPECIFIC_PROCESS_RECOVERY_REASONS:
        return canonical
    return reason_for_category(category, canonical)


def _display_recovery_reason(reason_key: str, canonical: str, reason_msg: str = "", context: Optional[RecoveryAttemptContext] = None) -> str:
    trigger = str(getattr(context, "trigger", "") or "").strip().lower() if context else ""
    detail = " ".join(
        part for part in (
            str(reason_msg or "").strip().lower(),
            str(getattr(context, "detail", "") or "").strip().lower() if context else "",
        )
        if part
    )
    raw = str(reason_key or "").strip().lower()
    if raw == "lua_wait_timeout" or trigger == "lua_wait_timeout" or "waiting for lua" in detail or "lua did not confirm" in detail:
        return "lua_wait_timeout"
    return canonical


class RecoveryCoordinator:
    """Central recovery/rejoin controller. Every path that wants recovery reports here."""

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
        timeline=None,
        runtime_state: Optional[RuntimeStateManager] = None,
        runtime_orchestrator: Optional[RuntimeOrchestrator] = None,
        scheduler: Optional[RuntimeScheduler] = None,
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
        self._lock = threading.Lock()
        self._closed = False
        self._owner_registry = RecoveryOwnerRegistry()
        self._runtime_state = runtime_state or RuntimeStateManager(logger=flog_kv)
        self._runtime_orchestrator = runtime_orchestrator or RuntimeOrchestrator(
            self._runtime_state,
            timeline=timeline,
            logger=flog_kv,
        )
        self._runtime_orchestrator.bind_recovery(self)
        self._account_runtime = self._runtime_orchestrator.account_runtime
        self._duplicate_window = max(1.0, float(cfg.get("recovery_duplicate_window", 8) or 8))
        self._recovery_dedupe = RecoveryDedupeTracker(float(cfg.get("recovery_dedupe_window_seconds", 3) or 3))
        self._signal_router = RecoverySignalRouter(
            self._runtime_state,
            is_closed=self._is_closed,
            log_decision=self._log_recovery_decision,
            active_recovery_blocks=self._active_recovery_blocks,
            dedupe_recovery_context=self._dedupe_recovery_context,
            duplicate_window=self._duplicate_window,
        )
        self._session_conflicts = SessionConflictTracker()
        self._owns_scheduler = scheduler is None
        self._scheduler = scheduler or RuntimeScheduler(
            stop=stop,
            state_manager=self._runtime_state,
            timeline=timeline,
            logger=flog_kv,
            name="RecoveryScheduler",
        )
        self._evaluator = RecoveryEvaluator(self)
        self._storm = RecoveryStormController(cfg, self._accounts)
        self._guard_recovery_lock = threading.Lock()
        self._guard_recovery_scheduled = False
        self._guard_recovery_attempt = 0
        self._guard_recovery_last_at = 0.0

    def update_config(self, cfg: dict, accounts: Optional[List[Account]] = None) -> None:
        with self._lock:
            self._cfg = cfg
            if accounts is not None:
                self._accounts = accounts
        self._storm.update_config(cfg)
        if accounts is not None:
            self._storm.set_accounts(accounts)

    @property
    def runtime_orchestrator(self) -> RuntimeOrchestrator:
        return self._runtime_orchestrator
    def _is_closed(self) -> bool:
        with self._lock:
            return self._closed or self._stop.is_set()
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
        with self._lock:
            self._closed = True
        active = self._owner_registry.clear()
        self._signal_router.clear()
        pending = sum(self._scheduler.cancel(f"recovery:{acc._config_username}", reason="recovery_stop") for acc in self._accounts)
        if self._owns_scheduler:
            pending += self._scheduler.cancel_all(reason="recovery_stop")
            self._scheduler.stop()
        try:
            self._queue.cancel_all("recovery_stop")
        except Exception as exc:
            flog_kv("RECOVERY", "queue_cancel_failed", "warning", error=exc)
        flog_kv("RECOVERY", "coordinator_stopped", pending_cancelled=pending, active_cancelled=active)

    def _dedupe_recovery_context(self, ctx: RecoveryAttemptContext, acc: Account, reason_key: str) -> bool:
        result = self._recovery_dedupe.check_and_mark(ctx)
        if not result.get("ignore"):
            return False
        ctx_fields = ctx.to_dict()
        result_fields = {key: value for key, value in result.items() if key not in ctx_fields and key != "reason"}
        self._log_recovery_decision("recovery_ignored", acc, reason_key, **result_fields, **ctx_fields)
        return True

    def _active_recovery_blocks(self, acc: Account, ctx: RecoveryAttemptContext, reason_key: str) -> bool:
        result = self._owner_registry.block_reason(acc._config_username, ctx)
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
        return max_concurrent_accounts(self._cfg)

    def _queue_delay_seconds(self) -> float:
        return queue_delay_seconds(self._cfg)

    def _active_slot_count(self, excluding: Optional[Account] = None) -> int:
        return active_slot_count(self._accounts, excluding=excluding)

    def _queue_slot_available(self, acc: Account) -> bool:
        return queue_slot_available(self._accounts, self._cfg, acc)

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self._cfg.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
        return bool(value)

    def _cfg_float(self, key: str, default: float, minimum: float = 0.0, maximum: float = 3600.0) -> float:
        try:
            value = float(self._cfg.get(key, default) or default)
        except Exception:
            value = default
        return max(minimum, min(maximum, value))

    def _mark_guard_recovering(self, acc: Account, reason_msg: str, delay: float) -> None:
        with acc._lock:
            acc.manual_status = "Multi Roblox guard recovering"
            acc.last_error = str(reason_msg or "")
            acc.last_crash_reason = "multi_roblox_guard_failed"
            self._runtime_state.set_cooldown(acc, time.time() + max(0.0, delay), reason="multi_roblox_guard_self_heal")
            self._runtime_state.set_recovery(
                acc,
                status="guard_recovering",
                reason="multi_roblox_guard_failed",
                inflight=True,
            )
        self._state_mgr.transition(acc, AccountState.COOLDOWN, reason="multi_roblox_guard_self_heal", force=True)

    def _schedule_multi_roblox_guard_recovery(self, acc: Account, reason_msg: str = "", retry_delay: Optional[float] = None) -> bool:
        if not self._cfg_bool("multi_roblox_guard_self_heal_enabled", True):
            return False
        if self._closed or self._stop.is_set():
            return False
        now = time.time()
        first_delay = self._cfg_float("multi_roblox_guard_self_heal_delay_seconds", 5.0, 0.0, 300.0)
        delay = first_delay if retry_delay is None else max(0.0, float(retry_delay or 0.0))
        joined_existing = False
        with self._guard_recovery_lock:
            if self._guard_recovery_scheduled:
                joined_existing = True
            else:
                self._guard_recovery_scheduled = True
                if retry_delay is None:
                    self._guard_recovery_attempt = 1
                else:
                    self._guard_recovery_attempt += 1
                self._guard_recovery_last_at = now
                attempt = self._guard_recovery_attempt
        if joined_existing:
            self._mark_guard_recovering(acc, reason_msg, delay)
            self._log_recovery_decision(
                "multi_roblox_guard_self_heal_joined",
                acc,
                "multi_roblox_guard_failed",
                delay=f"{delay:.1f}",
            )
            return True
        self._mark_guard_recovering(acc, reason_msg, delay)
        self._scheduler.schedule_once(
            "recovery:multi_roblox_guard_self_heal",
            self._run_multi_roblox_guard_self_heal,
            delay=delay,
            reason="multi_roblox_guard_failed",
            payload={
                "trigger_account": acc._config_username,
                "reason_msg": reason_msg,
                "attempt": attempt,
            },
        )
        self._log_recovery_decision(
            "multi_roblox_guard_self_heal_scheduled",
            acc,
            "multi_roblox_guard_failed",
            delay=f"{delay:.1f}",
            attempt=attempt,
            reason_msg=reason_msg,
        )
        self._persist_runtime(force=True)
        return True

    def _guard_recovery_retry_delay(self) -> float:
        base = self._cfg_float("multi_roblox_guard_self_heal_retry_seconds", 30.0, 5.0, 3600.0)
        maximum = self._cfg_float("multi_roblox_guard_self_heal_max_retry_seconds", 300.0, 5.0, 3600.0)
        with self._guard_recovery_lock:
            attempt = max(1, int(self._guard_recovery_attempt or 1))
        return min(maximum, base * attempt)

    def _run_multi_roblox_guard_self_heal(self, job: RuntimeScheduledJob) -> None:
        payload = dict(job.payload or {})
        trigger_account = str(payload.get("trigger_account") or "")
        trigger = next((item for item in self._accounts if item._config_username == trigger_account), None)
        try:
            killed = 0
            if self._cfg_bool("multi_roblox_guard_self_heal_close_all", True):
                killed = ProcessService.kill_all_roblox_clients(
                    wait_seconds=4.0,
                    reason="multi_roblox_guard_self_heal",
                )
            from roblox_hybrid import ensure_multi_roblox_guard, release_multi_roblox_guard

            release_multi_roblox_guard()
            time.sleep(1.0)
            ok, detail = ensure_multi_roblox_guard()
            if not ok:
                raise RuntimeError(detail)
            requeued = self._requeue_after_multi_roblox_guard_recovery()
            with self._guard_recovery_lock:
                self._guard_recovery_scheduled = False
                self._guard_recovery_attempt = 0
                self._guard_recovery_last_at = time.time()
            flog_kv(
                "MULTI_ROBLOX",
                "guard_self_heal_completed",
                account=trigger.display_name if trigger else trigger_account,
                killed=killed,
                requeued=requeued,
                detail=detail,
            )
            self._persist_runtime(force=True)
        except Exception as exc:
            retry_delay = self._guard_recovery_retry_delay()
            with self._guard_recovery_lock:
                self._guard_recovery_scheduled = False
            if trigger is not None:
                self._log_recovery_decision(
                    "multi_roblox_guard_self_heal_retry",
                    trigger,
                    "multi_roblox_guard_failed",
                    error=str(exc),
                    delay=f"{retry_delay:.1f}",
                )
                self._schedule_multi_roblox_guard_recovery(trigger, str(exc), retry_delay=retry_delay)
            else:
                flog_kv(
                    "MULTI_ROBLOX",
                    "guard_self_heal_retry_missing_account",
                    "warning",
                    account=trigger_account,
                    error=str(exc),
                    delay=f"{retry_delay:.1f}",
                )

    def _requeue_after_multi_roblox_guard_recovery(self) -> int:
        requeued = 0
        for acc in list(self._accounts):
            with acc._lock:
                desired = acc.desired_state
            if desired != AccountState.IN_GAME:
                continue
            auth_gate = evaluate_account_auth_gate(acc)
            if auth_gate.blocked:
                continue
            with acc._lock:
                if acc.last_crash_reason == "multi_roblox_guard_failed":
                    acc.last_crash_reason = ""
                if "guard" in str(acc.manual_status or "").lower():
                    acc.manual_status = ""
                if "guard" in str(acc.last_error or "").lower():
                    acc.last_error = ""
                acc.fail_count = 0
                acc.retry_count = 0
                acc.launch_fail_count = 0
                acc.recovery_budget_attempts.clear()
                self._runtime_state.clear_process_binding(
                    acc,
                    reason="multi_roblox_guard_self_heal_requeue",
                    increment_generation=True,
                )
                self._runtime_state.set_cooldown(acc, 0.0, reason="multi_roblox_guard_self_heal_requeue")
                self._runtime_state.set_recovery(
                    acc,
                    status="queued",
                    reason="multi_roblox_guard_self_heal_requeue",
                    inflight=True,
                )
            self._state_mgr.transition(acc, AccountState.READY, reason="multi_roblox_guard_self_heal_requeue", force=True)
            self._queue_account(acc, "multi_roblox_guard_self_heal_requeue")
            requeued += 1
        return requeued

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
        return self._signal_router.route(
            self,
            acc,
            signal,
            reason,
            payload=payload,
            expected_runtime_generation=expected_runtime_generation,
            expected_session_id=expected_session_id,
            expected_launch_nonce=expected_launch_nonce,
            expected_transaction_id=expected_transaction_id,
        )

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
        budget_reason = ""
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
                    **{key: value for key, value in owner_check.items() if key != "accepted"},
                )
                return None
            if owner_check.get("replaced"):
                self._log_recovery_decision(
                    "recovery_owner_replaced",
                    acc,
                    canonical,
                    **{key: value for key, value in owner_check.items() if key not in {"accepted", "replaced"}},
                )

            budget_reason = record_recovery_budget_attempt(self._cfg, acc, canonical, bucket, now)
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
            self._log_recovery_decision("recovery_budget_exceeded", acc, canonical, reason_msg=budget_reason, bucket=bucket)
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
        runtime_generation: Optional[int],
        recovery_generation: Optional[int],
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
        return _schedule_cooldown(self, acc, delay, reason, transition_reason, display_reason=display_reason)

    def _detect_relaunch_loop(self, acc: Account, reason_key: str) -> Optional[str]:
        return detect_relaunch_loop(acc, reason_key, self._cfg, self._net, flog)

    def set_desired(self, acc: Account, desired: AccountState):
        with acc._lock:
            self._runtime_state.set_desired(acc, desired, reason="recovery_set_desired")

    def _log_hold(self, acc: Account, trigger: str, reason: str):
        flog(f"[RECOVERY] hold {acc.display_name}: trigger={trigger} reason={reason}")

    def request_evaluate(self, acc: Account, trigger: str, force_restart: bool = False) -> bool:
        return self._runtime_orchestrator.request_evaluate(acc, trigger=trigger, force_restart=force_restart)

    def request_rejoin(self, acc: Account, reason: str = "force_rejoin") -> bool:
        return self._runtime_orchestrator.request_rejoin(acc, reason=reason, bump_runtime_generation=True)

    def _retry_bucket_exceeded(self, acc: Account) -> Optional[str]:
        return retry_bucket_exceeded(self._cfg, acc)

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
        return self._evaluator.evaluate(
            acc,
            trigger=trigger,
            force_restart=force_restart,
            expected_runtime_generation=expected_runtime_generation,
            expected_recovery_generation=expected_recovery_generation,
            expected_session_id=expected_session_id,
            expected_launch_nonce=expected_launch_nonce,
            expected_transaction_id=expected_transaction_id,
        )

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
        canonical = _canonical_recovery_reason(reason_key, context)
        display_reason = _display_recovery_reason(reason_key, canonical, reason_msg, context)
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
        self._schedule_cooldown(acc, wait_for, canonical, f"recover:{canonical}", display_reason=display_reason)
        self._persist_runtime()

    def report_launch_failure(self, acc: Account, reason: str):
        reason_l = str(reason or "").lower()
        if is_captcha_status_text(reason_l):
            set_account_captcha_hold(acc, reason, source="launch_failure", runtime_writer=self._runtime_state)
            self.fail_account(acc, CAPTCHA_REASON, CAPTCHA_BLOCK_REASON)
            return
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

    def report_lua_waiting(self, acc: Account, trigger: str = "lua_required"):
        with acc._lock:
            runtime_generation = int(acc.runtime_generation or 0)
            recovery_generation = int(acc.recovery_generation or 0)
        mark_waiting_for_lua(acc, self._runtime_state, self._state_mgr, trigger)
        self._release_recovery_owner(acc._config_username, runtime_generation, recovery_generation, "waiting_for_lua")
        self._persist_runtime()

    def report_launch_success(self, acc: Account, trigger: str = "launch_success", count_rejoin: Optional[bool] = None, lua_confirmed: bool = False):
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
        handle_network_restored(self, accounts)

    def force_rejoin(self, acc: Account):
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

    def _queue_account(self, acc: Account, reason: str):
        return _queue_account(self, acc, reason)

    def _schedule(self, acc: Account, delay: float, reason: str):
        return _schedule_recovery(self, acc, delay, reason)

    def _run_scheduled_recovery(self, job: RuntimeScheduledJob) -> None:
        return _run_scheduled_recovery(self, job)
RecoveryEngine = RecoveryCoordinator
