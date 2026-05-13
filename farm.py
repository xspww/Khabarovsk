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
from services.network_monitor import NetworkMonitor, NET_ONLINE
from services.ram_service import RAMManager
from services.vip_tracker import VipTracker
from services.process_service import ProcessManager, ProcessService
from services.resource_monitor import get_rt_monitor
from services.roblox_log_evidence import collect_recent_log_evidence
from runtime.account_runtime_controller import AccountRuntimeController
from runtime.command_tracker import RuntimeCommandTracker
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
from runtime.runtime_orchestrator import RuntimeOrchestrator
from runtime.lua_identity import lua_event_requires_pid_guard, resolve_lua_account
from runtime.farm_lifecycle import FarmLifecycleService
from runtime.recovery_view import recovery_step_for_account
from runtime.telemetry_view import build_runtime_telemetry
from runtime.runtime_view_model import RuntimeViewModelBuilder
from runtime.supervisor_runtime import SupervisorRuntime
from runtime.system_maintenance import (
    SystemMaintenance,
    _account_presence_user_id,
    _apply_cpu_limiter_for_bound_process,
    _window_arrange_settings_from_config,
    _window_resize_target_from_config,
)
from runtime.launch_controller import LaunchController, _redact_launch_detail
from runtime.roblox_watchdog import RobloxWatchdog
from runtime.recovery_support import _set_account_cookie_block, _clear_account_cookie_block
from runtime.recovery_engine import RecoveryCoordinator, RecoveryEngine
from runtime.account_worker import AccountWorker
from runtime.dispatcher import Dispatcher


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

        self._total_rejoin = 0
        self._total_crash = 0
        self._event_log: list = []
        self._event_lock = threading.RLock()
        self._status_lock = threading.Lock()
        self._status_revision = 0
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
                bind_result = ProcessService.bind_account_process(
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
            adopt = ProcessService.safe_adopt_visible_process(
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
        return self._lifecycle.start()

    def stop(self):
        return self._lifecycle.stop()

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

    def handle_lua_rejoin_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        event_name = str(payload.get("event") or "").strip().lower()
        if not event_name:
            return {"ok": False, "status_code": 400, "accepted": False, "msg": "Missing Lua event name"}
        resolution = resolve_lua_account(self._accounts, payload)
        identity = resolution.identity
        identity_name = identity.username or identity.account or identity.configured_account
        if not identity_name and not identity.user_id:
            return {"ok": False, "status_code": 400, "accepted": False, "msg": "Missing Lua identity"}
        if resolution.ambiguous:
            return {
                "ok": False,
                "status_code": 409,
                "accepted": False,
                "event": event_name,
                "account": identity_name,
                "candidates": list(resolution.candidates),
                "msg": "Lua identity matched multiple accounts",
            }
        if not resolution.account:
            return {
                "ok": False,
                "status_code": 404,
                "accepted": False,
                "event": event_name,
                "account": identity_name,
                "msg": "Account not found",
            }
        acc = resolution.account

        reason = str(payload.get("reason_key") or f"lua_{event_name}").strip() or f"lua_{event_name}"
        event_payload = {
            "trigger": event_name,
            "reason_key": reason,
            "detail": str(payload.get("detail") or payload.get("message") or reason),
            "popup_code": str(payload.get("error_code") or ""),
            "error_code": str(payload.get("error_code") or ""),
            "place_id": str(payload.get("place_id") or ""),
            "job_id": str(payload.get("job_id") or ""),
            "evidence_source": str(payload.get("evidence_source") or "lua_helper"),
            "visual_disconnect": str(payload.get("visual_disconnect") or "").lower() == "true",
            "lua_username": identity.username,
            "lua_user_id": identity.user_id,
            "lua_account": identity.account,
            "configured_account": identity.configured_account,
            "lua_pid": identity.pid or "",
            "matched_pid": resolution.bound_pid or "",
            "identity_match": resolution.match_reason,
        }

        if identity.pid and not resolution.pid_match and lua_event_requires_pid_guard(event_name):
            self._push_event(
                "lua",
                f"Lua helper ignored PID mismatch - {acc.display_name}",
                account=acc,
                severity="warning",
                reason="lua_pid_mismatch",
                lua_event=event_name,
                lua_pid=identity.pid,
                matched_pid=resolution.bound_pid or "",
                identity_match=resolution.match_reason,
                accepted=False,
            )
            return {
                "ok": True,
                "accepted": False,
                "event": event_name,
                "account": acc._config_username,
                "signal": "",
                "matched_pid": resolution.bound_pid,
                "lua_pid": identity.pid,
                "msg": "Lua event ignored because PID does not match Argus binding",
            }

        signal = ""
        accepted = True
        severity = "info"
        if event_name in {"loaded", "in_game"}:
            signal = RuntimeSignal.LAUNCH_SUCCESS.value
            accepted = self._runtime_orchestrator.handle_runtime_signal(
                acc,
                signal,
                reason,
                payload={**event_payload, "count_rejoin": None},
            )
        elif event_name in {"disconnect", "error_code"}:
            signal = RuntimeSignal.DISCONNECT_DETECTED.value
            severity = "critical"
            accepted = self._runtime_orchestrator.handle_runtime_signal(
                acc,
                signal,
                reason,
                payload=event_payload,
            )
            worker = self._workers.get(acc._config_username)
            if worker:
                worker.wake()
        elif event_name == "teleport_error":
            signal = RuntimeSignal.FAULT.value
            severity = "warning"
            accepted = self._runtime_orchestrator.handle_runtime_signal(
                acc,
                signal,
                reason,
                payload=event_payload,
            )
            worker = self._workers.get(acc._config_username)
            if worker:
                worker.wake()
        elif event_name == "rejoin_requested":
            signal = RuntimeSignal.REJOIN_REQUESTED.value
            severity = "warning"
            accepted = self._runtime_orchestrator.handle_runtime_signal(acc, signal, reason, payload=event_payload)
        elif event_name in {"heartbeat", "teleport_state"}:
            accepted = True
        else:
            return {
                "ok": False,
                "status_code": 400,
                "accepted": False,
                "event": event_name,
                "account": identity_name,
                "msg": "Unsupported Lua event",
            }

        self._push_event(
            "lua",
            f"Lua helper: {event_name} - {acc.display_name}",
            account=acc,
            severity=severity,
            reason=reason,
            lua_event=event_name,
            signal=signal,
            error_code=event_payload.get("error_code", ""),
            accepted=accepted,
        )
        return {
            "ok": True,
            "accepted": bool(accepted),
            "event": event_name,
            "account": acc._config_username,
            "matched_pid": resolution.bound_pid,
            "identity_match": resolution.match_reason,
            "signal": signal,
            "msg": "Lua event accepted" if accepted else "Lua event routed but not accepted",
        }

    def get_status(self) -> dict:
        return RuntimeViewModelBuilder(self).build_status()

    def get_runtime_health(self) -> dict:
        status = self.get_status()
        return {
            "ok": True,
            "runtime_health": status.get("runtime_health", {}),
            "queue_snapshot": status.get("queue_snapshot", {}),
            "status_revision": status.get("status_revision", 0),
            "status_updated_at": status.get("status_updated_at", 0.0),
        }

    def get_runtime_telemetry(self) -> dict:
        return build_runtime_telemetry(self.get_status())

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
