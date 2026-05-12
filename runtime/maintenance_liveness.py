from __future__ import annotations

import time

from core import AccountState, flog, flog_kv
from services.process_service import ProcessManager
from runtime.maintenance_performance import _apply_cpu_limiter_for_bound_process


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
                    self._recovery.handle_runtime_signal(
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
                self._recovery.handle_runtime_signal(
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
                self._recovery.request_evaluate(acc, trigger="queue_timeout")

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

            bind_result = ProcessManager.bind_account_process(
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
            self._recovery.handle_runtime_signal(
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
                    if not candidate.pid or candidate.recovery_inflight:
                        continue
                    candidate_in_game_for = now - (candidate.in_game_since or now)
                    candidate_presence_mismatch = bool(candidate.presence_mismatch_since)
                if candidate_in_game_for >= startup_grace or candidate_presence_mismatch:
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
                presence_mismatch_active = bool(acc.presence_mismatch_since)

            worker = self._workers.get(acc._config_username)
            if not pid:
                if worker:
                    worker.handle_missing_bound_process("maintenance_pid_missing")
                continue
            if in_game_for < startup_grace and not presence_mismatch_active and not recovery_inflight:
                continue

            popup_key = acc._config_username
            popup_periodic_allowed = bool(popup_enabled and popup_key in popup_batch_keys)
            inspect_ui = popup_enabled and (
                presence_mismatch_active
                or popup_periodic_allowed
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
                presence_mismatch=presence_mismatch_active,
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

            if state == "missing":
                if worker:
                    worker.handle_missing_bound_process("maintenance_pid_missing")
                continue

            inactive_for = max(0.0, now - last_activity)
            dialog_rejoin: Optional[Dict[str, Any]] = None
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

                if state in {"alive", "idle"} and score >= 4.0:
                    if self._handle_presence_disconnect_assist(
                        acc,
                        worker,
                        now,
                        int(pid or 0),
                        in_game_for,
                        loading_grace,
                        allow_rejoin=False,
                    ):
                        continue
                    acc.last_activity_at = now
                    acc.last_activity_reason = f"liveness:{state}"
                    acc.liveness_suspect_since = 0.0
                    continue
                if state in {"loading", "reconnecting", "teleporting"}:
                    if state == "reconnecting" and popup_enabled and dialog.get("matched") and dialog.get("recovery_allowed") and str(dialog.get("action") or "rejoin") in {"rejoin", "conditional_rejoin"}:
                        dialog_hold = max(1.0, float(self._cfg.get("connection_error_hold_time", 3) or 3))
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
                            acc.presence_rejoin_suppressed_until = 0.0
                            acc.presence_rejoin_pending_clear = False
                            acc.last_watchdog_classification = "disconnect_dialog_rejoin"
                            acc.liveness_state = "reconnecting"
                        else:
                            runtime_state = getattr(self, "_runtime_state", None) or getattr(self._recovery, "_runtime_state", None)
                            if runtime_state:
                                runtime_state.set_recovery(
                                    acc,
                                    status="checking_disconnect",
                                    reason=reason_key or str(dialog.get("reason_key") or "connection_error"),
                                    inflight=False,
                                )
                            self._state_mgr.set_binding_status(acc, "verified", reason=f"liveness:{state}")
                            continue
                    else:
                        self._reset_presence_mismatch(acc, f"liveness:{state}")
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

            if dialog_rejoin:
                pid_was = pid
                reason_key = str(dialog_rejoin.get("reason_key") or "connection_error")
                detail = str(dialog_rejoin.get("detail") or "")
                error_code = str(dialog_rejoin.get("error_code") or "")
                flog_kv(
                    "WATCHDOG",
                    "disconnect_dialog_rejoin_signal",
                    "warning",
                    account=acc.display_name,
                    pid=pid_was,
                    reason=reason_key,
                    error_code=error_code,
                    confidence=f"{float(dialog_rejoin.get('popup_confidence') or 0.0):.2f}",
                    source=dialog_rejoin.get("evidence_source", ""),
                    visual_source=dialog_rejoin.get("visual_evidence_source", ""),
                    detail=detail,
                    reconnecting_for=f"{float(dialog_rejoin.get('reconnecting_for') or 0.0):.1f}",
                    runtime_generation=dialog_rejoin.get("runtime_generation"),
                    session_id=dialog_rejoin.get("session_id"),
                    transaction_id=dialog_rejoin.get("transaction_id"),
                )
                self._recovery.handle_runtime_signal(
                    acc,
                    "disconnect_detected",
                    reason_key,
                    payload={
                        "trigger": "watchdog_popup",
                        "detail": f"PID={pid_was} UI={detail}",
                        "reason_msg": f"PID={pid_was} UI={detail}",
                        "popup_code": error_code,
                        "popup_confidence": dialog_rejoin.get("popup_confidence", 0.0),
                        "disconnect_category": dialog_rejoin.get("disconnect_category", ""),
                        "visual_disconnect": bool(dialog_rejoin.get("visual_disconnect", False)),
                        "evidence_source": dialog_rejoin.get("evidence_source", ""),
                        "visual_evidence_source": dialog_rejoin.get("visual_evidence_source", ""),
                    },
                    expected_runtime_generation=int(dialog_rejoin.get("runtime_generation") or 0),
                    expected_session_id=str(dialog_rejoin.get("session_id") or ""),
                    expected_launch_nonce=str(dialog_rejoin.get("launch_nonce") or ""),
                    expected_transaction_id=str(dialog_rejoin.get("transaction_id") or ""),
                )
                continue

            if inactive_for < activity_timeout or suspect_for < hold_sec:
                continue

            pid_was = pid
            with acc._lock:
                runtime_generation = acc.runtime_generation
                session_id = acc.session_id
                launch_nonce = acc.launch_nonce
                transaction_id = acc.rejoin_transaction_id
            flog_kv(
                "WATCHDOG",
                "frozen_recovery_signal",
                "warning",
                account=acc.display_name,
                pid=pid_was,
                reason=reason_key,
                state=state,
                score=f"{score:.1f}",
                inactive=f"{inactive_for:.1f}",
                suspect=f"{suspect_for:.1f}",
                cpu=f"{cpu:.2f}",
                ram=f"{ram:.1f}",
                windows=windows,
            )
            if self._supervisor:
                self._supervisor.emit(
                    "WatchdogSupervisor",
                    "WATCHDOG_TIMEOUT",
                    account=acc,
                    severity="warning",
                    reason=reason_key,
                    payload={
                        "state": state,
                        "score": score,
                        "inactive_for": inactive_for,
                        "suspect_for": suspect_for,
                        "cpu": cpu,
                        "ram_mb": ram,
                        "windows": windows,
                    },
                )
            with acc._lock:
                acc.liveness_state = "frozen"
                acc.last_watchdog_classification = "frozen"
                acc.liveness_suspect_since = 0.0
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
            if worker:
                worker.report_fault(
                    reason_key,
                    f"PID={pid_was} state={state} score={score:.1f} inactive={inactive_for:.1f}s",
                    expected_runtime_generation=runtime_generation,
                    expected_session_id=session_id,
                    expected_launch_nonce=launch_nonce,
                    expected_transaction_id=transaction_id,
                )
