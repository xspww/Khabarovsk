from __future__ import annotations

import time
from typing import Dict, List, Optional

from core import AccountState, flog, flog_kv
from services.process_service import ProcessManager, ProcessService
from services.captcha_guard import CAPTCHA_BLOCK_REASON, CAPTCHA_REASON, is_account_captcha_required, set_account_captcha_hold
from runtime.home_rejoin_guard import detect_home_rejoin_issue
from runtime.maintenance_performance import _apply_cpu_limiter_for_bound_process
from runtime.maintenance_watchdog_actions import (
    handle_disconnect_dialog_rejoin,
    handle_frozen_recovery_signal,
    handle_memory_pressure_rejoin,
    log_memory_pressure_hold,
)


class MaintenanceLivenessMixin:
    def _reconcile_duplicate_pid_claims(self):
        owners: Dict[int, List[Account]] = {}
        for acc in self._accounts:
            if acc.pid:
                owners.setdefault(int(acc.pid), []).append(acc)
        for pid, accounts in owners.items():
            if len(accounts) <= 1:
                continue
            ordered = sorted(
                accounts,
                key=lambda item: (float(item.last_reconcile_at or 0.0), float(item.last_pid_change_at or 0.0)),
                reverse=True,
            )
            keeper = ordered[0]
            flog(
                f"[MAINT] duplicate PID claim detected for {pid}: "
                f"{', '.join(a.display_name for a in ordered)} -> keep {keeper.display_name}",
                "warning",
            )
            for acc in ordered[1:]:
                if acc.pid == pid:
                    self._state_mgr.clear_process_binding(acc, reason="duplicate_pid_claim")
                worker = self._workers.get(acc._config_username)
                if worker:
                    worker.wake()

    def _recover_stale_joining_states(self):
        now = time.time()
        verify_window = max(15.0, float(self._cfg.get("launch_verify_window", 25) or 25) + 10.0)
        queue_window = max(30.0, float(self._cfg.get("queue_timeout", 90) or 90))
        for acc in self._accounts:
            with acc._lock:
                state = acc.state
                age = now - float(acc.last_state_change_at or now)
                pid = acc.pid
                identity = acc.bound_process_identity
                runtime_generation = acc.runtime_generation
                session_id = acc.session_id
                launch_nonce = acc.launch_nonce
                transaction_id = acc.rejoin_transaction_id
            if state in (AccountState.LAUNCHING, AccountState.VERIFY):
                if age < verify_window:
                    continue
                pid_live = bool(pid and ProcessManager.is_bound_game_alive(
                    pid,
                    owner_key=acc._config_username,
                    expected_identity=identity,
                    expected_browser_tracker_id=acc.browser_tracker_id,
                ))
                if pid_live:
                    self._runtime_signal(
                        acc,
                        "launch_success",
                        "stale_joining_recovered",
                        payload={"trigger": "stale_joining_recovered", "count_rejoin": False},
                        expected_runtime_generation=runtime_generation,
                        expected_session_id=session_id,
                        expected_launch_nonce=launch_nonce,
                        expected_transaction_id=transaction_id,
                    )
                    continue
                flog_kv(
                    "MAINT",
                    "stale_joining_recovery",
                    "warning",
                    account=acc.display_name,
                    state=state.name,
                    age=f"{age:.1f}",
                    pid=pid or "",
                )
                self._runtime_signal(
                    acc,
                    "launch_failure",
                    "launch_verify_timeout",
                    payload={"detail": "launch_verify_timeout"},
                    expected_runtime_generation=runtime_generation,
                    expected_session_id=session_id,
                    expected_launch_nonce=launch_nonce,
                    expected_transaction_id=transaction_id,
                )
            elif state == AccountState.QUEUED and age >= queue_window:
                flog_kv(
                    "MAINT",
                    "stale_queue_recovery",
                    "warning",
                    account=acc.display_name,
                    age=f"{age:.1f}",
                )
                self._runtime_evaluate(acc, trigger="queue_timeout")

    def _recover_failed_live_sessions(self):
        for acc in self._accounts:
            with acc._lock:
                state = acc.state
                desired = acc.desired_state
                last_launch_at = acc.last_launch_at
                current_pid = acc.pid
                runtime_generation = acc.runtime_generation
                expected_identity = acc.bound_process_identity
            if state != AccountState.FAILED or desired != AccountState.IN_GAME:
                continue

            if not current_pid or not expected_identity:
                live = ProcessManager.list_live_game_processes(launched_after=last_launch_at)
                if live:
                    flog_kv(
                        "MAINT",
                        "failed_live_rebind_skipped",
                        "warning",
                        account=acc.display_name,
                        candidates=len(live),
                        reason="missing_persisted_pid_identity",
                    )
                continue

            if any(other is not acc and other.pid == current_pid for other in self._accounts):
                flog_kv(
                    "MAINT",
                    "failed_live_rebind_skipped",
                    "warning",
                    account=acc.display_name,
                    pid=current_pid,
                    reason="pid_claimed_by_other_account",
                )
                continue

            bind_result = ProcessService.bind_account_process(
                acc,
                current_pid,
                self._state_mgr,
                reason="failed_live_session_rebind",
                expected_identity=expected_identity,
                launched_after=None,
                process_name=acc.bound_process_name or "RobloxPlayerBeta.exe",
                min_ram_mb=0.0,
                expected_runtime_generation=runtime_generation,
            )
            if not bind_result.get("ok"):
                flog_kv(
                    "MAINT",
                    "failed_live_rebind_rejected",
                    "warning",
                    account=acc.display_name,
                    pid=current_pid,
                    reason=bind_result.get("reason", ""),
                    previous_pid=current_pid or "",
                )
                continue
            validation = bind_result.get("validation") or {}

            with acc._lock:
                acc.pid_missing_since = 0.0
                acc.liveness_state = "alive"
                acc.liveness_score = max(float(acc.liveness_score or 0.0), 6.0)
                acc.last_watchdog_classification = "alive"
                acc.last_activity_at = time.time()
                acc.last_activity_reason = "failed_live_session_rebind"
            flog_kv(
                "MAINT",
                "failed_live_session_recovered",
                account=acc.display_name,
                pid=current_pid,
                previous_pid=current_pid or "",
                confidence=validation.get("confidence", 0.0),
            )
            _apply_cpu_limiter_for_bound_process(self._accounts, self._cfg, "failed_live_session_rebind", acc)
            with acc._lock:
                post_bind_generation = acc.runtime_generation
                session_id = acc.session_id
                launch_nonce = acc.launch_nonce
                transaction_id = acc.rejoin_transaction_id
            self._runtime_signal(
                acc,
                "launch_success",
                "failed_live_session_rebind",
                payload={"trigger": "failed_live_session_rebind", "count_rejoin": False},
                expected_runtime_generation=post_bind_generation,
                expected_session_id=session_id,
                expected_launch_nonce=launch_nonce,
                expected_transaction_id=transaction_id,
            )

    def _popup_periodic_scan_batch(self, now: float, candidate_keys: list, interval: float, max_parallel: int) -> set:
        if not candidate_keys:
            self._popup_scan_cursor = 0
            return set()

        try:
            cursor = int(getattr(self, "_popup_scan_cursor", 0) or 0)
        except Exception:
            cursor = 0
        cursor %= len(candidate_keys)

        try:
            last_batch_at = float(getattr(self, "_last_popup_batch_at", 0.0) or 0.0)
        except Exception:
            last_batch_at = 0.0
        if last_batch_at and (now - last_batch_at) < interval:
            return set()

        try:
            count = max(1, int(max_parallel or 1))
        except Exception:
            count = 1
        count = min(count, len(candidate_keys))

        selected = [candidate_keys[(cursor + offset) % len(candidate_keys)] for offset in range(count)]
        self._popup_scan_cursor = (cursor + count) % len(candidate_keys)
        self._last_popup_batch_at = now

        popup_scan_at = getattr(self, "_last_popup_scan_at", None)
        if popup_scan_at is None:
            popup_scan_at = {}
            self._last_popup_scan_at = popup_scan_at
        for key in selected:
            popup_scan_at[key] = now
        return set(selected)

    def _scan_liveness_watchdog(self):
        if not self._cfg.get("watchdog_enabled", True):
            return
        now = time.time()
        hold_sec = max(5.0, float(self._cfg.get("watchdog_hold_time", 60) or 60))
        activity_timeout = max(hold_sec, float(self._cfg.get("watchdog_activity_timeout", 180) or 180))
        loading_grace = max(30.0, float(self._cfg.get("watchdog_loading_grace", 90) or 90))
        cpu_low = float(self._cfg.get("watchdog_cpu_low", 0.9) or 0.9)
        startup_grace = max(0.0, float(self._cfg.get("popup_startup_grace_seconds", 8) or 8))
        popup_scan_interval = max(5.0, float(self._cfg.get("popup_scan_interval_seconds", 30.0) or 30.0))
        try:
            popup_scan_max_parallel = max(1, int(float(self._cfg.get("popup_scan_max_parallel", 2) or 2)))
        except Exception:
            popup_scan_max_parallel = 2
        popup_enabled = bool(self._cfg.get("popup_disconnected_enabled", True))
        memory_guard_enabled = bool(self._cfg.get("roblox_memory_guard_enabled", True))
        try:
            memory_guard_mb = max(512.0, float(self._cfg.get("roblox_memory_guard_mb", 6144.0) or 6144.0))
        except Exception:
            memory_guard_mb = 6144.0
        try:
            memory_guard_hold = max(5.0, float(self._cfg.get("roblox_memory_guard_hold_seconds", 30.0) or 30.0))
        except Exception:
            memory_guard_hold = 30.0
        net_online = self._recovery._net.is_online()
        popup_scan_at = getattr(self, "_last_popup_scan_at", None)
        if popup_scan_at is None:
            popup_scan_at = {}
            self._last_popup_scan_at = popup_scan_at
        popup_batch_keys = set()
        if popup_enabled:
            popup_candidates = []
            for candidate in self._accounts:
                with candidate._lock:
                    if candidate.state != AccountState.IN_GAME:
                        continue
                    if not candidate.pid:
                        continue
                    candidate_in_game_for = now - (candidate.in_game_since or now)
                if candidate_in_game_for >= startup_grace:
                    popup_candidates.append(candidate._config_username)
            popup_batch_keys = self._popup_periodic_scan_batch(
                now,
                popup_candidates,
                popup_scan_interval,
                popup_scan_max_parallel,
            )

        for acc in self._accounts:
            with acc._lock:
                if acc.state != AccountState.IN_GAME:
                    acc.liveness_suspect_since = 0.0
                    continue
                pid = acc.pid
                previous_cpu = acc.last_activity_cpu
                previous_ram = acc.last_activity_ram_mb
                in_game_for = now - (acc.in_game_since or now)
                last_activity = acc.last_activity_at or acc.in_game_since or now
                recovery_inflight = acc.recovery_inflight
                old_state = acc.liveness_state

            worker = self._workers.get(acc._config_username)
            if not pid:
                if worker:
                    worker.handle_missing_bound_process("maintenance_pid_missing")
                continue
            if in_game_for < startup_grace and not recovery_inflight:
                continue

            popup_key = acc._config_username
            popup_periodic_allowed = bool(popup_enabled and popup_key in popup_batch_keys)
            inspect_ui = popup_enabled and (
                popup_periodic_allowed
                or old_state in {"suspect_frozen", "frozen", "reconnecting", "teleporting"}
            )
            liveness = ProcessManager.assess_liveness(
                pid,
                previous_cpu=previous_cpu,
                previous_ram_mb=previous_ram,
                net_online=net_online,
                recovery_inflight=recovery_inflight,
                in_game_for=in_game_for,
                loading_grace=loading_grace,
                cpu_threshold=cpu_low,
                inspect_ui=inspect_ui,
            )
            state = str(liveness.get("state") or "unknown")
            score = float(liveness.get("score") or 0.0)
            validation = liveness.get("validation") or {}
            reason_key = str(liveness.get("reason_key") or "watchdog_timeout")
            dialog = liveness.get("dialog") or {}
            log_evidence = liveness.get("log_evidence") or {}
            cpu = float(validation.get("cpu") or 0.0)
            ram = float(validation.get("ram_mb") or 0.0)
            windows = int(validation.get("windows") or 0)
            if log_evidence.get("matched"):
                flog_kv(
                    "WATCHDOG",
                    "roblox_log_evidence",
                    account=acc.display_name,
                    pid=pid,
                    error_code=log_evidence.get("error_code", ""),
                    confidence=log_evidence.get("confidence", 0.0),
                    source=log_evidence.get("source", "roblox_log"),
                )

            if state == "captcha" or str(dialog.get("reason_key") or "") == CAPTCHA_REASON:
                detail = str(dialog.get("detail") or "").strip() or "Roblox Security verification CAPTCHA visible"
                with acc._lock:
                    captcha_pid = acc.pid
                    captcha_runtime_generation = acc.runtime_generation
                if not is_account_captcha_required(acc):
                    flog_kv(
                        "WATCHDOG",
                        "captcha_dialog_hold",
                        "warning",
                        account=acc.display_name,
                        pid=pid,
                        confidence=f"{float(dialog.get('popup_confidence', dialog.get('confidence', 0.0)) or 0.0):.2f}",
                        source=dialog.get("evidence_source", ""),
                        detail=detail,
                    )
                set_account_captcha_hold(acc, detail, source="watchdog_popup", runtime_writer=self._state_mgr)
                if captcha_pid and hasattr(self._state_mgr, "clear_process_binding"):
                    kill_result = ProcessService.safe_kill_bound_process(
                        acc,
                        self._state_mgr,
                        reason="captcha_hold",
                        expected_runtime_generation=captcha_runtime_generation,
                        increment_generation=False,
                    )
                    flog_kv(
                        "CAPTCHA",
                        "account_process_closed",
                        "warning",
                        account=acc.display_name,
                        pid=captcha_pid,
                        killed=bool(kill_result.get("killed")),
                        kill_reason=kill_result.get("reason", ""),
                    )
                else:
                    flog_kv(
                        "CAPTCHA",
                        "account_process_close_skipped",
                        "warning",
                        account=acc.display_name,
                        pid=captcha_pid or "",
                        reason="missing_bound_pid" if not captcha_pid else "state_manager_unavailable",
                    )
                if hasattr(self._recovery, "fail_account"):
                    self._recovery.fail_account(acc, CAPTCHA_REASON, CAPTCHA_BLOCK_REASON)
                continue

            if state == "missing":
                if worker:
                    worker.handle_missing_bound_process("maintenance_pid_missing")
                continue

            home_issue = detect_home_rejoin_issue(acc, self._cfg, now, in_game_for)
            if home_issue and not recovery_inflight:
                reason_key = str(home_issue.get("reason_key") or "home_screen_stuck")
                try:
                    home_hold_sec = max(1.0, float(self._cfg.get("home_rejoin_hold_seconds", 5) or 5))
                except Exception:
                    home_hold_sec = 5.0
                with acc._lock:
                    if acc.last_watchdog_classification != "home_screen_stuck" or not acc.liveness_suspect_since:
                        acc.liveness_suspect_since = now
                        home_suspect_for = 0.0
                    else:
                        home_suspect_for = max(0.0, now - acc.liveness_suspect_since)
                    runtime_generation = acc.runtime_generation
                    session_id = acc.session_id
                    launch_nonce = acc.launch_nonce
                    transaction_id = acc.rejoin_transaction_id
                    acc.liveness_state = "home_screen_stuck"
                    acc.last_watchdog_classification = "home_screen_stuck"
                    acc.last_activity_reason = f"home_guard:{reason_key}"
                    acc.sync_runtime("home_screen_stuck")
                self._set_recovery_status(acc, status="home_screen_stuck", reason=reason_key, inflight=False)
                if home_suspect_for < home_hold_sec:
                    flog_kv(
                        "WATCHDOG",
                        "home_screen_suspect",
                        "warning",
                        account=acc.display_name,
                        pid=pid,
                        reason=reason_key,
                        suspect=f"{home_suspect_for:.1f}",
                        hold=f"{home_hold_sec:.1f}",
                        detail=home_issue.get("detail", ""),
                        observed_place_id=home_issue.get("observed_place_id", ""),
                        observed_job_id=home_issue.get("observed_job_id", ""),
                    )
                    continue
                flog_kv(
                    "WATCHDOG",
                    "home_screen_rejoin_signal",
                    "warning",
                    account=acc.display_name,
                    pid=pid,
                    reason=reason_key,
                    suspect=f"{home_suspect_for:.1f}",
                    detail=home_issue.get("detail", ""),
                    launch_age=f"{float(home_issue.get('launch_age') or 0.0):.1f}",
                    observed_place_id=home_issue.get("observed_place_id", ""),
                    observed_job_id=home_issue.get("observed_job_id", ""),
                    runtime_generation=runtime_generation,
                    session_id=session_id,
                    transaction_id=transaction_id,
                )
                with acc._lock:
                    acc.liveness_suspect_since = 0.0
                self._runtime_signal(
                    acc,
                    "loading_freeze",
                    reason_key,
                    payload={
                        "trigger": "home_screen_guard",
                        "detail": f"PID={pid} {home_issue.get('detail', '')}",
                        "reason_msg": f"PID={pid} {home_issue.get('detail', '')}",
                    },
                    expected_runtime_generation=runtime_generation,
                    expected_session_id=session_id,
                    expected_launch_nonce=launch_nonce,
                    expected_transaction_id=transaction_id,
                )
                continue

            inactive_for = max(0.0, now - last_activity)
            dialog_rejoin: Optional[Dict[str, Any]] = None
            memory_pressure_hold: Optional[Dict[str, Any]] = None
            memory_pressure_rejoin: Optional[Dict[str, Any]] = None
            with acc._lock:
                if state != acc.liveness_state:
                    flog_kv(
                        "WATCHDOG",
                        "liveness_change",
                        account=acc.display_name,
                        pid=pid,
                        old=acc.liveness_state,
                        new=state,
                        score=f"{score:.1f}",
                        cpu=f"{cpu:.2f}",
                        ram=f"{ram:.1f}",
                        windows=windows,
                    )
                acc.liveness_state = state
                acc.liveness_score = score
                acc.last_watchdog_classification = state
                acc.last_activity_cpu = cpu
                acc.last_activity_ram_mb = ram

                if memory_guard_enabled and ram >= memory_guard_mb and not recovery_inflight:
                    if acc.resource_pressure_reason != "process_memory_pressure" or not acc.resource_pressure_since:
                        acc.resource_pressure_since = now
                        memory_high_for = 0.0
                    else:
                        memory_high_for = max(0.0, now - acc.resource_pressure_since)
                    acc.resource_pressure_reason = "process_memory_pressure"
                    acc.liveness_state = "memory_pressure"
                    acc.last_watchdog_classification = "memory_pressure"
                    acc.last_activity_reason = f"resource:memory_pressure:{ram:.1f}MB"
                    pressure_payload = {
                        "runtime_generation": acc.runtime_generation,
                        "session_id": acc.session_id,
                        "launch_nonce": acc.launch_nonce,
                        "transaction_id": acc.rejoin_transaction_id,
                        "ram_mb": ram,
                        "limit_mb": memory_guard_mb,
                        "high_for": memory_high_for,
                    }
                    if memory_high_for >= memory_guard_hold:
                        acc.resource_pressure_since = 0.0
                        memory_pressure_rejoin = pressure_payload
                    else:
                        memory_pressure_hold = pressure_payload
                elif not memory_guard_enabled or ram < memory_guard_mb:
                    acc.resource_pressure_since = 0.0
                    acc.resource_pressure_reason = ""

                if memory_pressure_hold or memory_pressure_rejoin:
                    pass
                elif state in {"alive", "idle"} and score >= 4.0:
                    if acc.recovery_status in {"checking_disconnect", "disconnect_detected"} and not acc.recovery_inflight:
                        self._set_recovery_status(acc, status="in_game", reason="liveness_alive_clear_disconnect_check", inflight=False)
                    acc.last_activity_at = now
                    acc.last_activity_reason = f"liveness:{state}"
                    acc.liveness_suspect_since = 0.0
                    continue
                if state in {"loading", "reconnecting", "teleporting"}:
                    if state == "reconnecting" and popup_enabled and dialog.get("matched") and dialog.get("recovery_allowed") and str(dialog.get("action") or "rejoin") in {"rejoin", "conditional_rejoin"}:
                        error_code = str(dialog.get("error_code") or "")
                        dialog_hold = 1.0 if error_code in {"267", "268", "273", "277", "279"} else max(1.0, float(self._cfg.get("connection_error_hold_time", 3) or 3))
                        if not acc.liveness_suspect_since:
                            acc.liveness_suspect_since = now
                            reconnecting_for = 0.0
                        else:
                            reconnecting_for = now - acc.liveness_suspect_since
                        acc.last_watchdog_classification = "disconnect_dialog_hold"
                        acc.last_activity_reason = f"dialog:{reason_key}"
                        if reconnecting_for >= dialog_hold and not recovery_inflight:
                            dialog_rejoin = {
                                "runtime_generation": acc.runtime_generation,
                                "session_id": acc.session_id,
                                "launch_nonce": acc.launch_nonce,
                                "transaction_id": acc.rejoin_transaction_id,
                                "reason_key": reason_key or str(dialog.get("reason_key") or "connection_error"),
                                "detail": str(dialog.get("detail") or ""),
                                "error_code": str(dialog.get("error_code") or ""),
                                "action": str(dialog.get("action") or "rejoin"),
                                "popup_confidence": float(dialog.get("popup_confidence", dialog.get("confidence", 0.0)) or 0.0),
                                "disconnect_category": str(dialog.get("disconnect_category") or ""),
                                "visual_disconnect": bool(dialog.get("visual_disconnect", False)),
                                "evidence_source": str(dialog.get("evidence_source") or ""),
                                "visual_evidence_source": str(dialog.get("visual_evidence_source") or ""),
                                "reconnecting_for": reconnecting_for,
                            }
                            acc.liveness_suspect_since = 0.0
                            acc.last_watchdog_classification = "disconnect_dialog_rejoin"
                            acc.liveness_state = "reconnecting"
                        else:
                            self._set_recovery_status(
                                acc,
                                status="disconnect_detected",
                                reason=reason_key or str(dialog.get("reason_key") or "connection_error"),
                                inflight=False,
                            )
                            self._state_mgr.set_binding_status(acc, "verified", reason=f"liveness:{state}")
                            continue
                    else:
                        acc.liveness_suspect_since = 0.0
                        self._state_mgr.set_binding_status(acc, "verified", reason=f"liveness:{state}")
                        continue
                    self._state_mgr.set_binding_status(acc, "verified", reason=f"liveness:{state}")
                if recovery_inflight:
                    acc.liveness_suspect_since = 0.0
                    continue
                if not acc.liveness_suspect_since:
                    acc.liveness_suspect_since = now
                    suspect_for = 0.0
                else:
                    suspect_for = now - acc.liveness_suspect_since

            if memory_pressure_rejoin:
                handle_memory_pressure_rejoin(self, acc, pid, memory_pressure_rejoin)
                continue

            if memory_pressure_hold:
                log_memory_pressure_hold(acc, pid, memory_pressure_hold, memory_guard_hold)
                continue

            if dialog_rejoin:
                handle_disconnect_dialog_rejoin(self, acc, pid, dialog_rejoin)
                continue

            if inactive_for < activity_timeout or suspect_for < hold_sec:
                continue

            handle_frozen_recovery_signal(
                self,
                acc,
                worker,
                pid,
                reason_key,
                state,
                score,
                inactive_for,
                suspect_for,
                cpu,
                ram,
                windows,
            )
