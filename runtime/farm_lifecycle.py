from __future__ import annotations

import threading
import time
from typing import Any

from core import AccountState, GlobalLaunchLimiter, SmartQueue, StateManager, flog, flog_kv
from services.network_monitor import NetworkMonitor
from services.process_service import ProcessManager, ProcessService
from services.resource_monitor import get_rt_monitor
from services.vip_tracker import VipTracker
from runtime.account_worker import AccountWorker
from runtime.dispatcher import Dispatcher
from runtime.launch_controller import LaunchController
from runtime.recovery_engine import RecoveryEngine
from runtime.runtime_scheduler import RuntimeScheduler
from runtime.system_maintenance import SystemMaintenance


def _clear_manual_start_failure_gate(acc: Any, runtime_state: Any, max_fail_count: int) -> bool:
    max_fail = max(1, int(max_fail_count or 5))
    reason = str(getattr(acc, "last_crash_reason", "") or "")
    failed_status = str(getattr(acc, "recovery_status", "") or "") == "failed"
    over_fail_limit = int(getattr(acc, "fail_count", 0) or 0) >= max_fail
    if reason not in {"max_fail", "max_retry"} and not (failed_status and over_fail_limit):
        return False

    acc.retry_count = 0
    acc.fail_count = 0
    acc.launch_fail_count = 0
    acc.crash_retry_count = 0
    acc.network_retry_count = 0
    acc.session_retry_count = 0
    acc.session_wait_started_at = 0.0
    acc.pid_missing_since = 0.0
    acc.last_network_lost_at = None
    acc.last_crash_reason = ""
    acc.last_recovery_reason = ""
    acc.recovery_status = ""
    acc.recovery_inflight = False
    acc.recovery_scheduled_at = 0.0
    acc.last_rejoin_trigger = ""
    runtime_state.set_cooldown(acc, 0.0, reason="manual_start_reset_failure_gate")
    acc.sync_runtime("manual_start_reset_failure_gate")
    flog_kv(
        "RUNTIME",
        "manual_start_failure_gate_reset",
        account=acc.display_name,
        max_fail_count=max_fail,
    )
    return True


class FarmLifecycleService:
    def __init__(self, farm: Any):
        self._farm = farm

    def start(self) -> None:
        farm = self._farm
        if farm.running:
            return

        cfg = farm.cfg_mgr.snapshot()
        max_fail_count = int(cfg.get("max_fail_count", 5) or 5)
        farm._shutting_down = False
        farm._stop = threading.Event()
        if bool(cfg.get("multi_roblox_enabled", True)):
            from roblox_hybrid import ensure_multi_roblox_guard, multi_roblox_guard_status

            guard_ok, guard_detail = ensure_multi_roblox_guard()
            guard_status = multi_roblox_guard_status()
            if not guard_ok:
                flog_kv("MULTI_ROBLOX", "guard_start_blocked", "error", detail=guard_detail)
                raise RuntimeError(f"Multi Roblox guard failed: {guard_detail}")
            flog_kv(
                "MULTI_ROBLOX",
                "guard_ready_before_farm_start",
                pid=guard_status.get("pid", 0),
                detail=guard_detail,
            )
        else:
            from roblox_hybrid import release_multi_roblox_guard

            release_multi_roblox_guard()
        farm.running = True
        farm.start_ts = time.time()
        farm._bump_status_revision()
        get_rt_monitor().start()

        for acc in farm._accounts:
            with acc._lock:
                farm._runtime_state.set_desired(
                    acc,
                    AccountState.IN_GAME,
                    reason="farm_start_desired",
                    increment_generation=False,
                )
                farm._runtime_state.set_cooldown(acc, 0.0, reason="farm_start_clear_cooldown")
            if acc.vip_links:
                acc._vip_tracker = VipTracker(acc.vip_links)
                flog(f"[FARM] VipTracker initialized for {acc.display_name}")

        farm._sync_accounts_from_ram(persist=True)
        farm.cfg_mgr.restore_runtime(farm._accounts)

        reset_failure_gate = False
        for acc in farm._accounts:
            restored_pid = acc.pid
            restored_identity = acc.bound_process_identity
            if restored_pid and (
                not restored_identity or
                not ProcessManager.is_bound_game_alive(
                    restored_pid,
                    owner_key=acc._config_username,
                    expected_identity=restored_identity,
                )
            ):
                ProcessService.evict_pid_cache(restored_pid, reason="restored_pid_rejected", account=acc)
                with acc._lock:
                    if acc.pid == restored_pid:
                        farm._runtime_state.clear_process_binding(
                            acc,
                            reason="restored_pid_rejected",
                            increment_generation=True,
                        )
                flog_kv(
                    "FARM",
                    "restored_pid_rejected",
                    "warning",
                    account=acc.display_name,
                    pid=restored_pid,
                    reason="identity_or_owner_not_verified",
                )
            with acc._lock:
                farm._runtime_orchestrator.request_start_epoch(acc)
                acc.session_wait_started_at = 0.0
                acc.rapid_relaunch_count = 0
                acc.presence_rejoin_pending_clear = False
                acc.presence_rejoin_suppressed_until = 0.0
                acc.last_presence_rejoin_at = 0.0
                acc.presence_mismatch_since = 0.0
                acc.presence_mismatch_status = ""
                acc.presence_mismatch_reason = ""
                if acc.cooldown_until and acc.cooldown_until <= time.time():
                    farm._runtime_state.set_cooldown(acc, 0.0, reason="expired_restored_cooldown")
                reset_failure_gate = (
                    _clear_manual_start_failure_gate(acc, farm._runtime_state, max_fail_count)
                    or reset_failure_gate
                )
        if reset_failure_gate:
            farm.cfg_mgr.save_runtime(farm._accounts)

        state_mgr = StateManager(farm.bus)
        farm._state_mgr = state_mgr
        farm._net_mon = NetworkMonitor(
            bus=farm.bus,
            interval=cfg.get("network_check_interval", 5),
            debounce=cfg.get("network_debounce", 3),
            stop=farm._stop,
        )
        farm._net_mon.start()
        time.sleep(0.5)
        farm._initial_state_sync(state_mgr)

        queue = SmartQueue()
        farm._queue = queue
        farm._runtime_scheduler = RuntimeScheduler(
            stop=farm._stop,
            state_manager=farm._runtime_state,
            timeline=farm._timeline,
            logger=flog_kv,
            name="RuntimeScheduler",
        )
        limiter = GlobalLaunchLimiter(
            interval=max(
                float(cfg.get("launch_rate_interval", 6) or 6),
                float(cfg.get("account_switch_cooldown", 10) or 10),
            )
        )
        launcher = LaunchController(
            limiter,
            state_mgr,
            farm.bus,
            cfg,
            accounts=farm._accounts,
            runtime_state=farm._runtime_state,
            runtime_store=farm._runtime_store,
            supervisor=farm._supervisor,
        )
        farm._recovery = RecoveryEngine(
            queue,
            state_mgr,
            farm.bus,
            farm._net_mon,
            farm._stop,
            cfg,
            accounts=farm._accounts,
            persist_callback=lambda: farm.cfg_mgr.save_runtime(farm._accounts),
            timeline=farm._timeline,
            runtime_state=farm._runtime_state,
            runtime_orchestrator=farm._runtime_orchestrator,
            scheduler=farm._runtime_scheduler,
        )
        blocked_accounts = farm._preflight_cookie_blocks()

        farm._workers = {}
        for acc in farm._accounts:
            worker = AccountWorker(
                acc=acc,
                state_mgr=state_mgr,
                bus=farm.bus,
                cfg=cfg,
                recovery=farm._recovery,
                stop=farm._stop,
                supervisor=farm._supervisor,
                accounts=farm._accounts,
            )
            farm._workers[acc._config_username] = worker

        farm._dispatcher = Dispatcher(
            queue=queue,
            launcher=launcher,
            state_mgr=state_mgr,
            bus=farm.bus,
            workers=farm._workers,
            recovery=farm._recovery,
            net=farm._net_mon,
            stop=farm._stop,
            cfg=cfg,
            runtime_state=farm._runtime_state,
            runtime_store=farm._runtime_store,
            supervisor=farm._supervisor,
        )
        farm._dispatcher.start()
        farm._maintenance = SystemMaintenance(
            farm._accounts,
            farm._workers,
            farm._recovery,
            state_mgr,
            cfg,
            farm._stop,
            supervisor=farm._supervisor,
            scheduler=farm._runtime_scheduler,
        )
        farm._maintenance.start()

        for worker in farm._workers.values():
            if worker.acc._config_username in blocked_accounts:
                continue
            worker.start()
            time.sleep(0.1)

        farm._runtime_orchestrator.request_reconcile_all(farm._accounts, trigger="farm_start")
        launchable_count = len(farm._accounts) - len(blocked_accounts)
        flog(f"[FARM] Started {len(farm._accounts)} accounts (launchable={launchable_count} blocked={len(blocked_accounts)})")
        message = f"Farm started - {launchable_count}/{len(farm._accounts)} launchable"
        if blocked_accounts:
            message += f", {len(blocked_accounts)} blocked"
        farm._push_event("system", message, severity="success" if launchable_count else "warning")

    def stop(self) -> None:
        farm = self._farm
        if not farm.running:
            return

        farm._shutting_down = True
        farm._stop.set()
        farm.running = False
        farm._bump_status_revision()
        if farm._recovery:
            farm._recovery.stop()
        if farm._queue:
            farm._queue.cancel_all("farm_stop")
        farm._cancel_commands_for_shutdown()

        for acc in farm._accounts:
            if acc.pid:
                ProcessService.safe_kill_bound_process(
                    acc,
                    None,
                    reason="farm_stop",
                )
            with acc._lock:
                farm._runtime_state.forced_reset(acc, desired=AccountState.IDLE, reason="farm_stop_reset")
                flog_kv(
                    "STATE",
                    "forced_reset",
                    account=acc.display_name,
                    state=acc.state.name,
                    reason="farm_stop",
                    runtime_generation=acc.runtime_generation,
                    recovery_generation=acc.recovery_generation,
                    command_generation=acc.command_generation,
                )

        for worker in farm._workers.values():
            worker.wake()
            worker.join(timeout=2.0)

        if farm._dispatcher:
            farm._dispatcher.join(timeout=2.0)
        if farm._maintenance:
            farm._maintenance.join(timeout=2.0)
        if farm._net_mon:
            farm._net_mon.join(timeout=2.0)
        if farm._runtime_scheduler:
            farm._runtime_scheduler.stop()
            farm._runtime_scheduler = None
        farm._state_mgr = None

        farm.cfg_mgr.save_runtime(farm._accounts)
        get_rt_monitor().stop()
        try:
            from roblox_hybrid import release_multi_roblox_guard

            release_multi_roblox_guard()
        except Exception as exc:
            flog_kv("MULTI_ROBLOX", "guard_stop_failed", "warning", error=exc)
        flog("[FARM] Stopped")
        farm._push_event("system", "Farm stopped", severity="info")
        farm._shutting_down = False
