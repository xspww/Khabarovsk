from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Tuple

from core import Account, AccountState, EventBus, SmartQueue, StateManager, flog, flog_kv
from domain.session_identity import build_launch_intent
from services.network_monitor import NetworkMonitor
from services.process_service import ProcessManager, ProcessService
from runtime.account_worker import AccountWorker
from runtime.launch_controller import LaunchController
from runtime.recovery_engine import RecoveryEngine
from runtime.runtime_state_manager import RuntimeStateManager
from runtime.runtime_store import RuntimeStore
from runtime.supervisor_runtime import SupervisorRuntime
from runtime.system_maintenance import _window_arrange_settings_from_config, _window_resize_target_from_config


class Dispatcher(threading.Thread):
    def __init__(
        self,
        queue: SmartQueue,
        launcher: LaunchController,
        state_mgr: StateManager,
        bus: EventBus,
        workers: Dict[str, AccountWorker],
        recovery: RecoveryEngine,
        net: NetworkMonitor,
        stop: threading.Event,
        cfg: Optional[dict] = None,
        runtime_state: Optional[RuntimeStateManager] = None,
        runtime_store: Optional[RuntimeStore] = None,
        supervisor: Optional[SupervisorRuntime] = None,
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
        self._stop = stop
        self._cfg = cfg or {}
        self._runtime_state = runtime_state or RuntimeStateManager(logger=flog_kv)
        self._runtime_store = runtime_store
        self._supervisor = supervisor

    def _apply_window_resize_after_launch(self, acc: Account) -> None:
        target = _window_resize_target_from_config(self._cfg)
        if not target:
            return
        width, height = target
        arrange = _window_arrange_settings_from_config(self._cfg)
        if arrange:
            width, height, columns, gap, margin = arrange
            result = ProcessService.arrange_roblox_windows(
                width,
                height,
                columns,
                gap,
                margin,
                reason="post_launch_window_apply",
                account=acc,
            )
            changed = int(result.get("arranged") or 0)
            event = "post_launch_arrange"
        else:
            result = ProcessService.resize_roblox_windows(
                width,
                height,
                reason="post_launch_window_apply",
                account=acc,
            )
            changed = int(result.get("resized") or 0)
            event = "post_launch_resize"
        if changed > 0:
            flog_kv(
                "WINDOW",
                event,
                account=acc.display_name,
                arranged=result.get("arranged", 0),
                resized=result.get("resized", 0),
                count=result.get("count", 0),
                width=width,
                height=height,
                columns=result.get("columns", ""),
            )

    def _record_transaction(self, acc: Account, snapshot: Dict[str, Any], session_status: str = "active"):
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

    def _record_stale_transaction(self, acc: Account, expected: Dict[str, Any], reason: str):
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

    def _destination_evidence(self, acc: Account) -> Dict[str, Any]:
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
                from roblox_hybrid import parse_launch_destination_from_cmdline

                parsed = parse_launch_destination_from_cmdline(ProcessManager.get_pid_cmdline(acc.pid))
                if parsed:
                    evidence.update({k: v for k, v in parsed.items() if v not in (None, "")})
        except Exception:
            pass
        return evidence

    def _validate_launch_intent(self, acc: Account, evidence: Dict[str, Any]) -> Tuple[bool, str, str]:
        intent = dict(getattr(acc, "launch_intent", {}) or {})
        expected_place = str(intent.get("place_id", "") or "").strip()
        actual_place = str(evidence.get("observed_place_id", "") or "").strip()
        if not actual_place and str(evidence.get("place_id_source", "") or "").lower() == "observed":
            actual_place = str(evidence.get("place_id", "") or "").strip()
        if expected_place and actual_place and expected_place != actual_place:
            return False, "intent_mismatch_place_id", f"place_id mismatch expected={expected_place} actual={actual_place}"

        expected_server = str(intent.get("server_type", "") or "").strip().upper()
        actual_server = str(evidence.get("observed_server_type", "") or "").strip().upper()
        if not actual_server and str(evidence.get("server_type_source", "") or "").lower() == "observed":
            actual_server = str(evidence.get("server_type", "") or "").strip().upper()
        if expected_server and actual_server and "UNKNOWN" not in {expected_server, actual_server} and expected_server != actual_server:
            return False, "intent_mismatch_server_type", f"server_type mismatch expected={expected_server} actual={actual_server}"

        expected_private = bool(intent.get("private_server_intent"))
        expected_private_hash = str(intent.get("active_private_link_code_hash", "") or "")
        configured_private_hashes = {
            str(item or "")
            for item in (intent.get("configured_private_link_code_hashes") or [])
            if str(item or "")
        }
        expected_private_hashes = {item for item in [expected_private_hash, *configured_private_hashes] if item}
        observed_private_hash = str(evidence.get("observed_private_link_code_hash", "") or "")
        if expected_private and observed_private_hash and expected_private_hashes and observed_private_hash in expected_private_hashes:
            if expected_place and actual_place and expected_place != actual_place:
                return False, "intent_mismatch_place_id", f"place_id mismatch expected={expected_place} actual={actual_place}"
            return True, "private_server_verified", ""
        if expected_private_hashes and observed_private_hash and observed_private_hash not in expected_private_hashes:
            return False, "intent_mismatch_private_server", "private server intent mismatch"

        if actual_place and expected_place == actual_place:
            return True, "intent_verified_place", ""

        if expected_private:
            flog_kv(
                "VERIFY",
                "destination_evidence_limited",
                "warning",
                account=acc.display_name,
                validation="private_server_unverified",
                evidence_source=str(evidence.get("evidence_source", "")),
                reason="no_observed_private_server_or_job_evidence",
                session_id=acc.session_id,
                transaction_id=acc.rejoin_transaction_id,
                runtime_generation=acc.runtime_generation,
            )
            return True, "private_server_unverified", ""

        if expected_place:
            flog_kv(
                "VERIFY",
                "destination_evidence_limited",
                "warning",
                account=acc.display_name,
                validation="intent_verified_no_job_evidence",
                evidence_source=str(evidence.get("evidence_source", "")),
                reason="no_observed_job_evidence",
                session_id=acc.session_id,
                transaction_id=acc.rejoin_transaction_id,
                runtime_generation=acc.runtime_generation,
            )
            return True, "intent_verified_no_job_evidence", ""

        flog_kv(
            "VERIFY",
            "destination_evidence_limited",
            "warning",
            account=acc.display_name,
            validation="intent_recorded_no_destination_evidence",
            evidence_source=str(evidence.get("evidence_source", "")),
            reason="no_configured_or_observed_destination",
            session_id=acc.session_id,
            transaction_id=acc.rejoin_transaction_id,
            runtime_generation=acc.runtime_generation,
        )
        return True, "intent_recorded_no_destination_evidence", ""

    def _finish_transaction(
        self,
        acc: Account,
        status: str,
        reason: str,
        evidence: Optional[Dict[str, Any]] = None,
        server_validation: str = "",
        expected: Optional[Dict[str, Any]] = None,
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

    def run(self):
        flog("[DISPATCHER] started")
        while not self._stop.is_set():
            if not self._net.is_online():
                self._net.wait_until_online(timeout=5)
                continue

            self._queue.wait_until_free(self._stop)
            if self._stop.is_set():
                break

            acc = self._queue.pop(timeout=1.0)
            if acc is None:
                continue

            if acc.state != AccountState.QUEUED:
                flog(f"[DISPATCHER] skip {acc.display_name} (state={acc.state.name})")
                continue

            self._queue.mark_busy()
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
                self._supervisor.emit("RecoverySupervisor", "BEGIN_REJOIN_TRANSACTION", account=acc, reason="dispatcher_launch", payload=tx_snapshot)
            self._state_mgr.transition(acc, AccountState.LAUNCHING, reason="dispatcher_launch")

            try:
                flog(f"[DISPATCHER] launching {acc.display_name}")
                success = self._launcher.launch(acc, self._stop)
                if self._stop.is_set() or acc.desired_state != AccountState.IN_GAME:
                    flog_kv(
                        "DISPATCHER",
                        "launch_result_ignored",
                        account=acc.display_name,
                        stopped=self._stop.is_set(),
                        desired=getattr(acc.desired_state, "name", acc.desired_state),
                    )
                    self._finish_transaction(
                        acc,
                        "rolled_back",
                        "stopped_or_not_desired",
                        server_validation="aborted",
                        expected=tx_guard,
                    )
                    continue
                pid_is_live = bool(acc.pid and ProcessManager.is_bound_game_alive(
                    acc.pid,
                    owner_key=acc._config_username,
                    expected_identity=acc.bound_process_identity,
                ))
                if success or (acc.state == AccountState.IN_GAME and pid_is_live):
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
                        rolled_back = self._finish_transaction(
                            acc,
                            "rolled_back",
                            intent_failure or "server_intent_mismatch",
                            evidence=evidence,
                            server_validation=server_validation,
                            expected=tx_guard,
                        )
                        if rolled_back:
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
                                intent_failure or "server_intent_mismatch",
                                payload={"detail": intent_failure or "server_intent_mismatch"},
                                expected_runtime_generation=signal_generation,
                                expected_session_id=str(tx_guard.get("session_id", "") or ""),
                                expected_launch_nonce=str(tx_guard.get("launch_nonce", "") or ""),
                                expected_transaction_id=str(tx_guard.get("transaction_id", "") or ""),
                            )
                        continue

                    committed = self._finish_transaction(
                        acc,
                        "committed",
                        launch_trigger,
                        evidence=evidence,
                        server_validation=server_validation,
                        expected=tx_guard,
                    )
                    if not committed:
                        continue
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
                else:
                    rolled_back = self._finish_transaction(acc, "rolled_back", "launch_failed", server_validation="launch_failed", expected=tx_guard)
                    if rolled_back:
                        self._runtime_owner.handle_runtime_signal(
                            acc,
                            "launch_failure",
                            "launch_failed",
                            payload={"detail": "launch_failed"},
                            expected_runtime_generation=tx_guard.get("runtime_generation"),
                            expected_session_id=str(tx_guard.get("session_id", "") or ""),
                            expected_launch_nonce=str(tx_guard.get("launch_nonce", "") or ""),
                            expected_transaction_id=str(tx_guard.get("transaction_id", "") or ""),
                        )
            finally:
                self._queue.mark_free()

        flog("[DISPATCHER] stopped")
