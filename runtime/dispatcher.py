from __future__ import annotations

import threading
import time
from typing import Any

from core import Account, AccountState, EventBus, SmartQueue, StateManager, flog, flog_kv
from domain.session_identity import build_launch_intent
from runtime.account_worker import AccountWorker
from runtime.launch_controller import LaunchController
from runtime.maintenance_performance import _window_arrange_settings_from_config, _window_resize_target_from_config
from runtime.recovery_engine import RecoveryEngine
from runtime.runtime_state_manager import RuntimeStateManager
from runtime.runtime_store import RuntimeStore
from runtime.supervisor_runtime import SupervisorRuntime
from services.network_monitor import NetworkMonitor
from services.process_service import ProcessManager, ProcessService


def _intent_text(intent: dict[str, Any], key: str, upper: bool = False) -> str:
    value = str(intent.get(key, "") or "").strip()
    return value.upper() if upper else value


def _observed_text(
    evidence: dict[str, Any],
    observed_key: str,
    source_key: str,
    value_key: str,
    upper: bool = False,
) -> str:
    value = str(evidence.get(observed_key, "") or "").strip()
    if not value and str(evidence.get(source_key, "") or "").lower() == "observed":
        value = str(evidence.get(value_key, "") or "").strip()
    return value.upper() if upper else value


def _place_mismatch(expected: str, actual: str) -> str:
    if not (expected and actual) or expected == actual:
        return ""
    return f"place_id mismatch expected={expected} actual={actual}"


def _server_mismatch(expected: str, actual: str) -> str:
    if not (expected and actual) or "UNKNOWN" in {expected, actual} or expected == actual:
        return ""
    return f"server_type mismatch expected={expected} actual={actual}"


def _private_hashes(intent: dict[str, Any], expected_hash: str) -> set[str]:
    configured = {
        str(item or "")
        for item in (intent.get("configured_private_link_code_hashes") or [])
        if str(item or "")
    }
    return {item for item in [expected_hash, *configured] if item}


def _private_match(expected_private: bool, observed_hash: str, expected_hashes: set[str]) -> bool:
    return bool(
        expected_private
        and observed_hash
        and expected_hashes
        and observed_hash in expected_hashes
    )


def _private_conflict(expected_hashes: set[str], observed_hash: str) -> bool:
    return bool(expected_hashes and observed_hash and observed_hash not in expected_hashes)


def _log_limited_evidence(
    acc: Account,
    evidence: dict[str, Any],
    validation: str,
    reason: str,
) -> None:
    flog_kv(
        "VERIFY",
        "destination_evidence_limited",
        "warning",
        account=acc.display_name,
        validation=validation,
        evidence_source=str(evidence.get("evidence_source", "")),
        reason=reason,
        session_id=acc.session_id,
        transaction_id=acc.rejoin_transaction_id,
        runtime_generation=acc.runtime_generation,
    )


def _place_server_result(
    intent: dict[str, Any],
    evidence: dict[str, Any],
) -> tuple[bool, str, str] | None:
    expected_place = _intent_text(intent, "place_id")
    actual_place = _observed_text(evidence, "observed_place_id", "place_id_source", "place_id")
    mismatch = _place_mismatch(expected_place, actual_place)
    if mismatch:
        return False, "intent_mismatch_place_id", mismatch
    expected_server = _intent_text(intent, "server_type", upper=True)
    actual_server = _observed_text(
        evidence,
        "observed_server_type",
        "server_type_source",
        "server_type",
        upper=True,
    )
    mismatch = _server_mismatch(expected_server, actual_server)
    if mismatch:
        return False, "intent_mismatch_server_type", mismatch
    return None


def _private_validation(
    intent: dict[str, Any],
    evidence: dict[str, Any],
    expected_place: str,
    actual_place: str,
) -> tuple[bool, str, str] | None:
    expected_private = bool(intent.get("private_server_intent"))
    expected_hash = str(intent.get("active_private_link_code_hash", "") or "")
    expected_hashes = _private_hashes(intent, expected_hash)
    observed_hash = str(evidence.get("observed_private_link_code_hash", "") or "")
    if _private_match(expected_private, observed_hash, expected_hashes):
        if expected_place and actual_place and expected_place != actual_place:
            return (
                False,
                "intent_mismatch_place_id",
                f"place_id mismatch expected={expected_place} actual={actual_place}",
            )
        return True, "private_server_verified", ""
    if _private_conflict(expected_hashes, observed_hash):
        return False, "intent_mismatch_private_server", "private server intent mismatch"
    return None


class Dispatcher(threading.Thread):
    def __init__(
        self,
        queue: SmartQueue,
        launcher: LaunchController,
        state_mgr: StateManager,
        bus: EventBus,
        workers: dict[str, AccountWorker],
        recovery: RecoveryEngine,
        net: NetworkMonitor,
        stop: threading.Event,
        cfg: dict | None = None,
        runtime_state: RuntimeStateManager | None = None,
        runtime_store: RuntimeStore | None = None,
        supervisor: SupervisorRuntime | None = None,
        accounts: list | None = None, machine_supervisor: Any | None = None,
    ):
        super().__init__(daemon=True, name="Dispatcher")
        self._queue = queue
        self._launcher = launcher
        self._state_mgr = state_mgr
        self._bus = bus
        self._workers = workers
        self._recovery = recovery
        self._runtime_owner = getattr(recovery, "runtime_orchestrator", recovery)
        self._net = net
        # NOTE: must not be named `self._stop` — it shadows
        # threading.Thread._stop() and breaks join() on Python <=3.13
        # ("TypeError: 'Event' object is not callable").
        self._stop_event = stop
        self._cfg = cfg or {}
        self._runtime_state = runtime_state or RuntimeStateManager(logger=flog_kv)
        self._runtime_store = runtime_store
        self._supervisor = supervisor
        self._accounts = accounts or []
        self._machine_supervisor = machine_supervisor

    def update_config(self, cfg: dict) -> None:
        self._cfg = cfg or {}
        if self._launcher:
            self._launcher.update_config(self._cfg)

    def _requeue_machine_hold(self, acc: Account, decision: Any) -> None:
        # Single owner for machine-supervisor deferrals. Same generations +
        # delay math as before, only extracted for readability.
        with acc._lock:
            runtime_generation = acc.runtime_generation
            recovery_generation = acc.recovery_generation
        self._queue.push(
            acc,
            reason=f"machine_hold:{decision.reason}",
            runtime_generation=runtime_generation,
            recovery_generation=recovery_generation,
            delay_seconds=max(1.0, float(decision.retry_after_seconds or 1.0)),
        )

    def _apply_window_layout(
        self,
        acc: Account,
        target: Any,
        arrange: Any,
    ) -> tuple[dict[str, Any], int, str]:
        if arrange:
            width, height, columns, rows, gap, margin = arrange
            result = ProcessService.arrange_roblox_windows(
                width,
                height,
                columns,
                gap,
                margin,
                unlock_size=bool(self._cfg.get("roblox_window_unlock_size_enabled", True)),
                resize=bool(self._cfg.get("roblox_window_resize_enabled", False)),
                rows=rows,
                reason="post_launch_window_apply",
                account=acc,
            )
            return result, int(result.get("arranged") or 0), "post_launch_arrange"
        if target:
            width, height = target
            result = ProcessService.resize_roblox_windows(
                width,
                height,
                unlock_size=bool(self._cfg.get("roblox_window_unlock_size_enabled", True)),
                reason="post_launch_window_apply",
                account=acc,
            )
            return result, int(result.get("resized") or 0), "post_launch_resize"
        return {}, 0, "post_launch_hide"

    def _log_window_apply(
        self,
        acc: Account,
        event: str,
        result: dict[str, Any],
        changed: int,
    ) -> None:
        if changed <= 0:
            return
        flog_kv(
            "WINDOW",
            event,
            account=acc.display_name,
            arranged=result.get("arranged", 0),
            resized=result.get("resized", 0),
            count=result.get("count", 0),
            width=result.get("width", ""),
            height=result.get("height", ""),
            columns=result.get("columns", ""),
        )

    def _apply_window_resize_after_launch(self, acc: Account) -> None:
        target = _window_resize_target_from_config(self._cfg)
        arrange = _window_arrange_settings_from_config(self._cfg)
        hide_enabled = bool(self._cfg.get("roblox_window_hide_enabled", False))
        if not (target or arrange or hide_enabled):
            return
        result, changed, event = self._apply_window_layout(acc, target, arrange)
        self._log_window_apply(acc, event, result, changed)
        if hide_enabled:
            try:
                ProcessService.hide_roblox_windows(reason="post_launch_window_hide", account=acc)
            except Exception:
                "intentional: ignore exception"

    def _record_transaction(self, acc: Account, snapshot: dict[str, Any], session_status: str = "active"):
        if not self._runtime_store:
            return
        try:
            session = snapshot.get("session") or snapshot
            if session.get("session_id"):
                session_record = dict(session)
                session_record["recovery_generation"] = snapshot.get("recovery_generation", acc.recovery_generation)
                session_record["command_generation"] = snapshot.get("command_generation", acc.command_generation)
                self._runtime_store.record_session(session_record, status=session_status)
            if snapshot.get("transaction_id"):
                self._runtime_store.record_transaction(snapshot)
            self._runtime_store.record_account_snapshot(acc._config_username, acc.runtime_snapshot())
        except Exception as e:
            flog_kv("RUNTIME", "store_transaction_failed", "warning", account=acc.display_name, error=e)

    def _record_stale_transaction(self, acc: Account, expected: dict[str, Any], reason: str):
        if not expected:
            return
        snapshot = {
            "transaction_id": str(expected.get("transaction_id", "") or ""),
            "account_id": acc._config_username,
            "runtime_generation": int(expected.get("runtime_generation", 0) or 0),
            "recovery_generation": acc.recovery_generation,
            "command_generation": acc.command_generation,
            "account_runtime_id": acc.account_runtime_id,
            "session_id": str(expected.get("session_id", "") or ""),
            "launch_nonce": str(expected.get("launch_nonce", "") or ""),
            "status": "rolled_back",
            "step": "stale_rejected",
            "reason": reason,
            "failure_reason": "stale_work_rejected",
            "launch_intent": dict(acc.launch_intent or {}),
            "destination_evidence": {},
            "created_at": acc.session_started_at or time.time(),
            "updated_at": time.time(),
            "completed_at": time.time(),
        }
        self._record_transaction(acc, snapshot, session_status="rolled_back")
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
        if self._supervisor:
            self._supervisor.emit("RecoverySupervisor", "TRANSACTION_STALE_REJECTED", account=acc, severity="warning", reason=reason, payload=snapshot)

    def _destination_evidence(self, acc: Account) -> dict[str, Any]:
        current_intent = build_launch_intent(acc, reason="destination_evidence")
        evidence = {
            "configured_place_id": str(getattr(acc, "place_id", "") or ""),
            "configured_server_type": getattr(getattr(acc, "server_type", None), "value", str(getattr(acc, "server_type", "") or "")),
            "observed_place_id": "",
            "observed_server_type": "",
            "observed_private_link_code_hash": "",
            "active_vip_present": bool(getattr(acc, "active_vip", "") or ""),
            "active_private_link_code_hash": str(current_intent.get("active_private_link_code_hash", "") or ""),
            "private_server_intent": bool(current_intent.get("private_server_intent", False)),
            "launch_strategy": str(getattr(acc, "launch_strategy", "") or ""),
            "evidence_source": "launch_intent_and_account_config",
        }
        try:
            if acc.pid:
                from domain.roblox_private_servers import parse_launch_destination_from_cmdline

                parsed = parse_launch_destination_from_cmdline(ProcessManager.get_pid_cmdline(acc.pid))
                if parsed:
                    evidence.update({k: v for k, v in parsed.items() if v not in (None, "")})
        except Exception:
            "intentional: ignore exception"
        return evidence

    def _validate_launch_intent(self, acc: Account, evidence: dict[str, Any]) -> tuple[bool, str, str]:
        intent = dict(getattr(acc, "launch_intent", {}) or {})
        result = _place_server_result(intent, evidence)
        if result is not None:
            return result

        expected_place = _intent_text(intent, "place_id")
        actual_place = _observed_text(evidence, "observed_place_id", "place_id_source", "place_id")
        result = _private_validation(intent, evidence, expected_place, actual_place)
        if result is not None:
            return result

        if actual_place and expected_place == actual_place:
            return True, "intent_verified_place", ""

        if bool(intent.get("private_server_intent")):
            _log_limited_evidence(
                acc,
                evidence,
                "private_server_unverified",
                "no_observed_private_server_or_job_evidence",
            )
            return True, "private_server_unverified", ""

        if expected_place:
            _log_limited_evidence(
                acc,
                evidence,
                "intent_verified_no_job_evidence",
                "no_observed_job_evidence",
            )
            return True, "intent_verified_no_job_evidence", ""

        _log_limited_evidence(
            acc,
            evidence,
            "intent_recorded_no_destination_evidence",
            "no_configured_or_observed_destination",
        )
        return True, "intent_recorded_no_destination_evidence", ""

    def _finish_transaction(
        self,
        acc: Account,
        status: str,
        reason: str,
        evidence: dict[str, Any] | None = None,
        server_validation: str = "",
        expected: dict[str, Any] | None = None,
    ) -> bool:
        with acc._lock:
            expected = expected or {}
            if not self._runtime_state.guard_session_identity(
                acc,
                expected_generation=expected.get("runtime_generation"),
                expected_session_id=str(expected.get("session_id", "") or ""),
                expected_launch_nonce=str(expected.get("launch_nonce", "") or ""),
                expected_transaction_id=str(expected.get("transaction_id", "") or ""),
                reason=f"finish_transaction:{reason}",
            ):
                self._record_stale_transaction(acc, expected, f"finish_transaction:{reason}")
                return False
            snapshot = self._runtime_state.finish_rejoin_transaction(
                acc,
                status=status,
                reason=reason,
                destination_evidence=evidence or {},
                server_validation=server_validation,
            )
        self._record_transaction(acc, snapshot, session_status=status)
        if self._supervisor:
            event_name = "END_REJOIN_TRANSACTION" if status == "committed" else "ROLLBACK_REJOIN_TRANSACTION"
            self._supervisor.emit("RecoverySupervisor", event_name, account=acc, severity="success" if status == "committed" else "warning", reason=reason, payload=snapshot)
        return True

    def _wait_for_online(self) -> bool:
        if self._net.is_online():
            return True
        self._net.wait_until_online(timeout=5)
        return False

    def _next_account(self) -> Account | None:
        self._queue.wait_until_free(self._stop_event)
        if self._stop_event.is_set():
            return None
        acc = self._queue.pop(timeout=1.0)
        if acc is None:
            return None
        if acc.state != AccountState.QUEUED:
            flog(f"[DISPATCHER] skip {acc.display_name} (state={acc.state.name})")
            return None
        return acc

    def _executor_hold(self, acc: Account) -> bool:
        try:
            recovery = getattr(self, "_recovery", None)
            held = bool(recovery._executor_held()) if hasattr(recovery, "_executor_held") else False
        except Exception:
            held = False
        if not held:
            return False
        try:
            self._recovery._park_for_executor_hold(acc, "dispatcher_hold")
        except Exception:
            "intentional: ignore exception"
        return True

    def _machine_hold(self, acc: Account) -> bool:
        if not self._machine_supervisor:
            return False
        self._machine_supervisor.set_accounts(self._accounts)
        decision = self._machine_supervisor.launch_decision(acc)
        if decision.allowed:
            return False
        self._machine_supervisor.log_decision(acc, decision)
        self._requeue_machine_hold(acc, decision)
        return True

    def _begin_launch(self, acc: Account) -> dict[str, Any]:
        launch_intent = build_launch_intent(acc, reason="dispatcher_launch")
        with acc._lock:
            tx_snapshot = self._runtime_state.begin_rejoin_transaction(
                acc,
                reason="dispatcher_launch",
                launch_intent=launch_intent,
            )
            tx_guard = {
                "transaction_id": tx_snapshot.get("transaction_id", ""),
                "session_id": tx_snapshot.get("session_id", ""),
                "launch_nonce": tx_snapshot.get("launch_nonce", ""),
                "runtime_generation": tx_snapshot.get("runtime_generation", 0),
            }
        self._record_transaction(acc, tx_snapshot, session_status="pending")
        if self._supervisor:
            self._supervisor.emit(
                "RecoverySupervisor",
                "BEGIN_REJOIN_TRANSACTION",
                account=acc,
                reason="dispatcher_launch",
                payload=tx_snapshot,
            )
        self._state_mgr.transition(acc, AccountState.LAUNCHING, reason="dispatcher_launch")
        return tx_guard

    def _launch_and_settle(self, acc: Account, tx_guard: dict[str, Any]) -> None:
        flog(f"[DISPATCHER] launching {acc.display_name}")
        success = self._launcher.launch(acc, self._stop_event)
        if self._stop_event.is_set() or acc.desired_state != AccountState.IN_GAME:
            flog_kv(
                "DISPATCHER",
                "launch_result_ignored",
                account=acc.display_name,
                stopped=self._stop_event.is_set(),
                desired=getattr(acc.desired_state, "name", acc.desired_state),
            )
            self._finish_transaction(
                acc,
                "rolled_back",
                "stopped_or_not_desired",
                server_validation="aborted",
                expected=tx_guard,
            )
            return
        pid_is_live = bool(
            acc.pid
            and ProcessManager.is_bound_game_alive(
                acc.pid,
                owner_key=acc._config_username,
                expected_identity=acc.bound_process_identity,
            )
        )
        if success or (acc.state == AccountState.IN_GAME and pid_is_live):
            self._accept_launch(acc, tx_guard, success)
        else:
            self._fail_launch(acc, tx_guard)

    def _accept_launch(self, acc: Account, tx_guard: dict[str, Any], success: bool) -> None:
        if not success:
            flog(
                f"[DISPATCHER] suppressing launch_failed for {acc.display_name} "
                f"because account is already back IN_GAME on pid={acc.pid}"
            )
        with acc._lock:
            launch_trigger = acc.last_rejoin_trigger or "dispatcher_launch"
        evidence = self._destination_evidence(acc)
        evidence.update({
            "pid": acc.pid,
            "process_identity": acc.bound_process_identity,
        })
        intent_ok, server_validation, intent_failure = self._validate_launch_intent(acc, evidence)
        if not intent_ok:
            self._rollback_intent(acc, tx_guard, evidence, server_validation, intent_failure)
            return
        committed = self._finish_transaction(
            acc,
            "committed",
            launch_trigger,
            evidence=evidence,
            server_validation=server_validation,
            expected=tx_guard,
        )
        if not committed:
            return
        self._runtime_owner.handle_runtime_signal(
            acc,
            "launch_success",
            launch_trigger,
            payload={"trigger": launch_trigger},
            expected_runtime_generation=tx_guard.get("runtime_generation"),
            expected_session_id=str(tx_guard.get("session_id", "") or ""),
            expected_launch_nonce=str(tx_guard.get("launch_nonce", "") or ""),
            expected_transaction_id=str(tx_guard.get("transaction_id", "") or ""),
        )
        self._apply_window_resize_after_launch(acc)
        worker = self._workers.get(acc._config_username)
        if worker:
            worker.wake()

    def _rollback_intent(
        self,
        acc: Account,
        tx_guard: dict[str, Any],
        evidence: dict[str, Any],
        server_validation: str,
        intent_failure: str,
    ) -> None:
        detail = intent_failure or "server_intent_mismatch"
        rolled_back = self._finish_transaction(
            acc,
            "rolled_back",
            detail,
            evidence=evidence,
            server_validation=server_validation,
            expected=tx_guard,
        )
        if not rolled_back:
            return
        ProcessService.safe_kill_bound_process(
            acc,
            self._state_mgr,
            reason="server_intent_mismatch",
            expected_runtime_generation=tx_guard.get("runtime_generation"),
        )
        with acc._lock:
            signal_generation = acc.runtime_generation
        self._runtime_owner.handle_runtime_signal(
            acc,
            "launch_failure",
            detail,
            payload={"detail": detail},
            expected_runtime_generation=signal_generation,
            expected_session_id=str(tx_guard.get("session_id", "") or ""),
            expected_launch_nonce=str(tx_guard.get("launch_nonce", "") or ""),
            expected_transaction_id=str(tx_guard.get("transaction_id", "") or ""),
        )

    def _specific_launch_detail(self, acc: Account) -> str:
        specific_detail = ""
        try:
            with acc._lock:
                specific_detail = str(
                    acc.last_transaction_failure_reason
                    or acc.last_transaction_reason
                    or ""
                )
        except Exception:
            specific_detail = ""
        if not specific_detail.strip() or specific_detail.strip().lower() in {
            "launch_failed",
            "launch_sent",
            "dispatcher_launch",
        }:
            specific_detail = "launch_failed"
        return specific_detail

    def _fail_launch(self, acc: Account, tx_guard: dict[str, Any]) -> None:
        specific_detail = self._specific_launch_detail(acc)
        rolled_back = self._finish_transaction(
            acc,
            "rolled_back",
            "launch_failed",
            server_validation="launch_failed",
            expected=tx_guard,
        )
        if not rolled_back:
            return
        try:
            with acc._lock:
                acc.last_transaction_failure_reason = specific_detail
        except Exception:
            "intentional: ignore exception"
        self._runtime_owner.handle_runtime_signal(
            acc,
            "launch_failure",
            "launch_failed",
            payload={"detail": specific_detail},
            expected_runtime_generation=tx_guard.get("runtime_generation"),
            expected_session_id=str(tx_guard.get("session_id", "") or ""),
            expected_launch_nonce=str(tx_guard.get("launch_nonce", "") or ""),
            expected_transaction_id=str(tx_guard.get("transaction_id", "") or ""),
        )

    def run(self):
        flog("[DISPATCHER] started")
        while not self._stop_event.is_set():
            if not self._wait_for_online():
                continue
            acc = self._next_account()
            if acc is None:
                continue
            if self._executor_hold(acc):
                continue
            if self._machine_hold(acc):
                continue
            self._queue.mark_busy()
            tx_guard = self._begin_launch(acc)
            try:
                self._launch_and_settle(acc, tx_guard)
            finally:
                self._queue.mark_free()
        flog("[DISPATCHER] stopped")
