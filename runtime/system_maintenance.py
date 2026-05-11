from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from core import Account, AccountState, StateManager, flog, flog_kv
from services.process_service import ProcessManager
from services.presence_service import PRESENCE_SERVICE
from runtime.supervisor_runtime import SupervisorRuntime

def _window_resize_target_from_config(cfg: dict) -> Optional[Tuple[int, int]]:
    if not bool(cfg.get("roblox_window_resize_enabled", False)):
        return None
    try:
        width = int(float(cfg.get("roblox_window_width", 640) or 640))
    except Exception:
        width = 640
    try:
        height = int(float(cfg.get("roblox_window_height", 480) or 480))
    except Exception:
        height = 480
    width = max(320, min(width, 1920))
    height = max(240, min(height, 1080))
    return width, height

def _window_arrange_settings_from_config(cfg: dict) -> Optional[Tuple[int, int, int, int, int]]:
    target = _window_resize_target_from_config(cfg)
    if not target or not bool(cfg.get("roblox_window_arrange_enabled", False)):
        return None
    width, height = target
    try:
        columns = int(float(cfg.get("roblox_window_arrange_columns", 6) or 6))
    except Exception:
        columns = 6
    try:
        gap = int(float(cfg.get("roblox_window_arrange_gap", 2) or 2))
    except Exception:
        gap = 2
    try:
        margin = int(float(cfg.get("roblox_window_arrange_margin", 0) or 0))
    except Exception:
        margin = 0
    columns = max(1, min(columns, 32))
    gap = max(0, min(gap, 80))
    margin = max(0, min(margin, 300))
    return width, height, columns, gap, margin

def _apply_cpu_limiter_for_bound_process(
    accounts: List[Account],
    cfg: dict,
    reason: str,
    account: Optional[Account] = None,
) -> None:
    try:
        from services.cpu_limiter import CPU_LIMITER, normalize_cpu_limiter_settings

        settings = normalize_cpu_limiter_settings(cfg)
        if not bool(settings.get("enabled")):
            return
        result = CPU_LIMITER.apply(accounts, settings)
        row = None
        if account:
            account_key = getattr(account, "_config_username", "") or getattr(account, "username", "")
            row = next((item for item in result.get("rows", []) if item.get("username") == account_key), None)
        flog_kv(
            "PERFORMANCE",
            "cpu_limiter_bound_apply",
            account=getattr(account, "display_name", "") if account else "",
            reason=reason,
            mode=result.get("mode", ""),
            status=(row or {}).get("status", ""),
            pid=(row or {}).get("pid", ""),
            limit_percent=(row or {}).get("limit_percent", ""),
            applied=result.get("applied", 0),
            fallback=result.get("fallback", 0),
            failed=result.get("failed", 0),
        )
    except Exception as exc:
        flog_kv(
            "PERFORMANCE",
            "cpu_limiter_bound_apply_failed",
            "warning",
            account=getattr(account, "display_name", "") if account else "",
            reason=reason,
            error=str(exc),
        )

def _account_presence_user_id(acc: Account) -> str:
    return str(getattr(acc, "user_id", "") or getattr(acc, "cookie_user_id", "") or "").strip()

class SystemMaintenance(threading.Thread):
    def __init__(
        self,
        accounts: List[Account],
        workers: Dict[str, AccountWorker],
        recovery: RecoveryEngine,
        state_mgr: StateManager,
        cfg: dict,
        stop: threading.Event,
        supervisor: Optional[SupervisorRuntime] = None,
    ):
        super().__init__(daemon=True, name="Maintenance")
        self._accounts = accounts
        self._workers = workers
        self._recovery = recovery
        self._state_mgr = state_mgr
        self._cfg = cfg
        self._stop = stop
        self._supervisor = supervisor
        self._last_auto_close_at = time.time()
        self._last_priority_apply_at = 0.0
        self._last_cpu_limiter_apply_at = 0.0
        self._last_window_resize_at = 0.0

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

    def _presence_disconnect_reason(
        self,
        acc: Account,
        now: float,
        in_game_for: float,
        loading_grace: float,
    ) -> Tuple[str, Dict[str, Any]]:
        if not bool(self._cfg.get("presence_api_enabled", False)):
            return "", {}
        if not bool(self._cfg.get("presence_assist_rejoin_enabled", True)):
            return "", {}
        if not bool(self._cfg.get("connection_error_rejoin", True)):
            return "", {}
        uid = _account_presence_user_id(acc)
        if not uid:
            return "", {}
        poll_interval = float(self._cfg.get("presence_poll_interval_seconds", 30) or 30)
        cache_ttl = float(self._cfg.get("presence_cache_ttl_seconds", 30) or 30)
        hold = max(1.0, float(self._cfg.get("connection_error_hold_time", 3) or 3))
        launch_grace = max(20.0, min(float(loading_grace or 90.0), poll_interval + hold))
        if in_game_for < launch_grace:
            return "", {}
        result = PRESENCE_SERVICE.refresh(
            [uid],
            enabled=True,
            poll_interval=poll_interval,
            cache_ttl=cache_ttl,
            force=False,
        )
        presence = (result.get("presences") or {}).get(uid) or PRESENCE_SERVICE.get_cached(uid)
        if not presence:
            return "", {}
        try:
            presence_type = int(presence.get("presence_type") if presence.get("presence_type") is not None else -1)
        except Exception:
            presence_type = -1
        fetched_at = float(presence.get("presence_fetched_at") or 0.0)
        presence_age = float(presence.get("presence_age_seconds") if presence.get("presence_age_seconds") is not None else max(0.0, now - fetched_at))
        if fetched_at and presence_age > max(cache_ttl + poll_interval + 5.0, 45.0):
            return "", presence

        with acc._lock:
            expected_places = {
                str(acc.place_id or "").strip(),
                str((acc.launch_intent or {}).get("place_id") or "").strip(),
                str((acc.launch_intent_summary or {}).get("place_id") or "").strip(),
            }
        expected_places.discard("")

        if presence_type == 2:
            observed_places = {
                str(presence.get("presence_place_id") or "").strip(),
                str(presence.get("presence_root_place_id") or "").strip(),
            }
            observed_places.discard("")
            if not observed_places:
                return "", presence
            if expected_places and not observed_places.intersection(expected_places):
                return "presence_place_mismatch", presence
            return "", presence
        if presence_type in {0, 1, 3, 4}:
            return f"presence_not_ingame:{presence.get('presence_type_name') or presence_type}", presence
        return "", presence

    def _reset_presence_mismatch(self, acc: Account, reason: str = "") -> None:
        with acc._lock:
            had_mismatch = bool(acc.presence_mismatch_since)
            acc.presence_mismatch_since = 0.0
            acc.presence_mismatch_status = ""
            acc.presence_mismatch_reason = ""
        if had_mismatch:
            flog_kv("PRESENCE", "presence_disconnect_cleared", account=acc.display_name, reason=reason or "presence_recovered")

    def _handle_presence_disconnect_assist(
        self,
        acc: Account,
        worker: Optional[AccountWorker],
        now: float,
        pid: int,
        in_game_for: float,
        loading_grace: float,
        allow_rejoin: bool = True,
    ) -> bool:
        reason, presence = self._presence_disconnect_reason(acc, now, in_game_for, loading_grace)
        if not reason:
            try:
                presence_type = int(presence.get("presence_type") if presence.get("presence_type") is not None else -1) if presence else -1
            except Exception:
                presence_type = -1
            if presence_type == 2:
                with acc._lock:
                    acc.presence_rejoin_pending_clear = False
                    acc.presence_rejoin_suppressed_until = 0.0
            self._reset_presence_mismatch(acc, "presence_ingame_or_unavailable")
            return False
        if not allow_rejoin:
            with acc._lock:
                if not acc.presence_mismatch_since:
                    acc.presence_mismatch_since = now
                acc.presence_mismatch_status = str(presence.get("presence_type_name") or "")
                acc.presence_mismatch_reason = reason
                acc.last_watchdog_classification = "presence_mismatch_observed"
                acc.last_activity_reason = f"presence_observed:{reason}"
            flog_kv(
                "PRESENCE",
                "presence_mismatch_observed",
                "warning",
                account=acc.display_name,
                pid=pid,
                reason=reason,
                presence_type=presence.get("presence_type_name", ""),
                last_location=presence.get("presence_last_location", ""),
                action="hold_local_process_alive",
            )
            return False
        hold = max(1.0, float(self._cfg.get("connection_error_hold_time", 3) or 3))
        default_cooldown = max(10.0, hold * 2.0)
        rejoin_cooldown = max(5.0, float(self._cfg.get("presence_rejoin_cooldown_seconds", default_cooldown) or default_cooldown))
        with acc._lock:
            suppressed_until = float(getattr(acc, "presence_rejoin_suppressed_until", 0.0) or 0.0)
            if suppressed_until > now:
                acc.presence_mismatch_since = acc.presence_mismatch_since or now
                acc.presence_mismatch_status = str(presence.get("presence_type_name") or "")
                acc.presence_mismatch_reason = reason
                acc.liveness_state = "reconnecting"
                acc.last_watchdog_classification = "presence_disconnect_suppressed"
                acc.last_activity_reason = f"presence_suppressed:{reason}"
                remaining = suppressed_until - now
                flog_kv(
                    "PRESENCE",
                    "presence_disconnect_suppressed",
                    "warning",
                    account=acc.display_name,
                    pid=pid,
                    reason=reason,
                    presence_type=presence.get("presence_type_name", ""),
                    last_location=presence.get("presence_last_location", ""),
                    remaining=f"{remaining:.1f}",
                )
                return False
            if not acc.presence_mismatch_since:
                acc.presence_mismatch_since = now
                acc.presence_mismatch_status = str(presence.get("presence_type_name") or "")
                acc.presence_mismatch_reason = reason
                acc.liveness_state = "reconnecting"
                acc.last_watchdog_classification = "presence_not_ingame_hold"
                acc.last_activity_reason = f"presence:{reason}"
                mismatch_for = 0.0
            else:
                mismatch_for = now - float(acc.presence_mismatch_since or now)
            runtime_generation = acc.runtime_generation
            session_id = acc.session_id
            launch_nonce = acc.launch_nonce
            transaction_id = acc.rejoin_transaction_id
        if mismatch_for < hold:
            flog_kv(
                "PRESENCE",
                "presence_disconnect_hold",
                "warning",
                account=acc.display_name,
                pid=pid,
                reason=reason,
                hold=f"{hold:.1f}",
                mismatch_for=f"{mismatch_for:.1f}",
                presence_type=presence.get("presence_type_name", ""),
                last_location=presence.get("presence_last_location", ""),
            )
            return False

        with acc._lock:
            acc.presence_mismatch_since = 0.0
            acc.presence_mismatch_status = str(presence.get("presence_type_name") or "")
            acc.presence_mismatch_reason = reason
            acc.last_presence_rejoin_at = now
            acc.presence_rejoin_suppressed_until = now + rejoin_cooldown
            acc.presence_rejoin_pending_clear = True
            acc.liveness_state = "presence_disconnected"
            acc.last_watchdog_classification = "presence_disconnected"
            acc.last_activity_reason = f"presence:{reason}"
        flog_kv(
            "PRESENCE",
            "presence_disconnect_rejoin_signal",
            "warning",
            account=acc.display_name,
            pid=pid,
            reason=reason,
            presence_type=presence.get("presence_type_name", ""),
            last_location=presence.get("presence_last_location", ""),
            mismatch_for=f"{mismatch_for:.1f}",
            runtime_generation=runtime_generation,
            session_id=session_id,
            transaction_id=transaction_id,
        )
        if self._supervisor:
            self._supervisor.emit(
                "WatchdogSupervisor",
                "PRESENCE_DISCONNECT",
                account=acc,
                severity="warning",
                reason="connection_error",
                payload={
                    "presence_reason": reason,
                    "presence_type": presence.get("presence_type_name", ""),
                    "last_location": presence.get("presence_last_location", ""),
                    "mismatch_for": mismatch_for,
                },
            )
        if worker:
            worker.report_fault(
                "connection_error",
                f"Presence API no longer reports InGame ({reason}, location={presence.get('presence_last_location', '')})",
                expected_runtime_generation=runtime_generation,
                expected_session_id=session_id,
                expected_launch_nonce=launch_nonce,
                expected_transaction_id=transaction_id,
            )
        return True

    def _scan_liveness_watchdog(self):
        if not self._cfg.get("watchdog_enabled", True):
            return
        now = time.time()
        hold_sec = max(5.0, float(self._cfg.get("watchdog_hold_time", 60) or 60))
        activity_timeout = max(hold_sec, float(self._cfg.get("watchdog_activity_timeout", 180) or 180))
        loading_grace = max(30.0, float(self._cfg.get("watchdog_loading_grace", 90) or 90))
        cpu_low = float(self._cfg.get("watchdog_cpu_low", 0.9) or 0.9)
        startup_grace = max(0.0, float(self._cfg.get("popup_startup_grace_seconds", 8) or 8))
        popup_enabled = bool(self._cfg.get("popup_disconnected_enabled", True))
        net_online = self._recovery._net.is_online()

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

            inspect_ui = popup_enabled and (presence_mismatch_active or old_state in {"suspect_frozen", "frozen", "reconnecting", "teleporting"})
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
            cpu = float(validation.get("cpu") or 0.0)
            ram = float(validation.get("ram_mb") or 0.0)
            windows = int(validation.get("windows") or 0)

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
                                "reconnecting_for": reconnecting_for,
                            }
                            acc.liveness_suspect_since = 0.0
                            acc.presence_rejoin_suppressed_until = 0.0
                            acc.presence_rejoin_pending_clear = False
                            acc.last_watchdog_classification = "disconnect_dialog_rejoin"
                            acc.liveness_state = "reconnecting"
                        else:
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

    def _queue_delay_seconds(self) -> float:
        try:
            return max(1.0, float(self._cfg.get("queue_delay_seconds", self._cfg.get("launch_rate_interval", 15)) or 15))
        except Exception:
            return 15.0

    def _queue_duration_seconds(self) -> float:
        if bool(self._cfg.get("multi_roblox_enabled", True)) and not bool(self._cfg.get("rt_rotation_enabled", False)):
            return 0.0
        try:
            return max(0.0, float(self._cfg.get("queue_duration_seconds", 0) or 0))
        except Exception:
            return 0.0

    def _cycle_account(self, acc: Account, reason: str):
        with acc._lock:
            pid = acc.pid
            runtime_generation = acc.runtime_generation
        if pid:
            ProcessManager.safe_kill_bound_process(
                acc,
                self._state_mgr,
                reason=reason,
                expected_runtime_generation=runtime_generation,
            )
        delay = self._queue_delay_seconds()
        with acc._lock:
            self._state_mgr.set_cooldown(acc, time.time() + delay, reason=reason)
        self._state_mgr.transition(acc, AccountState.READY, reason=reason, force=True)
        flog_kv("QUEUE", "cycle_account", account=acc.display_name, reason=reason, delay=f"{delay:.1f}")
        self._recovery.request_evaluate(acc, trigger=reason)
        worker = self._workers.get(acc._config_username)
        if worker:
            worker.wake()

    def _enforce_queue_duration(self):
        duration = self._queue_duration_seconds()
        if duration <= 0:
            return
        now = time.time()
        for acc in self._accounts:
            with acc._lock:
                if acc.desired_state != AccountState.IN_GAME or acc.state != AccountState.IN_GAME:
                    continue
                started = float(acc.in_game_since or 0.0)
            if started and (now - started) >= duration:
                self._cycle_account(acc, "queue_duration_elapsed")

    def _enforce_auto_close(self):
        if not bool(self._cfg.get("auto_close_enabled", False)):
            self._last_auto_close_at = time.time()
            return
        try:
            minutes = max(0.0, float(self._cfg.get("auto_close_minutes", 0) or 0))
        except Exception:
            minutes = 0.0
        seconds = minutes * 60.0
        if seconds <= 0:
            self._last_auto_close_at = time.time()
            return
        now = time.time()
        if (now - self._last_auto_close_at) < seconds:
            return
        self._last_auto_close_at = now
        killed = ProcessManager.kill_all_roblox_clients(wait_seconds=4.0)
        flog_kv("QUEUE", "auto_close_cycle", killed=killed, minutes=f"{minutes:.1f}", seconds=f"{seconds:.1f}")
        for acc in self._accounts:
            with acc._lock:
                if acc.pid:
                    self._state_mgr.clear_process_binding(acc, reason="auto_close_cycle", increment_generation=True)
            self._state_mgr.transition(acc, AccountState.READY, reason="auto_close_cycle", force=True)
            self._recovery.request_evaluate(acc, trigger="auto_close_cycle")
            worker = self._workers.get(acc._config_username)
            if worker:
                worker.wake()

    def _apply_auto_process_priority(self):
        if not bool(self._cfg.get("auto_process_priority_enabled", False)):
            return
        now = time.time()
        if (now - self._last_priority_apply_at) < 10.0:
            return
        self._last_priority_apply_at = now
        try:
            from performance_settings import apply_process_priority_to_roblox

            result = apply_process_priority_to_roblox(self._cfg.get("process_priority", "low"))
            if int(result.get("applied") or 0) > 0:
                flog_kv(
                    "PERFORMANCE",
                    "auto_process_priority_applied",
                    priority=result.get("priority", ""),
                    applied=result.get("applied", 0),
                    count=result.get("count", 0),
                )
        except Exception as exc:
            flog_kv("PERFORMANCE", "auto_process_priority_failed", "warning", error=str(exc))

    def _apply_cpu_limiter(self):
        now = time.time()
        try:
            from services.cpu_limiter import CPU_LIMITER, normalize_cpu_limiter_settings

            settings = normalize_cpu_limiter_settings(self._cfg)
            if not bool(settings.get("enabled")):
                CPU_LIMITER.release_all()
                self._last_cpu_limiter_apply_at = now
                return
            if (now - self._last_cpu_limiter_apply_at) < 10.0:
                return
            self._last_cpu_limiter_apply_at = now
            result = CPU_LIMITER.apply(self._accounts, settings)
            if any(int(result.get(key) or 0) > 0 for key in ("applied", "fallback", "failed")):
                flog_kv(
                    "PERFORMANCE",
                    "cpu_limiter_applied",
                    mode=result.get("mode", ""),
                    applied=result.get("applied", 0),
                    fallback=result.get("fallback", 0),
                    failed=result.get("failed", 0),
                )
        except Exception as exc:
            flog_kv("PERFORMANCE", "cpu_limiter_failed", "warning", error=str(exc))

    def _enforce_window_resize(self):
        target = _window_resize_target_from_config(self._cfg)
        if not target:
            self._last_window_resize_at = time.time()
            return
        try:
            seconds = max(1.0, float(self._cfg.get("roblox_window_resize_interval_seconds", 10) or 10))
        except Exception:
            seconds = 10.0
        now = time.time()
        if (now - self._last_window_resize_at) < seconds:
            return
        self._last_window_resize_at = now
        width, height = target
        arrange = _window_arrange_settings_from_config(self._cfg)
        if arrange:
            width, height, columns, gap, margin = arrange
            result = ProcessManager.arrange_roblox_windows(width, height, columns, gap, margin)
            changed = int(result.get("arranged") or 0)
            event = "auto_window_arrange_cycle"
        else:
            result = ProcessManager.resize_roblox_windows(width, height)
            changed = int(result.get("resized") or 0)
            event = "auto_window_resize_cycle"
        if changed > 0:
            flog_kv(
                "WINDOW",
                event,
                arranged=result.get("arranged", 0),
                resized=result.get("resized", 0),
                count=result.get("count", 0),
                width=width,
                height=height,
                columns=result.get("columns", ""),
                seconds=f"{seconds:.1f}",
            )

    def run(self):
        flog("[MAINT] started")
        interval = max(1.0, min(5.0, float(self._cfg.get("periodic_reconcile_interval", 15) or 15)))
        while not self._stop.wait(timeout=interval):
            ProcessManager.cleanup_stale_pid_claims()
            self._reconcile_duplicate_pid_claims()
            self._recover_stale_joining_states()
            self._recover_failed_live_sessions()
            self._scan_liveness_watchdog()
            self._enforce_queue_duration()
            self._enforce_auto_close()
            self._apply_auto_process_priority()
            self._apply_cpu_limiter()
            self._enforce_window_resize()
            self._recovery.reconcile_all(self._accounts, trigger="periodic_reconcile", force_restart=False)
            for worker in self._workers.values():
                worker.wake()
        flog("[MAINT] stopped")
