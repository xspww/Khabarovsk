from __future__ import annotations

import threading
import time
from typing import Any

from core import (
    Account,
    EventBus,
    EventName,
    GlobalLaunchLimiter,
    StateManager,
    flog_kv,
)
from domain._util import as_int as _validation_int
from domain._util import mapping_text as _expected_text
from runtime.launch_attempt import LaunchAttempt
from runtime.maintenance_performance import _apply_cpu_limiter_for_bound_process
from runtime.runtime_state_manager import RuntimeStateManager
from runtime.runtime_store import RuntimeStore
from runtime.supervisor_runtime import SupervisorRuntime
from services.process_service import ProcessManager, ProcessService


def _transaction_reason(reason: str, step: str, status: str) -> str:
    return f"transaction_update:{reason or step or status}"


def _validation_ram_mb(validation: dict[str, Any]) -> float:
    return float(validation.get("ram_mb") or validation.get("rss_mb") or 0.0)


def _stable_binding(
    validation: dict[str, Any],
    expected_identity: str,
    acc: Account,
    pid: int,
) -> bool:
    identity = str(validation.get("identity") or "")
    owner = str(validation.get("owner") or ProcessManager.get_pid_owner(pid) or "")
    exact_existing_identity = bool(expected_identity and identity == expected_identity)
    owner_claim_matches = bool(owner and owner == acc._config_username)
    return (
        _validation_int(validation, "windows") > 0
        or _validation_ram_mb(validation) >= 100.0
        or exact_existing_identity
        or owner_claim_matches
    )


class LaunchController:
    def __init__(
        self,
        limiter: GlobalLaunchLimiter,
        state_mgr: StateManager,
        bus: EventBus,
        cfg: dict,
        accounts: list[Account] | None = None,
        runtime_state: RuntimeStateManager | None = None,
        runtime_store: RuntimeStore | None = None,
        supervisor: SupervisorRuntime | None = None,
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
                "intentional: ignore exception"

    def _record_transaction(self, acc: Account, snapshot: dict[str, Any]):
        if self._runtime_store and snapshot.get("transaction_id"):
            try:
                self._runtime_store.record_transaction(snapshot)
                self._runtime_store.record_account_snapshot(acc._config_username, acc.runtime_snapshot())
            except Exception as e:
                flog_kv("RUNTIME", "store_transaction_failed", "warning", account=acc.display_name, error=e)

    def _record_stale_transaction(self, acc: Account, expected: dict[str, Any], reason: str):
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

    def _guard_transaction_identity(self, acc: Account, expected: dict[str, Any], label: str) -> bool:
        return self._runtime_state.guard_session_identity(
            acc,
            expected_generation=expected.get("runtime_generation"),
            expected_session_id=_expected_text(expected, "session_id"),
            expected_launch_nonce=_expected_text(expected, "launch_nonce"),
            expected_transaction_id=_expected_text(expected, "transaction_id"),
            reason=label,
        )

    def _transaction_update(
        self,
        acc: Account,
        status: str = "",
        step: str = "",
        reason: str = "",
        server_validation: str = "",
        expected: dict[str, Any] | None = None,
    ) -> bool:
        label = _transaction_reason(reason, step, status)
        with acc._lock:
            expected = expected or {}
            if expected and not self._guard_transaction_identity(acc, expected, label):
                self._record_stale_transaction(acc, expected, label)
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
            self._supervisor.emit(
                "JoinSupervisor",
                f"TRANSACTION_{(step or status or 'UPDATE').upper()}",
                account=acc,
                reason=reason,
                payload=snapshot,
            )
        return True

    def _bind_live_game(
        self,
        acc: Account,
        pid: int,
        process_name: str,
        reason: str,
        expected_runtime_generation: int | None = None,
        launched_after: float | None = None,
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
        launched_after: float | None,
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

        if _stable_binding(validation, expected_identity, acc, pid):
            return True

        windows = _validation_int(validation, "windows")
        ram_mb = _validation_ram_mb(validation)
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
        launched_after: float | None = None,
        expected_runtime_generation: int | None = None,
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
        expected_runtime_generation: int | None = None,
    ) -> dict[str, Any]:
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

    def _visible_process_presence(self, exclude_pids: list[int] | None = None) -> dict[str, Any]:
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
