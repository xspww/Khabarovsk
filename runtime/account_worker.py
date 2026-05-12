from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from core import (
    Account,
    AccountState,
    EventBus,
    StateManager,
    account_launch_block_reason,
    cookie_identity_block_reason,
    flog,
    flog_kv,
)
from services.process_service import ProcessManager
from services.presence_service import PRESENCE_SERVICE
from services.ram_service import RAMManager
from runtime.roblox_watchdog import RobloxWatchdog
from runtime.supervisor_runtime import SupervisorRuntime
from runtime.system_maintenance import _account_presence_user_id, _apply_cpu_limiter_for_bound_process
from runtime.recovery_support import RECOVERY_REASON_MESSAGES, _set_account_cookie_block, compute_backoff


class AccountWorker(threading.Thread):
    """
    AccountWorker observes process health.
    It no longer decides how recovery should happen.
    """

    REASON_MESSAGES = RECOVERY_REASON_MESSAGES

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
                            effective_hold_sec = 1.0 if error_code in {"267", "268", "273", "277"} else hold_sec
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
                                runtime_state = getattr(self.recovery, "_runtime_state", None)
                                if runtime_state:
                                    runtime_state.set_recovery(acc, status="checking_disconnect", reason=reason_key, inflight=False)
                                flog(f"[WORKER] {acc.display_name} disconnect dialog detected - will recover in {effective_hold_sec:.0f}s ({reason_key}{detail_suffix} {evidence_note}: {detail})")
                                self._wake.wait(timeout=min(1.0, effective_hold_sec))
                                self._wake.clear()
                                continue
                            elif time.time() - self._connection_error_since >= effective_hold_sec:
                                flog(f"[WORKER] {acc.display_name} disconnect dialog held {effective_hold_sec:.0f}s -> force recover ({reason_key}{detail_suffix} {evidence_note})")
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
                                self._wake.wait(timeout=min(1.0, max(0.2, effective_hold_sec / 2.0)))
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

                wait_timeout = float(crash_to)
                if acc.pid and acc.in_game_since and self.cfg.get("connection_error_rejoin", True) and self.cfg.get("popup_disconnected_enabled", True):
                    wait_timeout = min(
                        wait_timeout,
                        max(1.0, float(self.cfg.get("popup_scan_interval_seconds", 2.0) or 2.0)),
                    )
                self._wake.wait(timeout=wait_timeout)
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
