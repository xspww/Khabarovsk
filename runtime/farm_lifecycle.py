from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, NamedTuple

from core import AccountState, GlobalLaunchLimiter, SmartQueue, StateManager, flog, flog_kv
from runtime.account_selection import is_runtime_account_selected, runtime_account_filter_blocked_keys
from runtime.account_worker import AccountWorker
from runtime.dispatcher import Dispatcher
from runtime.launch_controller import LaunchController
from runtime.recovery_engine import RecoveryEngine
from runtime.runtime_scheduler import RuntimeScheduler
from runtime.system_maintenance import SystemMaintenance
from services.network_monitor import NetworkMonitor
from services.process_service import ProcessManager, ProcessService
from services.resource_monitor import get_rt_monitor
from services.vip_tracker import VipTracker


def _clear_manual_start_failure_gate(acc: Any, runtime_state: Any, max_fail_count: int) -> bool:
    max_fail = max(1, int(max_fail_count or 5))
    reset = bool(runtime_state.clear_manual_start_failure_gate(acc, max_fail_count=max_fail))
    if not reset:
        return False
    flog_kv(
        "RUNTIME",
        "manual_start_failure_gate_reset",
        account=acc.display_name,
        max_fail_count=max_fail,
    )
    return True


def _prepare_multi_roblox_guard(cfg: dict[str, Any]) -> None:
    if bool(cfg.get("multi_roblox_enabled", True)):
        from services.roblox_multibox import (
            ensure_multi_roblox_guard,
            multi_roblox_guard_status,
        )

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
        return
    from services.roblox_multibox import release_multi_roblox_guard

    release_multi_roblox_guard()


def _best_effort(action: Callable[[], Any]) -> None:
    try:
        action()
    except Exception:
        "intentional: ignore exception"


def _call_if_present(component: Any, method: str, *args: Any) -> None:
    if component:
        getattr(component, method)(*args)


def _best_effort_dotted(target: Any, path: str, *args: Any) -> None:
    try:
        for name in path.split("."):
            target = getattr(target, name)
        target(*args)
    except Exception:
        "intentional: ignore exception"


def _kill_bound_processes_fast(accounts: Any) -> None:
    for acc in list(accounts or []):
        try:
            if getattr(acc, "pid", 0):
                ProcessService.safe_kill_bound_process(
                    acc,
                    None,
                    reason="farm_stop_fast",
                )
        except Exception:
            "intentional: ignore exception"


def _release_guard_import() -> None:
    from services.roblox_multibox import release_multi_roblox_guard

    release_multi_roblox_guard()


def _release_guard_logged() -> None:
    try:
        _release_guard_import()
    except Exception as exc:
        flog_kv("MULTI_ROBLOX", "guard_stop_failed", "warning", error=exc)


def _apply_start_desired_states(farm: Any, cfg: dict[str, Any]) -> bool:
    runtime_filter_active = bool(cfg.get("runtime_account_allowlist"))
    for acc in farm._accounts:
        with acc._lock:
            if is_runtime_account_selected(acc, cfg):
                farm._runtime_state.set_desired(
                    acc,
                    AccountState.IN_GAME,
                    reason="farm_start_desired",
                    increment_generation=False,
                )
                farm._runtime_state.set_cooldown(
                    acc,
                    0.0,
                    reason="farm_start_clear_cooldown",
                )
            else:
                farm._runtime_state.forced_reset(
                    acc,
                    desired=AccountState.IDLE,
                    reason="runtime_account_allowlist_excluded",
                )
        if acc.vip_links:
            acc._vip_tracker = VipTracker(acc.vip_links)
            flog(f"[FARM] VipTracker initialized for {acc.display_name}")
    return runtime_filter_active


def _clear_excluded_account(farm: Any, acc: Any) -> None:
    with acc._lock:
        farm._runtime_state.forced_reset(
            acc,
            desired=AccountState.IDLE,
            reason="runtime_account_allowlist_excluded",
        )


def _reject_restored_pid(
    farm: Any,
    acc: Any,
    restored_pid: Any,
    restored_identity: str,
) -> None:
    ProcessService.evict_pid_cache(
        restored_pid,
        reason="restored_pid_rejected",
        account=acc,
    )
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


def _reset_account_start_state(
    farm: Any,
    acc: Any,
    max_fail_count: int,
) -> bool:
    with acc._lock:
        farm._runtime_orchestrator.request_start_epoch(acc)
        acc.session_wait_started_at = 0.0
        acc.rapid_relaunch_count = 0
        if acc.cooldown_until and acc.cooldown_until <= time.time():
            farm._runtime_state.set_cooldown(
                acc,
                0.0,
                reason="expired_restored_cooldown",
            )
        return _clear_manual_start_failure_gate(
            acc,
            farm._runtime_state,
            max_fail_count,
        )


def _restore_runtime_state(
    farm: Any,
    cfg: dict[str, Any],
    max_fail_count: int,
) -> None:
    farm.cfg_mgr.restore_runtime(farm._accounts)
    reset_failure_gate = False
    for acc in farm._accounts:
        if not is_runtime_account_selected(acc, cfg):
            _clear_excluded_account(farm, acc)
            continue
        restored_pid = acc.pid
        restored_identity = acc.bound_process_identity
        if restored_pid and (
            not restored_identity
            or not ProcessManager.is_bound_game_alive(
                restored_pid,
                owner_key=acc._config_username,
                expected_identity=restored_identity,
            )
        ):
            _reject_restored_pid(farm, acc, restored_pid, restored_identity)
        reset_failure_gate = (
            _reset_account_start_state(farm, acc, max_fail_count)
            or reset_failure_gate
        )
    if reset_failure_gate:
        farm.cfg_mgr.save_runtime(farm._accounts)


class _RuntimeComponents(NamedTuple):
    queue: Any
    launcher: Any


def _start_recovery_engine(
    farm: Any,
    queue: Any,
    state_mgr: Any,
    cfg: dict[str, Any],
) -> None:
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
    try:
        setter = getattr(farm._recovery, "set_executor_hold_check", None)
        if callable(setter):
            setter(farm.is_executor_hold)
    except Exception:
        "intentional: ignore exception"


def _start_runtime_components(
    farm: Any,
    cfg: dict[str, Any],
    state_mgr: Any,
) -> _RuntimeComponents:
    queue = SmartQueue()
    farm._queue = queue
    farm._runtime_scheduler = RuntimeScheduler(
        stop=farm._stop,
        state_manager=farm._runtime_state,
        timeline=farm._timeline,
        logger=flog_kv,
        name="RuntimeScheduler",
        dispatch_worker_count=int(
            cfg.get("runtime_scheduler_dispatch_workers", 0) or 0
        ),
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
    _start_recovery_engine(farm, queue, state_mgr, cfg)
    return _RuntimeComponents(queue, launcher)


def _blocked_accounts(
    farm: Any,
    cfg: dict[str, Any],
    runtime_filter_active: bool,
) -> set[str]:
    blocked_accounts = set(farm._preflight_cookie_blocks())
    blocked_accounts.update(
        runtime_account_filter_blocked_keys(farm._accounts, cfg)
    )
    if runtime_filter_active:
        selected = [
            acc.display_name
            for acc in farm._accounts
            if is_runtime_account_selected(acc, cfg)
        ]
        flog_kv(
            "FARM",
            "runtime_account_allowlist_applied",
            selected=",".join(selected),
            blocked=len(blocked_accounts),
        )
    return blocked_accounts


def _start_workers(farm: Any, state_mgr: Any, cfg: dict[str, Any]) -> None:
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


def _start_dispatcher_and_maintenance(
    farm: Any,
    queue: Any,
    launcher: Any,
    state_mgr: Any,
    cfg: dict[str, Any],
) -> None:
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
        accounts=farm._accounts,
        machine_supervisor=farm._machine_supervisor,
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
        record_runtime_event=farm._record_timeline,
    )
    farm._maintenance.start()


def _start_worker_threads(farm: Any, blocked_accounts: set[str]) -> None:
    for worker in farm._workers.values():
        if worker.acc._config_username in blocked_accounts:
            continue
        worker.start()
        time.sleep(0.1)


def _announce_started(farm: Any, blocked_accounts: set[str]) -> None:
    farm._runtime_orchestrator.request_reconcile_all(
        farm._accounts,
        trigger="farm_start",
    )
    launchable_count = len(farm._accounts) - len(blocked_accounts)
    flog_kv(
        "FARM",
        "started",
        accounts=len(farm._accounts),
        launchable=launchable_count,
        blocked=len(blocked_accounts),
    )
    flog(
        f"[FARM] Started {len(farm._accounts)} accounts "
        f"(launchable={launchable_count} blocked={len(blocked_accounts)})"
    )
    message = f"Farm started - {launchable_count}/{len(farm._accounts)} launchable"
    if blocked_accounts:
        message += f", {len(blocked_accounts)} blocked"
    farm._push_event(
        "system",
        message,
        severity="success" if launchable_count else "warning",
    )


def _stop_accounts(farm: Any) -> None:
    for acc in farm._accounts:
        if acc.pid:
            ProcessService.safe_kill_bound_process(
                acc,
                None,
                reason="farm_stop",
            )
        with acc._lock:
            farm._runtime_state.forced_reset(
                acc,
                desired=AccountState.IDLE,
                reason="farm_stop_reset",
            )
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


def _join_workers(farm: Any) -> None:
    for worker in farm._workers.values():
        worker.wake()
        if worker.is_alive():
            worker.join(timeout=2.0)


def _join_components(farm: Any) -> None:
    if farm._dispatcher and farm._dispatcher.is_alive():
        farm._dispatcher.join(timeout=2.0)
    if farm._maintenance and farm._maintenance.is_alive():
        farm._maintenance.join(timeout=2.0)
    if farm._net_mon:
        farm._net_mon.join(timeout=2.0)
    if farm._runtime_scheduler:
        farm._runtime_scheduler.stop()
        farm._runtime_scheduler = None
    farm._state_mgr = None


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
        _prepare_multi_roblox_guard(cfg)
        farm.running = True
        farm.start_ts = time.time()
        farm._bump_status_revision()
        get_rt_monitor().start()

        runtime_filter_active = _apply_start_desired_states(farm, cfg)
        _restore_runtime_state(farm, cfg, max_fail_count)

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

        components = _start_runtime_components(farm, cfg, state_mgr)
        blocked_accounts = _blocked_accounts(
            farm,
            cfg,
            runtime_filter_active,
        )
        _start_workers(farm, state_mgr, cfg)
        _start_dispatcher_and_maintenance(
            farm,
            components.queue,
            components.launcher,
            state_mgr,
            cfg,
        )
        _start_worker_threads(farm, blocked_accounts)
        _announce_started(farm, blocked_accounts)

    def stop_fast(self) -> None:
        """Fast window-close path: signal + kill Roblox, skip long joins.

        Full stop() joins every worker/dispatcher/maintenance with 2s
        timeouts sequentially (10s+ with several accounts), which is what
        kept the console hanging after the window was already gone.
        All those threads are daemon, so they die with os._exit anyway.
        This does only the load-bearing steps synchronously: signal stop,
        kill bound Roblox processes, persist runtime, release the guard.
        """
        farm = self._farm
        _best_effort(lambda: setattr(farm, "_shutting_down", True))
        _best_effort_dotted(farm, "_stop.set")
        _best_effort(lambda: setattr(farm, "running", False))
        _best_effort_dotted(farm, "_bump_status_revision")
        _best_effort(lambda: _call_if_present(farm._recovery, "stop"))
        _best_effort(
            lambda: _call_if_present(
                farm._queue,
                "cancel_all",
                "farm_stop_fast",
            )
        )
        _best_effort_dotted(farm, "_cancel_commands_for_shutdown")
        _best_effort(
            lambda: _kill_bound_processes_fast(
                getattr(farm, "_accounts", None)
            )
        )
        _best_effort(lambda: farm.cfg_mgr.save_runtime(farm._accounts))
        _best_effort(_release_guard_import)

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

        _stop_accounts(farm)
        _join_workers(farm)
        _join_components(farm)

        farm.cfg_mgr.save_runtime(farm._accounts)
        get_rt_monitor().stop()
        _release_guard_logged()
        flog_kv("FARM", "stopped", accounts=len(farm._accounts))
        flog("[FARM] Stopped")
        farm._push_event("system", "Farm stopped", severity="info")
        farm._shutting_down = False
