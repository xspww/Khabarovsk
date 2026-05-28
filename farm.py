from __future__ import annotations

import random
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from account_hybrid import ACCOUNT_STORE, audit_event
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
from services.network_monitor import NetworkMonitor, NET_ONLINE
from services.vip_tracker import VipTracker
from services.process_service import ProcessManager, ProcessService
from services.resource_monitor import get_rt_monitor
from services.roblox_log_evidence import collect_recent_log_evidence
from services.captcha_guard import (
    CAPTCHA_BLOCK_REASON,
    CAPTCHA_REASON,
    clear_account_captcha_hold,
    is_captcha_text,
    set_account_captcha_hold,
)
from services.auth_gate import evaluate_account_auth_gate, mark_account_auth_quarantined
from runtime.farm_health import (
    build_farm_status,
    get_account as get_farm_account,
    get_detailed_farm_health as get_detailed_farm_health_payload,
    get_public_farm_health as get_public_farm_health_payload,
    get_runtime_diagnostics as get_runtime_diagnostics_payload,
    get_runtime_events as get_runtime_events_payload,
    get_runtime_health as get_runtime_health_payload,
    get_runtime_telemetry as get_runtime_telemetry_payload,
)
from runtime.account_runtime_controller import AccountRuntimeController
from runtime.command_tracker import RuntimeCommandTracker
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
from runtime.runtime_orchestrator import RuntimeOrchestrator
from runtime.machine_supervisor import MachineSupervisor
from runtime.lua_liveness_policy import lua_liveness_required, mark_waiting_for_lua
from runtime.farm_lifecycle import FarmLifecycleService
from runtime.lua_server_detection import LuaServerDetection, detect_lua_server
from runtime.recovery_view import recovery_step_for_account
from runtime.supervisor_runtime import SupervisorRuntime
from runtime.system_maintenance import SystemMaintenance
from runtime.config_snapshot import apply_runtime_config_snapshot
from runtime.recovery_engine import RecoveryCoordinator, RecoveryEngine
from runtime.account_worker import AccountWorker
from runtime.command_rate_limit import FORCE_REJOIN_INTERVAL_SECONDS, PerAccountRateLimiter
from runtime.dispatcher import Dispatcher
from runtime.farm_initial_sync import initial_state_sync
from runtime.lua_rejoin_events import handle_lua_rejoin_event as _handle_lua_rejoin_event
from runtime.farm_preflight import preflight_cookie_blocks
from runtime.lua_identity import resolve_lua_account
from runtime.lua_event_guard import lua_event_handler_error_response, validate_lua_event_payload


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
        self._runtime_scheduler: Optional[Any] = None
        self._shutting_down = False
        self._last_control_plane_restart_at = 0.0
        self._force_rejoin_limiter = PerAccountRateLimiter(FORCE_REJOIN_INTERVAL_SECONDS)

        self._total_rejoin = 0
        self._total_crash = 0
        self._event_log: list = []
        self._event_lock = threading.RLock()
        self._status_lock = threading.Lock()
        self._status_revision = 0
        self._status_cache_lock = threading.Lock()
        self._status_cache_snapshot: Optional[dict] = None
        self._status_cache_revision = -1
        self._status_cache_expires_at = 0.0
        self._status_cache_hits = 0
        self._status_cache_misses = 0
        self._status_cache_last_build_ms = 0.0
        self._status_stream_clients = 0
        self._dashboard_process_cache_lock = threading.Lock()
        self._dashboard_process_cache: Dict[Any, Any] = {}
        self._last_runtime_truth_state: Dict[str, str] = {}
        self._last_runtime_truth_progress: Dict[str, int] = {}
        self._runtime_state = RuntimeStateManager(logger=flog_kv)
        self._runtime_store = RuntimeStore(os.path.join(APP_DATA_DIR, "cronus_runtime.db"))
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
        self._machine_supervisor = MachineSupervisor(cfg_mgr.snapshot(), self._accounts)
        try:
            from console_activity import set_lua_liveness_required

            set_lua_liveness_required(lua_liveness_required(cfg_mgr.snapshot()))
        except Exception:
            pass
        self._runtime_orchestrator = RuntimeOrchestrator(
            self._runtime_state,
            timeline=self._timeline,
            logger=flog_kv,
        )
        self._command_tracker = RuntimeCommandTracker(
            runtime_state=self._runtime_state,
            find_account=self._find_account,
            capability=self._command_capability,
            record_timeline=self._record_timeline,
            bump_status_revision=self._bump_status_revision,
            logger=flog_kv,
            is_shutting_down=lambda: self._shutting_down,
        )
        self._lifecycle = FarmLifecycleService(self)

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

    def get_status_revision(self) -> int:
        with self._status_lock:
            return int(self._status_revision)

    def _status_cache_ttl(self) -> float:
        try:
            value = float(self.cfg_mgr.get("status_snapshot_cache_ttl_seconds", 1.0) or 0.0)
        except Exception:
            value = 1.0
        if value <= 0:
            return 0.0
        return min(2.0, max(0.25, value))

    def open_status_stream(self) -> int:
        with self._status_cache_lock:
            self._status_stream_clients += 1
            return self._status_stream_clients

    def close_status_stream(self) -> int:
        with self._status_cache_lock:
            self._status_stream_clients = max(0, self._status_stream_clients - 1)
            return self._status_stream_clients

    def _status_perf_snapshot(self, cache_hit: bool, cache_age: float = 0.0) -> Dict[str, Any]:
        return {
            "cache_hit": bool(cache_hit),
            "cache_age_seconds": round(max(0.0, cache_age), 3),
            "cache_hits": int(self._status_cache_hits),
            "cache_misses": int(self._status_cache_misses),
            "last_build_ms": round(float(self._status_cache_last_build_ms or 0.0), 2),
            "active_stream_clients": int(self._status_stream_clients),
        }

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

    def begin_command(
        self,
        key: str,
        action: str,
        account: str = "",
        ttl: float = 15.0,
        idempotency_key: str = "",
        request_fingerprint: str = "",
    ) -> Tuple[bool, Dict[str, Any]]:
        return self._command_tracker.begin(
            key,
            action,
            account=account,
            ttl=ttl,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )

    def finish_command(self, key: str, command_id: str, ok: bool = True, error: str = "", response: Optional[Dict[str, Any]] = None):
        self._command_tracker.finish(key, command_id, ok=ok, error=error, response=response)

    def _cancel_commands_for_shutdown(self) -> None:
        self._command_tracker.cancel_for_shutdown()

    def command_inflight(self, key: str) -> Optional[Dict[str, Any]]:
        return self._command_tracker.command_inflight(key)

    def _recovery_step_for_account(self, acc: Account, display_state: AccountState) -> Tuple[str, int, float]:
        network_state = self._net_mon.get_state() if self._net_mon else NET_ONLINE
        return recovery_step_for_account(acc, display_state, network_state=network_state)

    def _initial_state_sync(self, state_mgr: Optional[StateManager] = None):
        if state_mgr is None:
            state_mgr = StateManager(self.bus)
        cfg = self.cfg_mgr.snapshot() if hasattr(self, "cfg_mgr") else {}
        initial_state_sync(
            self._accounts,
            state_mgr,
            lua_required=lua_liveness_required(cfg),
            runtime_state=getattr(self, "_runtime_state", None),
        )

    def _preflight_cookie_blocks(self) -> Dict[str, str]:
        return preflight_cookie_blocks(self._accounts, self._recovery, self._state_mgr, self._runtime_state)

    def start(self):
        return self._lifecycle.start()

    def stop(self):
        return self._lifecycle.stop()

    def set_accounts(self, accounts: List[Account]):
        self._accounts = accounts
        self._machine_supervisor.set_accounts(self._accounts)
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
        try:
            from console_activity import set_lua_liveness_required

            set_lua_liveness_required(lua_liveness_required(cfg))
        except Exception:
            pass
        try:
            apply_runtime_config_snapshot(
                cfg=cfg,
                accounts=list(self._accounts),
                machine_supervisor=self._machine_supervisor,
                recovery=self._recovery,
                maintenance=self._maintenance,
                workers=dict(self._workers),
                dispatcher=self._dispatcher,
            )
        except Exception as e:
            flog_kv("CONFIG", "runtime_config_snapshot_failed", "warning", error=e)
        self._bump_status_revision()

    def _sync_accounts_from_ram(self, persist: bool = False):
        return

    def _check_force_rejoin_rate_limit(self, account_key: str) -> Tuple[bool, str]:
        limiter = getattr(self, "_force_rejoin_limiter", None)
        if limiter is None:
            limiter = PerAccountRateLimiter(FORCE_REJOIN_INTERVAL_SECONDS)
            self._force_rejoin_limiter = limiter
        return limiter.check(account_key)

    def force_rejoin(self, username: str):
        acc = self._find_account(username)
        if acc and self._recovery:
            allowed, rate_msg = self._check_force_rejoin_rate_limit(acc._config_username)
            if not allowed:
                flog_kv("COMMAND", "force_rejoin_rate_limited", "warning", account=acc.display_name, msg=rate_msg)
                return False, rate_msg
            routed = self._runtime_orchestrator.request_rejoin(acc, "force_rejoin")
            if not routed:
                return False, "Rejoin rejected"
            worker = self._workers.get(acc._config_username)
            if worker:
                worker.wake()
            self._push_event("rejoin", f"Force rejoin: {acc.display_name}", account=acc, severity="warn", reason="force_rejoin")
            return True, f"Rejoin: {username}"
        return False, "Account not running or recovery coordinator unavailable"

    def close_all_roblox(
        self,
        wait_seconds: float = 4.0,
        reason: str = "api_close_all_roblox",
        idempotency_key: str = "",
        command_id: str = "",
    ) -> int:
        return self._runtime_orchestrator.request_close_all_roblox(
            wait_seconds=wait_seconds,
            reason=reason,
            idempotency_key=idempotency_key,
            command_id=command_id,
        )

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
        result = self._runtime_orchestrator.request_kill_account_pid(
            acc,
            self._runtime_state,
            reason=reason,
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
        result = self._runtime_orchestrator.request_verify_finished(
            acc,
            self._state_mgr or self._runtime_state,
            reason="manual_verify_finished",
        )
        killed = bool(result.get("killed"))
        self.cfg_mgr.save_accounts(self._accounts)
        self._bump_status_revision()
        flog_kv("COMMAND", "verify_finished", account=acc.display_name, killed=killed, finished_at=f"{now:.3f}")
        self._push_event("system", f"Verified finished: {acc.display_name}", account=acc, severity="success", reason="manual_verify_finished")
        return True, f"Verified finished: {username}" + (" (PID killed)" if killed else "")

    def _set_lua_account_description(self, acc: Account, description: str) -> Tuple[bool, str]:
        text = str(description or "").strip()
        if len(text) > 500:
            text = text[:500]
        with acc._lock:
            acc.description = text

        persisted = False
        try:
            persisted = ACCOUNT_STORE.update_record(acc._config_username, {"description": text}) is not None
        except Exception as e:
            flog_kv("ACCOUNT_DATA", "lua_description_update_failed", "warning", account=acc.display_name, error=e)

        try:
            self.cfg_mgr.save_accounts(self._accounts)
        except Exception as e:
            flog_kv("ACCOUNT_DATA", "lua_description_legacy_save_failed", "warning", account=acc.display_name, error=e)

        try:
            audit_event("lua_description", username=acc._config_username, ok=persisted, description=text)
        except Exception as e:
            flog_kv("ACCOUNT_DATA", "lua_description_audit_failed", "warning", account=acc.display_name, error=e)

        return persisted, text

    def resume_captcha_account(self, username: str) -> Tuple[bool, str]:
        acc = self._find_account(username)
        if not acc:
            return False, "Account not found"
        was_captcha = clear_account_captcha_hold(acc, runtime_writer=self._runtime_state)
        try:
            decision = evaluate_account_auth_gate(acc)
        except Exception as e:
            flog_kv("FARM", "captcha_resume_auth_gate_error", "warning", account=acc.display_name, error=e)
            return False, f"Captcha cleared for {username}, but auth gate unavailable. Retry later."
        if decision.blocked:
            try:
                mark_account_auth_quarantined(acc, decision, source="manual_resume", runtime_writer=self._runtime_state)
                if self._recovery:
                    self._recovery.fail_account(acc, decision.reason_key, decision.reason)
            except Exception as e:
                flog_kv("FARM", "captcha_resume_quarantine_error", "warning", account=acc.display_name, error=e)
                return False, f"Captcha cleared for {username}, but auth quarantine failed. Retry later."
            self.cfg_mgr.save_accounts(self._accounts)
            self.cfg_mgr.save_runtime(self._accounts)
            self._bump_status_revision()
            self._push_event(
                "captcha",
                f"Captcha cleared: {acc.display_name} - still blocked: {decision.reason}",
                account=acc,
                severity="warn",
                reason=decision.reason_key,
                was_captcha=was_captcha,
            )
            return False, f"Captcha cleared for {username}, but launch is still blocked: {decision.reason}"
        if self._runtime_state:
            with acc._lock:
                self._runtime_state.set_desired(acc, AccountState.IN_GAME, reason="manual_resume")
                self._runtime_state.set_cooldown(acc, 0.0, reason="manual_resume")
        live_session = False
        with acc._lock:
            pid = acc.pid
            identity = acc.bound_process_identity
            tracker_id = acc.browser_tracker_id
        if pid:
            live_session = bool(
                ProcessManager.is_bound_game_alive(
                    pid,
                    owner_key=acc._config_username,
                    expected_identity=identity,
                    expected_browser_tracker_id=tracker_id,
                )
            )
        lua_required = lua_liveness_required(self.cfg_mgr.snapshot())
        if self._state_mgr:
            if live_session:
                if lua_required:
                    mark_waiting_for_lua(acc, self._runtime_state, self._state_mgr, "manual_resume_live_session")
                else:
                    self._state_mgr.transition(acc, AccountState.IN_GAME, reason="manual_resume_live_session", force=True)
                    self._state_mgr.clear_recovery(acc, reason="manual_resume_live_session", inflight=False)
                self._state_mgr.set_binding_status(acc, "verified", reason="manual_resume_live_session")
            else:
                self._state_mgr.transition(acc, AccountState.IDLE, reason="manual_resume", force=True)
        self.cfg_mgr.save_accounts(self._accounts)
        self.cfg_mgr.save_runtime(self._accounts)
        self._bump_status_revision()
        self._push_event(
            "captcha",
            f"Captcha cleared: {acc.display_name} - resume requested",
            account=acc,
            severity="warn",
            reason="captcha_resume",
            was_captcha=was_captcha,
        )
        if not self.running:
            return True, f"Captcha cleared for {username}. Start Cronus when ready."
        if not self._recovery or not self._state_mgr:
            return False, "Recovery coordinator unavailable"
        if live_session:
            worker = self._workers.get(acc._config_username)
            if worker:
                worker.wake()
            if lua_required:
                return True, f"Captcha cleared for {username}. Waiting for Lua."
            return True, f"Captcha cleared for {username}. Live session verified."
        worker = self._workers.get(acc._config_username)
        if not worker or not worker.is_alive():
            worker = AccountWorker(
                acc=acc,
                state_mgr=self._state_mgr,
                bus=self.bus,
                cfg=self.cfg_mgr.snapshot(),
                recovery=self._recovery,
                stop=self._stop,
                supervisor=self._supervisor,
                accounts=self._accounts,
            )
            self._workers[acc._config_username] = worker
            worker.start()
        else:
            worker.wake()
        self._runtime_orchestrator.request_evaluate(acc, trigger="manual_resume")
        worker.wake()
        return True, f"Captcha cleared for {username}. Resume requested."

    @staticmethod
    def _account_expects_private_server(acc: Account) -> bool:
        server_type = str(getattr(getattr(acc, "server_type", None), "value", getattr(acc, "server_type", "")) or "").upper()
        if server_type == "PUBLIC":
            return False
        launch_intent = dict(getattr(acc, "launch_intent", {}) or {})
        return bool(
            str(getattr(acc, "active_vip", "") or "").strip()
            or server_type == "VIP"
            or launch_intent.get("private_server_intent")
            or str(launch_intent.get("active_private_link_code_hash") or "").strip()
        )

    def _apply_lua_server_detection(self, acc: Account, payload: Dict[str, Any]) -> Dict[str, Any]:
        detection = detect_lua_server(payload)
        if not detection.observed:
            return {}

        now = time.time()
        expects_private = self._account_expects_private_server(acc)
        if detection.server_type == "PUBLIC" and expects_private:
            detection = LuaServerDetection(
                observed=True,
                server_type="VIP",
                is_vip=True,
                private_server_id=detection.private_server_id,
                private_server_owner_id=detection.private_server_owner_id,
                place_id=detection.place_id,
                job_id=detection.job_id,
                universe_id=detection.universe_id,
            )
        pid_key = str(
            payload.get("pid")
            or payload.get("process_id")
            or payload.get("roblox_pid")
            or getattr(acc, "pid", "")
            or ""
        ).strip()
        log_key = ":".join(
            [
                detection.server_type,
                detection.private_server_id,
                detection.private_server_owner_id,
                detection.job_id,
                pid_key,
            ]
        )
        with acc._lock:
            previous_type = str(acc.observed_server_type or "")
            previous_private_id = str(acc.observed_private_server_id or "")
            previous_log_key = str(getattr(acc, "_last_server_detection_log_key", "") or "")
            acc.observed_server_type = detection.server_type
            acc.observed_private_server_id = detection.private_server_id
            acc.observed_private_server_owner_id = detection.private_server_owner_id
            acc.observed_place_id = detection.place_id
            acc.observed_job_id = detection.job_id
            acc.observed_universe_id = detection.universe_id
            acc.observed_server_at = now
            acc.sync_runtime("lua_server_detection")
            should_log = (
                previous_type != detection.server_type
                or previous_private_id != detection.private_server_id
                or (detection.is_vip and previous_log_key != log_key)
            )
            if should_log:
                setattr(acc, "_last_server_detection_log_key", log_key)

        if should_log:
            private_label = detection.private_server_id[:8] if detection.private_server_id else ""
            flog_kv(
                "VIP",
                "server_detected",
                account=acc.display_name,
                pid=pid_key,
                is_vip=detection.is_vip,
                server_type=detection.server_type,
                private_server_id=private_label,
                place_id=detection.place_id,
                job_id=detection.job_id,
            )
            if hasattr(self, "_bump_status_revision"):
                self._bump_status_revision()

        return {
            "observed_server_type": detection.server_type,
            "observed_is_vip": detection.is_vip,
            "observed_private_server_id": detection.private_server_id,
            "observed_private_server_owner_id": detection.private_server_owner_id,
            "observed_place_id": detection.place_id,
            "observed_job_id": detection.job_id,
            "observed_universe_id": detection.universe_id,
            "observed_server_at": now,
        }

    def _lua_event_handler_error(self, acc: Account, event_name: str, error: Exception) -> Dict[str, Any]:
        flog_kv(
            "LUA_EVENT",
            "event_handler_exception",
            "warning",
            account=acc.display_name,
            event=event_name,
            error=error,
        )
        return lua_event_handler_error_response(acc, event_name, error)

    def handle_lua_rejoin_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return _handle_lua_rejoin_event(
            self,
            payload,
            validate_payload=validate_lua_event_payload,
            resolve_account=resolve_lua_account,
            log=flog_kv,
            process_service=ProcessService,
        )

    def get_status(self) -> dict:
        if not self.running:
            return build_farm_status(self)

        ttl = self._status_cache_ttl()
        revision = self.get_status_revision()
        now = time.time()
        if ttl > 0:
            with self._status_cache_lock:
                cached = self._status_cache_snapshot
                if cached is not None and self._status_cache_revision == revision and now < self._status_cache_expires_at:
                    self._status_cache_hits += 1
                    cached_view = dict(cached)
                    cached_view["status_perf"] = self._status_perf_snapshot(True, now - float(cached.get("status_updated_at") or now))
                    return cached_view

        started = time.perf_counter()
        snapshot = build_farm_status(self)
        build_ms = (time.perf_counter() - started) * 1000.0
        with self._status_cache_lock:
            self._status_cache_misses += 1
            self._status_cache_last_build_ms = build_ms
            snapshot["status_perf"] = self._status_perf_snapshot(False)
            if ttl > 0:
                self._status_cache_snapshot = snapshot
                self._status_cache_revision = int(snapshot.get("status_revision", revision) or 0)
                self._status_cache_expires_at = time.time() + ttl
        return snapshot

    def get_public_farm_health(self) -> dict:
        return get_public_farm_health_payload(self)

    def get_detailed_farm_health(self) -> dict:
        return get_detailed_farm_health_payload(self)

    def get_runtime_health(self) -> dict:
        return get_runtime_health_payload(self)

    def get_runtime_telemetry(self) -> dict:
        return get_runtime_telemetry_payload(self)

    def get_runtime_events(
        self,
        account_id: str = "",
        limit: int = 100,
        event_type: str = "",
        severity: str = "",
    ) -> dict:
        return get_runtime_events_payload(self, account_id=account_id, limit=limit, event_type=event_type, severity=severity)

    def get_runtime_diagnostics(
        self,
        account_id: str = "",
        limit: int = 200,
        event_type: str = "",
        severity: str = "",
    ) -> dict:
        return get_runtime_diagnostics_payload(self, account_id=account_id, limit=limit, event_type=event_type, severity=severity)

    def get_account(self, username: str) -> Optional[dict]:
        return get_farm_account(self, username)

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

    def _on_crash(self, account: Account, reason: str = "", reason_msg: str = "", **fields):
        with self._event_lock:
            self._total_crash += 1
        self._bump_status_revision()
        display_reason = reason_msg or reason
        self._push_event(
            "crash",
            f"Lost: {account.display_name} - {display_reason}",
            account=account,
            severity="critical",
            reason=reason,
            **fields,
        )
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
                self._runtime_orchestrator.request_network_lost(
                    acc,
                    "network_drop",
                    payload={"trigger": f"net:{new.lower()}"},
                )

    def _push_event(self, kind: str, msg: str, account: Optional[Account] = None, severity: str = "info", reason: str = "", **fields: Any):
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
        for key, value in fields.items():
            if key in {"account", "msg", "kind", "event_type", "severity", "reason"}:
                continue
            item[key] = value
        self._timeline.record(item, account_snapshot=runtime_snapshot if account else None, account_id=account_key)
        self._bump_status_revision()
        flog(f"[EVENT] [{kind}] {msg}")
