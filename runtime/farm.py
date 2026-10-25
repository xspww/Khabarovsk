from __future__ import annotations

import os
import threading
import time
from typing import Any

from core import (
    APP_DATA_DIR,
    Account,
    AccountState,
    ConfigManager,
    EventBus,
    EventName,
    SmartQueue,
    StateManager,
    flog,
    flog_kv,
)
from runtime.account_worker import AccountWorker
from runtime.command_rate_limit import FORCE_REJOIN_INTERVAL_SECONDS, PerAccountRateLimiter
from runtime.command_tracker import RuntimeCommandTracker
from runtime.dispatcher import Dispatcher
from runtime.farm_health import (
    build_farm_status,
)
from runtime.farm_health import (
    get_account as get_farm_account,
)
from runtime.farm_health import (
    get_detailed_farm_health as get_detailed_farm_health_payload,
)
from runtime.farm_health import (
    get_public_farm_health as get_public_farm_health_payload,
)
from runtime.farm_health import (
    get_runtime_diagnostics as get_runtime_diagnostics_payload,
)
from runtime.farm_health import (
    get_runtime_events as get_runtime_events_payload,
)
from runtime.farm_health import (
    get_runtime_health as get_runtime_health_payload,
)
from runtime.farm_health import (
    get_runtime_telemetry as get_runtime_telemetry_payload,
)
from runtime.farm_initial_sync import initial_state_sync
from runtime.farm_lifecycle import FarmLifecycleService
from runtime.lua_event_guard import lua_event_handler_error_response, validate_lua_event_payload
from runtime.lua_identity import resolve_lua_account
from runtime.lua_liveness_policy import lua_liveness_required, mark_waiting_for_lua
from runtime.lua_rejoin_events import handle_lua_rejoin_event as _handle_lua_rejoin_event
from runtime.lua_server_detection import LuaServerDetection, detect_lua_server
from runtime.machine_supervisor import MachineSupervisor
from runtime.recovery_engine import RecoveryEngine
from runtime.recovery_support import _clear_account_cookie_block
from runtime.recovery_view import recovery_step_for_account
from runtime.runtime_orchestrator import RuntimeOrchestrator
from runtime.runtime_state_manager import RuntimeStateManager
from runtime.runtime_store import RuntimeStore
from runtime.runtime_timeline import RuntimeTimeline
from runtime.supervisor_runtime import SupervisorRuntime
from runtime.system_maintenance import SystemMaintenance
from services.account_hybrid import ACCOUNT_STORE, audit_event
from services.auth_gate import evaluate_account_auth_gate, mark_account_auth_quarantined
from services.captcha_guard import (
    clear_account_captcha_hold,
)
from services.network_monitor import NET_OFFLINE, NET_ONLINE, NetworkMonitor
from services.process_service import ProcessManager, ProcessService
from services.verify_guard import clear_account_locked_hold, clear_account_recovery_hold


class FarmController:
    def __init__(self, cfg_mgr: ConfigManager):
        self.cfg_mgr = cfg_mgr
        self.bus = EventBus(
            workers=int(cfg_mgr.get("event_bus_workers", 4) or 4),
            max_pending=int(cfg_mgr.get("event_bus_max_pending", 128) or 128),
        )
        self._stop = threading.Event()
        self.running = False
        self.start_ts: float | None = None
        self._init_executor_hold()
        self._init_account_state()
        self._init_counters()
        self._init_persistence()
        self._init_console_liveness()
        self._init_command_plane()
        self._subscribe_events()

    def _init_executor_hold(self) -> None:
        self._executor_start_guard = None
        self._executor_hold = False
        self._executor_hold_reason = ""
        self._executor_hold_detail = ""
        self._executor_hold_lock = threading.RLock()

    def _init_account_state(self) -> None:
        self._accounts: list[Account] = []
        self._workers: dict[str, AccountWorker] = {}
        self._net_mon: NetworkMonitor | None = None
        self._dispatcher: Dispatcher | None = None
        self._queue: SmartQueue | None = None
        self._recovery: RecoveryEngine | None = None
        self._maintenance: SystemMaintenance | None = None
        self._state_mgr: StateManager | None = None
        self._runtime_scheduler: Any | None = None
        self._shutting_down = False
        self._last_control_plane_restart_at = 0.0
        self._force_rejoin_limiter = PerAccountRateLimiter(FORCE_REJOIN_INTERVAL_SECONDS)

    def _init_counters(self) -> None:
        self._total_rejoin = 0
        self._total_crash = 0
        self._event_log: list = []
        self._event_lock = threading.RLock()
        self._status_lock = threading.Lock()
        self._status_revision = 0
        self._status_cache_lock = threading.Lock()
        self._status_cache_snapshot: dict | None = None
        self._status_cache_revision = -1
        self._status_cache_expires_at = 0.0
        self._status_cache_hits = 0
        self._status_cache_misses = 0
        self._status_cache_last_build_ms = 0.0
        self._status_stream_clients = 0
        self._dashboard_process_cache_lock = threading.Lock()
        self._dashboard_process_cache: dict[Any, Any] = {}
        self._last_runtime_truth_state: dict[str, str] = {}
        self._last_runtime_truth_progress: dict[str, int] = {}
        self._runtime_state = RuntimeStateManager(logger=flog_kv)

    def _init_persistence(self) -> None:
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
        self._machine_supervisor = MachineSupervisor(self.cfg_mgr.snapshot(), self._accounts)

    def _init_console_liveness(self) -> None:
        try:
            from infra.console_activity import set_lua_liveness_required

            set_lua_liveness_required(lua_liveness_required(self.cfg_mgr.snapshot()))
        except Exception:
            "intentional: ignore exception"

    def _init_command_plane(self) -> None:
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

    def _subscribe_events(self) -> None:
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
            value = float(self.cfg_mgr.get("status_snapshot_cache_ttl_seconds", 5.0) or 0.0)
        except Exception:
            value = 5.0
        if value <= 0:
            return 0.0
        return min(10.0, max(0.5, value))

    def open_status_stream(self) -> int:
        with self._status_cache_lock:
            self._status_stream_clients += 1
            return self._status_stream_clients

    def close_status_stream(self) -> int:
        with self._status_cache_lock:
            self._status_stream_clients = max(0, self._status_stream_clients - 1)
            return self._status_stream_clients

    def _status_perf_snapshot(self, cache_hit: bool, cache_age: float = 0.0) -> dict[str, Any]:
        # Shape is public API (dashboard/diagnostics may read it) — keep keys.
        # Single owner for hit/miss accounting; called under _status_cache_lock.
        return {
            "cache_hit": bool(cache_hit),
            "cache_age_seconds": round(max(0.0, cache_age), 3),
            "cache_hits": int(self._status_cache_hits),
            "cache_misses": int(self._status_cache_misses),
            "last_build_ms": round(float(self._status_cache_last_build_ms or 0.0), 2),
            "active_stream_clients": int(self._status_stream_clients),
        }

    def _get_cached_status(self, revision: int, now: float) -> dict | None:
        cached = self._status_cache_snapshot
        if cached is None:
            return None
        if self._status_cache_revision != revision:
            return None
        if now >= self._status_cache_expires_at:
            return None
        self._status_cache_hits += 1
        cached_view = dict(cached)
        cached_view["status_perf"] = self._status_perf_snapshot(
            True, now - float(cached.get("status_updated_at") or now)
        )
        return cached_view

    def _store_status_snapshot(self, snapshot: dict, revision: int, build_ms: float, ttl: float) -> dict:
        self._status_cache_misses += 1
        self._status_cache_last_build_ms = build_ms
        snapshot["status_perf"] = self._status_perf_snapshot(False)
        if ttl > 0:
            self._status_cache_snapshot = snapshot
            self._status_cache_revision = int(snapshot.get("status_revision", revision) or 0)
            self._status_cache_expires_at = time.time() + ttl
        return snapshot

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

    def _find_account(self, username: str) -> Account | None:
        return next(
            (
                a for a in self._accounts
                if a._config_username == username or a.username == username
            ),
            None,
        )

    def mark_account_finished(self, acc: Any) -> None:
        """Drop pending recovery/cooldown so a Finished account stays quiet.

        Single owner for finish-side runtime reset — route modules leverage
        this instead of reaching into farm privates directly.
        """
        self._reset_account_runtime(acc, reason="account_finished")
        try:
            worker = (getattr(self, "_workers", None) or {}).get(
                getattr(acc, "_config_username", "")
            )
            if worker is not None:
                worker.wake()
        except Exception:
            "intentional: ignore exception"
        try:
            if hasattr(self, "_bump_status_revision"):
                self._bump_status_revision()
        except Exception:
            "intentional: ignore exception"

    def _reset_account_runtime(self, acc: Any, reason: str = "runtime_reset") -> None:
        """Clear recovery/process state so an account stays quiet."""
        if acc is None:
            return
        try:
            from core import AccountState as _AccountState

            runtime_state = getattr(self, "_runtime_state", None)
            if runtime_state is not None:
                with acc._lock:
                    if hasattr(runtime_state, "clear_recovery"):
                        runtime_state.clear_recovery(acc, reason=reason, inflight=False)
                    runtime_state.set_cooldown(acc, 0.0, reason=reason)
                    try:
                        runtime_state.set_desired(acc, _AccountState.IDLE, reason=reason)
                    except Exception:
                        "intentional: ignore exception"
                    # Clear stale markers so the card does not stick on Rejoining.
                    # Must mirror revive: recovery_view keys off these texts +
                    # last_recovery_at, so clear them all and stamp the reason.
                    try:
                        acc.last_crash_reason = ""
                        acc.last_recovery_reason = ""
                        acc.last_state_reason = reason
                        acc.last_recovery_at = 0.0
                        acc.last_rejoin_trigger = ""
                        acc.recovery_scheduled_at = 0.0
                        acc.recovery_inflight = False
                        acc.recovery_status = ""
                        acc.pid_missing_since = 0.0
                    except Exception:
                        "intentional: ignore exception"
                    try:
                        acc.sync_runtime(reason)
                    except Exception:
                        "intentional: ignore exception"
        except Exception:
            "intentional: ignore exception"

    def stop_account(self, username: str, reason: str = "game_unassigned") -> tuple[bool, str]:
        """Stop one account's client without marking the account Finished.

        Used when an account loses its launch target ("No game"): the old
        client must not keep farming the removed game, and the worker must
        not immediately try to rejoin a target-less account.
        """
        acc = self._find_account(username)
        if not acc:
            return False, "Account not found"
        if getattr(acc, "pid", 0):
            try:
                self.kill_account_pid(username, reason=reason)
            except Exception:
                "intentional: ignore exception"
        self._reset_account_runtime(acc, reason=reason)
        self._push_event(
            "system",
            f"Stopped - no game assigned: {acc.display_name}",
            account=acc,
            severity="warn",
            reason=reason,
        )
        try:
            if hasattr(self, "_bump_status_revision"):
                self._bump_status_revision()
        except Exception:
            "intentional: ignore exception"
        return True, f"Stopped: {username}"

    def _clear_stale_pid(self, acc: Account) -> None:
        try:
            from services.process_service import ProcessManager

            with acc._lock:
                pid = acc.pid
                try:
                    alive = bool(pid) and bool(
                        ProcessManager.is_bound_game_alive(
                            pid,
                            owner_key=acc._config_username,
                            expected_identity=acc.bound_process_identity,
                        )
                    )
                except Exception:
                    alive = False
                if not alive:
                    acc.pid = None
                    acc.bound_process_identity = None
                    acc.pid_missing_since = 0.0
        except Exception:
            "intentional: ignore exception"

    def _reset_unfinished_runtime(self, acc: Account) -> None:
        try:
            from core import AccountState as _AccountState

            runtime_state = getattr(self, "_runtime_state", None)
            if runtime_state is not None:
                with acc._lock:
                    if hasattr(runtime_state, "clear_recovery"):
                        runtime_state.clear_recovery(acc, reason="account_unfinished", inflight=False)
                    runtime_state.set_cooldown(acc, 0.0, reason="account_unfinished")
                    try:
                        runtime_state.set_desired(acc, _AccountState.IN_GAME, reason="account_unfinished")
                    except Exception:
                        "intentional: ignore exception"
                    # Drop stale Rejoining markers (kill/crash/rejoin text + old timestamps).
                    try:
                        acc.last_crash_reason = ""
                        acc.last_recovery_reason = ""
                        acc.last_state_reason = "account_unfinished"
                        acc.last_recovery_at = 0.0
                        acc.recovery_scheduled_at = 0.0
                        acc.last_rejoin_trigger = ""
                        acc.pid_missing_since = 0.0
                        acc.recovery_inflight = False
                        acc.recovery_status = ""
                        acc.sync_runtime("account_unfinished")
                    except Exception:
                        "intentional: ignore exception"
        except Exception:
            "intentional: ignore exception"

    def _force_idle_public_state(self, acc: Account) -> None:
        try:
            from core import AccountState as _AccountState

            state_mgr = getattr(self, "_state_mgr", None)
            if state_mgr is not None:
                try:
                    state_mgr.transition(acc, _AccountState.IDLE, reason="account_unfinished", force=True)
                except Exception:
                    "intentional: ignore exception"
                return
            rs = getattr(self, "_runtime_state", None)
            if rs is not None and hasattr(rs, "transition_public"):
                try:
                    rs.transition_public(acc, _AccountState.IDLE, reason="account_unfinished", force=True)
                except Exception:
                    "intentional: ignore exception"
        except Exception:
            "intentional: ignore exception"

    def _request_unfinished_evaluate(self, acc: Account) -> None:
        try:
            orchestrator = getattr(self, "_runtime_orchestrator", None)
            if orchestrator is not None and hasattr(orchestrator, "request_evaluate"):
                orchestrator.request_evaluate(acc, trigger="account_unfinished")
        except Exception:
            "intentional: ignore exception"

    def _wake_account_worker(self, acc: Account) -> None:
        try:
            worker = (getattr(self, "_workers", None) or {}).get(acc._config_username)
            if worker is not None:
                worker.wake()
        except Exception:
            "intentional: ignore exception"

    def _announce_unfinished_relaunch(self, acc: Account, log: Any) -> None:
        try:
            if hasattr(self, "_bump_status_revision"):
                self._bump_status_revision()
            if hasattr(self, "_push_event"):
                self._push_event(
                    "system",
                    f"Unfinished relaunch: {acc.display_name}",
                    account=acc,
                    severity="success",
                    reason="account_unfinished",
                )
        except Exception:
            "intentional: ignore exception"
        try:
            log("API", "account_unfinished_relaunched", account=acc.display_name)
        except Exception:
            "intentional: ignore exception"

    def revive_unfinished_account(self, username: str) -> bool:
        """Give an Unfinished account a clean relaunch while the farm runs.

        Single owner for unfinished-revive — route modules leverage this
        instead of reimplementing worker/state manipulation past the seam.
        """
        from core import flog_kv as _flog_kv

        acc = self._find_account(username)
        if acc is None:
            return False
        self._clear_stale_pid(acc)
        self._reset_unfinished_runtime(acc)
        try:
            from services.account_reload import (
                _prepare_added_runtime_account,
                _start_added_worker,
            )

            if not _prepare_added_runtime_account(self, acc):
                return False
            # Force public state back to IDLE so worker + recovery can launch fresh.
            # Without this, state != IN_GAME branch in AccountWorker just sleeps.
            self._force_idle_public_state(acc)
            started = bool(_start_added_worker(self, acc))
            # Critical: wake is not enough — queue a fresh evaluate like manual resume.
            self._request_unfinished_evaluate(acc)
            self._wake_account_worker(acc)
            self._announce_unfinished_relaunch(acc, _flog_kv)
            return started
        except Exception:
            return False

    def _capability_start(self, acc: Account | None) -> tuple[bool, str, Account | None]:
        if self.running:
            return False, "Already running", None
        return True, "", None

    def _capability_stop(self, acc: Account | None) -> tuple[bool, str, Account | None]:
        if not self.running:
            return False, "Not running", None
        return True, "", None

    def _force_rejoin_block_reason(self, acc: Account) -> str:
        """Single manual-rejoin gate shared by the API capability and the action."""
        recovery = self._recovery
        check = getattr(recovery, "force_rejoin_block_reason", None) if recovery else None
        if callable(check):
            try:
                return str(check(acc) or "")
            except Exception as exc:
                # Fail closed: an unavailable safety check must not become a
                # silent bypass of the captcha/auth/ban gate.
                flog_kv(
                    "COMMAND",
                    "force_rejoin_block_check_failed",
                    "warning",
                    account=getattr(acc, "display_name", ""),
                    error=exc,
                )
                return "Rejoin blocked: account check failed"
        try:
            from domain.account_model import is_account_finished

            if is_account_finished(acc):
                return "Account is Finished"
        except Exception:
            "intentional: ignore exception"
        if recovery is None:
            with acc._lock:
                if acc.state == AccountState.FAILED:
                    return "Account failed"
        return ""

    def _capability_force_rejoin(self, acc: Account | None) -> tuple[bool, str, Account | None]:
        if not self.running:
            return False, "Guard stopped", acc
        if not acc:
            return False, "Account not found", None
        block_reason = self._force_rejoin_block_reason(acc)
        if block_reason:
            return False, block_reason, acc
        return True, "", acc

    def _capability_kill_pid(self, acc: Account | None) -> tuple[bool, str, Account | None]:
        if not acc:
            return False, "Account not found", None
        with acc._lock:
            if not acc.pid:
                return False, "No active PID", acc
        return True, "", acc

    def _command_capability(self, action: str, account: str = "") -> tuple[bool, str, Account | None]:
        acc = self._find_account(account) if account else None
        checks = {
            "start": self._capability_start,
            "stop": self._capability_stop,
            "force_rejoin": self._capability_force_rejoin,
            "kill_pid": self._capability_kill_pid,
        }
        check = checks.get(action)
        if check is None:
            return True, "", acc
        return check(acc)

    def begin_command(
        self,
        key: str,
        action: str,
        account: str = "",
        ttl: float = 15.0,
        idempotency_key: str = "",
        request_fingerprint: str = "",
    ) -> tuple[bool, dict[str, Any]]:
        return self._command_tracker.begin(
            key,
            action,
            account=account,
            ttl=ttl,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )

    def finish_command(self, key: str, command_id: str, ok: bool = True, error: str = "", response: dict[str, Any] | None = None):
        self._command_tracker.finish(key, command_id, ok=ok, error=error, response=response)

    def _cancel_commands_for_shutdown(self) -> None:
        self._command_tracker.cancel_for_shutdown()

    def command_inflight(self, key: str) -> dict[str, Any] | None:
        return self._command_tracker.command_inflight(key)

    def _recovery_step_for_account(self, acc: Account, display_state: AccountState) -> tuple[str, int, float]:
        network_state = self._net_mon.get_state() if self._net_mon else NET_ONLINE
        return recovery_step_for_account(acc, display_state, network_state=network_state)

    def _initial_state_sync(self, state_mgr: StateManager | None = None):
        if state_mgr is None:
            state_mgr = StateManager(self.bus)
        cfg = self.cfg_mgr.snapshot() if hasattr(self, "cfg_mgr") else {}
        initial_state_sync(
            self._accounts,
            state_mgr,
            lua_required=lua_liveness_required(cfg),
            runtime_state=getattr(self, "_runtime_state", None),
        )

    def _preflight_quarantine(self, acc: Account, decision: Any, recovery: Any, runtime_state: Any) -> bool:
        try:
            mark_account_auth_quarantined(acc, decision, source="preflight", runtime_writer=runtime_state)
            recovery.fail_account(acc, decision.reason_key, decision.reason)
            return True
        except Exception as e:
            flog_kv("FARM", "preflight_fail_account_error", "warning", account=acc.display_name, error=e)
            return False

    def _log_preflight_block(self, acc: Account, decision: Any) -> None:
        try:
            flog_kv("FARM", "account_preflight_blocked", "warning", account=acc.display_name, **decision.to_dict())
        except Exception as e:
            flog_kv("FARM", "preflight_fail_account_error", "warning", account=acc.display_name, error=e)

    def _clear_cookie_mismatch(self, acc: Account, state_mgr: Any, runtime_state: Any) -> None:
        try:
            with acc._lock:
                if acc.state == AccountState.FAILED and acc.last_crash_reason == "cookie_mismatch":
                    _clear_account_cookie_block(acc)
                    runtime_state.clear_recovery(acc, reason="cookie_mismatch_cleared", inflight=False)
                    runtime_state.set_cooldown(acc, 0.0, reason="cookie_mismatch_cleared")
                    state_mgr.transition(acc, AccountState.IDLE, reason="cookie_mismatch_cleared", force=True)
        except Exception as e:
            flog_kv("FARM", "preflight_cookie_clear_error", "warning", account=acc.display_name, error=e)

    def _preflight_cookie_blocks(self) -> dict[str, str]:
        blocked: dict[str, str] = {}
        recovery = self._recovery
        state_mgr = self._state_mgr
        runtime_state = self._runtime_state
        if not recovery or not state_mgr:
            return blocked
        for acc in self._accounts:
            try:
                decision = evaluate_account_auth_gate(acc)
            except Exception as e:
                flog_kv("FARM", "preflight_auth_gate_error", "warning", account=acc.display_name, error=e)
                continue
            if decision.blocked:
                if self._preflight_quarantine(acc, decision, recovery, runtime_state):
                    blocked[acc._config_username] = decision.reason
                    self._log_preflight_block(acc, decision)
                continue
            self._clear_cookie_mismatch(acc, state_mgr, runtime_state)
        return blocked

    def start(self):
        if not self.running and self._executor_start_guard is not None:
            if not self._executor_start_guard():
                raise RuntimeError("Selected Roblox Executor could not be started")
        return self._lifecycle.start()

    def set_executor_start_guard(self, guard):
        self._executor_start_guard = guard

    def set_executor_hold(self, active: bool, reason: str = "", detail: str = "") -> None:
        """Hold new launches/rejoins while the executor waits for a patch.

        Keeps farm.running True so in-game clients stay alive; only new
        launch/rejoin scheduling is parked with reason wait_exploit_patch.
        """
        with self._executor_hold_lock:
            self._executor_hold = bool(active)
            self._executor_hold_reason = str(reason or "")
            self._executor_hold_detail = str(detail or "")
        try:
            recovery = getattr(self, "_recovery", None)
            check_setter = getattr(recovery, "set_executor_hold_check", None)
            if callable(check_setter):
                check_setter(self.is_executor_hold)
        except Exception:
            "intentional: ignore exception"
        try:
            self._bump_status_revision()
        except Exception:
            "intentional: ignore exception"

    def is_executor_hold(self) -> bool:
        try:
            with self._executor_hold_lock:
                return bool(self._executor_hold)
        except Exception:
            return False

    def get_executor_hold(self) -> dict[str, Any]:
        try:
            with self._executor_hold_lock:
                return {
                    "active": bool(self._executor_hold),
                    "reason": str(self._executor_hold_reason or ""),
                    "detail": str(self._executor_hold_detail or ""),
                }
        except Exception:
            return {"active": False, "reason": "", "detail": ""}

    def stop(self):
        try:
            self.set_executor_hold(False)
        except Exception:
            "intentional: ignore exception"
        return self._lifecycle.stop()

    def stop_fast(self):
        try:
            self.set_executor_hold(False)
        except Exception:
            "intentional: ignore exception"
        return self._lifecycle.stop_fast()

    def set_accounts(self, accounts: list[Account]):
        self._accounts = accounts
        try:
            from infra.console_activity import set_total_accounts

            set_total_accounts(len(self._accounts))
        except Exception:
            "intentional: ignore exception"
        self._machine_supervisor.set_accounts(self._accounts)
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
            from infra.console_activity import set_lua_liveness_required

            set_lua_liveness_required(lua_liveness_required(cfg))
        except Exception:
            "intentional: ignore exception"
        try:
            self._apply_runtime_config_snapshot(cfg)
        except Exception as e:
            flog_kv("CONFIG", "runtime_config_snapshot_failed", "warning", error=e)
        self._bump_status_revision()

    def _apply_runtime_config_snapshot(self, cfg: dict) -> None:
        accounts = list(self._accounts)
        if self._machine_supervisor:
            self._machine_supervisor.update_config(cfg)
            self._machine_supervisor.set_accounts(accounts)
        if self._recovery:
            update = getattr(self._recovery, "update_config", None)
            if callable(update):
                update(cfg, accounts)
        for component in (self._maintenance, self._dispatcher, *self._workers.values()):
            update = getattr(component, "update_config", None)
            if callable(update):
                update(cfg)

    def _check_force_rejoin_rate_limit(self, account_key: str) -> tuple[bool, str]:
        limiter = getattr(self, "_force_rejoin_limiter", None)
        if limiter is None:
            limiter = PerAccountRateLimiter(FORCE_REJOIN_INTERVAL_SECONDS)
            self._force_rejoin_limiter = limiter
        return limiter.check(account_key)

    def force_rejoin(
        self,
        username: str,
        reason: str = "force_rejoin",
        *,
        ignore_rate_limit: bool = False,
    ) -> tuple[bool, str]:
        acc = self._find_account(username)
        if acc:
            block_reason = self._force_rejoin_block_reason(acc)
            if block_reason:
                return False, block_reason
        if not (acc and self._recovery):
            return False, "Account not running or recovery coordinator unavailable"
        if not ignore_rate_limit:
            allowed, rate_msg = self._check_force_rejoin_rate_limit(acc._config_username)
            if not allowed:
                flog_kv("COMMAND", "force_rejoin_rate_limited", "warning", account=acc.display_name, msg=rate_msg)
                return False, rate_msg
        route_error = self._route_rejoin_command(acc, reason)
        if route_error:
            return False, route_error
        self._wake_rejoin_worker(acc)
        message = (
            f"Game changed - rejoin: {acc.display_name}"
            if reason == "game_changed"
            else f"Force rejoin: {acc.display_name}"
        )
        self._push_event("rejoin", message, account=acc, severity="warn", reason=reason)
        return True, f"Rejoin: {username}"

    def _route_rejoin_command(self, acc, reason: str) -> str:
        """Ask the orchestrator to rejoin; returns an empty string on success."""
        if self._runtime_orchestrator.request_rejoin(acc, reason):
            return ""
        with acc._lock:
            parked = str(acc.recovery_status or "") == "wait_exploit_patch"
        if parked:
            return "Rejoin deferred: waiting for executor patch"
        return "Rejoin rejected"

    def _wake_rejoin_worker(self, acc) -> None:
        worker = self._workers.get(acc._config_username)
        alive = bool(worker and getattr(worker, "is_alive", lambda: False)())
        if not alive:
            try:
                worker = self._ensure_resume_worker(acc)
            except Exception:
                "intentional: ignore exception"
        if not worker:
            return
        try:
            worker.wake()
        except Exception:
            "intentional: ignore exception"

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

    def kill_account_pid(self, username: str, reason: str = "api_kill_pid") -> tuple[bool, str]:
        acc = self._find_account(username)
        if not acc:
            return False, "Account not found"
        with acc._lock:
            pid = acc.pid
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

    def _set_lua_account_description(self, acc: Account, description: str) -> tuple[bool, str]:
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

    def resume_captcha_account(self, username: str) -> tuple[bool, str]:
        return self._resume_blocked_account(username, clear_account_captcha_hold, "Captcha", "captcha", "captcha_resume")

    def resume_locked_account(self, username: str) -> tuple[bool, str]:
        return self._resume_blocked_account(username, clear_account_locked_hold, "Locked", "verify", "locked_resume")

    def resume_recovery_account(self, username: str) -> tuple[bool, str]:
        return self._resume_blocked_account(username, clear_account_recovery_hold, "Recovery", "verify", "recovery_resume")

    def _clear_any_verify_hold(self, acc: Account) -> bool:
        was_any = False
        try:
            was_any = bool(clear_account_locked_hold(acc, runtime_writer=self._runtime_state)) or was_any
        except Exception:
            "intentional: ignore exception"
        try:
            was_any = bool(clear_account_recovery_hold(acc, runtime_writer=self._runtime_state)) or was_any
        except Exception:
            "intentional: ignore exception"
        try:
            was_any = bool(clear_account_captcha_hold(acc, runtime_writer=self._runtime_state)) or was_any
        except Exception:
            "intentional: ignore exception"
        return was_any

    def resume_verify_account(self, username: str) -> tuple[bool, str]:
        """Clear whichever verify hold is present (Locked / Recovery / Captcha)."""
        acc = self._find_account(username)
        if not acc:
            return False, "Account not found"
        try:
            from services.captcha_guard import is_account_captcha_required
            from services.verify_guard import is_account_locked, is_account_recovery

            if is_account_locked(acc):
                return self.resume_locked_account(username)
            if is_account_recovery(acc):
                return self.resume_recovery_account(username)
            if is_account_captcha_required(acc):
                return self.resume_captcha_account(username)
        except Exception:
            "intentional: ignore exception"
        # Fallback: try all clears so a stale mark never sticks.
        if self._clear_any_verify_hold(acc):
            return self._resume_blocked_account(username, lambda a, runtime_writer=None: False, "Verify", "verify", "verify_resume")
        return False, f"No Locked/Recovery/Captcha hold for {username}"

    def _clear_hold(self, acc: Account, clear_fn: Any) -> bool:
        try:
            return bool(clear_fn(acc, runtime_writer=self._runtime_state))
        except Exception:
            return False

    def _resume_still_blocked(
        self,
        acc: Account,
        username: str,
        decision: Any,
        kind_label: str,
        event_kind: str,
        was_blocked: bool,
    ) -> tuple[bool, str]:
        try:
            mark_account_auth_quarantined(acc, decision, source="manual_resume", runtime_writer=self._runtime_state)
            if self._recovery:
                self._recovery.fail_account(acc, decision.reason_key, decision.reason)
        except Exception as e:
            flog_kv("FARM", "captcha_resume_quarantine_error", "warning", account=acc.display_name, error=e)
            return False, f"{kind_label} cleared for {username}, but auth quarantine failed. Retry later."
        self.cfg_mgr.save_accounts(self._accounts)
        self.cfg_mgr.save_runtime(self._accounts)
        self._bump_status_revision()
        self._push_event(
            event_kind,
            f"{kind_label} cleared: {acc.display_name} - still blocked: {decision.reason}",
            account=acc,
            severity="warn",
            reason=decision.reason_key,
            was_captcha=was_blocked,
        )
        return False, f"{kind_label} cleared for {username}, but launch is still blocked: {decision.reason}"

    def _resume_set_desired(self, acc: Account) -> None:
        if not self._runtime_state:
            return
        with acc._lock:
            self._runtime_state.set_desired(acc, AccountState.IN_GAME, reason="manual_resume")
            self._runtime_state.set_cooldown(acc, 0.0, reason="manual_resume")

    def _resume_live_session(self, acc: Account) -> bool:
        with acc._lock:
            pid = acc.pid
            identity = acc.bound_process_identity
            tracker_id = acc.browser_tracker_id
        if not pid:
            return False
        return bool(
            ProcessManager.is_bound_game_alive(
                pid,
                owner_key=acc._config_username,
                expected_identity=identity,
                expected_browser_tracker_id=tracker_id,
            )
        )

    def _resume_apply_state(self, acc: Account, live_session: bool, lua_required: bool) -> None:
        if not self._state_mgr:
            return
        if not live_session:
            self._state_mgr.transition(acc, AccountState.IDLE, reason="manual_resume", force=True)
            return
        if lua_required:
            mark_waiting_for_lua(acc, self._runtime_state, self._state_mgr, "manual_resume_live_session")
        else:
            self._state_mgr.transition(acc, AccountState.IN_GAME, reason="manual_resume_live_session", force=True)
            self._state_mgr.clear_recovery(acc, reason="manual_resume_live_session", inflight=False)
        self._state_mgr.set_binding_status(acc, "verified", reason="manual_resume_live_session")

    def _resume_save_and_announce(
        self,
        acc: Account,
        kind_label: str,
        event_kind: str,
        resume_reason: str,
        was_blocked: bool,
    ) -> None:
        self.cfg_mgr.save_accounts(self._accounts)
        self.cfg_mgr.save_runtime(self._accounts)
        self._bump_status_revision()
        self._push_event(
            event_kind,
            f"{kind_label} cleared: {acc.display_name} - resume requested",
            account=acc,
            severity="warn",
            reason=resume_reason,
            was_captcha=was_blocked,
        )

    def _ensure_resume_worker(self, acc: Account) -> Any:
        worker = self._workers.get(acc._config_username)
        if worker and worker.is_alive():
            worker.wake()
            return worker
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
        return worker

    def _resume_finish(
        self,
        acc: Account,
        username: str,
        kind_label: str,
        live_session: bool,
        lua_required: bool,
    ) -> tuple[bool, str]:
        if not self.running:
            return True, f"{kind_label} cleared for {username}. Start Cronus when ready."
        if not self._recovery or not self._state_mgr:
            return False, "Recovery coordinator unavailable"
        if live_session:
            worker = self._workers.get(acc._config_username)
            if worker:
                worker.wake()
            if lua_required:
                return True, f"{kind_label} cleared for {username}. Waiting for Lua."
            return True, f"{kind_label} cleared for {username}. Live session verified."
        worker = self._ensure_resume_worker(acc)
        self._runtime_orchestrator.request_evaluate(acc, trigger="manual_resume")
        worker.wake()
        return True, f"{kind_label} cleared for {username}. Resume requested."

    def _resume_blocked_account(self, username: str, clear_fn: Any, kind_label: str, event_kind: str, resume_reason: str) -> tuple[bool, str]:
        acc = self._find_account(username)
        if not acc:
            return False, "Account not found"
        was_blocked = self._clear_hold(acc, clear_fn)
        try:
            decision = evaluate_account_auth_gate(acc)
        except Exception as e:
            flog_kv("FARM", "captcha_resume_auth_gate_error", "warning", account=acc.display_name, error=e)
            return False, f"{kind_label} cleared for {username}, but auth gate unavailable. Retry later."
        if decision.blocked:
            return self._resume_still_blocked(acc, username, decision, kind_label, event_kind, was_blocked)
        self._resume_set_desired(acc)
        live_session = self._resume_live_session(acc)
        lua_required = lua_liveness_required(self.cfg_mgr.snapshot())
        self._resume_apply_state(acc, live_session, lua_required)
        self._resume_save_and_announce(acc, kind_label, event_kind, resume_reason, was_blocked)
        return self._resume_finish(acc, username, kind_label, live_session, lua_required)

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

    def _normalize_server_detection(self, acc: Account, detection: LuaServerDetection) -> LuaServerDetection:
        if detection.server_type != "PUBLIC" or not self._account_expects_private_server(acc):
            return detection
        return LuaServerDetection(
            observed=True,
            server_type="VIP",
            is_vip=True,
            private_server_id=detection.private_server_id,
            private_server_owner_id=detection.private_server_owner_id,
            place_id=detection.place_id,
            job_id=detection.job_id,
            universe_id=detection.universe_id,
        )

    @staticmethod
    def _server_pid_key(acc: Account, payload: dict[str, Any]) -> str:
        return str(
            payload.get("pid")
            or payload.get("process_id")
            or payload.get("roblox_pid")
            or getattr(acc, "pid", "")
            or ""
        ).strip()

    @staticmethod
    def _server_log_key(detection: LuaServerDetection, pid_key: str) -> str:
        return ":".join(
            [
                detection.server_type,
                detection.private_server_id,
                detection.private_server_owner_id,
                detection.job_id,
                pid_key,
            ]
        )

    def _store_server_detection(
        self,
        acc: Account,
        detection: LuaServerDetection,
        now: float,
        log_key: str,
    ) -> bool:
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
                acc._last_server_detection_log_key = log_key
        return should_log

    def _log_server_detection(self, acc: Account, detection: LuaServerDetection, pid_key: str) -> None:
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

    def _hop_same_server(self, acc: Account, detection: LuaServerDetection) -> None:
        try:
            if detection.job_id and not detection.is_vip:
                from services.same_server_guard import check_and_hop_same_server

                check_and_hop_same_server(
                    self,
                    acc,
                    detection_job_id=detection.job_id,
                    detection_place_id=detection.place_id,
                )
        except Exception:
            "intentional: ignore exception"

    def _apply_lua_server_detection(self, acc: Account, payload: dict[str, Any]) -> dict[str, Any]:
        detection = detect_lua_server(payload)
        if not detection.observed:
            return {}

        now = time.time()
        detection = self._normalize_server_detection(acc, detection)
        pid_key = self._server_pid_key(acc, payload)
        log_key = self._server_log_key(detection, pid_key)
        if self._store_server_detection(acc, detection, now, log_key):
            self._log_server_detection(acc, detection, pid_key)

        # Block same-server: if another account already sits in this JobId,
        # hop this account to a different server (public servers only hop
        # cleanly; VIP private links stay put).
        self._hop_same_server(acc, detection)

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

    def _lua_event_handler_error(self, acc: Account, event_name: str, error: Exception) -> dict[str, Any]:
        flog_kv(
            "LUA_EVENT",
            "event_handler_exception",
            "warning",
            account=acc.display_name,
            event=event_name,
            error=error,
        )
        return lua_event_handler_error_response(acc, event_name)

    def handle_lua_rejoin_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _handle_lua_rejoin_event(
            self,
            payload,
            validate_payload=validate_lua_event_payload,
            resolve_account=resolve_lua_account,
            log=flog_kv,
            process_service=ProcessService,
        )

    def get_status(self, include_diagnostics: bool = False) -> dict:
        if include_diagnostics:
            return build_farm_status(self, include_diagnostics=True)
        if not self.running:
            return build_farm_status(self)

        ttl = self._status_cache_ttl()
        revision = self.get_status_revision()
        now = time.time()
        if ttl > 0:
            with self._status_cache_lock:
                cached_view = self._get_cached_status(revision, now)
                if cached_view is not None:
                    return cached_view

        started = time.perf_counter()
        snapshot = build_farm_status(self)
        build_ms = (time.perf_counter() - started) * 1000.0
        with self._status_cache_lock:
            return self._store_status_snapshot(snapshot, revision, build_ms, ttl)

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

    def get_account(self, username: str) -> dict | None:
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
        elif new == NET_OFFLINE:
            for acc in self._accounts:
                self._runtime_orchestrator.request_network_lost(
                    acc,
                    "network_drop",
                    payload={"trigger": f"net:{new.lower()}"},
                )
        else:
            # DEGRADED means the internet works but the Roblox homepage
            # probe flaked (heavy page, 4s timeout, regional filtering)
            # while game traffic is fine. Leave live sessions alone and
            # only log it — mass network_lost here used to kill playable
            # games into an endless Rejoining loop.
            flog_kv(
                "NET",
                "degraded_ignored",
                old=old,
                new=new,
                accounts=len(self._accounts),
            )

    def _push_event(self, kind: str, msg: str, account: Account | None = None, severity: str = "info", reason: str = "", **fields: Any):
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
