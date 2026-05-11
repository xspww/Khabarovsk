from __future__ import annotations

import random
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from account_hybrid import redact_secret
from core import (
    Account,
    AccountState,
    APP_DATA_DIR,
    ConfigManager,
    EventBus,
    EventName,
    GlobalLaunchLimiter,
    SmartQueue,
    StateManager,
    flog,
    flog_kv,
    account_launch_block_reason,
    cookie_identity_block_reason,
)
from domain.session_identity import build_launch_intent
from domain.runtime_signals import RuntimeSignal, is_recovery_signal, normalize_runtime_signal
from process_net import (
    NetworkMonitor,
    NET_ONLINE,
    RAMManager,
    VipTracker,
)
from services.process_service import ProcessManager
from services.presence_service import PRESENCE_SERVICE
from services.resource_monitor import get_rt_monitor
from runtime.account_runtime_controller import AccountRuntimeController
from runtime.runtime_health import account_health_flags, build_runtime_health
from runtime.recovery_context import (
    RecoveryAttemptContext,
    SESSION_CONFLICT,
    reason_for_category,
)
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
from runtime.runtime_store import RuntimeStore
from runtime.runtime_state_manager import RuntimeStateManager
from runtime.runtime_timeline import RuntimeTimeline
from runtime.supervisor_runtime import SupervisorRuntime
from runtime.system_maintenance import (
    SystemMaintenance,
    _account_presence_user_id,
    _apply_cpu_limiter_for_bound_process,
    _window_arrange_settings_from_config,
    _window_resize_target_from_config,
)


def compute_backoff(attempt: int, base: int = 5, cap: int = 120) -> float:
    exp = base * (2 ** max(0, attempt - 1))
    jitter = random.uniform(0, 3)
    return min(exp + jitter, float(cap))


def _redact_launch_detail(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    text = re.sub(
        r"([?&](?:privateServerLinkCode|linkCode|code|accessCode|reservedServerAccessCode)=)[^&\s]+",
        r"\1<redacted>",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _persist_cookie_identity_status(
    acc: Account,
    cookie_username: str = "",
    cookie_user_id: str = "",
    cookie_mismatch: bool = True,
):
    try:
        from account_hybrid import ACCOUNT_STORE

        ACCOUNT_STORE.update_record(
            acc.username,
            {
                "cookie_username": str(cookie_username or getattr(acc, "cookie_username", "") or ""),
                "cookie_user_id": str(cookie_user_id or getattr(acc, "cookie_user_id", "") or ""),
                "cookie_mismatch": bool(cookie_mismatch),
                "import_status": "cookie_mismatch" if cookie_mismatch else "",
            },
        )
    except Exception as e:
        flog_kv("ACCOUNT_DATA", "cookie_identity_status_persist_failed", "warning", account=acc.display_name, error=e)


def _set_account_cookie_block(acc: Account, reason: str, cookie_username: str = ""):
    with acc._lock:
        if cookie_username:
            acc.cookie_username = str(cookie_username)
        acc.cookie_mismatch = True
        acc.session_checked = True
        acc.session_valid = False
        acc.manual_status = reason
        acc.last_error = reason
        acc.last_crash_reason = "cookie_mismatch"
    _persist_cookie_identity_status(acc, cookie_username=cookie_username or acc.cookie_username, cookie_mismatch=True)


def _clear_account_cookie_block(acc: Account):
    with acc._lock:
        acc.cookie_mismatch = False
        if acc.last_crash_reason == "cookie_mismatch":
            acc.last_crash_reason = ""
        if "cookie" in str(acc.manual_status or "").lower():
            acc.manual_status = ""
        if "cookie" in str(acc.last_error or "").lower():
            acc.last_error = ""
    _persist_cookie_identity_status(acc, cookie_username=acc.cookie_username, cookie_user_id=acc.cookie_user_id, cookie_mismatch=False)


class RobloxWatchdog(threading.Thread):
    CHECK_INTERVAL = 5.0

    def __init__(self, acc: Account, worker: "AccountWorker", cfg: dict, stop: threading.Event):
        super().__init__(daemon=True, name=f"Watchdog-{acc.username}")
        self.acc = acc
        self.worker = worker
        self.cfg = cfg
        self._stop = stop
        self._mon = get_rt_monitor()

    def run(self):
        acc = self.acc
        flog_kv("WATCHDOG", "started", account=acc.display_name)
        abnormal_since: Optional[float] = None

        while not self._stop.wait(timeout=self.CHECK_INTERVAL):
            if not self.cfg.get("watchdog_enabled", True):
                abnormal_since = None
                continue

            if acc.state != AccountState.IN_GAME:
                abnormal_since = None
                continue

            pid = acc.pid
            if not pid or not ProcessManager.is_bound_game_alive(
                pid,
                owner_key=acc._config_username,
                expected_identity=acc.bound_process_identity,
            ):
                abnormal_since = None
                self.worker.handle_missing_bound_process("watchdog_pid_missing")
                continue

            now = time.time()
            runtime = now - (acc.in_game_since or now)
            loading_grace = max(30.0, float(self.cfg.get("watchdog_loading_grace", 90) or 90))
            if runtime < loading_grace:
                abnormal_since = None
                with acc._lock:
                    acc.last_activity_at = now
                    acc.last_activity_reason = "loading_grace"
                continue

            cpu_low = float(self.cfg.get("watchdog_cpu_low", 0.9))
            ram_low = float(self.cfg.get("watchdog_ram_low", 90.0))
            hold_sec = float(self.cfg.get("watchdog_hold_time", 60))
            activity_timeout = max(hold_sec, float(self.cfg.get("watchdog_activity_timeout", 180) or 180))
            activity = ProcessManager.get_game_activity(pid)
            cpu = float(activity.get("cpu") or 0.0)
            ram = float(activity.get("ram_mb") or 0.0)
            windows = int(activity.get("windows") or 0)
            if cpu <= 0.0 and ram <= 0.0:
                abnormal_since = None
                continue

            responsive_window = windows > 0 and not ProcessManager.is_not_responding(pid)
            resource_active = cpu >= cpu_low
            memory_present = ram >= ram_low
            if responsive_window or resource_active:
                if abnormal_since is not None:
                    flog_kv("WATCHDOG", "resource_recovered", account=acc.display_name, pid=pid)
                abnormal_since = None
                with acc._lock:
                    acc.last_activity_at = now
                    acc.last_activity_reason = "window" if responsive_window else "resource"
                    acc.last_activity_cpu = cpu
                    acc.last_activity_ram_mb = ram
                    acc.last_watchdog_classification = "active"
                continue

            is_abnormal = (cpu < cpu_low) and (memory_present or windows > 0)
            if not is_abnormal:
                abnormal_since = None
                with acc._lock:
                    acc.last_watchdog_classification = "loading"
                continue

            if self.worker.connection_recovery_active():
                abnormal_since = None
                with acc._lock:
                    acc.last_watchdog_classification = "reconnecting"
                continue

            with acc._lock:
                last_activity = acc.last_activity_at or acc.in_game_since or now
            inactive_for = max(0.0, now - last_activity)
            if inactive_for < activity_timeout:
                classification = "frozen_hold" if windows > 0 else "loading"
                with acc._lock:
                    acc.last_watchdog_classification = classification
                if abnormal_since is None:
                    abnormal_since = now
                continue

            if abnormal_since is None:
                abnormal_since = now
                flog_kv(
                    "WATCHDOG",
                    "abnormal_hold_started",
                    "warning",
                    account=acc.display_name,
                    pid=pid,
                    cpu=f"{cpu:.2f}",
                    ram=f"{ram:.1f}",
                    windows=windows,
                    inactive=f"{inactive_for:.1f}",
                    hold=f"{hold_sec:.1f}",
                )
            elif now - abnormal_since >= hold_sec:
                reason_key = "loading_freeze" if windows <= 0 else "watchdog_timeout"
                flog_kv(
                    "WATCHDOG",
                    "frozen_recovery_signal",
                    "warning",
                    account=acc.display_name,
                    pid=pid,
                    reason=reason_key,
                    cpu=f"{cpu:.2f}",
                    ram=f"{ram:.1f}",
                    windows=windows,
                    inactive=f"{inactive_for:.1f}",
                )
                pid_was = pid
                with acc._lock:
                    runtime_generation = acc.runtime_generation
                    session_id = acc.session_id
                    launch_nonce = acc.launch_nonce
                    transaction_id = acc.rejoin_transaction_id
                    acc.last_watchdog_classification = reason_key
                    acc.last_activity_reason = f"watchdog:{reason_key}"
                flog_kv(
                    "WATCHDOG",
                    "verified_kill_deferred",
                    "warning",
                    account=acc.display_name,
                    pid=pid_was,
                    reason=reason_key,
                    runtime_generation=runtime_generation,
                    session_id=session_id,
                    transaction_id=transaction_id,
                )
                with acc._lock:
                    signal_generation = acc.runtime_generation
                abnormal_since = None
                self.worker.report_fault(
                    reason_key,
                    f"PID={pid_was} CPU={cpu:.2f}% RAM={ram:.1f}MB windows={windows} inactive={inactive_for:.1f}s",
                    expected_runtime_generation=signal_generation,
                    expected_session_id=session_id,
                    expected_launch_nonce=launch_nonce,
                    expected_transaction_id=transaction_id,
                )

        flog_kv("WATCHDOG", "stopped", account=acc.display_name)


class LaunchController:
    def __init__(
        self,
        limiter: GlobalLaunchLimiter,
        state_mgr: StateManager,
        bus: EventBus,
        cfg: dict,
        accounts: Optional[List[Account]] = None,
        runtime_state: Optional[RuntimeStateManager] = None,
        runtime_store: Optional[RuntimeStore] = None,
        supervisor: Optional[SupervisorRuntime] = None,
    ):
        self._limiter = limiter
        self._state_mgr = state_mgr
        self._bus = bus
        self._cfg = cfg
        self._accounts = accounts or []
        self._lock = threading.Lock()
        self._runtime_state = runtime_state or RuntimeStateManager(logger=flog_kv)
        self._runtime_store = runtime_store
        self._supervisor = supervisor

    def _record_transaction(self, acc: Account, snapshot: Dict[str, Any]):
        if self._runtime_store and snapshot.get("transaction_id"):
            try:
                self._runtime_store.record_transaction(snapshot)
                self._runtime_store.record_account_snapshot(acc._config_username, acc.runtime_snapshot())
            except Exception as e:
                flog_kv("RUNTIME", "store_transaction_failed", "warning", account=acc.display_name, error=e)

    def _record_stale_transaction(self, acc: Account, expected: Dict[str, Any], reason: str):
        if not expected:
            return
        snapshot = {
            "transaction_id": str(expected.get("transaction_id", "") or ""),
            "account_id": getattr(acc, "_config_username", getattr(acc, "username", "")),
            "runtime_generation": int(expected.get("runtime_generation", 0) or 0),
            "recovery_generation": getattr(acc, "recovery_generation", 0),
            "command_generation": getattr(acc, "command_generation", 0),
            "account_runtime_id": getattr(acc, "account_runtime_id", ""),
            "session_id": str(expected.get("session_id", "") or ""),
            "launch_nonce": str(expected.get("launch_nonce", "") or ""),
            "status": "rolled_back",
            "step": "stale_rejected",
            "reason": reason,
            "failure_reason": "stale_work_rejected",
            "launch_intent": getattr(acc, "launch_intent", {}) or {},
            "destination_evidence": {},
            "created_at": getattr(acc, "session_started_at", 0.0) or time.time(),
            "updated_at": time.time(),
            "completed_at": time.time(),
        }
        self._record_transaction(acc, snapshot)
        flog_kv(
            "RUNTIME",
            "transaction_stale_rejected",
            "warning",
            account=acc.display_name,
            transaction_id=snapshot["transaction_id"],
            session_id=snapshot["session_id"],
            expected_runtime_generation=snapshot["runtime_generation"],
            current_runtime_generation=acc.runtime_generation,
            reason=reason,
            thread=threading.current_thread().name,
        )

    def _transaction_update(
        self,
        acc: Account,
        status: str = "",
        step: str = "",
        reason: str = "",
        server_validation: str = "",
        expected: Optional[Dict[str, Any]] = None,
    ) -> bool:
        with acc._lock:
            expected = expected or {}
            if expected and not self._runtime_state.guard_session_identity(
                acc,
                expected_generation=expected.get("runtime_generation"),
                expected_session_id=str(expected.get("session_id", "") or ""),
                expected_launch_nonce=str(expected.get("launch_nonce", "") or ""),
                expected_transaction_id=str(expected.get("transaction_id", "") or ""),
                reason=f"transaction_update:{reason or step or status}",
            ):
                self._record_stale_transaction(acc, expected, f"transaction_update:{reason or step or status}")
                return False
            snapshot = self._runtime_state.update_rejoin_transaction(
                acc,
                status=status,
                step=step,
                reason=reason,
                server_validation=server_validation,
            )
        self._record_transaction(acc, snapshot)
        if self._supervisor:
            self._supervisor.emit("JoinSupervisor", f"TRANSACTION_{(step or status or 'UPDATE').upper()}", account=acc, reason=reason, payload=snapshot)
        return True

    def _bind_live_game(
        self,
        acc: Account,
        pid: int,
        process_name: str,
        reason: str,
        expected_runtime_generation: Optional[int] = None,
        launched_after: Optional[float] = None,
    ) -> bool:
        bind_result = ProcessManager.bind_account_process(
            acc,
            pid,
            self._state_mgr,
            reason=reason,
            expected_identity=acc.bound_process_identity if pid == acc.pid else "",
            launched_after=launched_after,
            process_name=process_name,
            min_ram_mb=20.0,
            expected_runtime_generation=expected_runtime_generation,
        )
        validation = bind_result.get("validation") or {}
        if not bind_result.get("ok"):
            flog_kv(
                "LAUNCH",
                "bind_rejected",
                "warning",
                account=acc.display_name,
                pid=pid,
                reason=reason,
                reject=validation.get("reason", ""),
            )
            return False
        flog_kv(
            "LAUNCH",
            "bound_existing",
            account=acc.display_name,
            pid=pid,
            reason=reason,
            confidence=validation.get("confidence", 0.0),
        )
        if self._runtime_store:
            self._runtime_store.record_process_binding(
                acc._config_username,
                pid,
                acc.bound_process_identity,
                acc.session_id,
                acc.rejoin_transaction_id,
                "verified",
                reason,
            )
        _apply_cpu_limiter_for_bound_process(self._accounts, self._cfg, reason, acc)
        self._bus.emit(EventName.LAUNCH_SUCCESS, account=acc, pid=pid)
        return True

    def _quick_bind_candidate_is_stable(
        self,
        acc: Account,
        pid: int,
        reason: str,
        launched_after: Optional[float],
    ) -> bool:
        expected_identity = acc.bound_process_identity if pid == acc.pid else ""
        validation = ProcessManager.validate_binding(
            acc,
            pid,
            expected_identity=expected_identity,
            reason=f"{reason}:quick_bind_precheck",
            launched_after=launched_after,
            min_ram_mb=20.0,
            log_success=False,
            log_failure=False,
        )
        if not validation.get("ok"):
            flog_kv(
                "LAUNCH",
                "quick_bind_rejected_unstable_pid",
                "warning",
                account=acc.display_name,
                pid=pid,
                reason=reason,
                reject=validation.get("reason", ""),
                runtime_generation=acc.runtime_generation,
                session_id=acc.session_id,
                transaction_id=acc.rejoin_transaction_id,
            )
            return False

        windows = int(validation.get("windows") or 0)
        ram_mb = float(validation.get("ram_mb") or validation.get("rss_mb") or 0.0)
        identity = str(validation.get("identity") or "")
        owner = str(validation.get("owner") or ProcessManager.get_pid_owner(pid) or "")
        exact_existing_identity = bool(expected_identity and identity == expected_identity)
        owner_claim_matches = bool(owner and owner == acc._config_username)
        if windows > 0 or ram_mb >= 100.0 or exact_existing_identity or owner_claim_matches:
            return True

        flog_kv(
            "LAUNCH",
            "quick_bind_rejected_unstable_pid",
            "warning",
            account=acc.display_name,
            pid=pid,
            reason=reason,
            windows=windows,
            ram=f"{ram_mb:.1f}",
            reject="no_window_or_stable_runtime",
            runtime_generation=acc.runtime_generation,
            session_id=acc.session_id,
            transaction_id=acc.rejoin_transaction_id,
        )
        with acc._lock:
            acc.process_reject_reason = "quick_bind_rejected_unstable_pid"
            acc.sync_runtime("quick_bind_rejected_unstable_pid")
        return False

    def _try_bind_any_live_game(
        self,
        acc: Account,
        reason: str,
        launched_after: Optional[float] = None,
        expected_runtime_generation: Optional[int] = None,
    ) -> bool:
        pid, name = ProcessManager.find_bound_game_process(
            preferred_pid=acc.pid,
            launched_after=launched_after,
            owner_key=acc._config_username,
            expected_identity=acc.bound_process_identity,
        )
        if not pid and (acc.bound_process_identity or ProcessManager.get_pid_owner(acc.pid) == acc._config_username):
            pid, name = ProcessManager.find_bound_game_process(
                preferred_pid=acc.pid,
                launched_after=None,
                owner_key=acc._config_username,
                expected_identity=acc.bound_process_identity,
            )
        if not pid and launched_after is not None:
            flog_kv(
                "LAUNCH",
                "single_live_bind_skipped",
                "warning",
                account=acc.display_name,
                reason=reason,
                launched_after=f"{float(launched_after):.3f}",
                detail="unclaimed_live_processes_are_not_auto_bound",
            )
        if not pid:
            return False
        if not self._quick_bind_candidate_is_stable(acc, int(pid), reason, launched_after):
            return False
        return self._bind_live_game(
            acc,
            int(pid),
            name,
            reason,
            expected_runtime_generation=expected_runtime_generation,
            launched_after=launched_after,
        )

    def _safe_adopt_visible(
        self,
        acc: Account,
        reason: str,
        expected_runtime_generation: Optional[int] = None,
    ) -> Dict[str, Any]:
        result = ProcessManager.safe_adopt_visible_process(
            acc,
            self._state_mgr,
            accounts=self._accounts,
            reason=reason,
            expected_runtime_generation=expected_runtime_generation,
        )
        if result.get("ok") and self._runtime_store:
            self._runtime_store.record_process_binding(
                acc._config_username,
                int(result.get("pid") or 0),
                acc.bound_process_identity,
                acc.session_id,
                acc.rejoin_transaction_id,
                "adopted_visible_singleton",
                reason,
            )
        if result.get("ok"):
            _apply_cpu_limiter_for_bound_process(self._accounts, self._cfg, reason, acc)
        return result

    def _visible_process_presence(self, exclude_pids: Optional[List[int]] = None) -> Dict[str, Any]:
        excluded = {int(pid) for pid in (exclude_pids or []) if pid}
        live = ProcessManager.list_live_game_processes()
        visible = [
            item for item in live
            if int(item.get("pid") or 0) not in excluded
            and (int(item.get("windows") or 0) > 0 or float(item.get("rss_mb") or 0.0) >= 100.0)
        ]
        return {
            "live": live,
            "visible": visible,
            "visible_count": len(visible),
            "visible_pids": [int(item.get("pid") or 0) for item in visible if item.get("pid")],
        }

    def launch(self, acc: Account, stop: threading.Event) -> bool:
        with self._lock:
            with acc._lock:
                launch_guard = {
                    "runtime_generation": acc.runtime_generation,
                    "session_id": acc.session_id,
                    "launch_nonce": acc.launch_nonce,
                    "transaction_id": acc.rejoin_transaction_id,
                }
            use_ram = bool(self._cfg.get("use_ram_account_manager", False))
            use_ram_launch = use_ram and bool(self._cfg.get("ram_launch_via_api", True))
            multi_roblox = bool(self._cfg.get("multi_roblox_enabled", True))
            ProcessManager.MULTI_ROBLOX_ENABLED = multi_roblox
            ProcessManager.GLOBAL_VIP_LINK = str(self._cfg.get("game_private_server_url", "") or "").strip()
            ProcessManager.AUTO_CREATE_PRIVATE_SERVER_ENABLED = bool(self._cfg.get("auto_create_private_server_enabled", False))
            ProcessManager.AUTO_CREATE_PRIVATE_SERVER_FREE_ONLY = bool(self._cfg.get("auto_create_private_server_free_only", True))
            warmup_delay = max(0.0, float(self._cfg.get("login_warmup_delay", 6) or 0))
            attempted_vip = ""
            restart_reasons = {"watchdog_timeout", "loading_freeze", "teleport_timeout", "render_freeze"}
            with acc._lock:
                last_recovery_reason = str(acc.last_recovery_reason or acc.last_crash_reason or "")
            allow_existing_reuse = last_recovery_reason not in restart_reasons

            existing_pid = acc.pid if ProcessManager.is_bound_game_alive(
                acc.pid,
                owner_key=acc._config_username,
                expected_identity=acc.bound_process_identity,
            ) else None
            existing_name = acc.bound_process_name
            if existing_pid and allow_existing_reuse:
                return self._bind_live_game(
                    acc,
                    existing_pid,
                    existing_name,
                    "prelaunch_probe",
                    expected_runtime_generation=launch_guard["runtime_generation"],
                )
            if existing_pid and not allow_existing_reuse:
                flog_kv(
                    "LAUNCH",
                    "verified_kill_deferred",
                    "warning",
                    account=acc.display_name,
                    pid=existing_pid,
                    reason=last_recovery_reason,
                    runtime_generation=launch_guard["runtime_generation"],
                    session_id=launch_guard["session_id"],
                    transaction_id=launch_guard["transaction_id"],
                )

            ProcessManager.LOGIN_WARMUP_DELAY = warmup_delay

            def prepare_direct_launch() -> None:
                protected_pids = [
                    int(item.get("pid"))
                    for item in ProcessManager.list_live_game_processes()
                    if item.get("pid")
                ] if multi_roblox else []
                killed = ProcessManager.kill_all_roblox_clients(
                    wait_seconds=4.0,
                    exclude_pids=protected_pids,
                )
                if protected_pids:
                    flog(
                        f"[LAUNCH] Preserving live Roblox game PID(s) during relaunch: {sorted(set(protected_pids))}"
                    )
                if killed:
                    flog(f"[LAUNCH] Cleared {killed} existing Roblox client process(es) before relaunch")
                time.sleep(1.2)

            def inject_cookie_for_direct_launch() -> Tuple[bool, str]:
                if not acc.cookie:
                    return False, "no cookie available"
                from process_net import IsolationManager
                return IsolationManager.inject_cookie(acc.username, acc.cookie)

            if use_ram:
                ok_sync, sync_detail = RAMManager.sync_account_profile(acc, self._cfg)
                if ok_sync:
                    flog(f"[LAUNCH] {acc.display_name} {sync_detail}")
                else:
                    flog(f"[LAUNCH] {acc.display_name} RAM sync skipped: {sync_detail}", "warning")

            skip_shared_cookie_inject = bool(multi_roblox and acc.cookie)
            if not use_ram_launch:
                prepare_direct_launch()
                if skip_shared_cookie_inject:
                    flog(
                        f"[LAUNCH] Skipping shared cookie injection for {acc.display_name} "
                        "because Multi Roblox auth-ticket launch is enabled"
                    )
                else:
                    ok, detail = inject_cookie_for_direct_launch()
                    if not ok:
                        flog(f"[LAUNCH] Cookie inject warning for {acc.display_name}: {detail}", "warning")
                    else:
                        flog(f"[LAUNCH] Cookie injected for {acc.display_name}: {detail}")

            self._limiter.wait(stop)
            if stop.is_set():
                return False

            before_pids = ProcessManager.snapshot_pids()

            if acc.pid:
                stale_pid = acc.pid
                kill_result = ProcessManager.safe_kill_bound_process(
                    acc,
                    self._state_mgr,
                    reason="prelaunch_stale_pid",
                    expected_runtime_generation=launch_guard["runtime_generation"],
                )
                if not kill_result.get("killed"):
                    flog_kv(
                        "LAUNCH",
                        "stale_pid_not_killed",
                        "warning",
                        account=acc.display_name,
                        pid=stale_pid,
                        reason=kill_result.get("reason", "identity_or_owner_not_verified"),
                    )
                time.sleep(1.0)

            if use_ram_launch:
                ok, detail = RAMManager.launch_account(acc, self._cfg)
                attempted_vip = acc.active_vip
                if not ok:
                    flog(
                        f"[LAUNCH] RAM launch failed for {acc.display_name} -> fallback to direct launch: {_redact_launch_detail(detail)}",
                        "warning",
                    )

                    if not acc.cookie:
                        ok_sync, sync_detail = RAMManager.sync_account_profile(acc, self._cfg)
                        if ok_sync:
                            flog(f"[LAUNCH] {acc.display_name} {sync_detail}")
                        else:
                            flog(f"[LAUNCH] {acc.display_name} RAM sync skipped: {sync_detail}", "warning")

                    if acc.cookie:
                        prepare_direct_launch()
                        if skip_shared_cookie_inject:
                            flog(
                                f"[LAUNCH] Skipping shared cookie injection for {acc.display_name} "
                                "because Multi Roblox auth-ticket launch is enabled"
                            )
                        else:
                            ok_inject, inject_detail = inject_cookie_for_direct_launch()
                            if not ok_inject:
                                flog(f"[LAUNCH] Cookie inject warning for {acc.display_name}: {inject_detail}", "warning")
                            else:
                                flog(f"[LAUNCH] Cookie injected for {acc.display_name}: {inject_detail}")
                        ok, detail, attempted_vip = ProcessManager.launch(acc)
                    else:
                        ok = False
                        detail = f"{detail}; no cookie available for fallback"
            else:
                ok, detail, attempted_vip = ProcessManager.launch(acc)
            safe_detail = _redact_launch_detail(detail)
            if not ok:
                flog(f"[LAUNCH] Failed for {acc.display_name}: {safe_detail}", "warning")
                if self._transaction_update(acc, status="failed", step="launch_failed", reason=str(safe_detail or "launch_failed"), server_validation="launch_failed", expected=launch_guard):
                    self._bus.emit(EventName.LAUNCH_FAILED, account=acc, reason=safe_detail)
                if attempted_vip and acc._vip_tracker:
                    acc._vip_tracker.mark_crash(attempted_vip)
                return False

            flog(f"[LAUNCH] Sent for {acc.display_name} ({safe_detail[:80]})")
            with acc._lock:
                self._runtime_state.update_launch_intent(
                    acc,
                    build_launch_intent(acc, reason="launch_sent"),
                    reason="launch_sent",
                    expected_generation=launch_guard["runtime_generation"],
                )
            if not self._transaction_update(
                acc,
                status="launching",
                step="launch_sent",
                reason="launch_sent",
                server_validation="intent_recorded",
                expected=launch_guard,
            ):
                return False
            self._state_mgr.transition(
                acc,
                AccountState.VERIFY,
                reason="launch_sent",
                expected_generation=launch_guard["runtime_generation"],
            )
            launch_ts = acc.last_launch_at or time.time()

        verify_window = self._cfg.get("launch_verify_window", 25)
        quick_bind_deadline = time.time() + min(6.0, max(1.0, float(verify_window) / 3.0))
        while not stop.is_set() and time.time() < quick_bind_deadline:
            presence = self._visible_process_presence()
            if presence.get("visible_count"):
                adopt = self._safe_adopt_visible(
                    acc,
                    "post_launch_visible_adopt",
                    expected_runtime_generation=launch_guard["runtime_generation"],
                )
                if adopt.get("ok"):
                    self._transaction_update(
                        acc,
                        status="process_bound",
                        step="adopted_existing_window",
                        reason="adopted_existing_window",
                        server_validation="process_verified_destination_pending",
                        expected=launch_guard,
                    )
                    return True
            if self._try_bind_any_live_game(acc, "post_launch_existing", launched_after=launch_ts, expected_runtime_generation=launch_guard["runtime_generation"]):
                ProcessManager.cleanup_extra_launch_processes(
                    before_pids,
                    keep_pids=[acc.pid] if acc.pid else [],
                    launched_after=launch_ts,
                )
                return True
            time.sleep(0.5)

        pid = ProcessManager.detect_new_pid(
            before_pids,
            timeout=verify_window,
            launched_after=launch_ts,
            created_after_slack=warmup_delay + 2.0,
        )

        if stop.is_set():
            return False

        if not pid:
            presence = self._visible_process_presence()
            if presence.get("visible_count"):
                adopt = self._safe_adopt_visible(
                    acc,
                    "verify_fallback_visible_adopt",
                    expected_runtime_generation=launch_guard["runtime_generation"],
                )
                if adopt.get("ok"):
                    self._transaction_update(
                        acc,
                        status="process_bound",
                        step="adopted_existing_window",
                        reason="adopted_existing_window",
                        server_validation="process_verified_destination_pending",
                        expected=launch_guard,
                    )
                    return True
            if self._try_bind_any_live_game(acc, "verify_fallback", launched_after=launch_ts, expected_runtime_generation=launch_guard["runtime_generation"]):
                ProcessManager.cleanup_extra_launch_processes(
                    before_pids,
                    keep_pids=[acc.pid] if acc.pid else [],
                    launched_after=launch_ts,
                )
                return True

            flog(f"[LAUNCH] PID not detected for {acc.display_name} within {verify_window}s", "warning")
            if attempted_vip and acc._vip_tracker:
                acc._vip_tracker.mark_crash(attempted_vip)
            if self._transaction_update(acc, status="failed", step="failed", reason="PID not detected", server_validation="unverified_no_pid", expected=launch_guard):
                self._bus.emit(EventName.LAUNCH_FAILED, account=acc, reason="PID not detected")
            return False

        presence = self._visible_process_presence(exclude_pids=[pid])
        pid_validation = ProcessManager.validate_binding(
            acc,
            pid,
            reason="post_launch_detected_precheck",
            launched_after=launch_ts - max(0.0, warmup_delay + 2.0),
            min_ram_mb=20.0,
            log_success=False,
            log_failure=False,
        )
        if pid_validation.get("ok") and int(pid_validation.get("windows") or 0) <= 0 and presence.get("visible_count"):
            flog_kv(
                "LAUNCH",
                "transient_launch_pid_rejected",
                "warning",
                account=acc.display_name,
                pid=pid,
                visible_pids=",".join(str(item) for item in presence.get("visible_pids", [])),
                reason="no_window_with_visible_preserved_process",
            )
            with acc._lock:
                acc.process_reject_reason = "transient_launch_pid_rejected"
                acc.adopt_reject_reason = ""
                acc.sync_runtime("transient_launch_pid_rejected")
            adopt = self._safe_adopt_visible(
                acc,
                "transient_pid_visible_adopt",
                expected_runtime_generation=launch_guard["runtime_generation"],
            )
            if adopt.get("ok"):
                self._transaction_update(
                    acc,
                    status="process_bound",
                    step="adopted_existing_window",
                    reason="adopted_existing_window",
                    server_validation="process_verified_destination_pending",
                    expected=launch_guard,
                )
                return True
            if self._transaction_update(
                acc,
                status="failed",
                step="failed",
                reason=f"visible process adopt failed: {adopt.get('reason', 'unknown')}",
                server_validation="visible_process_unowned",
                expected=launch_guard,
            ):
                self._bus.emit(EventName.LAUNCH_FAILED, account=acc, reason=f"visible process adopt failed: {adopt.get('reason', 'unknown')}")
            return False

        if pid_validation.get("ok") and int(pid_validation.get("windows") or 0) <= 0:
            settle_deadline = time.time() + min(3.0, max(1.0, float(verify_window) * 0.12))
            stable_validation = dict(pid_validation)
            while not stop.is_set() and time.time() < settle_deadline:
                time.sleep(0.5)
                stable_validation = ProcessManager.validate_binding(
                    acc,
                    pid,
                    reason="post_launch_no_window_settle",
                    launched_after=launch_ts - max(0.0, warmup_delay + 2.0),
                    min_ram_mb=20.0,
                    log_success=False,
                    log_failure=False,
                )
                if not stable_validation.get("ok"):
                    break
                stable_windows = int(stable_validation.get("windows") or 0)
                stable_ram = float(stable_validation.get("rss_mb") or stable_validation.get("ram_mb") or 0.0)
                if stable_windows > 0 or stable_ram >= 100.0:
                    pid_validation = stable_validation
                    break
            final_windows = int(pid_validation.get("windows") or 0)
            final_ram = float(pid_validation.get("rss_mb") or pid_validation.get("ram_mb") or 0.0)
            if final_windows <= 0 and final_ram < 100.0:
                flog_kv(
                    "LAUNCH",
                    "transient_launch_pid_rejected",
                    "warning",
                    account=acc.display_name,
                    pid=pid,
                    reason="no_window_or_stable_runtime_after_settle",
                    ram=f"{final_ram:.1f}",
                )
                with acc._lock:
                    acc.process_reject_reason = "transient_launch_pid_rejected_no_window"
                    acc.sync_runtime("transient_launch_pid_rejected_no_window")
                if self._transaction_update(
                    acc,
                    status="failed",
                    step="failed",
                    reason="transient launch PID rejected: no window or stable runtime",
                    server_validation="transient_launch_pid_rejected",
                    expected=launch_guard,
                ):
                    self._bus.emit(EventName.LAUNCH_FAILED, account=acc, reason="transient launch PID rejected")
                return False

        bind_result = ProcessManager.bind_account_process(
            acc,
            pid,
            self._state_mgr,
            reason="post_launch_detected",
            launched_after=launch_ts - max(0.0, warmup_delay + 2.0),
            min_ram_mb=20.0,
            expected_runtime_generation=launch_guard["runtime_generation"],
        )
        validation = bind_result.get("validation") or {}
        if not bind_result.get("ok"):
            flog_kv(
                "LAUNCH",
                "detected_pid_rejected",
                "warning",
                account=acc.display_name,
                pid=pid,
                reject=validation.get("reason", ""),
            )
            if self._transaction_update(acc, status="failed", step="failed", reason=f"PID rejected: {validation.get('reason', '')}", server_validation="process_rejected", expected=launch_guard):
                self._bus.emit(EventName.LAUNCH_FAILED, account=acc, reason=f"PID rejected: {validation.get('reason', '')}")
            return False
        flog_kv(
            "LAUNCH",
            "pid_bound",
            account=acc.display_name,
            pid=pid,
            confidence=validation.get("confidence", 0.0),
        )
        self._transaction_update(
            acc,
            status="process_bound",
            step="process_bound",
            reason="post_launch_detected",
            server_validation="process_verified_destination_pending",
            expected=launch_guard,
        )
        if self._runtime_store:
            self._runtime_store.record_process_binding(
                acc._config_username,
                pid,
                acc.bound_process_identity,
                acc.session_id,
                acc.rejoin_transaction_id,
                "verified",
                "post_launch_detected",
            )
        _apply_cpu_limiter_for_bound_process(self._accounts, self._cfg, "post_launch_detected", acc)
        extra_killed = ProcessManager.cleanup_extra_launch_processes(
            before_pids,
            keep_pids=[pid],
            launched_after=launch_ts,
        )
        if extra_killed:
            flog(f"[LAUNCH] Cleaned {extra_killed} leftover Roblox process(es) after bind for {acc.display_name}")
        if attempted_vip and acc._vip_tracker:
            acc._vip_tracker.mark_success(attempted_vip)
        self._bus.emit(EventName.LAUNCH_SUCCESS, account=acc, pid=pid)
        return True


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
            self.fail_account(acc, "max_fail", AccountWorker.REASON_MESSAGES["max_fail"])
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
                self.fail_account(acc, "cookie_invalid", AccountWorker.REASON_MESSAGES["cookie_invalid"])
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
        )

        if bool(policy.get("fatal")):
            self.fail_account(acc, canonical, reason_msg)
            return
        if loop_reason:
            self.fail_account(acc, "relaunch_loop", loop_reason)
            return
        if fail_count >= max_fail:
            self.fail_account(acc, "max_fail", AccountWorker.REASON_MESSAGES["max_fail"])
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
            reason_msg=AccountWorker.REASON_MESSAGES["launch_fail"],
        )
        if bool(policy.get("fatal")):
            self.fail_account(acc, canonical, AccountWorker.REASON_MESSAGES.get(canonical, canonical))
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


class AccountWorker(threading.Thread):
    """
    AccountWorker observes process health.
    It no longer decides how recovery should happen.
    """

    REASON_MESSAGES = {
        "pid_dead": "หลุด - Process หายไป (game crashed/closed)",
        "not_responding": "หลุด - Not Responding (เกมค้าง, ตรวจพบจาก Task Manager)",
        "network_drop": "หลุด - เน็ตหลุด (network dropped)",
        "launch_fail": "หลุด - Launch ล้มเหลว (ไม่สามารถเปิดเกมได้)",
        "cookie_invalid": "หยุด - Cookie ไม่ถูกต้อง (session expired)",
        "cookie_missing": "หยุด - ไม่มี cookie login จาก Roblox Account Manager",
        "max_fail": "หยุด - เกิน fail limit (FAILED state)",
        "relaunch_loop": "หยุด - Roblox เด้งเร็วหลายรอบติดกัน จึงหยุด auto rejoin",
        "watchdog_low_resource": "หลุด - CPU/RAM ต่ำผิดปกติ (Watchdog kill)",
        "cookie_mismatch": "Stopped - cookie belongs to a different Roblox account. Reimport the correct cookie.",
        "process_crash": "Process crashed or disappeared",
        "watchdog_timeout": "Watchdog timeout - no process activity",
        "loading_freeze": "Loading freeze - no heartbeat during loading",
        "teleport_timeout": "Teleport timeout",
        "auth_failure": "Authentication failure",
        "server_full": "Server full",
        "connection_error": "Connection Error / Disconnected",
        "account_launched_elsewhere": "Session conflict (Error 273)",
        "session_conflict": "Session conflict (Error 273)",
        "unexpected_client_behavior": "Rejoining - Roblox disconnected (Error 268)",
        "security_kick": "Rejoining - Roblox data session ended (Error 267)",
        "multi_roblox_guard_failed": "Stopped - Multi Roblox guard failed. Roblox closed another account while launching; restart RT after the guard is ready.",
    }

    def __init__(
        self,
        acc: Account,
        state_mgr: StateManager,
        bus: EventBus,
        cfg: dict,
        recovery: RecoveryEngine,
        stop: threading.Event,
        supervisor: Optional[SupervisorRuntime] = None,
        accounts: Optional[List[Account]] = None,
    ):
        super().__init__(daemon=True, name=f"Worker-{acc.username}")
        self.acc = acc
        self.state_mgr = state_mgr
        self.bus = bus
        self.cfg = cfg
        self.recovery = recovery
        self._stop = stop
        self._supervisor = supervisor
        self._accounts = accounts or [acc]
        self._wake = threading.Event()
        self._not_responding_since: Optional[float] = None
        self._connection_error_since: Optional[float] = None
        self._watchdog: Optional[RobloxWatchdog] = None
        self._last_ram_hold_log = 0.0

    def wake(self):
        self._wake.set()

    def connection_recovery_active(self) -> bool:
        return self._connection_error_since is not None

    def report_fault(
        self,
        reason_key: str,
        extra: str = "",
        expected_runtime_generation: Optional[int] = None,
        expected_session_id: str = "",
        expected_launch_nonce: str = "",
        expected_transaction_id: str = "",
    ):
        msg = self.REASON_MESSAGES.get(reason_key, reason_key)
        if extra:
            msg += f" [{extra}]"
        flog(f"[WORKER] {self.acc.display_name} fault: {msg}")
        if self._supervisor:
            self._supervisor.emit(
                "AccountSupervisor",
                "FAULT_SIGNAL",
                account=self.acc,
                severity="warning",
                reason=reason_key,
                payload={"detail": msg},
            )
        self.recovery.handle_runtime_signal(
            self.acc,
            "fault",
            reason_key,
            payload={"detail": msg, "reason_msg": msg},
            expected_runtime_generation=expected_runtime_generation,
            expected_session_id=expected_session_id,
            expected_launch_nonce=expected_launch_nonce,
            expected_transaction_id=expected_transaction_id,
        )
        self._wake.set()

    def _rebind_live_game_process(self, reason: str) -> bool:
        acc = self.acc
        reconciliation = ProcessManager.staged_orphan_reconcile(
            acc,
            launched_after=acc.last_launch_at,
            quarantine_seconds=max(15.0, min(30.0, float(self.cfg.get("launch_verify_window", 25) or 25))),
        )
        validation = reconciliation.get("validation") or {}
        pid = validation.get("pid")
        name = str(validation.get("name") or "")
        if not pid:
            return False
        confidence = float(validation.get("confidence") or 0.0)
        action = str(reconciliation.get("action") or "")
        if action != "auto_bind":
            flog_kv(
                "WORKER",
                "rebind_rejected",
                "warning",
                account=acc.display_name,
                pid=pid,
                reason=reason,
                action=action,
                confidence=f"{confidence:.1f}",
                level=reconciliation.get("confidence_level", ""),
                reject=reconciliation.get("reason", ""),
            )
            return False
        with acc._lock:
            old_pid = acc.pid
            runtime_generation = acc.runtime_generation
        bind_result = ProcessManager.bind_account_process(
            acc,
            pid,
            self.state_mgr,
            reason=reason,
            expected_identity=str(validation.get("identity") or ""),
            launched_after=acc.last_launch_at,
            process_name=name or "RobloxPlayerBeta.exe",
            min_ram_mb=20.0,
            expected_runtime_generation=runtime_generation,
        )
        if not bind_result.get("ok"):
            self.state_mgr.set_binding_status(acc, "rebind_rejected", reason=bind_result.get("reason", reason))
            flog_kv(
                "WORKER",
                "rebind_validation_rejected",
                "warning",
                account=acc.display_name,
                pid=pid,
                reason=reason,
                reject=bind_result.get("reason", ""),
            )
            return False
        if pid != old_pid:
            flog(
                f"[WORKER] {acc.display_name} rebound live game PID {old_pid} -> {pid} "
                f"({reason})"
            )
        else:
            flog_kv(
                "WORKER",
                "rebind_refreshed",
                account=acc.display_name,
                pid=pid,
                reason=reason,
                confidence=f"{confidence:.1f}",
            )
        _apply_cpu_limiter_for_bound_process(self._accounts, self.cfg, reason, acc)
        return True

    def _safe_adopt_visible_process(self, reason: str) -> bool:
        acc = self.acc
        with acc._lock:
            runtime_generation = acc.runtime_generation
        result = ProcessManager.safe_adopt_visible_process(
            acc,
            self.state_mgr,
            accounts=self._accounts,
            reason=reason,
            expected_runtime_generation=runtime_generation,
        )
        if result.get("ok"):
            flog_kv(
                "WORKER",
                "visible_process_adopted",
                account=acc.display_name,
                pid=result.get("pid"),
                reason=reason,
                runtime_generation=runtime_generation,
            )
            _apply_cpu_limiter_for_bound_process(self._accounts, self.cfg, reason, acc)
            return True
        if result.get("reason") not in {"no_visible_candidate", "desired_state_not_in_game"}:
            flog_kv(
                "WORKER",
                "visible_process_adopt_rejected",
                "warning",
                account=acc.display_name,
                pid=result.get("pid") or "",
                reason=reason,
                reject=result.get("reason", ""),
                runtime_generation=runtime_generation,
            )
        return False

    def _presence_assist_missing_bound_process(self) -> Dict[str, object]:
        if not bool(self.cfg.get("presence_api_enabled", False)):
            return {"status": "disabled"}
        if not bool(self.cfg.get("presence_assist_rejoin_enabled", True)):
            return {"status": "disabled"}
        uid = _account_presence_user_id(self.acc)
        if not uid:
            return {"status": "skipped", "reason": "missing_user_id"}
        result = PRESENCE_SERVICE.refresh(
            [uid],
            enabled=True,
            poll_interval=float(self.cfg.get("presence_poll_interval_seconds", 30) or 30),
            cache_ttl=float(self.cfg.get("presence_cache_ttl_seconds", 30) or 30),
            force=False,
        )
        presence = (result.get("presences") or {}).get(uid) or PRESENCE_SERVICE.get_cached(uid)
        if not presence:
            return {"status": "unknown", "reason": str(result.get("msg") or "no_presence"), "presence_api": result}
        try:
            presence_type = int(presence.get("presence_type") or -1)
        except Exception:
            presence_type = -1
        if presence_type != 2:
            return {"status": "not_ingame", "presence": presence, "presence_api": result}
        observed_places = {
            str(presence.get("presence_place_id") or "").strip(),
            str(presence.get("presence_root_place_id") or "").strip(),
        }
        observed_places.discard("")
        if not observed_places:
            return {"status": "limited", "reason": "presence_limited", "presence": presence, "presence_api": result}
        with self.acc._lock:
            expected_places = {
                str(self.acc.place_id or "").strip(),
                str((self.acc.launch_intent or {}).get("place_id") or "").strip(),
                str((self.acc.launch_intent_summary or {}).get("place_id") or "").strip(),
            }
        expected_places.discard("")
        if expected_places and observed_places.intersection(expected_places):
            return {"status": "hold", "reason": "roblox_presence_ingame", "presence": presence, "presence_api": result}
        return {"status": "mismatch", "presence": presence, "presence_api": result}

    def _assess_missing_bound_process(self, source: str) -> Dict[str, object]:
        acc = self.acc
        now = time.time()
        grace_period = max(4.0, min(float(self.cfg.get("crash_timeout", 30)), 10.0))

        if self._rebind_live_game_process(source):
            return {"status": "rebound", "grace_period": grace_period}

        if self._safe_adopt_visible_process(f"{source}_visible_adopt"):
            return {"status": "rebound", "grace_period": grace_period, "adopted": True}

        ram_online = None
        ram_detail = ""
        if self.cfg.get("use_ram_account_manager", False):
            ram_online, ram_detail, _ = RAMManager.resolve_account_online(
                acc, self.cfg, force_refresh=True
            )

        if ram_online is True:
            return {
                "status": "hold",
                "reason": "ram_online",
                "ram_detail": ram_detail,
                "grace_period": grace_period,
            }

        presence_assist = self._presence_assist_missing_bound_process()
        if presence_assist.get("status") == "hold":
            return {
                "status": "hold",
                "reason": str(presence_assist.get("reason") or "roblox_presence_ingame"),
                "presence": ProcessManager.summarize_game_presence(launched_after=acc.last_launch_at),
                "roblox_presence": presence_assist.get("presence") or {},
                "ram_detail": ram_detail,
                "missing_for": now - float(acc.pid_missing_since or now),
                "grace_period": grace_period,
            }

        reconciliation = ProcessManager.staged_orphan_reconcile(
            acc,
            launched_after=acc.last_launch_at,
            quarantine_seconds=max(15.0, min(30.0, float(self.cfg.get("launch_verify_window", 25) or 25))),
        )
        validation = reconciliation.get("validation") or {}
        presence = ProcessManager.summarize_game_presence(launched_after=acc.last_launch_at)
        with acc._lock:
            acc.last_signal_confidence = float(validation.get("confidence") or 0.0)
            acc.last_reconcile_at = time.time()
        if reconciliation.get("action") == "auto_bind" and self._rebind_live_game_process(f"{source}_presence"):
            return {"status": "rebound", "presence": presence, "validation": validation, "ram_detail": ram_detail, "grace_period": grace_period}

        with acc._lock:
            if not acc.pid_missing_since:
                acc.pid_missing_since = now
            missing_for = now - acc.pid_missing_since
            pid_was = acc.pid

        has_multi_signal = reconciliation.get("action") == "quarantine"
        if self._looks_like_multi_roblox_guard_failure(pid_was, presence, missing_for, grace_period):
            return {
                "status": "multi_roblox_guard_failed",
                "presence": presence,
                "validation": validation,
                "ram_detail": ram_detail,
                "missing_for": missing_for,
                "grace_period": grace_period,
            }

        if missing_for < grace_period or has_multi_signal:
            return {
                "status": "hold",
                "reason": str(reconciliation.get("reason") or "presence_hold"),
                "presence": presence,
                "validation": validation,
                "ram_detail": ram_detail,
                "missing_for": missing_for,
                "grace_period": grace_period,
            }

        return {
            "status": "dead",
            "presence": presence,
            "validation": validation,
            "ram_detail": ram_detail,
            "missing_for": missing_for,
            "grace_period": grace_period,
        }

    def _looks_like_multi_roblox_guard_failure(
        self,
        pid_was: Optional[int],
        presence: Dict[str, Any],
        missing_for: float,
        grace_period: float,
    ) -> bool:
        if not bool(self.cfg.get("multi_roblox_enabled", False)):
            return False
        if bool(self.cfg.get("rt_rotation_enabled", False)):
            return False
        if not pid_was or missing_for < grace_period:
            return False
        try:
            window = max(grace_period, float(self.cfg.get("multi_roblox_guard_failure_window", 180) or 180))
        except Exception:
            window = 180.0
        try:
            overlap_window = float(
                self.cfg.get("multi_roblox_guard_failure_overlap_seconds", grace_period) or grace_period
            )
        except Exception:
            overlap_window = grace_period
        overlap_window = max(1.0, min(overlap_window, window))
        with self.acc._lock:
            launch_age = time.time() - float(self.acc.last_launch_at or 0.0)
            missing_since = float(self.acc.pid_missing_since or 0.0)
        newest_created = float(presence.get("newest_created") or 0.0)
        if not newest_created or not missing_since:
            return False
        if not self._has_active_multi_roblox_launch_overlap():
            return False
        if launch_age > window and newest_created < (time.time() - window):
            return False
        if newest_created < (missing_since - overlap_window):
            return False
        if newest_created > (missing_since + max(5.0, grace_period)):
            return False
        pids = []
        for item in presence.get("pids", []) or []:
            try:
                pid = int(item)
            except Exception:
                continue
            if pid:
                pids.append(pid)
        other_pids = [pid for pid in pids if pid != int(pid_was)]
        return bool(other_pids)

    def _has_active_multi_roblox_launch_overlap(self) -> bool:
        accounts = list(getattr(self, "_accounts", []) or [])
        if not accounts:
            return True
        active_launch_states = {AccountState.QUEUED, AccountState.LAUNCHING, AccountState.VERIFY}
        for other in accounts:
            if other is self.acc:
                continue
            try:
                with other._lock:
                    if other.desired_state == AccountState.IN_GAME and other.state in active_launch_states:
                        return True
            except Exception:
                continue
        return False

    def handle_missing_bound_process(self, source: str) -> str:
        acc = self.acc
        with acc._lock:
            pid_was = acc.pid
            observed_runtime_generation = acc.runtime_generation
            observed_session_id = acc.session_id
            observed_launch_nonce = acc.launch_nonce
            observed_transaction_id = acc.rejoin_transaction_id
        assessment = self._assess_missing_bound_process(source)
        status = str(assessment.get("status") or "")
        if status == "rebound":
            return status

        if status == "hold":
            now = time.time()
            reason = str(assessment.get("reason") or "")
            ram_detail = str(assessment.get("ram_detail") or "")
            presence = assessment.get("presence") or {
                "pids": [],
                "visible_windows": 0,
                "max_ram_mb": 0.0,
                "max_cpu": 0.0,
            }
            missing_for = float(assessment.get("missing_for") or 0.0)
            if now - self._last_ram_hold_log >= (10.0 if reason == "ram_online" else 5.0):
                if reason == "ram_online":
                    flog(
                        f"[WORKER] {acc.display_name} PID missing but RAM still reports online "
                        f"({ram_detail}) - holding IN_GAME state"
                    )
                else:
                    flog(
                        f"[WORKER] {acc.display_name} bound PID missing but game signals remain "
                        f"(missing={missing_for:.1f}s pids={presence['pids']} "
                        f"windows={presence['visible_windows']} ram={presence['max_ram_mb']:.1f}MB "
                        f"cpu={presence['max_cpu']:.2f}%)"
                    )
                self._last_ram_hold_log = now
            return status

        presence = assessment.get("presence") or {
            "visible_windows": 0,
            "max_ram_mb": 0.0,
            "max_cpu": 0.0,
        }
        ram_detail = str(assessment.get("ram_detail") or "")
        grace_period = float(assessment.get("grace_period") or 0.0)
        guard_failed = status == "multi_roblox_guard_failed"
        with acc._lock:
            if not self.recovery._runtime_state.guard_session_identity(
                acc,
                expected_generation=observed_runtime_generation,
                expected_session_id=observed_session_id,
                expected_launch_nonce=observed_launch_nonce,
                expected_transaction_id=observed_transaction_id,
                reason=f"missing_bound_process:{source}",
            ):
                flog_kv(
                    "WORKER",
                    "stale_worker_signal_rejected",
                    "warning",
                    account=acc.display_name,
                    source=source,
                    pid=pid_was or "",
                    expected_runtime_generation=observed_runtime_generation,
                    current_runtime_generation=acc.runtime_generation,
                    expected_session_id=observed_session_id,
                    current_session_id=acc.session_id,
                    expected_transaction_id=observed_transaction_id,
                    current_transaction_id=acc.rejoin_transaction_id,
                )
                return "stale"
        if pid_was:
            ProcessManager.evict_pid_cache(pid_was)
        if pid_was:
            self.state_mgr.clear_process_binding(
                acc,
                reason="missing_bound_process_dead",
                increment_generation=True,
            )
        else:
            with acc._lock:
                acc.pid_missing_since = 0.0
        with acc._lock:
            signal_runtime_generation = acc.runtime_generation
            signal_session_id = acc.session_id
            signal_launch_nonce = acc.launch_nonce
            signal_transaction_id = acc.rejoin_transaction_id

        extra = f"PID={pid_was}" if pid_was else "PID=<none>"
        if ram_detail:
            extra += f" RAM={ram_detail}"
        extra += (
            f" grace={grace_period:.1f}s"
            f" windows={presence['visible_windows']}"
            f" ram={float(presence['max_ram_mb'] or 0.0):.1f}MB"
            f" cpu={float(presence['max_cpu'] or 0.0):.2f}%"
        )
        if guard_failed:
            msg = self.REASON_MESSAGES["multi_roblox_guard_failed"]
            try:
                from roblox_hybrid import record_multi_roblox_guard_failure

                record_multi_roblox_guard_failure(f"{acc.display_name}: {extra}")
            except Exception:
                pass
            with acc._lock:
                acc.manual_status = msg
                acc.last_error = f"{msg} [{extra}]"
            flog_kv("MULTI_ROBLOX", "guard_runtime_failure_detected", "error", account=acc.display_name, detail=extra)
            self.recovery.handle_runtime_signal(
                acc,
                "fatal",
                "multi_roblox_guard_failed",
                payload={"detail": f"{msg} [{extra}]", "reason_msg": msg},
                expected_runtime_generation=signal_runtime_generation,
                expected_session_id=signal_session_id,
                expected_launch_nonce=signal_launch_nonce,
                expected_transaction_id=signal_transaction_id,
            )
            self._wake.set()
            return status
        self.report_fault(
            "pid_dead",
            extra,
            expected_runtime_generation=signal_runtime_generation,
            expected_session_id=signal_session_id,
            expected_launch_nonce=signal_launch_nonce,
            expected_transaction_id=signal_transaction_id,
        )
        return status

    def run(self):
        acc = self.acc
        flog(f"[WORKER] {acc.display_name} started")

        block_reason = account_launch_block_reason(acc)
        if block_reason:
            _set_account_cookie_block(acc, block_reason)
            flog(f"[WORKER] {acc.display_name} launch blocked: {block_reason}", "warning")
            self.recovery.handle_runtime_signal(
                acc,
                "fatal",
                "cookie_mismatch",
                payload={"reason_msg": block_reason, "detail": block_reason},
            )
            return

        if self.cfg.get("use_ram_account_manager", False) and not acc.cookie:
            ok_sync, sync_detail = RAMManager.sync_account_profile(acc, self.cfg)
            if ok_sync:
                flog(f"[WORKER] {acc.display_name} {sync_detail}")
            else:
                flog(f"[WORKER] {acc.display_name} RAM sync skipped: {sync_detail}", "warning")

        if acc.cookie:
            validate_attempt = 0
            while not self._stop.is_set():
                while not self.recovery._net.is_online() and not self._stop.is_set():
                    self._wake.wait(timeout=2.0)
                    self._wake.clear()
                if self._stop.is_set():
                    return

                ok, username, detail, transient = self._validate_cookie(acc.cookie)
                if ok:
                    mismatch_reason = cookie_identity_block_reason(acc.username, username, bool(username and username.lower() != acc.username.lower()))
                    if mismatch_reason:
                        _set_account_cookie_block(acc, mismatch_reason, cookie_username=username)
                        flog(f"[WORKER] {acc.display_name} launch blocked: {mismatch_reason}", "warning")
                        self.recovery.handle_runtime_signal(
                            acc,
                            "fatal",
                            "cookie_mismatch",
                            payload={"reason_msg": mismatch_reason, "detail": mismatch_reason},
                        )
                        return
                    with acc._lock:
                        acc.cookie_username = str(username or acc.cookie_username or "")
                        acc.cookie_mismatch = False
                        acc.session_valid = True
                        acc.session_checked = True
                        acc.session_wait_started_at = 0.0
                    if username and username != acc.username:
                        flog(f"[WORKER] {acc.display_name} cookie validated as '{username}'")
                    flog(f"[WORKER] {acc.display_name} cookie valid ({username})")
                    self.recovery.request_evaluate(acc, trigger="cookie_validated")
                    break

                with acc._lock:
                    acc.session_checked = True
                    acc.session_valid = False
                    acc.last_crash_reason = "cookie_check_transient" if transient else "cookie_invalid"

                if transient:
                    validate_attempt += 1
                    delay = compute_backoff(validate_attempt, base=3, cap=30)
                    flog(
                        f"[WORKER] {acc.display_name} cookie validation transient error -> retry in {delay:.1f}s: {detail}",
                        "warning",
                    )
                    self.recovery.request_evaluate(acc, trigger="cookie_validation_retry")
                    self._wake.wait(timeout=delay)
                    self._wake.clear()
                    continue

                flog(f"[WORKER] {acc.display_name} cookie invalid -> FAILED: {detail}", "warning")
                self.recovery.handle_runtime_signal(
                    acc,
                    "fatal",
                    "cookie_invalid",
                    payload={"reason_msg": self.REASON_MESSAGES["cookie_invalid"], "detail": detail},
                )
                return
        else:
            if self.cfg.get("use_ram_account_manager", False):
                with acc._lock:
                    acc.session_checked = True
                flog(
                    f"[WORKER] {acc.display_name} no cookie from RAM -> block launch to avoid Roblox login screen",
                    "warning",
                )
                self.recovery.handle_runtime_signal(
                    acc,
                    "fatal",
                    "cookie_missing",
                    payload={"reason_msg": self.REASON_MESSAGES["cookie_missing"]},
                )
                return
            with acc._lock:
                acc.session_valid = True
                acc.session_checked = True
            flog(f"[WORKER] {acc.display_name} no cookie - skipping validation")

        initial_bound = False
        if acc.state != AccountState.IN_GAME:
            if self._rebind_live_game_process("initial_probe"):
                initial_bound = True
            elif self._safe_adopt_visible_process("initial_probe_visible_adopt"):
                initial_bound = True

        if initial_bound:
            with acc._lock:
                runtime_generation = acc.runtime_generation
                session_id = acc.session_id
                launch_nonce = acc.launch_nonce
                transaction_id = acc.rejoin_transaction_id
            self.recovery.handle_runtime_signal(
                acc,
                "launch_success",
                "initial_probe",
                payload={"trigger": "initial_probe", "count_rejoin": False},
                expected_runtime_generation=runtime_generation,
                expected_session_id=session_id,
                expected_launch_nonce=launch_nonce,
                expected_transaction_id=transaction_id,
            )
            self._wake.wait(timeout=1.0)
            self._wake.clear()

        self.recovery.request_evaluate(acc, trigger="initial_boot")

        crash_to = self.cfg.get("crash_timeout", 30)
        nr_timeout = self.cfg.get("not_responding_timeout", 30)

        while not self._stop.is_set():
            if acc.state == AccountState.IN_GAME:
                if not acc.pid or not ProcessManager.is_bound_game_alive(
                    acc.pid,
                    owner_key=acc._config_username,
                    expected_identity=acc.bound_process_identity,
                ):
                    status = self.handle_missing_bound_process("bound_pid_missing")
                    if status == "rebound":
                        self._wake.wait(timeout=1.0)
                        self._wake.clear()
                        continue
                    if status == "hold":
                        self._wake.wait(timeout=min(float(crash_to), 5.0))
                        self._wake.clear()
                        continue
                    continue
                else:
                    with acc._lock:
                        acc.pid_missing_since = 0.0

                if acc.pid and acc.in_game_since:
                    runtime = time.time() - (acc.in_game_since or time.time())
                    if runtime > 10 and self.cfg.get("connection_error_rejoin", True) and self.cfg.get("popup_disconnected_enabled", True):
                        hold_sec = max(1.0, float(self.cfg.get("connection_error_hold_time", 3) or 3))
                        disconnect_info = ProcessManager.inspect_disconnect_dialog(acc.pid, sample_count=2)
                        if disconnect_info.get("matched"):
                            reason_key = str(disconnect_info.get("reason_key") or "connection_error")
                            detail = str(disconnect_info.get("detail") or "")
                            error_code = str(disconnect_info.get("error_code") or "")
                            action = str(disconnect_info.get("action") or "rejoin")
                            confidence = float(disconnect_info.get("popup_confidence", disconnect_info.get("confidence", 0.0)) or 0.0)
                            evidence_note = f"source={disconnect_info.get('evidence_source', '')} confidence={confidence:.2f} samples={disconnect_info.get('positive_samples', 0)}/{disconnect_info.get('sample_count', 0)}"
                            detail_suffix = f" code={error_code}" if error_code else ""
                            if not disconnect_info.get("recovery_allowed"):
                                flog(f"[WORKER] {acc.display_name} popup suspicious visual-only ignored ({evidence_note}: {detail})")
                                self._connection_error_since = None
                                self._wake.wait(timeout=min(1.0, max(0.2, hold_sec / 2.0)))
                                self._wake.clear()
                                continue
                            if action == "fail":
                                flog(f"[WORKER] {acc.display_name} disconnect dialog requires stop (reason={reason_key}{detail_suffix} {evidence_note})")
                                with acc._lock:
                                    runtime_generation = acc.runtime_generation
                                    session_id = acc.session_id
                                    launch_nonce = acc.launch_nonce
                                    transaction_id = acc.rejoin_transaction_id
                                self._connection_error_since = None
                                reason_msg = self.REASON_MESSAGES.get(reason_key, reason_key)
                                self.recovery.handle_runtime_signal(
                                    acc,
                                    "fatal",
                                    reason_key,
                                    payload={
                                        "reason_msg": f"{reason_msg} [PID={acc.pid} UI={detail}]",
                                        "detail": detail,
                                        "popup_code": error_code,
                                        "popup_confidence": confidence,
                                        "disconnect_category": disconnect_info.get("disconnect_category", ""),
                                    },
                                    expected_runtime_generation=runtime_generation,
                                    expected_session_id=session_id,
                                    expected_launch_nonce=launch_nonce,
                                    expected_transaction_id=transaction_id,
                                )
                                continue
                            if self._connection_error_since is None:
                                self._connection_error_since = time.time()
                                flog(f"[WORKER] {acc.display_name} disconnect dialog detected - will recover in {hold_sec:.0f}s ({reason_key}{detail_suffix} {evidence_note}: {detail})")
                                self._wake.wait(timeout=min(1.0, hold_sec))
                                self._wake.clear()
                                continue
                            elif time.time() - self._connection_error_since >= hold_sec:
                                flog(f"[WORKER] {acc.display_name} disconnect dialog held {hold_sec:.0f}s -> force recover ({reason_key}{detail_suffix} {evidence_note})")
                                with acc._lock:
                                    runtime_generation = acc.runtime_generation
                                    session_id = acc.session_id
                                    launch_nonce = acc.launch_nonce
                                    transaction_id = acc.rejoin_transaction_id
                                self._connection_error_since = None
                                if reason_key == "network_drop" and not self.recovery._net.is_online():
                                    self.recovery.handle_runtime_signal(
                                        acc,
                                        "network_lost",
                                        "network_drop",
                                        payload={"trigger": "disconnect_dialog", "detail": detail},
                                        expected_runtime_generation=runtime_generation,
                                        expected_session_id=session_id,
                                        expected_launch_nonce=launch_nonce,
                                        expected_transaction_id=transaction_id,
                                    )
                                else:
                                    self.recovery.handle_runtime_signal(
                                        acc,
                                        "disconnect_detected",
                                        reason_key,
                                        payload={
                                            "trigger": "disconnect_dialog",
                                            "detail": f"PID={acc.pid} UI={detail}",
                                            "reason_msg": f"PID={acc.pid} UI={detail}",
                                            "popup_code": error_code,
                                            "popup_confidence": confidence,
                                            "disconnect_category": disconnect_info.get("disconnect_category", ""),
                                            "visual_disconnect": bool(disconnect_info.get("visual_disconnect", False)),
                                        },
                                        expected_runtime_generation=runtime_generation,
                                        expected_session_id=session_id,
                                        expected_launch_nonce=launch_nonce,
                                        expected_transaction_id=transaction_id,
                                    )
                                continue
                            else:
                                self._wake.wait(timeout=min(1.0, max(0.2, hold_sec / 2.0)))
                                self._wake.clear()
                                continue
                        else:
                            self._connection_error_since = None

                    if runtime > 30:
                        if ProcessManager.is_not_responding(acc.pid):
                            if self._not_responding_since is None:
                                self._not_responding_since = time.time()
                                flog(f"[WORKER] {acc.display_name} Not Responding - will kill in {nr_timeout}s")
                            elif time.time() - self._not_responding_since >= nr_timeout:
                                flog(f"[WORKER] {acc.display_name} Not Responding for {nr_timeout}s -> force kill")
                                pid_was = acc.pid
                                with acc._lock:
                                    runtime_generation = acc.runtime_generation
                                kill_result = ProcessManager.safe_kill_bound_process(
                                    acc,
                                    self.state_mgr,
                                    reason="not_responding_recover",
                                    expected_runtime_generation=runtime_generation,
                                )
                                if kill_result.get("reason") == "stale_runtime_generation":
                                    continue
                                self._not_responding_since = None
                                with acc._lock:
                                    signal_generation = acc.runtime_generation
                                self.report_fault("not_responding", f"PID={pid_was}", expected_runtime_generation=signal_generation)
                                continue
                        else:
                            self._not_responding_since = None

                self._wake.wait(timeout=crash_to)
                self._wake.clear()
                continue

            self._wake.wait(timeout=2.0)
            self._wake.clear()

        flog(f"[WORKER] {acc.display_name} stopped")

    @staticmethod
    def _validate_cookie(cookie: str):
        try:
            req = urllib.request.Request(
                "https://users.roblox.com/v1/users/authenticated",
                headers={
                    "Cookie": f".ROBLOSECURITY={cookie.strip()}",
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = __import__("json").loads(resp.read())
            username = data.get("name", "")
            return (True, username, "", False) if username else (False, "", "no username in response", True)
        except urllib.error.HTTPError as e:
            transient = e.code >= 500 or e.code == 429
            detail = f"HTTP {e.code} {'(transient)' if transient else '(cookie expired or invalid)'}"
            return False, "", detail, transient
        except Exception as e:
            return False, "", str(e), True


class Dispatcher(threading.Thread):
    def __init__(
        self,
        queue: SmartQueue,
        launcher: LaunchController,
        state_mgr: StateManager,
        bus: EventBus,
        workers: Dict[str, AccountWorker],
        recovery: RecoveryEngine,
        net: NetworkMonitor,
        stop: threading.Event,
        cfg: Optional[dict] = None,
        runtime_state: Optional[RuntimeStateManager] = None,
        runtime_store: Optional[RuntimeStore] = None,
        supervisor: Optional[SupervisorRuntime] = None,
    ):
        super().__init__(daemon=True, name="Dispatcher")
        self._queue = queue
        self._launcher = launcher
        self._state_mgr = state_mgr
        self._bus = bus
        self._workers = workers
        self._recovery = recovery
        self._net = net
        self._stop = stop
        self._cfg = cfg or {}
        self._runtime_state = runtime_state or RuntimeStateManager(logger=flog_kv)
        self._runtime_store = runtime_store
        self._supervisor = supervisor

    def _apply_window_resize_after_launch(self, acc: Account) -> None:
        target = _window_resize_target_from_config(self._cfg)
        if not target:
            return
        width, height = target
        arrange = _window_arrange_settings_from_config(self._cfg)
        if arrange:
            width, height, columns, gap, margin = arrange
            result = ProcessManager.arrange_roblox_windows(width, height, columns, gap, margin)
            changed = int(result.get("arranged") or 0)
            event = "post_launch_arrange"
        else:
            result = ProcessManager.resize_roblox_windows(width, height)
            changed = int(result.get("resized") or 0)
            event = "post_launch_resize"
        if changed > 0:
            flog_kv(
                "WINDOW",
                event,
                account=acc.display_name,
                arranged=result.get("arranged", 0),
                resized=result.get("resized", 0),
                count=result.get("count", 0),
                width=width,
                height=height,
                columns=result.get("columns", ""),
            )

    def _record_transaction(self, acc: Account, snapshot: Dict[str, Any], session_status: str = "active"):
        if not self._runtime_store:
            return
        try:
            session = snapshot.get("session") or snapshot
            if session.get("session_id"):
                session_record = dict(session)
                session_record["recovery_generation"] = snapshot.get("recovery_generation", acc.recovery_generation)
                session_record["command_generation"] = snapshot.get("command_generation", acc.command_generation)
                self._runtime_store.record_session(session_record, status=session_status)
            if snapshot.get("transaction_id"):
                self._runtime_store.record_transaction(snapshot)
            self._runtime_store.record_account_snapshot(acc._config_username, acc.runtime_snapshot())
        except Exception as e:
            flog_kv("RUNTIME", "store_transaction_failed", "warning", account=acc.display_name, error=e)

    def _record_stale_transaction(self, acc: Account, expected: Dict[str, Any], reason: str):
        if not expected:
            return
        snapshot = {
            "transaction_id": str(expected.get("transaction_id", "") or ""),
            "account_id": acc._config_username,
            "runtime_generation": int(expected.get("runtime_generation", 0) or 0),
            "recovery_generation": acc.recovery_generation,
            "command_generation": acc.command_generation,
            "account_runtime_id": acc.account_runtime_id,
            "session_id": str(expected.get("session_id", "") or ""),
            "launch_nonce": str(expected.get("launch_nonce", "") or ""),
            "status": "rolled_back",
            "step": "stale_rejected",
            "reason": reason,
            "failure_reason": "stale_work_rejected",
            "launch_intent": dict(acc.launch_intent or {}),
            "destination_evidence": {},
            "created_at": acc.session_started_at or time.time(),
            "updated_at": time.time(),
            "completed_at": time.time(),
        }
        self._record_transaction(acc, snapshot, session_status="rolled_back")
        flog_kv(
            "RUNTIME",
            "transaction_stale_rejected",
            "warning",
            account=acc.display_name,
            transaction_id=snapshot["transaction_id"],
            session_id=snapshot["session_id"],
            expected_runtime_generation=snapshot["runtime_generation"],
            current_runtime_generation=acc.runtime_generation,
            reason=reason,
            thread=threading.current_thread().name,
        )
        if self._supervisor:
            self._supervisor.emit("RecoverySupervisor", "TRANSACTION_STALE_REJECTED", account=acc, severity="warning", reason=reason, payload=snapshot)

    def _destination_evidence(self, acc: Account) -> Dict[str, Any]:
        current_intent = build_launch_intent(acc, reason="destination_evidence")
        evidence = {
            "configured_place_id": str(getattr(acc, "place_id", "") or ""),
            "configured_server_type": getattr(getattr(acc, "server_type", None), "value", str(getattr(acc, "server_type", "") or "")),
            "observed_place_id": "",
            "observed_server_type": "",
            "observed_private_link_code_hash": "",
            "active_vip_present": bool(getattr(acc, "active_vip", "") or ""),
            "active_private_link_code_hash": str(current_intent.get("active_private_link_code_hash", "") or ""),
            "private_server_intent": bool(current_intent.get("private_server_intent", False)),
            "launch_strategy": str(getattr(acc, "launch_strategy", "") or ""),
            "evidence_source": "launch_intent_and_account_config",
        }
        try:
            if acc.pid:
                from roblox_hybrid import parse_launch_destination_from_cmdline

                parsed = parse_launch_destination_from_cmdline(ProcessManager.get_pid_cmdline(acc.pid))
                if parsed:
                    evidence.update({k: v for k, v in parsed.items() if v not in (None, "")})
        except Exception:
            pass
        return evidence

    def _validate_launch_intent(self, acc: Account, evidence: Dict[str, Any]) -> Tuple[bool, str, str]:
        intent = dict(getattr(acc, "launch_intent", {}) or {})
        expected_place = str(intent.get("place_id", "") or "").strip()
        actual_place = str(evidence.get("observed_place_id", "") or "").strip()
        if not actual_place and str(evidence.get("place_id_source", "") or "").lower() == "observed":
            actual_place = str(evidence.get("place_id", "") or "").strip()
        if expected_place and actual_place and expected_place != actual_place:
            return False, "intent_mismatch_place_id", f"place_id mismatch expected={expected_place} actual={actual_place}"

        expected_server = str(intent.get("server_type", "") or "").strip().upper()
        actual_server = str(evidence.get("observed_server_type", "") or "").strip().upper()
        if not actual_server and str(evidence.get("server_type_source", "") or "").lower() == "observed":
            actual_server = str(evidence.get("server_type", "") or "").strip().upper()
        if expected_server and actual_server and "UNKNOWN" not in {expected_server, actual_server} and expected_server != actual_server:
            return False, "intent_mismatch_server_type", f"server_type mismatch expected={expected_server} actual={actual_server}"

        expected_private = bool(intent.get("private_server_intent"))
        expected_private_hash = str(intent.get("active_private_link_code_hash", "") or "")
        configured_private_hashes = {
            str(item or "")
            for item in (intent.get("configured_private_link_code_hashes") or [])
            if str(item or "")
        }
        expected_private_hashes = {item for item in [expected_private_hash, *configured_private_hashes] if item}
        observed_private_hash = str(evidence.get("observed_private_link_code_hash", "") or "")
        if expected_private and observed_private_hash and expected_private_hashes and observed_private_hash in expected_private_hashes:
            if expected_place and actual_place and expected_place != actual_place:
                return False, "intent_mismatch_place_id", f"place_id mismatch expected={expected_place} actual={actual_place}"
            return True, "private_server_verified", ""
        if expected_private_hashes and observed_private_hash and observed_private_hash not in expected_private_hashes:
            return False, "intent_mismatch_private_server", "private server intent mismatch"

        if actual_place and expected_place == actual_place:
            return True, "intent_verified_place", ""

        if expected_private:
            flog_kv(
                "VERIFY",
                "destination_evidence_limited",
                "warning",
                account=acc.display_name,
                validation="private_server_unverified",
                evidence_source=str(evidence.get("evidence_source", "")),
                reason="no_observed_private_server_or_job_evidence",
                session_id=acc.session_id,
                transaction_id=acc.rejoin_transaction_id,
                runtime_generation=acc.runtime_generation,
            )
            return True, "private_server_unverified", ""

        if expected_place:
            flog_kv(
                "VERIFY",
                "destination_evidence_limited",
                "warning",
                account=acc.display_name,
                validation="intent_verified_no_job_evidence",
                evidence_source=str(evidence.get("evidence_source", "")),
                reason="no_observed_job_evidence",
                session_id=acc.session_id,
                transaction_id=acc.rejoin_transaction_id,
                runtime_generation=acc.runtime_generation,
            )
            return True, "intent_verified_no_job_evidence", ""

        flog_kv(
            "VERIFY",
            "destination_evidence_limited",
            "warning",
            account=acc.display_name,
            validation="intent_recorded_no_destination_evidence",
            evidence_source=str(evidence.get("evidence_source", "")),
            reason="no_configured_or_observed_destination",
            session_id=acc.session_id,
            transaction_id=acc.rejoin_transaction_id,
            runtime_generation=acc.runtime_generation,
        )
        return True, "intent_recorded_no_destination_evidence", ""

    def _finish_transaction(
        self,
        acc: Account,
        status: str,
        reason: str,
        evidence: Optional[Dict[str, Any]] = None,
        server_validation: str = "",
        expected: Optional[Dict[str, Any]] = None,
    ) -> bool:
        with acc._lock:
            expected = expected or {}
            if not self._runtime_state.guard_session_identity(
                acc,
                expected_generation=expected.get("runtime_generation"),
                expected_session_id=str(expected.get("session_id", "") or ""),
                expected_launch_nonce=str(expected.get("launch_nonce", "") or ""),
                expected_transaction_id=str(expected.get("transaction_id", "") or ""),
                reason=f"finish_transaction:{reason}",
            ):
                self._record_stale_transaction(acc, expected, f"finish_transaction:{reason}")
                return False
            snapshot = self._runtime_state.finish_rejoin_transaction(
                acc,
                status=status,
                reason=reason,
                destination_evidence=evidence or {},
                server_validation=server_validation,
            )
        self._record_transaction(acc, snapshot, session_status=status)
        if self._supervisor:
            event_name = "END_REJOIN_TRANSACTION" if status == "committed" else "ROLLBACK_REJOIN_TRANSACTION"
            self._supervisor.emit("RecoverySupervisor", event_name, account=acc, severity="success" if status == "committed" else "warning", reason=reason, payload=snapshot)
        return True

    def run(self):
        flog("[DISPATCHER] started")
        while not self._stop.is_set():
            if not self._net.is_online():
                self._net.wait_until_online(timeout=5)
                continue

            self._queue.wait_until_free(self._stop)
            if self._stop.is_set():
                break

            acc = self._queue.pop(timeout=1.0)
            if acc is None:
                continue

            if acc.state != AccountState.QUEUED:
                flog(f"[DISPATCHER] skip {acc.display_name} (state={acc.state.name})")
                continue

            self._queue.mark_busy()
            launch_intent = build_launch_intent(acc, reason="dispatcher_launch")
            with acc._lock:
                tx_snapshot = self._runtime_state.begin_rejoin_transaction(
                    acc,
                    reason="dispatcher_launch",
                    launch_intent=launch_intent,
                )
                tx_guard = {
                    "transaction_id": tx_snapshot.get("transaction_id", ""),
                    "session_id": tx_snapshot.get("session_id", ""),
                    "launch_nonce": tx_snapshot.get("launch_nonce", ""),
                    "runtime_generation": tx_snapshot.get("runtime_generation", 0),
                }
            self._record_transaction(acc, tx_snapshot, session_status="pending")
            if self._supervisor:
                self._supervisor.emit("RecoverySupervisor", "BEGIN_REJOIN_TRANSACTION", account=acc, reason="dispatcher_launch", payload=tx_snapshot)
            self._state_mgr.transition(acc, AccountState.LAUNCHING, reason="dispatcher_launch")

            try:
                flog(f"[DISPATCHER] launching {acc.display_name}")
                success = self._launcher.launch(acc, self._stop)
                if self._stop.is_set() or acc.desired_state != AccountState.IN_GAME:
                    flog_kv(
                        "DISPATCHER",
                        "launch_result_ignored",
                        account=acc.display_name,
                        stopped=self._stop.is_set(),
                        desired=getattr(acc.desired_state, "name", acc.desired_state),
                    )
                    self._finish_transaction(
                        acc,
                        "rolled_back",
                        "stopped_or_not_desired",
                        server_validation="aborted",
                        expected=tx_guard,
                    )
                    continue
                pid_is_live = bool(acc.pid and ProcessManager.is_bound_game_alive(
                    acc.pid,
                    owner_key=acc._config_username,
                    expected_identity=acc.bound_process_identity,
                ))
                if success or (acc.state == AccountState.IN_GAME and pid_is_live):
                    if not success:
                        flog(
                            f"[DISPATCHER] suppressing launch_failed for {acc.display_name} "
                            f"because account is already back IN_GAME on pid={acc.pid}"
                        )
                    with acc._lock:
                        launch_trigger = acc.last_rejoin_trigger or "dispatcher_launch"
                    evidence = self._destination_evidence(acc)
                    evidence.update({
                        "pid": acc.pid,
                        "process_identity": acc.bound_process_identity,
                    })
                    intent_ok, server_validation, intent_failure = self._validate_launch_intent(acc, evidence)
                    if not intent_ok:
                        rolled_back = self._finish_transaction(
                            acc,
                            "rolled_back",
                            intent_failure or "server_intent_mismatch",
                            evidence=evidence,
                            server_validation=server_validation,
                            expected=tx_guard,
                        )
                        if rolled_back:
                            ProcessManager.safe_kill_bound_process(
                                acc,
                                self._state_mgr,
                                reason="server_intent_mismatch",
                                expected_runtime_generation=tx_guard.get("runtime_generation"),
                            )
                            with acc._lock:
                                signal_generation = acc.runtime_generation
                            self._recovery.handle_runtime_signal(
                                acc,
                                "launch_failure",
                                intent_failure or "server_intent_mismatch",
                                payload={"detail": intent_failure or "server_intent_mismatch"},
                                expected_runtime_generation=signal_generation,
                                expected_session_id=str(tx_guard.get("session_id", "") or ""),
                                expected_launch_nonce=str(tx_guard.get("launch_nonce", "") or ""),
                                expected_transaction_id=str(tx_guard.get("transaction_id", "") or ""),
                            )
                        continue

                    committed = self._finish_transaction(
                        acc,
                        "committed",
                        launch_trigger,
                        evidence=evidence,
                        server_validation=server_validation,
                        expected=tx_guard,
                    )
                    if not committed:
                        continue
                    self._recovery.handle_runtime_signal(
                        acc,
                        "launch_success",
                        launch_trigger,
                        payload={"trigger": launch_trigger},
                        expected_runtime_generation=tx_guard.get("runtime_generation"),
                        expected_session_id=str(tx_guard.get("session_id", "") or ""),
                        expected_launch_nonce=str(tx_guard.get("launch_nonce", "") or ""),
                        expected_transaction_id=str(tx_guard.get("transaction_id", "") or ""),
                    )
                    self._apply_window_resize_after_launch(acc)
                    worker = self._workers.get(acc._config_username)
                    if worker:
                        worker.wake()
                else:
                    rolled_back = self._finish_transaction(acc, "rolled_back", "launch_failed", server_validation="launch_failed", expected=tx_guard)
                    if rolled_back:
                        self._recovery.handle_runtime_signal(
                            acc,
                            "launch_failure",
                            "launch_failed",
                            payload={"detail": "launch_failed"},
                            expected_runtime_generation=tx_guard.get("runtime_generation"),
                            expected_session_id=str(tx_guard.get("session_id", "") or ""),
                            expected_launch_nonce=str(tx_guard.get("launch_nonce", "") or ""),
                            expected_transaction_id=str(tx_guard.get("transaction_id", "") or ""),
                        )
            finally:
                self._queue.mark_free()

        flog("[DISPATCHER] stopped")




class FarmController:
    def __init__(self, cfg_mgr: ConfigManager):
        self.cfg_mgr = cfg_mgr
        self.bus = EventBus(
            workers=int(cfg_mgr.get("event_bus_workers", 4) or 4),
            max_pending=int(cfg_mgr.get("event_bus_max_pending", 128) or 128),
        )
        self._stop = threading.Event()
        self.running = False
        self.start_ts: Optional[float] = None

        self._accounts: List[Account] = []
        self._workers: Dict[str, AccountWorker] = {}
        self._net_mon: Optional[NetworkMonitor] = None
        self._dispatcher: Optional[Dispatcher] = None
        self._queue: Optional[SmartQueue] = None
        self._recovery: Optional[RecoveryEngine] = None
        self._maintenance: Optional[SystemMaintenance] = None
        self._state_mgr: Optional[StateManager] = None
        self._shutting_down = False

        self._total_rejoin = 0
        self._total_crash = 0
        self._event_log: list = []
        self._event_lock = threading.RLock()
        self._status_lock = threading.Lock()
        self._status_revision = 0
        self._command_lock = threading.RLock()
        self._commands: Dict[str, Dict[str, Any]] = {}
        self._command_seq = 0
        self._command_generation = 0
        self._runtime_state = RuntimeStateManager(logger=flog_kv)
        self._runtime_store = RuntimeStore(os.path.join(APP_DATA_DIR, "roboguard_runtime.db"))
        self._timeline = RuntimeTimeline(
            self._runtime_store,
            self._event_log,
            self._event_lock,
            logger=flog_kv,
            memory_limit=500,
        )
        try:
            rolled_back = self._runtime_store.rollback_open_transactions("backend_restart")
            if rolled_back:
                flog_kv("RUNTIME", "open_transactions_rolled_back", count=rolled_back, reason="backend_restart")
        except Exception as e:
            flog_kv("RUNTIME", "open_transaction_rollback_failed", "warning", error=e)
        self._supervisor = SupervisorRuntime(store=self._runtime_store, logger=flog_kv)

        self.bus.on(EventName.REJOIN_SUCCESS, self._on_rejoin)
        self.bus.on(EventName.ACCOUNT_CRASH, self._on_crash)
        self.bus.on(EventName.ACCOUNT_FAILED, self._on_failed)
        self.bus.on(EventName.STATE_CHANGE, self._on_state_change)
        self.bus.on(EventName.NETWORK_STATE_CHANGE, self._on_net_change)

    def _bump_status_revision(self) -> int:
        with self._status_lock:
            self._status_revision += 1
            flog_kv("STATUS", "revision_bumped", revision=self._status_revision)
            return self._status_revision

    def _record_timeline(
        self,
        event_type: str,
        account_key: str = "",
        severity: str = "info",
        reason: str = "",
        **fields: Any,
    ) -> None:
        acc = self._find_account(account_key) if account_key else None
        snapshot = {}
        display = ""
        if acc:
            with acc._lock:
                snapshot = acc.runtime_snapshot()
                display = acc.display_name
        item = {
            "ts": time.time(),
            "kind": event_type,
            "event_type": event_type,
            "msg": reason or event_type,
            "severity": severity,
            "reason": reason,
            "account": account_key or "",
            "display": display,
            "lifecycle_owner": "farm_controller",
            **fields,
        }
        self._timeline.record(item, account_snapshot=snapshot if acc else None, account_id=account_key)

    def _find_account(self, username: str) -> Optional[Account]:
        return next(
            (
                a for a in self._accounts
                if a._config_username == username or a.username == username
            ),
            None,
        )

    def _command_capability(self, action: str, account: str = "") -> Tuple[bool, str, Optional[Account]]:
        acc = self._find_account(account) if account else None
        if action == "start":
            if self.running:
                return False, "Already running", None
            return True, "", None
        if action == "stop":
            if not self.running:
                return False, "Not running", None
            return True, "", None
        if action == "force_rejoin":
            if not self.running:
                return False, "Guard stopped", acc
            if not acc:
                return False, "Account not found", None
            with acc._lock:
                if acc.state == AccountState.FAILED:
                    return False, "Account failed", acc
            return True, "", acc
        if action == "kill_pid":
            if not acc:
                return False, "Account not found", None
            with acc._lock:
                if not acc.pid:
                    return False, "No active PID", acc
            return True, "", acc
        return True, "", acc

    def _cleanup_commands_locked(self):
        now = time.time()
        expired = [key for key, item in self._commands.items() if float(item.get("expires_at") or 0.0) <= now]
        for key in expired:
            item = self._commands.pop(key, None)
            if item:
                acc = self._find_account(str(item.get("account", "") or ""))
                if acc:
                    with acc._lock:
                        self._runtime_state.finish_account_command(
                            acc,
                            str(item.get("command_id", "")),
                            ok=False,
                            error="expired",
                        )
                flog_kv(
                    "COMMAND",
                    "expired",
                    "warning",
                    key=key,
                    action=item.get("action", ""),
                    command_id=item.get("command_id", ""),
                    account=item.get("account", ""),
                )

    def _command_conflict_locked(self, key: str, action: str, account: str = "") -> Optional[Dict[str, Any]]:
        for existing_key, item in self._commands.items():
            if existing_key == key:
                continue
            return item
        return None

    def begin_command(self, key: str, action: str, account: str = "", ttl: float = 15.0) -> Tuple[bool, Dict[str, Any]]:
        with self._command_lock:
            self._cleanup_commands_locked()
            if self._shutting_down and action != "stop":
                rejected = {
                    "command_id": "",
                    "key": key,
                    "action": action,
                    "account": account,
                    "accepted": False,
                    "duplicate": False,
                    "rejected": True,
                    "msg": "Shutdown in progress",
                }
                flog_kv("COMMAND", "rejected", "warning", key=key, action=action, account=account, reason="shutdown_in_progress")
                return False, rejected
            existing = self._commands.get(key)
            if existing:
                flog_kv(
                    "COMMAND",
                    "duplicate",
                    "warning",
                    key=key,
                    action=action,
                    command_id=existing.get("command_id", ""),
                    account=account,
                )
                duplicate = dict(existing)
                duplicate["duplicate"] = True
                duplicate["accepted"] = False
                duplicate["msg"] = f"{action} already in progress"
                self._record_timeline("command_rejected", account, "warning", "duplicate_command", action=action, command_id=existing.get("command_id", ""))
                return False, duplicate
            conflict = self._command_conflict_locked(key, action, account)
            if conflict:
                rejected = dict(conflict)
                rejected["accepted"] = False
                rejected["duplicate"] = False
                rejected["rejected"] = True
                rejected["msg"] = f"{action} blocked by inflight {conflict.get('action', 'command')}"
                flog_kv(
                    "COMMAND",
                    "overlap_rejected",
                    "warning",
                    key=key,
                    action=action,
                    account=account,
                    blocked_by_key=conflict.get("key", ""),
                    blocked_by_action=conflict.get("action", ""),
                    blocked_by_command_id=conflict.get("command_id", ""),
                    command_generation=self._command_generation,
                    reason="command_inflight",
                )
                self._record_timeline("command_rejected", account, "warning", "command_inflight", action=action, blocked_by_action=conflict.get("action", ""))
                return False, rejected
            allowed, reject_reason, acc = self._command_capability(action, account)
            if not allowed:
                rejected = {
                    "command_id": "",
                    "key": key,
                    "action": action,
                    "account": account,
                    "accepted": False,
                    "duplicate": False,
                    "rejected": True,
                    "msg": reject_reason,
                }
                flog_kv(
                    "COMMAND",
                    "rejected",
                    "warning",
                    key=key,
                    action=action,
                    account=account,
                    reason=reject_reason,
                )
                self._record_timeline("command_rejected", account, "warning", reject_reason, action=action)
                return False, rejected
            self._command_seq += 1
            self._command_generation += 1
            command = {
                "command_id": f"{int(time.time() * 1000)}-{self._command_seq}",
                "key": key,
                "action": action,
                "account": account,
                "command_generation": self._command_generation,
                "started_at": time.time(),
                "expires_at": time.time() + max(1.0, float(ttl or 15.0)),
            }
            self._commands[key] = command
            if acc:
                with acc._lock:
                    account_generation = self._runtime_state.begin_account_command(acc, command)
                    command["account_command_generation"] = account_generation
            self._bump_status_revision()
            flog_kv(
                "COMMAND",
                "accepted",
                key=key,
                action=action,
                command_id=command["command_id"],
                account=account,
                command_generation=command["command_generation"],
                account_command_generation=command.get("account_command_generation", ""),
            )
            self._record_timeline("command_accepted", account, "info", action, action=action, command_id=command["command_id"], command_generation=command["command_generation"])
            return True, dict(command)

    def finish_command(self, key: str, command_id: str, ok: bool = True, error: str = ""):
        with self._command_lock:
            current = self._commands.get(key)
            if current and current.get("command_id") == command_id:
                self._commands.pop(key, None)
                acc = self._find_account(str(current.get("account", "") or ""))
                if acc:
                    with acc._lock:
                        self._runtime_state.finish_account_command(acc, command_id, ok=ok, error=error)
                self._bump_status_revision()
                flog_kv(
                    "COMMAND",
                    "finished",
                    key=key,
                    command_id=command_id,
                    ok=ok,
                    error=error,
                    action=current.get("action", ""),
                    account=current.get("account", ""),
                    command_generation=current.get("command_generation", ""),
                    account_command_generation=current.get("account_command_generation", ""),
                )
                self._record_timeline("command_finished", str(current.get("account", "") or ""), "info" if ok else "warning", error or "command_finished", action=current.get("action", ""), command_id=command_id, ok=ok)
            else:
                flog_kv(
                    "COMMAND",
                    "stale_work_rejected",
                    "warning",
                    key=key,
                    command_id=command_id,
                    current_command_id=current.get("command_id", "") if current else "",
                    ok=ok,
                    error=error,
                    reason="command_finish_mismatch",
                    command_generation=self._command_generation,
                    thread_name=threading.current_thread().name,
                )
                self._record_timeline("stale_work_rejected", "", "warning", "command_finish_mismatch", command_id=command_id, ok=ok, error=error)

    def _cancel_commands_for_shutdown(self) -> None:
        with self._command_lock:
            commands = []
            preserved: Dict[str, Dict[str, Any]] = {}
            for key, item in self._commands.items():
                if str(item.get("action", "")) == "stop":
                    preserved[key] = item
                else:
                    commands.append((key, item))
            self._commands = preserved
        for key, item in commands:
            acc = self._find_account(str(item.get("account", "") or ""))
            if acc:
                with acc._lock:
                    self._runtime_state.finish_account_command(
                        acc,
                        str(item.get("command_id", "") or ""),
                        ok=False,
                        error="shutdown",
                    )
            flog_kv(
                "COMMAND",
                "shutdown_cancelled",
                "warning",
                key=key,
                action=item.get("action", ""),
                command_id=item.get("command_id", ""),
                account=item.get("account", ""),
                reason="shutdown",
            )

    def command_inflight(self, key: str) -> Optional[Dict[str, Any]]:
        with self._command_lock:
            self._cleanup_commands_locked()
            item = self._commands.get(key)
            if not item:
                return None
            return {
                "command_id": item.get("command_id", ""),
                "action": item.get("action", ""),
                "account": item.get("account", ""),
                "command_generation": item.get("command_generation", 0),
                "account_command_generation": item.get("account_command_generation", 0),
                "age": round(max(0.0, time.time() - float(item.get("started_at") or time.time())), 2),
            }

    def _recovery_step_for_account(self, acc: Account, display_state: AccountState) -> Tuple[str, int, float]:
        reason_text = " ".join(
            str(value or "")
            for value in (
                acc.recovery_status,
                acc.last_recovery_reason,
                acc.last_crash_reason,
                acc.last_state_reason,
                acc.last_watchdog_classification,
                acc.liveness_state,
            )
        ).lower()
        state_name = display_state.name
        if state_name == "IN_GAME":
            return "Recovery Complete", 8, float(acc.in_game_since or acc.last_state_change_at or 0.0)
        if state_name == "COOLDOWN":
            return "Stabilizing", 7, float(acc.recovery_scheduled_at or acc.cooldown_until or acc.last_state_change_at or 0.0)
        if state_name == "VERIFY" or "verify" in reason_text:
            return "Verifying Session", 6, float(acc.last_state_change_at or acc.last_launch_at or 0.0)
        if "session_conflict" in reason_text or "273" in reason_text:
            return "Session Reconnect", 5, float(acc.recovery_scheduled_at or acc.last_recovery_at or 0.0)
        if "popup" in reason_text or "disconnect_dialog" in reason_text:
            return "Checking Disconnect", 4, float(acc.last_recovery_at or acc.last_state_change_at or 0.0)
        if "network_drop" in reason_text:
            return "Network Rejoin", 5, float(acc.recovery_scheduled_at or acc.last_recovery_at or 0.0)
        if "presence_limited" in reason_text:
            return "Presence Limited", 1, float(acc.last_recovery_at or acc.last_state_change_at or 0.0)
        if "connection_error" in reason_text or "visual_disconnect" in reason_text or "rejoin" in reason_text or state_name == "JOINING":
            return "Rejoining Server", 5, float(acc.recovery_scheduled_at or acc.last_recovery_at or 0.0)
        if state_name in {"LAUNCHING", "STARTING"} or "launch" in reason_text:
            return "Relaunching Roblox", 3, float(acc.last_launch_at or acc.last_state_change_at or 0.0)
        if "kill" in reason_text or "process" in reason_text:
            return "Killing Process", 2, float(acc.last_pid_change_at or acc.last_recovery_at or 0.0)
        if (
            (self._net_mon and self._net_mon.get_state() != NET_ONLINE)
            or "network" in reason_text
        ):
            return "Waiting Network", 1, float(acc.last_network_lost_at or acc.last_recovery_at or 0.0)
        if "disconnect" in reason_text or "reconnect" in reason_text:
            return "Rejoining Server", 5, float(acc.recovery_scheduled_at or acc.last_recovery_at or 0.0)
        if state_name in {"CRASH", "NETWORK_LOST", "QUEUED"} or acc.recovery_inflight:
            return "Detecting Disconnect", 0, float(acc.last_recovery_at or acc.last_crash_at or acc.last_state_change_at or 0.0)
        return "Idle", -1, float(acc.last_state_change_at or 0.0)

    def _initial_state_sync(self, state_mgr: Optional[StateManager] = None):
        if state_mgr is None:
            state_mgr = StateManager(self.bus)
        live_processes = ProcessManager.list_live_game_processes()
        if not live_processes:
            flog("[FARM] initial_state_sync: no live RobloxPlayerBeta.exe found")
            return

        claimed_pids = set()
        synced = 0

        for acc in self._accounts:
            with acc._lock:
                current_pid = acc.pid
                runtime_generation = acc.runtime_generation
            if current_pid and acc.bound_process_identity and ProcessManager.is_bound_game_alive(
                current_pid,
                owner_key=acc._config_username,
                expected_identity=acc.bound_process_identity,
            ):
                bind_result = ProcessManager.bind_account_process(
                    acc,
                    current_pid,
                    state_mgr,
                    reason="initial_state_sync_existing",
                    expected_identity=acc.bound_process_identity,
                    process_name=acc.bound_process_name or "RobloxPlayerBeta.exe",
                    min_ram_mb=0.0,
                    increment_generation=False,
                    expected_runtime_generation=runtime_generation,
                )
                if not bind_result.get("ok"):
                    flog_kv(
                        "FARM",
                        "initial_sync_existing_rejected",
                        "warning",
                        account=acc.display_name,
                        pid=current_pid,
                        reason=bind_result.get("reason", ""),
                    )
                    continue
                claimed_pids.add(current_pid)
                state_mgr.transition(acc, AccountState.IN_GAME, reason="initial_state_sync_existing", force=True)
                synced += 1

        candidates = [item for item in live_processes if item["pid"] not in claimed_pids]
        targets = [
            acc for acc in sorted(self._accounts, key=lambda a: int(a.priority or 50))
            if acc.desired_state == AccountState.IN_GAME and not acc.pid
        ]

        if candidates and len(targets) == 1:
            target = targets[0]
            with target._lock:
                runtime_generation = target.runtime_generation
            adopt = ProcessManager.safe_adopt_visible_process(
                target,
                state_mgr,
                accounts=self._accounts,
                reason="initial_state_sync_visible_adopt",
                expected_runtime_generation=runtime_generation,
            )
            if adopt.get("ok"):
                claimed_pids.add(int(adopt.get("pid") or 0))
                state_mgr.transition(target, AccountState.IN_GAME, reason="initial_state_sync_visible_adopt", force=True)
                synced += 1
                candidates = [item for item in candidates if int(item.get("pid") or 0) not in claimed_pids]

        if candidates:
            flog_kv(
                "FARM",
                "initial_sync_unclaimed_skipped",
                "warning",
                candidates=len(candidates),
                targets=len(targets),
                reason="unclaimed_processes_not_auto_bound",
            )

        remaining = len(live_processes) - len(claimed_pids)
        flog(
            f"[FARM] initial_state_sync complete: synced={synced} "
            f"live_processes={len(live_processes)} remaining_unclaimed={max(0, remaining)}"
        )

    def _preflight_cookie_blocks(self) -> Dict[str, str]:
        blocked: Dict[str, str] = {}
        if not self._recovery or not self._state_mgr:
            return blocked
        for acc in self._accounts:
            reason = account_launch_block_reason(acc)
            if reason:
                _set_account_cookie_block(acc, reason)
                self._recovery.fail_account(acc, "cookie_mismatch", reason)
                blocked[acc._config_username] = reason
                flog_kv("FARM", "account_preflight_blocked", "warning", account=acc.display_name, reason=reason)
                continue
            with acc._lock:
                if acc.state == AccountState.FAILED and acc.last_crash_reason == "cookie_mismatch":
                    _clear_account_cookie_block(acc)
                    self._runtime_state.set_recovery(acc, status="", reason="cookie_mismatch_cleared", inflight=False)
                    self._runtime_state.set_cooldown(acc, 0.0, reason="cookie_mismatch_cleared")
                    self._state_mgr.transition(acc, AccountState.IDLE, reason="cookie_mismatch_cleared", force=True)
        return blocked

    def start(self):
        if self.running:
            return

        cfg = self.cfg_mgr.snapshot()
        self._shutting_down = False
        self._stop = threading.Event()
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
        self.running = True
        self.start_ts = time.time()
        self._bump_status_revision()
        get_rt_monitor().start()

        for acc in self._accounts:
            with acc._lock:
                self._runtime_state.set_desired(
                    acc,
                    AccountState.IN_GAME,
                    reason="farm_start_desired",
                    increment_generation=False,
                )
                self._runtime_state.set_cooldown(acc, 0.0, reason="farm_start_clear_cooldown")
            if acc.vip_links:
                acc._vip_tracker = VipTracker(acc.vip_links)
                flog(f"[FARM] VipTracker initialized for {acc.display_name}")

        self._sync_accounts_from_ram(persist=True)

        self.cfg_mgr.restore_runtime(self._accounts)

        for acc in self._accounts:
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
                ProcessManager.evict_pid_cache(restored_pid)
                with acc._lock:
                    if acc.pid == restored_pid:
                        self._runtime_state.clear_process_binding(
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
                self._runtime_state.bump_runtime_generation(acc, "farm_start_epoch")
                acc.session_wait_started_at = 0.0
                acc.rapid_relaunch_count = 0
                acc.presence_rejoin_pending_clear = False
                acc.presence_rejoin_suppressed_until = 0.0
                acc.last_presence_rejoin_at = 0.0
                acc.presence_mismatch_since = 0.0
                acc.presence_mismatch_status = ""
                acc.presence_mismatch_reason = ""
                if acc.cooldown_until and acc.cooldown_until <= time.time():
                    self._runtime_state.set_cooldown(acc, 0.0, reason="expired_restored_cooldown")
                if acc.last_crash_reason == "max_fail":
                    acc.last_crash_reason = ""

        state_mgr = StateManager(self.bus)
        self._state_mgr = state_mgr
        self._net_mon = NetworkMonitor(
            bus=self.bus,
            interval=cfg.get("network_check_interval", 5),
            debounce=cfg.get("network_debounce", 3),
            stop=self._stop,
        )
        self._net_mon.start()
        time.sleep(0.5)
        self._initial_state_sync(state_mgr)

        queue = SmartQueue()
        self._queue = queue
        limiter = GlobalLaunchLimiter(
            interval=max(
                float(cfg.get("launch_rate_interval", 6) or 6),
                float(cfg.get("account_switch_cooldown", 10) or 10),
            )
        )
        launcher = LaunchController(
            limiter,
            state_mgr,
            self.bus,
            cfg,
            accounts=self._accounts,
            runtime_state=self._runtime_state,
            runtime_store=self._runtime_store,
            supervisor=self._supervisor,
        )
        self._recovery = RecoveryEngine(
            queue,
            state_mgr,
            self.bus,
            self._net_mon,
            self._stop,
            cfg,
            accounts=self._accounts,
            persist_callback=lambda: self.cfg_mgr.save_runtime(self._accounts),
        )
        blocked_accounts = self._preflight_cookie_blocks()

        self._workers = {}
        for acc in self._accounts:
            worker = AccountWorker(
                acc=acc,
                state_mgr=state_mgr,
                bus=self.bus,
                cfg=cfg,
                recovery=self._recovery,
                stop=self._stop,
                supervisor=self._supervisor,
                accounts=self._accounts,
            )
            self._workers[acc._config_username] = worker

        self._dispatcher = Dispatcher(
            queue=queue,
            launcher=launcher,
            state_mgr=state_mgr,
            bus=self.bus,
            workers=self._workers,
            recovery=self._recovery,
            net=self._net_mon,
            stop=self._stop,
            cfg=cfg,
            runtime_state=self._runtime_state,
            runtime_store=self._runtime_store,
            supervisor=self._supervisor,
        )
        self._dispatcher.start()
        self._maintenance = SystemMaintenance(
            self._accounts,
            self._workers,
            self._recovery,
            state_mgr,
            cfg,
            self._stop,
            supervisor=self._supervisor,
        )
        self._maintenance.start()

        for worker in self._workers.values():
            if worker.acc._config_username in blocked_accounts:
                continue
            worker.start()
            time.sleep(0.1)

        self._recovery.reconcile_all(self._accounts, trigger="farm_start")
        launchable_count = len(self._accounts) - len(blocked_accounts)
        flog(f"[FARM] Started {len(self._accounts)} accounts (launchable={launchable_count} blocked={len(blocked_accounts)})")
        message = f"Farm started - {launchable_count}/{len(self._accounts)} launchable"
        if blocked_accounts:
            message += f", {len(blocked_accounts)} blocked"
        self._push_event("system", message, severity="success" if launchable_count else "warning")

    def stop(self):
        if not self.running:
            return

        self._shutting_down = True
        self._stop.set()
        self.running = False
        self._bump_status_revision()
        if self._recovery:
            self._recovery.stop()
        if self._queue:
            self._queue.cancel_all("farm_stop")
        self._cancel_commands_for_shutdown()

        for acc in self._accounts:
            if acc.pid:
                ProcessManager.safe_kill_bound_process(
                    acc,
                    None,
                    reason="farm_stop",
                )
            with acc._lock:
                self._runtime_state.forced_reset(acc, desired=AccountState.IDLE, reason="farm_stop_reset")
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

        for worker in self._workers.values():
            worker.wake()
            worker.join(timeout=2.0)

        if self._dispatcher:
            self._dispatcher.join(timeout=2.0)
        if self._maintenance:
            self._maintenance.join(timeout=2.0)
        if self._net_mon:
            self._net_mon.join(timeout=2.0)
        self._state_mgr = None

        self.cfg_mgr.save_runtime(self._accounts)
        get_rt_monitor().stop()
        try:
            from roblox_hybrid import release_multi_roblox_guard

            release_multi_roblox_guard()
        except Exception as exc:
            flog_kv("MULTI_ROBLOX", "guard_stop_failed", "warning", error=exc)
        flog("[FARM] Stopped")
        self._push_event("system", "Farm stopped", severity="info")
        self._shutting_down = False

    def set_accounts(self, accounts: List[Account]):
        self._accounts = accounts
        self._sync_accounts_from_ram(persist=False)
        if self._recovery:
            self._recovery._accounts = self._accounts
        for acc in self._accounts:
            try:
                self._runtime_store.record_account_snapshot(acc._config_username, acc.runtime_snapshot())
            except Exception as e:
                flog_kv("RUNTIME", "store_snapshot_failed", "warning", account=acc.display_name, error=e)

    def apply_config_snapshot(self):
        cfg = self.cfg_mgr.snapshot()
        if self._recovery:
            self._recovery._cfg = cfg
            self._recovery._accounts = self._accounts
        if self._maintenance:
            self._maintenance._cfg = cfg
        for worker in self._workers.values():
            worker.cfg = cfg
        if self._dispatcher:
            self._dispatcher._cfg = cfg
            launcher = getattr(self._dispatcher, "_launcher", None)
            if launcher:
                launcher._cfg = cfg
                limiter = getattr(launcher, "_limiter", None)
                if limiter:
                    try:
                        limiter.interval = max(
                            float(cfg.get("queue_delay_seconds", cfg.get("launch_rate_interval", 6)) or 6),
                            float(cfg.get("account_switch_cooldown", 10) or 10),
                        )
                    except Exception:
                        pass
        self._bump_status_revision()

    def _sync_accounts_from_ram(self, persist: bool = False):
        return

    def force_rejoin(self, username: str):
        acc = self._find_account(username)
        if acc and self._recovery:
            routed = self._recovery.request_rejoin(acc, "force_rejoin")
            if not routed:
                return False, "Rejoin rejected"
            worker = self._workers.get(acc._config_username)
            if worker:
                worker.wake()
            self._push_event("rejoin", f"Force rejoin: {acc.display_name}", account=acc, severity="warn", reason="force_rejoin")
            return True, f"Rejoin: {username}"
        return False, "Account not running or recovery coordinator unavailable"

    def kill_account_pid(self, username: str, reason: str = "api_kill_pid") -> Tuple[bool, str]:
        acc = self._find_account(username)
        if not acc:
            return False, "Account not found"
        with acc._lock:
            pid = acc.pid
            identity = acc.bound_process_identity
            runtime_generation = acc.runtime_generation
        if not pid:
            return False, "No active PID"
        result = ProcessManager.safe_kill_bound_process(
            acc,
            self._runtime_state,
            reason=reason,
            expected_runtime_generation=runtime_generation,
        )
        killed = bool(result.get("killed"))
        self._bump_status_revision()
        flog_kv(
            "COMMAND",
            "kill_pid",
            account=acc.display_name,
            pid=pid,
            killed=killed,
            reason=reason,
            runtime_generation=acc.runtime_generation,
            recovery_generation=acc.recovery_generation,
            command_generation=acc.command_generation,
        )
        self._push_event("system", f"Kill PID requested: {acc.display_name} (PID {pid})", account=acc, severity="warn", reason=reason)
        return True, f"Killed PID for {username}" if killed else f"Released stale PID for {username}"

    def verify_account(self, username: str) -> Tuple[bool, str]:
        acc = self._find_account(username)
        if not acc:
            return False, "Account not found"
        now = time.time()
        with acc._lock:
            pid = acc.pid
            runtime_generation = acc.runtime_generation
            acc.manual_status = "finished"
            acc.finished_at = now
            acc.last_state_reason = "manual_verify_finished"
            acc.last_state_change_at = now
            self._runtime_state.set_desired(acc, AccountState.IDLE, reason="manual_verify_finished")
        killed = False
        if pid:
            result = ProcessManager.safe_kill_bound_process(
                acc,
                self._state_mgr or self._runtime_state,
                reason="manual_verify_finished",
                expected_runtime_generation=runtime_generation,
            )
            killed = bool(result.get("killed"))
        if self._state_mgr:
            self._state_mgr.transition(acc, AccountState.IDLE, reason="manual_verify_finished", force=True)
        else:
            with acc._lock:
                self._runtime_state.forced_reset(acc, desired=AccountState.IDLE, reason="manual_verify_finished")
        self.cfg_mgr.save_accounts(self._accounts)
        self._bump_status_revision()
        flog_kv("COMMAND", "verify_finished", account=acc.display_name, killed=killed, finished_at=f"{now:.3f}")
        self._push_event("system", f"Verified finished: {acc.display_name}", account=acc, severity="success", reason="manual_verify_finished")
        return True, f"Verified finished: {username}" + (" (PID killed)" if killed else "")

    def get_status(self) -> dict:
        from core import STATE_META

        uptime = int(time.time() - self.start_ts) if self.start_ts else 0
        h, r = divmod(uptime, 3600)
        m, s = divmod(r, 60)
        mon = get_rt_monitor()
        accounts_data = []
        cfg = self.cfg_mgr.snapshot()
        try:
            from roblox_hybrid import multi_roblox_guard_status

            multi_guard = multi_roblox_guard_status()
        except Exception as exc:
            multi_guard = {"state": "unknown", "pid": 0, "detail": str(exc), "last_failure": str(exc), "handle_names": []}
        ram_enabled = bool(cfg.get("use_ram_account_manager", False))
        ram_records: Dict[str, dict] = {}
        global_command = self.command_inflight("global")
        try:
            recent_runtime_events = self._runtime_store.list_recent_events(limit=100)
        except Exception as e:
            flog_kv("RUNTIME", "recent_events_failed", "warning", error=e)
            recent_runtime_events = []
        events_by_account: Dict[str, List[Dict[str, Any]]] = {}
        for event in recent_runtime_events:
            events_by_account.setdefault(str(event.get("account", "") or ""), []).append(event)

        if ram_enabled:
            ok, payload = RAMManager.get_accounts(cfg, include_cookies=False, force_refresh=False)
            if ok and isinstance(payload, list):
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    for key in ("Username", "username", "Alias", "alias", "Account"):
                        value = str(item.get(key, "") or "").strip().lower()
                        if value:
                            ram_records[value] = item

        presence_enabled = bool(cfg.get("presence_api_enabled", False))
        presence_user_ids = [_account_presence_user_id(acc) for acc in self._accounts]
        presence_result = PRESENCE_SERVICE.refresh(
            presence_user_ids,
            enabled=presence_enabled,
            poll_interval=float(cfg.get("presence_poll_interval_seconds", 30) or 30),
            cache_ttl=float(cfg.get("presence_cache_ttl_seconds", 30) or 30),
            force=False,
        )
        presence_by_user_id = presence_result.get("presences") if isinstance(presence_result.get("presences"), dict) else {}
        with self._command_lock:
            any_command_inflight = bool(self._commands)
        queue_snapshot = self._queue.snapshot() if self._queue else {
            "size": 0,
            "pending": 0,
            "busy": False,
            "closed": not self.running,
            "stale_rejections": 0,
            "oldest_age_seconds": 0,
            "entries": [],
        }

        for acc in self._accounts:
            runtime_snapshot = self._runtime_state.snapshot(acc)
            snapshot_pid = runtime_snapshot.get("pid")
            try:
                snapshot_pid = int(snapshot_pid) if snapshot_pid else None
            except (TypeError, ValueError):
                snapshot_pid = None
            snapshot_identity = str(runtime_snapshot.get("process_identity") or "")
            snapshot_public = str(runtime_snapshot.get("public_state") or getattr(getattr(acc, "state", None), "name", "IDLE"))
            display_state = AccountState.__members__.get(snapshot_public, AccountState.IDLE)
            pid_alive = bool(snapshot_pid and ProcessManager.is_bound_game_alive(
                snapshot_pid,
                owner_key=acc._config_username,
                expected_identity=snapshot_identity,
            ))
            if display_state == AccountState.IN_GAME and snapshot_pid and not pid_alive:
                display_state = AccountState.CRASH

            meta = STATE_META.get(display_state, {"label": display_state.name, "color": "#6b7280"})
            cpu = mon.get_cpu(snapshot_pid) if (snapshot_pid and pid_alive) else 0.0
            mem = mon.get_ram(snapshot_pid) if (snapshot_pid and pid_alive) else 0.0
            is_nr = bool(snapshot_pid and pid_alive and display_state == AccountState.IN_GAME and ProcessManager.is_not_responding(snapshot_pid))
            ram_online = None
            ram_detail = ""

            if ram_enabled:
                names = [
                    str(acc.username or "").strip().lower(),
                    str(acc.display_name or "").strip().lower(),
                    str(acc.alias or "").strip().lower(),
                ]
                record = None
                for name in names:
                    if name and name in ram_records:
                        record = ram_records[name]
                        break
                if record:
                    ram_online, ram_detail = RAMManager.resolve_record_online(record)

            vip_tracker_status = []
            if acc._vip_tracker:
                try:
                    vip_tracker_status = [
                        {**item, "link": redact_secret(item.get("link", ""))}
                        for item in acc._vip_tracker.status()
                        if isinstance(item, dict)
                    ]
                except Exception:
                    pass
            account_command = self.command_inflight(f"account:{acc._config_username}")
            recovery_step, recovery_step_index, recovery_step_started_at = self._recovery_step_for_account(acc, display_state)
            cooldown_until = float(acc.cooldown_until or 0.0)
            cooldown_left = max(0, int(cooldown_until - time.time()))
            blocked_reason = account_launch_block_reason(acc)
            if not blocked_reason and acc.last_crash_reason == "cookie_mismatch":
                blocked_reason = acc.manual_status or acc.last_error or AccountWorker.REASON_MESSAGES.get("cookie_mismatch", "cookie_mismatch")
            if not blocked_reason and acc.last_crash_reason == "multi_roblox_guard_failed":
                blocked_reason = acc.manual_status or acc.last_error or AccountWorker.REASON_MESSAGES.get("multi_roblox_guard_failed", "multi_roblox_guard_failed")
            launchable = not bool(blocked_reason)
            reported_liveness = acc.liveness_state or ""
            reported_liveness_score = round(float(acc.liveness_score or 0.0), 1)
            if not pid_alive:
                reported_liveness = "unbound" if snapshot_pid else "unknown"
                reported_liveness_score = 0.0
            presence_uid = _account_presence_user_id(acc)
            roblox_presence = dict(presence_by_user_id.get(presence_uid) or {})
            presence_age = roblox_presence.get("presence_age_seconds")
            presence_type_name = str(roblox_presence.get("presence_type_name") or "")

            account_payload = {
                "username": acc.username,
                "account_id": acc._config_username,
                "display": acc.display_name,
                "state": display_state.name,
                "public_state": snapshot_public,
                "desired_state": runtime_snapshot.get("desired_public_state", acc.desired_state.name),
                "state_label": meta["label"],
                "state_color": meta["color"],
                "description": acc.description,
                "manual_status": acc.manual_status,
                "finished_at": float(acc.finished_at or 0.0),
                "launchable": launchable,
                "blocked_reason": blocked_reason,
                "cookie_username": acc.cookie_username,
                "cookie_user_id": acc.cookie_user_id,
                "user_id": getattr(acc, "user_id", "") or acc.cookie_user_id,
                "cookie_mismatch": bool(acc.cookie_mismatch),
                "pid": snapshot_pid if pid_alive else None,
                "process_alive": pid_alive,
                "process_name": runtime_snapshot.get("process_name", acc.bound_process_name),
                "process_identity": snapshot_identity,
                "process_owner": ProcessManager.get_pid_owner(snapshot_pid) if snapshot_pid else "",
                "server_type": acc.server_type.value if acc.server_type else "UNKNOWN",
                "active_vip": redact_secret(acc.active_vip),
                "uptime": acc.uptime_str,
                "retry": acc.retry_count,
                "retry_count": acc.retry_count,
                "crash": acc.crash_count,
                "crash_count": acc.crash_count,
                "fail": acc.fail_count,
                "fail_count": acc.fail_count,
                "cpu": cpu,
                "mem_mb": mem,
                "is_vip": acc.is_vip,
                "session_valid": acc.session_valid,
                "last_crash_reason": acc.last_crash_reason,
                "last_state_reason": acc.last_state_reason,
                "last_state_change_at": float(acc.last_state_change_at or 0.0),
                "last_pid_change_at": float(acc.last_pid_change_at or 0.0),
                "vip_tracker": vip_tracker_status,
                "not_responding": is_nr,
                "ram_online": ram_online,
                "ram_detail": ram_detail,
                "cooldown_until": cooldown_until,
                "cooldown_left": cooldown_left,
                "pid_missing_for": max(0, int(time.time() - acc.pid_missing_since)) if acc.pid_missing_since else 0,
                "ownership_confidence": round(float(acc.ownership_confidence or 0.0), 1),
                "signal_confidence": round(float(acc.last_signal_confidence or 0.0), 1),
                "launch_strategy": acc.launch_strategy or "",
                "recovery_status": acc.recovery_status or "",
                "last_recovery_reason": acc.last_recovery_reason or "",
                "recovery_step": recovery_step,
                "recovery_step_index": recovery_step_index,
                "recovery_step_started_at": recovery_step_started_at,
                "watchdog_classification": acc.last_watchdog_classification or "",
                "liveness_state": reported_liveness,
                "liveness_score": reported_liveness_score,
                "presence_type": roblox_presence.get("presence_type"),
                "presence_type_name": presence_type_name,
                "presence_place_id": roblox_presence.get("presence_place_id", ""),
                "presence_root_place_id": roblox_presence.get("presence_root_place_id", ""),
                "presence_universe_id": roblox_presence.get("presence_universe_id", ""),
                "presence_game_id_present": bool(roblox_presence.get("presence_game_id_present", False)),
                "presence_last_location": roblox_presence.get("presence_last_location", ""),
                "presence_age_seconds": presence_age,
                "presence_limited": bool(roblox_presence.get("presence_limited", False)),
                "presence_disconnect_for": round(max(0.0, time.time() - float(acc.presence_mismatch_since or time.time())), 1) if acc.presence_mismatch_since else 0.0,
                "presence_disconnect_reason": acc.presence_mismatch_reason or "",
                "process_binding_status": acc.process_binding_status or "",
                "binding_decision": acc.binding_decision or runtime_snapshot.get("binding_decision", ""),
                "process_binding_confidence": round(float(acc.process_binding_confidence or runtime_snapshot.get("process_binding_confidence", 0.0) or 0.0), 1),
                "process_reject_reason": acc.process_reject_reason or runtime_snapshot.get("process_reject_reason", ""),
                "process_owner_claim": acc.process_owner_claim or runtime_snapshot.get("process_owner_claim", ""),
                "unmanaged_live_process_count": int(acc.unmanaged_live_process_count or runtime_snapshot.get("unmanaged_live_process_count", 0) or 0),
                "unmanaged_live_pids": list(acc.unmanaged_live_pids or runtime_snapshot.get("unmanaged_live_pids", []) or []),
                "adopt_candidate_pid": acc.adopt_candidate_pid or runtime_snapshot.get("adopt_candidate_pid"),
                "adopt_reject_reason": acc.adopt_reject_reason or runtime_snapshot.get("adopt_reject_reason", ""),
                "orphan_confidence": round(float(acc.orphan_confidence or 0.0), 1),
                "runtime_state": runtime_snapshot.get("runtime_state", ""),
                "runtime": runtime_snapshot,
                "runtime_generation": runtime_snapshot.get("runtime_generation", 0),
                "recovery_generation": runtime_snapshot.get("recovery_generation", acc.recovery_generation),
                "command_generation": runtime_snapshot.get("command_generation", acc.command_generation),
                "recovery_active": bool(runtime_snapshot.get("recovery_active", False)),
                "recovery_inflight": bool(runtime_snapshot.get("recovery_inflight", False)),
                "recovery_reason": runtime_snapshot.get("recovery_reason", acc.last_recovery_reason or ""),
                "bind_status": runtime_snapshot.get("bind_status", acc.process_binding_status or ""),
                "binding_status": runtime_snapshot.get("binding_status", acc.process_binding_status or ""),
                "last_heartbeat": runtime_snapshot.get("last_heartbeat", 0.0),
                "session_id": runtime_snapshot.get("session_id", ""),
                "launch_nonce": runtime_snapshot.get("launch_nonce", ""),
                "account_runtime_id": runtime_snapshot.get("account_runtime_id", ""),
                "rejoin_transaction_id": runtime_snapshot.get("rejoin_transaction_id", ""),
                "server_validation": runtime_snapshot.get("server_validation", acc.server_validation or ""),
                "destination_validation": runtime_snapshot.get("destination_validation", acc.destination_validation or acc.server_validation or ""),
                "scheduler_slot": runtime_snapshot.get("scheduler_slot", acc.scheduler_slot or ""),
                "supervisor_state": runtime_snapshot.get("supervisor_state", acc.supervisor_state or ""),
                "last_transaction_status": runtime_snapshot.get("last_transaction_status", acc.last_transaction_status or ""),
                "last_transaction_step": runtime_snapshot.get("last_transaction_step", acc.last_transaction_step or ""),
                "last_transaction_reason": runtime_snapshot.get("last_transaction_reason", acc.last_transaction_reason or ""),
                "last_transaction_started_at": runtime_snapshot.get("last_transaction_started_at", float(acc.last_transaction_started_at or acc.session_started_at or 0.0)),
                "last_transaction_completed_at": runtime_snapshot.get("last_transaction_completed_at", float(acc.last_transaction_completed_at or 0.0)),
                "last_transaction_failure_reason": runtime_snapshot.get("last_transaction_failure_reason", acc.last_transaction_failure_reason or ""),
                "session_started_at": float(acc.session_started_at or 0.0),
                "last_transaction_at": float(acc.last_transaction_at or 0.0),
                "launch_intent": dict(acc.launch_intent or {}),
                "launch_intent_summary": dict(acc.launch_intent_summary or runtime_snapshot.get("launch_intent_summary", {}) or {}),
                "recent_runtime_events": events_by_account.get(acc._config_username, [])[:20],
                "last_transition_at": runtime_snapshot.get("last_transition_at", 0.0),
                "last_transition_reason": runtime_snapshot.get("last_transition_reason", ""),
                "current_command": runtime_snapshot.get("current_command", ""),
                "command_inflight": account_command,
                "can_start": bool((not self.running) and not any_command_inflight),
                "can_stop": bool(self.running and not any_command_inflight),
                "can_rejoin": bool(self.running and not any_command_inflight and display_state != AccountState.FAILED),
                "can_kill": bool(pid_alive and snapshot_pid and not any_command_inflight),
            }
            account_payload["health_flags"] = account_health_flags(account_payload)
            accounts_data.append(account_payload)

        states = [a["state"] for a in accounts_data]
        blocked_count = sum(1 for a in accounts_data if a.get("blocked_reason"))
        launchable_count = sum(1 for a in accounts_data if a.get("launchable", True))
        with self._event_lock:
            total_rejoin = self._total_rejoin
            total_crash = self._total_crash
            event_log = list(self._event_log)
        with self._status_lock:
            status_revision = int(self._status_revision)
        runtime_health = build_runtime_health(accounts_data, queue_snapshot, recent_runtime_events)
        return {
            "running": self.running,
            "status_revision": status_revision,
            "status_updated_at": time.time(),
            "uptime": f"{h:02d}:{m:02d}:{s:02d}",
            "total_accounts": len(self._accounts),
            "launchable_count": launchable_count,
            "blocked_count": blocked_count,
            "in_game": states.count("IN_GAME"),
            "crash": states.count("CRASH"),
            "launching": states.count("LAUNCHING") + states.count("VERIFY"),
            "queued": states.count("QUEUED"),
            "failed": states.count("FAILED"),
            "total_rejoin": total_rejoin,
            "total_crash": total_crash,
            "network_state": self._net_mon.get_state() if self._net_mon else NET_ONLINE,
            "runtime_state": "RUNNING" if self.running else "STOPPED",
            "queue_duration_effective_seconds": self._maintenance._queue_duration_seconds() if self._maintenance else 0,
            "command_generation": self._command_generation,
            "command_inflight": global_command,
            "multi_roblox_guard_state": multi_guard.get("state", "unknown"),
            "multi_roblox_guard_pid": multi_guard.get("pid", 0),
            "multi_roblox_guard_detail": multi_guard.get("detail", ""),
            "last_multi_roblox_failure": multi_guard.get("last_failure", ""),
            "multi_roblox_guard_handles": multi_guard.get("handle_names", []),
            "presence_api": {
                "enabled": presence_enabled,
                "poll_interval_seconds": int(cfg.get("presence_poll_interval_seconds", 30) or 30),
                "cache_ttl_seconds": int(cfg.get("presence_cache_ttl_seconds", 30) or 30),
                "assist_rejoin_enabled": bool(cfg.get("presence_assist_rejoin_enabled", True)),
                **{k: v for k, v in presence_result.items() if k not in {"presences"}},
            },
            "queue_snapshot": queue_snapshot,
            "runtime_health": runtime_health,
            "can_start": bool((not self.running) and not any_command_inflight),
            "can_stop": bool(self.running and not any_command_inflight),
            "accounts": accounts_data,
            "event_log": event_log,
            "runtime_events": event_log,
            "recent_runtime_events": recent_runtime_events,
            "supervisor": self._supervisor.snapshot(),
        }

    def get_runtime_health(self) -> dict:
        status = self.get_status()
        return {
            "ok": True,
            "runtime_health": status.get("runtime_health", {}),
            "queue_snapshot": status.get("queue_snapshot", {}),
            "status_revision": status.get("status_revision", 0),
            "status_updated_at": status.get("status_updated_at", 0.0),
        }

    def get_runtime_events(self, account_id: str = "", limit: int = 100) -> dict:
        safe_limit = max(1, min(int(limit or 100), 500))
        try:
            events = self._runtime_store.list_recent_events(account_id=account_id, limit=safe_limit)
        except Exception as exc:
            flog_kv("RUNTIME", "runtime_events_query_failed", "warning", account=account_id, error=exc)
            events = []
        return {
            "ok": True,
            "account_id": account_id or "",
            "limit": safe_limit,
            "events": events,
        }

    def get_account(self, username: str) -> Optional[dict]:
        status = self.get_status()
        for item in status["accounts"]:
            if item["username"] == username:
                acc = next((x for x in self._accounts if x.username == username), None)
                if acc:
                    item["retry_history"] = acc.retry_history[-20:]
                    item["vip_links"] = [redact_secret(link) for link in list(acc.vip_links or [])]
                    item["place_id"] = acc.place_id
                    item["cookie_present"] = bool(acc.cookie)
                return item
        return None

    def _on_rejoin(self, account: Account, **_):
        with self._event_lock:
            self._total_rejoin += 1
        self._bump_status_revision()
        self._push_event("rejoin", f"Rejoin OK: {account.display_name} (server={account.server_type.value})", account=account, severity="success")
        account.retry_history.append({
            "ts": time.time(),
            "type": "success",
            "server": account.server_type.value,
        })

    def _on_crash(self, account: Account, reason: str = "", reason_msg: str = "", **_):
        with self._event_lock:
            self._total_crash += 1
        self._bump_status_revision()
        display_reason = reason_msg or reason
        self._push_event("crash", f"Lost: {account.display_name} - {display_reason}", account=account, severity="critical", reason=reason)
        account.retry_history.append({
            "ts": time.time(),
            "type": "crash",
            "reason": reason,
            "reason_msg": display_reason,
        })

    def _on_failed(self, account: Account, reason: str = "", reason_msg: str = "", **_):
        self._bump_status_revision()
        display_reason = reason_msg or reason
        self._push_event("error", f"Failed: {account.display_name} - {display_reason}", account=account, severity="critical", reason=reason)

    def _on_state_change(self, account: Account, old_state, new_state, **_):
        self._bump_status_revision()
        self._push_event(
            "state",
            f"{account.display_name}: {old_state.name} -> {new_state.name}",
            account=account,
            severity="info",
            reason=getattr(account, "last_state_reason", "") or "",
        )
        if new_state == AccountState.NETWORK_LOST and self._recovery:
            worker = self._workers.get(account._config_username)
            if worker:
                worker.wake()

    def _on_net_change(self, old: str, new: str, **_):
        self._bump_status_revision()
        icon = "OK" if new == "ONLINE" else "WARN"
        self._push_event(
            "network",
            f"{icon} Network: {old} -> {new}",
            severity="success" if new == "ONLINE" else "warn",
            reason=f"{old}->{new}",
        )
        if not self._recovery:
            return
        if new == "ONLINE":
            self._recovery.on_network_restored(self._accounts)
            for worker in self._workers.values():
                worker.wake()
        else:
            for acc in self._accounts:
                self._recovery.handle_runtime_signal(
                    acc,
                    "network_lost",
                    "network_drop",
                    payload={"trigger": f"net:{new.lower()}"},
                )

    def _push_event(self, kind: str, msg: str, account: Optional[Account] = None, severity: str = "info", reason: str = ""):
        if account:
            with account._lock:
                runtime_snapshot = account.runtime_snapshot()
                pid = account.pid
                account_key = account._config_username
                display = account.display_name
        else:
            runtime_snapshot = {}
            pid = None
            account_key = ""
            display = ""
        item = {
            "ts": time.time(),
            "kind": kind,
            "event_type": kind,
            "msg": msg,
            "severity": severity or "info",
            "reason": reason or "",
            "account": account_key,
            "display": display,
            "pid": pid,
            "session_id": runtime_snapshot.get("session_id", ""),
            "launch_nonce": runtime_snapshot.get("launch_nonce", ""),
            "account_runtime_id": runtime_snapshot.get("account_runtime_id", ""),
            "rejoin_transaction_id": runtime_snapshot.get("rejoin_transaction_id", ""),
            "server_validation": runtime_snapshot.get("server_validation", ""),
            "destination_validation": runtime_snapshot.get("destination_validation", ""),
            "binding_decision": runtime_snapshot.get("binding_decision", ""),
            "process_binding_confidence": runtime_snapshot.get("process_binding_confidence", 0.0),
            "process_reject_reason": runtime_snapshot.get("process_reject_reason", ""),
            "process_owner_claim": runtime_snapshot.get("process_owner_claim", ""),
            "supervisor_state": runtime_snapshot.get("supervisor_state", ""),
            "last_transaction_status": runtime_snapshot.get("last_transaction_status", ""),
            "runtime_state": runtime_snapshot.get("runtime_state", ""),
            "public_state": runtime_snapshot.get("public_state", ""),
            "runtime_generation": runtime_snapshot.get("runtime_generation", 0),
            "recovery_generation": runtime_snapshot.get("recovery_generation", 0),
            "command_generation": runtime_snapshot.get("command_generation", 0),
            "recovery_status": runtime_snapshot.get("recovery_status", ""),
            "command_inflight": runtime_snapshot.get("command_inflight"),
        }
        self._timeline.record(item, account_snapshot=runtime_snapshot if account else None, account_id=account_key)
        self._bump_status_revision()
        flog(f"[EVENT] [{kind}] {msg}")
