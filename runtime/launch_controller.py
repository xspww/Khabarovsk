from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from core import (
    Account,
    AccountState,
    EventBus,
    EventName,
    GlobalLaunchLimiter,
    StateManager,
    flog,
    flog_kv,
)
from domain.session_identity import build_launch_intent
from runtime.maintenance_performance import _apply_cpu_limiter_for_bound_process
from runtime.runtime_state_manager import RuntimeStateManager
from runtime.runtime_store import RuntimeStore
from runtime.supervisor_runtime import SupervisorRuntime
from services.process_service import ProcessManager, ProcessService


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

    def update_config(self, cfg: dict) -> None:
        with self._lock:
            self._cfg = cfg
            try:
                self._limiter.interval = max(
                    float(cfg.get("queue_delay_seconds", cfg.get("launch_rate_interval", 6)) or 6),
                    float(cfg.get("account_switch_cooldown", 10) or 10),
                )
            except Exception:
                pass

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
        bind_result = ProcessService.bind_account_process(
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
            expected_browser_tracker_id=acc.browser_tracker_id,
        )
        if not pid and (acc.bound_process_identity or ProcessManager.get_pid_owner(acc.pid) == acc._config_username):
            pid, name = ProcessManager.find_bound_game_process(
                preferred_pid=acc.pid,
                launched_after=None,
                owner_key=acc._config_username,
                expected_identity=acc.bound_process_identity,
                expected_browser_tracker_id=acc.browser_tracker_id,
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
        result = ProcessService.safe_adopt_visible_process(
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
                expected_browser_tracker_id=acc.browser_tracker_id,
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
                killed = ProcessService.kill_all_roblox_clients(
                    wait_seconds=4.0,
                    exclude_pids=protected_pids,
                    reason="prepare_direct_launch",
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

            skip_shared_cookie_inject = bool(multi_roblox and acc.cookie)
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
                kill_result = ProcessService.safe_kill_bound_process(
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
                ProcessService.cleanup_extra_launch_processes(
                    before_pids,
                    keep_pids=[acc.pid] if acc.pid else [],
                    launched_after=launch_ts,
                    reason="post_launch_existing_cleanup",
                    account=acc,
                )
                return True
            time.sleep(0.5)

        pid = ProcessManager.detect_new_pid(
            before_pids,
            timeout=verify_window,
            launched_after=launch_ts,
            created_after_slack=warmup_delay + 2.0,
            expected_browser_tracker_id=acc.browser_tracker_id,
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
                ProcessService.cleanup_extra_launch_processes(
                    before_pids,
                    keep_pids=[acc.pid] if acc.pid else [],
                    launched_after=launch_ts,
                    reason="verify_fallback_cleanup",
                    account=acc,
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

        bind_result = ProcessService.bind_account_process(
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
        extra_killed = ProcessService.cleanup_extra_launch_processes(
            before_pids,
            keep_pids=[pid],
            launched_after=launch_ts,
            reason="post_launch_detected_cleanup",
            account=acc,
        )
        if extra_killed:
            flog(f"[LAUNCH] Cleaned {extra_killed} leftover Roblox process(es) after bind for {acc.display_name}")
        if attempted_vip and acc._vip_tracker:
            acc._vip_tracker.mark_success(attempted_vip)
        self._bus.emit(EventName.LAUNCH_SUCCESS, account=acc, pid=pid)
        return True
