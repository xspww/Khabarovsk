"""Multi-roblox guard self-heal, network loss, and hold logging."""
from __future__ import annotations

import time

from core import Account, AccountState, EventName, flog, flog_kv
from runtime.runtime_scheduler import RuntimeScheduledJob
from services.auth_gate import evaluate_account_auth_gate
from services.process_service import ProcessService


class RecoveryGuardNetMixin:
    """Guard self-heal and network hold. Owns mark_network_lost and
    on_network_restored, which the router also invokes through the seam."""

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


    def _schedule_multi_roblox_guard_recovery(self, acc: Account, reason_msg: str = "", retry_delay: float | None = None) -> bool:
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
            from services.roblox_multibox import ensure_multi_roblox_guard, release_multi_roblox_guard

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


    def _clear_guard_account_locked(self, acc: Account) -> None:
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

    def _reset_guard_binding_locked(self, acc: Account) -> None:
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
                self._clear_guard_account_locked(acc)
                self._reset_guard_binding_locked(acc)
            self._state_mgr.transition(
                acc,
                AccountState.READY,
                reason="multi_roblox_guard_self_heal_requeue",
                force=True,
            )
            self._queue_account(acc, "multi_roblox_guard_self_heal_requeue")
            requeued += 1
        return requeued


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


    @staticmethod
    def _restore_expedite(acc: Account) -> bool:
        return (
            acc.state in {AccountState.NETWORK_LOST, AccountState.COOLDOWN, AccountState.CRASH}
            or acc.recovery_status in {"network_lost", "cooldown", "scheduled"}
            or bool(acc.cooldown_until)
        )

    def _reset_restored_account_locked(self, acc: Account) -> bool:
        acc.network_retry_count = 0
        expedite = self._restore_expedite(acc)
        if expedite:
            self._runtime_state.set_cooldown(acc, 0.0, reason="network_restored")
            acc.recovery_scheduled_at = 0.0
            acc.scheduler_slot = ""
        if acc.recovery_status == "network_lost" or expedite:
            self._runtime_state.set_recovery(
                acc,
                status="network_restored",
                reason="network_restored",
                inflight=True,
            )
            acc.sync_runtime("network_restored")
        return expedite

    def on_network_restored(self, accounts: list[Account]):
        if not self._cfg.get("auto_rejoin", True):
            flog("[RECOVERY] Auto rejoin disabled - skip reconcile on network restore", "warning")
            return
        for acc in accounts:
            if acc.desired_state != AccountState.IN_GAME or acc.state == AccountState.FAILED:
                continue
            with acc._lock:
                expedite = self._reset_restored_account_locked(acc)
            if expedite:
                self._scheduler.cancel(f"recovery:{acc._config_username}", reason="network_restored")
            self._log_recovery_decision("network_restored", acc, "network_restored", expedited=expedite)
            self.request_evaluate(acc, trigger="network_restored", force_restart=True)
