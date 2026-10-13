from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from core import (
    Account,
    EventBus,
    EventName,
    GlobalLaunchLimiter,
    StateManager,
    flog_kv,
)
from runtime.launch_attempt import LaunchAttempt
from runtime.maintenance_performance import _apply_cpu_limiter_for_bound_process
from runtime.runtime_state_manager import RuntimeStateManager
from runtime.runtime_store import RuntimeStore
from runtime.supervisor_runtime import SupervisorRuntime
from services.process_service import ProcessManager, ProcessService


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
        return LaunchAttempt(self, acc, stop).run()
