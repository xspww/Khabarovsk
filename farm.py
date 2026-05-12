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
from services.process_service import ProcessManager
from services.presence_service import PRESENCE_SERVICE
from services.resource_monitor import get_rt_monitor
from services.roblox_log_evidence import collect_recent_log_evidence
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
        recovery_status = str(acc.recovery_status or "").strip().lower()
        state_name = display_state.name
        if state_name == "COOLDOWN":
            return "Stabilizing", 7, float(acc.recovery_scheduled_at or acc.cooldown_until or acc.last_state_change_at or 0.0)
        if state_name == "IN_GAME" and not acc.recovery_inflight and str(acc.liveness_state or "").lower() in {"alive", "idle"}:
            return "Recovery Complete", 8, float(acc.in_game_since or acc.last_state_change_at or 0.0)
        if recovery_status == "checking_disconnect" or "checking_disconnect" in reason_text:
            return "Checking Disconnect", 4, float(acc.last_recovery_at or acc.last_state_change_at or 0.0)
        if state_name == "IN_GAME" and recovery_status in {"", "in_game"}:
            return "Recovery Complete", 8, float(acc.in_game_since or acc.last_state_change_at or 0.0)
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
            state_label = meta["label"]
            state_color = meta["color"]
            if recovery_step == "Checking Disconnect":
                state_label = "Checking Disconnect"
                state_color = "#a1a1aa"
            elif recovery_step in {"Rejoining Server", "Session Reconnect", "Network Rejoin", "Killing Process"}:
                state_label = "Rejoining"
                state_color = "#a1a1aa"
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
                "state_label": state_label,
                "state_color": state_color,
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
                self._recovery.handle_runtime_signal(
                    acc,
                    "network_lost",
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
