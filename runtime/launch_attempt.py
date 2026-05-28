from __future__ import annotations
import re
import threading
import time
from typing import Any, Tuple
from core import Account, AccountState, EventName, flog, flog_kv
from domain.session_identity import build_launch_intent
from runtime.maintenance_performance import _apply_cpu_limiter_for_bound_process
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
class LaunchAttempt:
    def __init__(self, controller: Any, account: Account, stop: threading.Event):
        self.controller = controller
        self.account = account
        self.stop = stop
    def run(self) -> bool:
        controller = self.controller
        acc = self.account
        stop = self.stop
        with controller._lock:
            with acc._lock:
                launch_guard = {
                    "runtime_generation": acc.runtime_generation,
                    "session_id": acc.session_id,
                    "launch_nonce": acc.launch_nonce,
                    "transaction_id": acc.rejoin_transaction_id,
                }
            multi_roblox = bool(controller._cfg.get("multi_roblox_enabled", True))
            ProcessManager.MULTI_ROBLOX_ENABLED = multi_roblox
            ProcessManager.GLOBAL_VIP_LINK = str(controller._cfg.get("game_private_server_url", "") or "").strip()
            ProcessManager.AUTO_CREATE_PRIVATE_SERVER_ENABLED = bool(controller._cfg.get("auto_create_private_server_enabled", False))
            ProcessManager.AUTO_CREATE_PRIVATE_SERVER_FREE_ONLY = bool(controller._cfg.get("auto_create_private_server_free_only", True))
            warmup_delay = max(0.0, float(controller._cfg.get("login_warmup_delay", 6) or 0))
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
                return controller._bind_live_game(
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
            controller._limiter.wait(stop)
            if stop.is_set():
                return False
            before_pids = ProcessManager.snapshot_pids()
            if acc.pid:
                stale_pid = acc.pid
                kill_result = ProcessService.safe_kill_bound_process(
                    acc,
                    controller._state_mgr,
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
                if controller._transaction_update(acc, status="failed", step="launch_failed", reason=str(safe_detail or "launch_failed"), server_validation="launch_failed", expected=launch_guard):
                    controller._bus.emit(EventName.LAUNCH_FAILED, account=acc, reason=safe_detail)
                if attempted_vip and acc._vip_tracker:
                    acc._vip_tracker.mark_crash(attempted_vip)
                return False
            flog(f"[LAUNCH] Sent for {acc.display_name} ({safe_detail[:80]})")
            with acc._lock:
                controller._runtime_state.update_launch_intent(
                    acc,
                    build_launch_intent(acc, reason="launch_sent"),
                    reason="launch_sent",
                    expected_generation=launch_guard["runtime_generation"],
                )
            if not controller._transaction_update(
                acc,
                status="launching",
                step="launch_sent",
                reason="launch_sent",
                server_validation="intent_recorded",
                expected=launch_guard,
            ):
                return False
            controller._state_mgr.transition(
                acc,
                AccountState.VERIFY,
                reason="launch_sent",
                expected_generation=launch_guard["runtime_generation"],
            )
            launch_ts = acc.last_launch_at or time.time()
        verify_window = controller._cfg.get("launch_verify_window", 25)
        quick_bind_deadline = time.time() + min(6.0, max(1.0, float(verify_window) / 3.0))
        try:
            quick_bind_poll = max(0.5, min(1.5, float(controller._cfg.get("launch_quick_bind_poll_seconds", 0.75) or 0.75)))
        except Exception:
            quick_bind_poll = 0.75
        try:
            visible_probe_interval = max(1.5, min(4.0, float(controller._cfg.get("launch_visible_probe_interval_seconds", 2.0) or 2.0)))
        except Exception:
            visible_probe_interval = 2.0
        next_visible_probe_at = time.time()
        while not stop.is_set() and time.time() < quick_bind_deadline:
            if controller._try_bind_any_live_game(acc, "post_launch_existing", launched_after=launch_ts, expected_runtime_generation=launch_guard["runtime_generation"]):
                ProcessService.cleanup_extra_launch_processes(
                    before_pids,
                    keep_pids=[acc.pid] if acc.pid else [],
                    launched_after=launch_ts,
                    reason="post_launch_existing_cleanup",
                    account=acc,
                )
                return True
            now = time.time()
            if now >= next_visible_probe_at:
                next_visible_probe_at = now + visible_probe_interval
                presence = controller._visible_process_presence()
            else:
                presence = {}
            if int(presence.get("visible_count") or 0) == 1:
                adopt = controller._safe_adopt_visible(
                    acc,
                    "post_launch_visible_adopt",
                    expected_runtime_generation=launch_guard["runtime_generation"],
                )
                if adopt.get("ok"):
                    controller._transaction_update(
                        acc,
                        status="process_bound",
                        step="adopted_existing_window",
                        reason="adopted_existing_window",
                        server_validation="process_verified_destination_pending",
                        expected=launch_guard,
                    )
                    return True
            stop.wait(timeout=quick_bind_poll)
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
            presence = controller._visible_process_presence()
            if presence.get("visible_count"):
                adopt = controller._safe_adopt_visible(
                    acc,
                    "verify_fallback_visible_adopt",
                    expected_runtime_generation=launch_guard["runtime_generation"],
                )
                if adopt.get("ok"):
                    controller._transaction_update(
                        acc,
                        status="process_bound",
                        step="adopted_existing_window",
                        reason="adopted_existing_window",
                        server_validation="process_verified_destination_pending",
                        expected=launch_guard,
                    )
                    return True
            if controller._try_bind_any_live_game(acc, "verify_fallback", launched_after=launch_ts, expected_runtime_generation=launch_guard["runtime_generation"]):
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
            if controller._transaction_update(acc, status="failed", step="failed", reason="PID not detected", server_validation="unverified_no_pid", expected=launch_guard):
                controller._bus.emit(EventName.LAUNCH_FAILED, account=acc, reason="PID not detected")
            return False
        presence = controller._visible_process_presence(exclude_pids=[pid])
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
            adopt = controller._safe_adopt_visible(
                acc,
                "transient_pid_visible_adopt",
                expected_runtime_generation=launch_guard["runtime_generation"],
            )
            if adopt.get("ok"):
                controller._transaction_update(
                    acc,
                    status="process_bound",
                    step="adopted_existing_window",
                    reason="adopted_existing_window",
                    server_validation="process_verified_destination_pending",
                    expected=launch_guard,
                )
                return True
            if controller._transaction_update(
                acc,
                status="failed",
                step="failed",
                reason=f"visible process adopt failed: {adopt.get('reason', 'unknown')}",
                server_validation="visible_process_unowned",
                expected=launch_guard,
            ):
                controller._bus.emit(EventName.LAUNCH_FAILED, account=acc, reason=f"visible process adopt failed: {adopt.get('reason', 'unknown')}")
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
                if controller._transaction_update(
                    acc,
                    status="failed",
                    step="failed",
                    reason="transient launch PID rejected: no window or stable runtime",
                    server_validation="transient_launch_pid_rejected",
                    expected=launch_guard,
                ):
                    controller._bus.emit(EventName.LAUNCH_FAILED, account=acc, reason="transient launch PID rejected")
                return False
        bind_result = ProcessService.bind_account_process(
            acc,
            pid,
            controller._state_mgr,
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
            if controller._transaction_update(acc, status="failed", step="failed", reason=f"PID rejected: {validation.get('reason', '')}", server_validation="process_rejected", expected=launch_guard):
                controller._bus.emit(EventName.LAUNCH_FAILED, account=acc, reason=f"PID rejected: {validation.get('reason', '')}")
            return False
        flog_kv(
            "LAUNCH",
            "pid_bound",
            account=acc.display_name,
            pid=pid,
            confidence=validation.get("confidence", 0.0),
        )
        controller._transaction_update(
            acc,
            status="process_bound",
            step="process_bound",
            reason="post_launch_detected",
            server_validation="process_verified_destination_pending",
            expected=launch_guard,
        )
        if controller._runtime_store:
            controller._runtime_store.record_process_binding(
                acc._config_username,
                pid,
                acc.bound_process_identity,
                acc.session_id,
                acc.rejoin_transaction_id,
                "verified",
                "post_launch_detected",
            )
        _apply_cpu_limiter_for_bound_process(controller._accounts, controller._cfg, "post_launch_detected", acc)
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
        controller._bus.emit(EventName.LAUNCH_SUCCESS, account=acc, pid=pid)
        return True
