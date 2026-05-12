from __future__ import annotations

import inspect
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional

from domain.account_state import AccountState, RuntimeState
from domain.public_state_mapper import runtime_state_for_public
from domain.runtime_lifecycle import lifecycle_for_public, lifecycle_for_legacy_runtime
from domain.session_identity import create_rejoin_transaction, create_session_identity
from domain.state_transitions import is_valid_runtime_transition
from runtime.runtime_invariants import check_runtime_invariants, invariant_snapshot


Logger = Callable[..., None]


class RuntimeStateManager:
    """Single-writer helper for lifecycle-critical Account runtime fields."""

    def __init__(self, logger: Optional[Logger] = None):
        self._log = logger

    def _emit(self, scope: str, event: str, level: str = "info", **fields: Any) -> None:
        if not self._log:
            return
        thread_name = threading.current_thread().name
        fields.setdefault("event_type", event)
        fields.setdefault("thread_name", thread_name)
        fields.setdefault("thread", thread_name)
        if "account_id" not in fields and fields.get("account"):
            fields["account_id"] = fields.get("account")
        if "PID" not in fields and "pid" in fields:
            fields["PID"] = fields.get("pid")
        if "pid" not in fields and "PID" in fields:
            fields["pid"] = fields.get("PID")
        try:
            self._log(scope, event, level, **fields)
        except TypeError:
            self._log(scope, event, **fields)
        except Exception as exc:
            print(f"[RUNTIME] logger_failed event={event} error={exc}", file=sys.stderr)

    def _caller(self) -> str:
        frame = inspect.stack()[2]
        return f"{frame.function}:{frame.lineno}"

    def _account_name(self, acc: Any) -> str:
        return str(
            getattr(acc, "_config_username", "")
            or getattr(acc, "display_name", "")
            or getattr(acc, "username", "")
            or ""
        )

    def _runtime_log_fields(self, acc: Any, reason: str = "", **fields: Any) -> Dict[str, Any]:
        state = getattr(acc, "state", None)
        runtime_state = ""
        public_state = ""
        if isinstance(state, AccountState):
            runtime_state = runtime_state_for_public(state).value
            public_state = state.name
            canonical_runtime_state = lifecycle_for_public(state).value
        else:
            public_state = str(getattr(state, "name", state or ""))
            try:
                canonical_runtime_state = lifecycle_for_legacy_runtime(RuntimeState(str(state))).value
            except Exception:
                canonical_runtime_state = ""
        payload = {
            "account": getattr(acc, "display_name", getattr(acc, "username", "")),
            "account_id": self._account_name(acc),
            "runtime_generation": getattr(acc, "runtime_generation", 0),
            "recovery_generation": getattr(acc, "recovery_generation", 0),
            "command_generation": getattr(acc, "command_generation", 0),
            "runtime_state": runtime_state,
            "canonical_runtime_state": canonical_runtime_state,
            "public_state": public_state,
            "PID": getattr(acc, "pid", None),
            "pid": getattr(acc, "pid", None),
            "reason": reason,
        }
        payload.update(fields)
        return payload

    def _emit_invariant_violations(self, acc: Any, reason: str, runtime_state: Optional[RuntimeState] = None) -> bool:
        violations = check_runtime_invariants(acc, runtime_state=runtime_state)
        if not violations:
            return True
        snapshot = invariant_snapshot(acc, runtime_state=runtime_state)
        for violation in violations:
            self._emit(
                "RUNTIME",
                "invariant_violation",
                "warning" if violation.get("severity") != "critical" else "error",
                **self._runtime_log_fields(
                    acc,
                    reason=reason,
                    lifecycle_owner="runtime_state_manager",
                    violation=violation.get("code", ""),
                    violation_detail=violation,
                    snapshot=snapshot,
                ),
            )
        return False

    def _transition_invariant_blockers(self, acc: Any, new_runtime: RuntimeState, reason: str) -> list:
        violations = check_runtime_invariants(acc, runtime_state=new_runtime)
        hard_codes = {
            "running_without_pid",
            "running_without_verified_binding",
            "running_pid_not_live",
            "recovering_without_recovery_owner",
            "backoff_without_cooldown",
            "command_owner_incomplete",
            "command_name_without_owner",
        }
        blockers = [item for item in violations if item.get("code") in hard_codes]
        if blockers:
            self._emit_invariant_violations(acc, reason or "transition_invariant_rejected", runtime_state=new_runtime)
        return blockers

    def snapshot(self, acc: Any) -> Dict[str, Any]:
        lock = getattr(acc, "_lock", None)
        try:
            if lock is not None:
                with lock:
                    return dict(acc.runtime_snapshot())
            return dict(acc.runtime_snapshot())
        except Exception as exc:
            self._emit(
                "RUNTIME",
                "snapshot_failed",
                "warning",
                account=getattr(acc, "display_name", getattr(acc, "username", "")),
                account_id=self._account_name(acc),
                error=str(exc),
            )
            return {
                "account_id": getattr(acc, "_config_username", getattr(acc, "username", "")),
                "state": getattr(getattr(acc, "state", None), "name", getattr(acc, "state", "")),
                "pid": getattr(acc, "pid", None),
                "runtime_generation": getattr(acc, "runtime_generation", 0),
                "recovery_generation": getattr(acc, "recovery_generation", 0),
                "command_generation": getattr(acc, "command_generation", 0),
            }

    def guard_runtime_generation(self, acc: Any, expected_generation: Optional[int], reason: str = "") -> bool:
        if expected_generation is None:
            return True
        current = int(getattr(acc, "runtime_generation", 0) or 0)
        if int(expected_generation) == current:
            return True
        self._emit(
            "RUNTIME",
            "stale_work_rejected",
            "warning",
            **self._runtime_log_fields(
                acc,
                reason=reason,
                expected_generation=expected_generation,
                current_generation=current,
                session_id=getattr(acc, "session_id", ""),
                transaction_id=getattr(acc, "rejoin_transaction_id", ""),
            ),
        )
        return False

    def guard_recovery_generation(self, acc: Any, expected_generation: Optional[int], reason: str = "") -> bool:
        if expected_generation is None:
            return True
        current = int(getattr(acc, "recovery_generation", 0) or 0)
        if int(expected_generation) == current:
            return True
        self._emit(
            "RUNTIME",
            "stale_work_rejected",
            "warning",
            **self._runtime_log_fields(
                acc,
                reason=reason,
                expected_recovery_generation=expected_generation,
                current_recovery_generation=current,
                session_id=getattr(acc, "session_id", ""),
                transaction_id=getattr(acc, "rejoin_transaction_id", ""),
            ),
        )
        return False

    def guard_session_identity(
        self,
        acc: Any,
        expected_generation: Optional[int] = None,
        expected_session_id: str = "",
        expected_launch_nonce: str = "",
        expected_transaction_id: str = "",
        reason: str = "",
    ) -> bool:
        if not self.guard_runtime_generation(acc, expected_generation, reason or "session_guard"):
            return False
        if expected_session_id and str(getattr(acc, "session_id", "") or "") != str(expected_session_id):
            self._emit(
                "RUNTIME",
                "stale_work_rejected",
                "warning",
                **self._runtime_log_fields(
                    acc,
                    reason=reason or "session_mismatch",
                    expected_session_id=expected_session_id,
                    current_session_id=getattr(acc, "session_id", ""),
                ),
            )
            return False
        if expected_launch_nonce and str(getattr(acc, "launch_nonce", "") or "") != str(expected_launch_nonce):
            self._emit(
                "RUNTIME",
                "stale_work_rejected",
                "warning",
                **self._runtime_log_fields(
                    acc,
                    reason=reason or "launch_nonce_mismatch",
                    expected_launch_nonce=expected_launch_nonce,
                    current_launch_nonce=getattr(acc, "launch_nonce", ""),
                ),
            )
            return False
        if expected_transaction_id and str(getattr(acc, "rejoin_transaction_id", "") or "") != str(expected_transaction_id):
            self._emit(
                "RUNTIME",
                "stale_work_rejected",
                "warning",
                **self._runtime_log_fields(
                    acc,
                    reason=reason or "transaction_mismatch",
                    expected_transaction_id=expected_transaction_id,
                    current_transaction_id=getattr(acc, "rejoin_transaction_id", ""),
                ),
            )
            return False
        return True

    def bump_runtime_generation(self, acc: Any, reason: str = "") -> int:
        acc.runtime_generation = int(getattr(acc, "runtime_generation", 0) or 0) + 1
        acc.sync_runtime(reason or "runtime_generation")
        self._emit(
            "RUNTIME",
            "generation_bumped",
            **self._runtime_log_fields(acc, reason=reason),
        )
        return int(acc.runtime_generation)

    def begin_rejoin_transaction(self, acc: Any, reason: str, launch_intent: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        account_id = str(getattr(acc, "_config_username", "") or getattr(acc, "username", "") or "")
        identity = create_session_identity(
            account_id=account_id,
            runtime_generation=int(getattr(acc, "runtime_generation", 0) or 0),
            account_runtime_id="",
            reason=reason,
            launch_intent=launch_intent or {},
        )
        tx = create_rejoin_transaction(
            identity,
            recovery_generation=int(getattr(acc, "recovery_generation", 0) or 0),
            command_generation=int(getattr(acc, "command_generation", 0) or 0),
            reason=reason,
        )
        acc.account_runtime_id = identity.account_runtime_id
        acc.session_id = identity.session_id
        acc.launch_nonce = identity.launch_nonce
        acc.rejoin_transaction_id = tx.transaction_id
        acc.launch_intent = dict(launch_intent or {})
        acc.server_validation = "intent_recorded"
        acc.scheduler_slot = "reserved"
        acc.supervisor_state = "transaction_pending"
        acc.last_transaction_status = tx.status
        acc.last_transaction_step = tx.step
        acc.last_transaction_reason = reason
        acc.last_transaction_started_at = tx.created_at
        acc.last_transaction_completed_at = 0.0
        acc.last_transaction_failure_reason = ""
        acc.session_started_at = identity.created_at
        acc.last_transaction_at = tx.created_at
        acc.sync_runtime(reason or "begin_rejoin_transaction")
        snapshot = tx.snapshot()
        snapshot["session"] = identity.snapshot()
        self._emit(
            "RUNTIME",
            "rejoin_transaction_begin",
            account=getattr(acc, "display_name", getattr(acc, "username", "")),
            transaction_id=tx.transaction_id,
            session_id=identity.session_id,
            launch_nonce=identity.launch_nonce,
            account_runtime_id=identity.account_runtime_id,
            runtime_generation=getattr(acc, "runtime_generation", 0),
            recovery_generation=getattr(acc, "recovery_generation", 0),
            command_generation=getattr(acc, "command_generation", 0),
            reason=reason,
        )
        self._emit(
            "RUNTIME",
            "transaction_begin",
            account=getattr(acc, "display_name", getattr(acc, "username", "")),
            transaction_id=tx.transaction_id,
            session_id=identity.session_id,
            runtime_generation=getattr(acc, "runtime_generation", 0),
            recovery_generation=getattr(acc, "recovery_generation", 0),
            command_generation=getattr(acc, "command_generation", 0),
            reason=reason,
            thread=threading.current_thread().name,
        )
        return snapshot

    def update_rejoin_transaction(self, acc: Any, status: str = "", step: str = "", reason: str = "", server_validation: str = "", scheduler_slot: str = "") -> Dict[str, Any]:
        if status:
            acc.last_transaction_status = status
        if step:
            acc.last_transaction_step = step
        if reason:
            acc.last_transaction_reason = reason
        if server_validation:
            acc.server_validation = server_validation
            acc.destination_validation = server_validation
        if scheduler_slot:
            acc.scheduler_slot = scheduler_slot
        if step:
            acc.supervisor_state = step
        acc.last_transaction_at = time.time()
        if status in {"committed", "rolled_back", "failed"}:
            acc.last_transaction_completed_at = acc.last_transaction_at
        if status in {"rolled_back", "failed"} and reason:
            acc.last_transaction_failure_reason = reason
        acc.sync_runtime(reason or step or status or "transaction_update")
        snapshot = {
            "transaction_id": getattr(acc, "rejoin_transaction_id", ""),
            "account_id": getattr(acc, "_config_username", getattr(acc, "username", "")),
            "runtime_generation": getattr(acc, "runtime_generation", 0),
            "recovery_generation": getattr(acc, "recovery_generation", 0),
            "command_generation": getattr(acc, "command_generation", 0),
            "account_runtime_id": getattr(acc, "account_runtime_id", ""),
            "session_id": getattr(acc, "session_id", ""),
            "launch_nonce": getattr(acc, "launch_nonce", ""),
            "status": status or getattr(acc, "last_transaction_status", ""),
            "step": step or getattr(acc, "last_transaction_step", "") or getattr(acc, "supervisor_state", ""),
            "reason": reason or getattr(acc, "last_transaction_reason", ""),
            "failure_reason": getattr(acc, "last_transaction_failure_reason", "") if (status in {"rolled_back", "failed"} or getattr(acc, "last_transaction_status", "") in {"rolled_back", "failed"}) else "",
            "launch_intent": getattr(acc, "launch_intent", {}) or {},
            "destination_evidence": {},
            "created_at": getattr(acc, "session_started_at", 0.0) or time.time(),
            "updated_at": getattr(acc, "last_transaction_at", 0.0) or time.time(),
            "completed_at": getattr(acc, "last_transaction_at", 0.0) if status in {"committed", "rolled_back", "failed"} else 0.0,
        }
        self._emit(
            "RUNTIME",
            "rejoin_transaction_update",
            account=getattr(acc, "display_name", getattr(acc, "username", "")),
            transaction_id=snapshot["transaction_id"],
            session_id=snapshot["session_id"],
            status=snapshot["status"],
            step=snapshot["step"],
            reason=snapshot["reason"],
            runtime_generation=snapshot["runtime_generation"],
            recovery_generation=snapshot["recovery_generation"],
            command_generation=snapshot["command_generation"],
        )
        self._emit(
            "RUNTIME",
            "transaction_step",
            account=getattr(acc, "display_name", getattr(acc, "username", "")),
            transaction_id=snapshot["transaction_id"],
            session_id=snapshot["session_id"],
            status=snapshot["status"],
            step=snapshot["step"],
            reason=snapshot["reason"],
            server_validation=getattr(acc, "server_validation", ""),
            runtime_generation=snapshot["runtime_generation"],
            recovery_generation=snapshot["recovery_generation"],
            command_generation=snapshot["command_generation"],
            thread=threading.current_thread().name,
        )
        return snapshot

    def finish_rejoin_transaction(
        self,
        acc: Any,
        status: str,
        reason: str,
        destination_evidence: Optional[Dict[str, Any]] = None,
        server_validation: str = "",
    ) -> Dict[str, Any]:
        final_status = status or "failed"
        acc.last_transaction_status = final_status
        acc.last_transaction_reason = reason
        acc.last_transaction_at = time.time()
        acc.last_transaction_completed_at = acc.last_transaction_at
        if final_status == "committed":
            acc.scheduler_slot = ""
            acc.supervisor_state = "running"
            acc.last_transaction_step = "committed"
            acc.last_transaction_failure_reason = ""
            acc.server_validation = server_validation or "intent_verified_no_destination_signal"
            acc.destination_validation = acc.server_validation
        elif final_status == "rolled_back":
            acc.scheduler_slot = ""
            acc.supervisor_state = "rolled_back"
            acc.last_transaction_step = "rolled_back"
            acc.last_transaction_failure_reason = reason
            acc.server_validation = server_validation or "unverified_rollback"
            acc.destination_validation = acc.server_validation
        else:
            acc.scheduler_slot = ""
            acc.supervisor_state = "failed"
            acc.last_transaction_step = "failed"
            acc.last_transaction_failure_reason = reason
            acc.server_validation = server_validation or "failed"
            acc.destination_validation = acc.server_validation
        acc.sync_runtime(reason or final_status)
        snapshot = {
            "transaction_id": getattr(acc, "rejoin_transaction_id", ""),
            "account_id": getattr(acc, "_config_username", getattr(acc, "username", "")),
            "runtime_generation": getattr(acc, "runtime_generation", 0),
            "recovery_generation": getattr(acc, "recovery_generation", 0),
            "command_generation": getattr(acc, "command_generation", 0),
            "account_runtime_id": getattr(acc, "account_runtime_id", ""),
            "session_id": getattr(acc, "session_id", ""),
            "launch_nonce": getattr(acc, "launch_nonce", ""),
            "status": final_status,
            "step": acc.last_transaction_step,
            "reason": reason,
            "failure_reason": acc.last_transaction_failure_reason,
            "launch_intent": getattr(acc, "launch_intent", {}) or {},
            "destination_evidence": dict(destination_evidence or {}),
            "created_at": getattr(acc, "session_started_at", 0.0) or time.time(),
            "updated_at": acc.last_transaction_at,
            "completed_at": acc.last_transaction_at,
        }
        self._emit(
            "RUNTIME",
            "rejoin_transaction_finish",
            account=getattr(acc, "display_name", getattr(acc, "username", "")),
            transaction_id=snapshot["transaction_id"],
            session_id=snapshot["session_id"],
            status=final_status,
            reason=reason,
            server_validation=acc.server_validation,
            runtime_generation=snapshot["runtime_generation"],
            recovery_generation=snapshot["recovery_generation"],
            command_generation=snapshot["command_generation"],
        )
        self._emit(
            "RUNTIME",
            "transaction_commit" if final_status == "committed" else "transaction_rollback",
            account=getattr(acc, "display_name", getattr(acc, "username", "")),
            transaction_id=snapshot["transaction_id"],
            session_id=snapshot["session_id"],
            status=final_status,
            step=snapshot["step"],
            reason=reason,
            failure_reason=snapshot["failure_reason"],
            server_validation=acc.server_validation,
            runtime_generation=snapshot["runtime_generation"],
            recovery_generation=snapshot["recovery_generation"],
            command_generation=snapshot["command_generation"],
            thread=threading.current_thread().name,
        )
        return snapshot

    def transition_public(
        self,
        acc: Any,
        new_state: AccountState,
        reason: str = "",
        force: bool = False,
        expected_generation: Optional[int] = None,
        increment_generation: bool = False,
    ) -> bool:
        if not self.guard_runtime_generation(acc, expected_generation, reason or "transition"):
            return False
        old_state = acc.state
        old_runtime = runtime_state_for_public(old_state)
        new_runtime = runtime_state_for_public(new_state)
        blockers = [] if (force and new_runtime not in {RuntimeState.RUNNING, RuntimeState.BACKOFF, RuntimeState.RECOVERING}) else self._transition_invariant_blockers(acc, new_runtime, reason or "transition")
        if blockers:
            self._emit(
                "STATE",
                "state_write_rejected",
                "warning",
                **self._runtime_log_fields(
                    acc,
                    reason=reason or "transition_invariant",
                    old=getattr(old_state, "name", old_state),
                    new=new_state.name,
                    old_runtime=old_runtime.value,
                    new_runtime=new_runtime.value,
                    reject="invariant_violation",
                    blockers=blockers,
                    snapshot=invariant_snapshot(acc, runtime_state=new_runtime),
                    caller=self._caller(),
                ),
            )
            return False
        if not force and not is_valid_runtime_transition(old_runtime, new_runtime):
            self._emit(
                "STATE",
                "state_write_rejected",
                "warning",
                **self._runtime_log_fields(
                    acc,
                    reason=reason,
                    old=old_state.name,
                    new=new_state.name,
                    old_runtime=old_runtime.value,
                    new_runtime=new_runtime.value,
                    session_id=getattr(acc, "session_id", ""),
                    transaction_id=getattr(acc, "rejoin_transaction_id", ""),
                    caller=self._caller(),
                ),
            )
            self._emit(
                "STATE",
                "invalid_transition",
                "warning",
                **self._runtime_log_fields(
                    acc,
                    reason=reason,
                    old=old_state.name,
                    new=new_state.name,
                    old_runtime=old_runtime.value,
                    new_runtime=new_runtime.value,
                    caller=self._caller(),
                    snapshot=self.snapshot(acc),
                ),
            )
            return False

        acc.state = new_state
        acc.last_state_reason = str(reason or "")
        acc.last_state_change_at = time.time()
        if increment_generation:
            self.bump_runtime_generation(acc, reason or "transition_epoch")
        else:
            acc.sync_runtime(reason or "transition")
        self._emit(
            "STATE",
            "transition_owned",
            **self._runtime_log_fields(
                acc,
                reason=reason,
                old=old_state.name,
                new=new_state.name,
                old_runtime=old_runtime.value,
                new_runtime=new_runtime.value,
            ),
        )
        self._emit_invariant_violations(acc, reason or "post_transition", runtime_state=new_runtime)
        return True

    def set_desired(self, acc: Any, desired: AccountState, reason: str = "", increment_generation: bool = False) -> None:
        acc.desired_state = desired
        if increment_generation:
            self.bump_runtime_generation(acc, reason or "desired_epoch")
        else:
            acc.sync_runtime(reason or "desired_state")
        self._emit(
            "RUNTIME",
            "desired_state_owned",
            **self._runtime_log_fields(acc, reason=reason or "desired_state", desired_public_state=desired.name),
        )

    def set_cooldown(self, acc: Any, until_ts: float, reason: str = "") -> None:
        acc.cooldown_until = max(0.0, float(until_ts or 0.0))
        acc.sync_runtime(reason or "cooldown")
        self._emit(
            "RUNTIME",
            "owned_mutation",
            **self._runtime_log_fields(
                acc,
                reason=reason,
                field="cooldown_until",
                cooldown_until=acc.cooldown_until,
            ),
        )

    def set_binding_status(self, acc: Any, status: str, reason: str = "") -> None:
        acc.process_binding_status = str(status or "unbound")
        if status:
            text = str(status)
            if text == "verified":
                acc.binding_decision = "verified"
            elif text.startswith("orphan"):
                acc.binding_decision = "quarantined"
            elif text in {"unbound", "released"}:
                acc.binding_decision = "released"
            else:
                acc.binding_decision = text
        acc.sync_runtime(reason or "binding_status")
        self._emit(
            "STATE",
            "process_binding_status",
            account=getattr(acc, "display_name", getattr(acc, "username", "")),
            pid=getattr(acc, "pid", None),
            status=acc.process_binding_status,
            runtime_generation=getattr(acc, "runtime_generation", 0),
            recovery_generation=getattr(acc, "recovery_generation", 0),
            command_generation=getattr(acc, "command_generation", 0),
            reason=reason,
        )

    def update_launch_intent(
        self,
        acc: Any,
        launch_intent: Dict[str, Any],
        reason: str = "",
        expected_generation: Optional[int] = None,
    ) -> bool:
        if not self.guard_runtime_generation(acc, expected_generation, reason or "launch_intent_update"):
            return False
        acc.launch_intent = dict(launch_intent or {})
        acc.launch_intent_summary = dict(acc.launch_intent.get("launch_intent_summary", {}) or {})
        acc.sync_runtime(reason or "launch_intent_update")
        self._emit(
            "RUNTIME",
            "owned_mutation",
            account=getattr(acc, "display_name", getattr(acc, "username", "")),
            field="launch_intent",
            launch_intent_summary=acc.launch_intent_summary,
            runtime_generation=getattr(acc, "runtime_generation", 0),
            recovery_generation=getattr(acc, "recovery_generation", 0),
            command_generation=getattr(acc, "command_generation", 0),
            session_id=getattr(acc, "session_id", ""),
            transaction_id=getattr(acc, "rejoin_transaction_id", ""),
            reason=reason,
        )
        return True

    def set_recovery(self, acc: Any, status: str = "", reason: str = "", inflight: Optional[bool] = None) -> None:
        if status:
            acc.recovery_status = str(status)
        if reason:
            acc.last_recovery_reason = str(reason)
        if inflight is not None:
            acc.recovery_inflight = bool(inflight)
        acc.sync_runtime(reason or status or "recovery")
        self._emit(
            "RUNTIME",
            "owned_mutation",
            **self._runtime_log_fields(
                acc,
                reason=reason or status,
                field="recovery",
                status=getattr(acc, "recovery_status", ""),
                inflight=getattr(acc, "recovery_inflight", False),
            ),
        )

    def bump_recovery_generation(self, acc: Any, reason: str = "", now: Optional[float] = None) -> int:
        acc.recovery_generation = int(getattr(acc, "recovery_generation", 0) or 0) + 1
        acc.last_recovery_at = float(now if now is not None else time.time())
        acc.sync_runtime(reason or "recovery_generation")
        self._emit(
            "RECOVERY",
            "generation_bumped",
            account=getattr(acc, "display_name", getattr(acc, "username", "")),
            runtime_generation=getattr(acc, "runtime_generation", 0),
            recovery_generation=getattr(acc, "recovery_generation", 0),
            command_generation=getattr(acc, "command_generation", 0),
            reason=reason,
            thread=threading.current_thread().name,
        )
        return int(acc.recovery_generation)

    def begin_recovery(
        self,
        acc: Any,
        status: str,
        reason: str,
        bucket: str = "crash",
        now: Optional[float] = None,
        count_retry: bool = True,
        count_crash: bool = True,
        count_fail: bool = True,
    ) -> int:
        event_ts = float(now if now is not None else time.time())
        generation = self.bump_recovery_generation(acc, reason or status or "recovery_begin", now=event_ts)
        acc.recovery_inflight = True
        acc.recovery_status = str(status or "recovering")
        acc.last_recovery_reason = str(reason or "")
        acc.last_crash_reason = str(reason or "")
        if count_retry:
            acc.retry_count = int(getattr(acc, "retry_count", 0) or 0) + 1
        if count_crash:
            acc.crash_count = int(getattr(acc, "crash_count", 0) or 0) + 1
        bucket_name = str(bucket or "crash")
        if bucket_name == "network":
            acc.network_retry_count = int(getattr(acc, "network_retry_count", 0) or 0) + 1
        elif bucket_name == "launch":
            acc.launch_fail_count = int(getattr(acc, "launch_fail_count", 0) or 0) + 1
        elif bucket_name == "session":
            acc.session_retry_count = int(getattr(acc, "session_retry_count", 0) or 0) + 1
        elif bucket_name != "manual":
            acc.crash_retry_count = int(getattr(acc, "crash_retry_count", 0) or 0) + 1
        if count_fail:
            acc.fail_count = int(getattr(acc, "fail_count", 0) or 0) + 1
        acc.sync_runtime(status or reason or "recovery_begin")
        self._emit(
            "RECOVERY",
            "begin_owned",
            account=getattr(acc, "display_name", getattr(acc, "username", "")),
            runtime_generation=getattr(acc, "runtime_generation", 0),
            recovery_generation=generation,
            command_generation=getattr(acc, "command_generation", 0),
            status=status,
            reason=reason,
            bucket=bucket_name,
            pid=getattr(acc, "pid", None),
            thread=threading.current_thread().name,
        )
        return generation

    def begin_account_command(self, acc: Any, command: Dict[str, Any]) -> int:
        current_id = str(getattr(acc, "current_command_id", "") or "")
        next_id = str(command.get("command_id", "") or "")
        if current_id and current_id != next_id:
            self._emit(
                "RUNTIME",
                "command_begin_rejected",
                "warning",
                **self._runtime_log_fields(
                    acc,
                    reason="command_already_inflight",
                    expected_command_id=next_id,
                    current_command_id=current_id,
                ),
            )
            return int(getattr(acc, "command_generation", 0) or 0)
        acc.command_generation = int(getattr(acc, "command_generation", 0) or 0) + 1
        acc.current_command_id = next_id
        acc.current_command = str(command.get("action", ""))
        acc.command_inflight_started_at = float(command.get("started_at") or time.time())
        acc.sync_runtime("command_begin")
        self._emit(
            "RUNTIME",
            "command_begin",
            **self._runtime_log_fields(
                acc,
                reason="command_begin",
                command_id=acc.current_command_id,
                command=acc.current_command,
            ),
        )
        return int(acc.command_generation)

    def finish_account_command(self, acc: Any, command_id: str, ok: bool = True, error: str = "") -> None:
        if command_id and getattr(acc, "current_command_id", "") != command_id:
            self._emit(
                "RUNTIME",
                "stale_work_rejected",
                "warning",
                **self._runtime_log_fields(
                    acc,
                    reason="command_finish",
                    expected_command_id=command_id,
                    current_command_id=getattr(acc, "current_command_id", ""),
                ),
            )
            return
        acc.current_command_id = ""
        acc.current_command = ""
        acc.command_inflight_started_at = 0.0
        if error:
            acc.last_error = str(error)
        acc.sync_runtime("command_finish")
        self._emit(
            "RUNTIME",
            "command_finish",
            **self._runtime_log_fields(
                acc,
                reason="command_finish",
                command_id=command_id,
                ok=ok,
                error=error,
            ),
        )

    def clear_process_binding(self, acc: Any, reason: str = "", increment_generation: bool = False) -> None:
        acc.pid = None
        acc.bound_process_name = ""
        acc.bound_process_identity = ""
        acc.ownership_confidence = 0.0
        acc.last_signal_confidence = 0.0
        acc.process_binding_status = "unbound"
        acc.binding_decision = "released"
        acc.process_binding_confidence = 0.0
        acc.process_reject_reason = ""
        acc.process_owner_claim = ""
        acc.unmanaged_live_process_count = 0
        acc.unmanaged_live_pids = []
        acc.adopt_candidate_pid = None
        acc.adopt_reject_reason = ""
        acc.orphan_confidence = 0.0
        acc.orphan_pid = None
        acc.orphan_identity = ""
        acc.pid_missing_since = time.time()
        if increment_generation:
            self.bump_runtime_generation(acc, reason or "process_unbound")
        else:
            acc.sync_runtime(reason or "process_unbound")
        self._emit(
            "STATE",
            "process_unbound",
            **self._runtime_log_fields(acc, reason=reason, pid="", PID=""),
        )

    def bind_process(
        self,
        acc: Any,
        pid: int,
        process_name: str,
        process_identity: str,
        reason: str = "",
        confidence: float = 100.0,
        increment_generation: bool = True,
    ) -> None:
        old_pid = getattr(acc, "pid", None)
        acc.pid = int(pid)
        acc.bound_process_name = process_name or "RobloxPlayerBeta.exe"
        acc.bound_process_identity = process_identity or ""
        acc.pid_missing_since = 0.0
        acc.last_pid_change_at = time.time()
        acc.ownership_confidence = float(confidence or 0.0)
        acc.last_signal_confidence = float(confidence or 0.0)
        acc.process_binding_status = "verified"
        acc.binding_decision = "verified"
        acc.process_binding_confidence = float(confidence or 0.0)
        acc.process_reject_reason = ""
        acc.unmanaged_live_process_count = 0
        acc.unmanaged_live_pids = []
        acc.adopt_candidate_pid = None
        acc.adopt_reject_reason = ""
        acc.orphan_confidence = 0.0
        if increment_generation and old_pid and int(old_pid) != int(pid):
            self.bump_runtime_generation(acc, reason or "process_bind_replace")
        else:
            acc.sync_runtime(reason or "process_bind")
        self._emit(
            "STATE",
            "process_bound",
            **self._runtime_log_fields(
                acc,
                reason=reason,
                pid=pid,
                PID=pid,
                old_pid=old_pid or "",
                session_id=getattr(acc, "session_id", ""),
                transaction_id=getattr(acc, "rejoin_transaction_id", ""),
            ),
        )

    def forced_reset(self, acc: Any, desired: AccountState = AccountState.IDLE, reason: str = "forced_reset") -> None:
        self.clear_process_binding(acc, reason, increment_generation=False)
        acc.desired_state = desired
        acc.cooldown_until = 0.0
        acc.recovery_inflight = False
        acc.recovery_status = ""
        acc.last_recovery_reason = ""
        acc.recovery_scheduled_at = 0.0
        acc.current_command_id = ""
        acc.current_command = ""
        acc.command_inflight_started_at = 0.0
        acc.session_id = ""
        acc.launch_nonce = ""
        acc.account_runtime_id = ""
        acc.rejoin_transaction_id = ""
        acc.scheduler_slot = ""
        acc.supervisor_state = "stopped"
        acc.last_transaction_status = ""
        acc.last_transaction_step = ""
        acc.last_transaction_reason = ""
        acc.last_transaction_started_at = 0.0
        acc.last_transaction_completed_at = 0.0
        acc.last_transaction_failure_reason = ""
        acc.server_validation = "unverified"
        acc.destination_validation = "unverified"
        acc.launch_intent = {}
        acc.launch_intent_summary = {}
        acc.state = desired
        acc.last_state_reason = reason
        acc.last_state_change_at = time.time()
        self.bump_runtime_generation(acc, reason)
        self._emit(
            "STATE",
            "forced_reset",
            **self._runtime_log_fields(
                acc,
                reason=reason,
                state=getattr(getattr(acc, "state", None), "name", getattr(acc, "state", "")),
            ),
        )
