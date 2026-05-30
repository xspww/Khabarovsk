from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .account_state import AccountState, RuntimeState
from .runtime_lifecycle import lifecycle_for_runtime_state


@dataclass
class AccountRuntime:
    account_id: str = ""
    lifecycle_state: RuntimeState = RuntimeState.STOPPED
    canonical_state: str = "STOPPED"
    public_state: str = AccountState.IDLE.name
    desired_public_state: str = AccountState.IN_GAME.name
    pid: Optional[int] = None
    process_identity: str = ""
    bind_status: str = "unbound"
    binding_status: str = "unbound"
    binding_decision: str = ""
    process_binding_confidence: float = 0.0
    process_proof_level: str = "untrusted"
    process_reject_reason: str = ""
    process_owner_claim: str = ""
    unmanaged_live_process_count: int = 0
    unmanaged_live_pids: list[int] = field(default_factory=list)
    adopt_candidate_pid: Optional[int] = None
    adopt_reject_reason: str = ""
    orphan_pid: Optional[int] = None
    orphan_identity: str = ""
    orphan_confidence: float = 0.0
    orphan_observed_at: float = 0.0
    orphan_verify_after: float = 0.0
    destination_validation: str = "unverified"
    launch_intent_summary: Dict[str, Any] = field(default_factory=dict)
    generation: int = 0
    runtime_generation: int = 0
    recovery_generation: int = 0
    command_generation: int = 0
    retry_count: int = 0
    crash_count: int = 0
    fail_count: int = 0
    recovery_budget_count: int = 0
    cooldown_until: float = 0.0
    recovery_status: str = ""
    recovery_reason: str = ""
    recovery_inflight: bool = False
    recovery_active: bool = False
    liveness_state: str = "unknown"
    liveness_score: float = 0.0
    last_heartbeat: float = 0.0
    session_id: str = ""
    launch_nonce: str = ""
    account_runtime_id: str = ""
    rejoin_transaction_id: str = ""
    server_validation: str = "unverified"
    observed_server_type: str = ""
    observed_private_server_id: str = ""
    observed_private_server_owner_id: str = ""
    observed_place_id: str = ""
    observed_job_id: str = ""
    observed_universe_id: str = ""
    observed_server_at: float = 0.0
    scheduler_slot: str = ""
    supervisor_state: str = "stopped"
    last_transaction_status: str = ""
    last_transaction_step: str = ""
    last_transaction_reason: str = ""
    last_transaction_started_at: float = 0.0
    last_transaction_completed_at: float = 0.0
    last_transaction_failure_reason: str = ""
    last_transition_at: float = field(default_factory=time.time)
    last_transition_reason: str = ""
    current_command: str = ""
    command_inflight: Optional[Dict[str, Any]] = None
    last_error: str = ""

    def snapshot(self) -> Dict[str, Any]:
        cooldown_left = max(0, int(float(self.cooldown_until or 0.0) - time.time()))
        return {
            "account_id": self.account_id,
            "runtime_state": self.lifecycle_state.value,
            "canonical_runtime_state": self.canonical_state or lifecycle_for_runtime_state(self.lifecycle_state).value,
            "public_state": self.public_state,
            "desired_public_state": self.desired_public_state,
            "pid": self.pid,
            "process_identity": self.process_identity,
            "bind_status": self.bind_status,
            "binding_status": self.binding_status,
            "binding_decision": self.binding_decision,
            "process_binding_confidence": self.process_binding_confidence,
            "process_proof_level": self.process_proof_level,
            "process_reject_reason": self.process_reject_reason,
            "process_owner_claim": self.process_owner_claim,
            "unmanaged_live_process_count": self.unmanaged_live_process_count,
            "unmanaged_live_pids": list(self.unmanaged_live_pids or []),
            "adopt_candidate_pid": self.adopt_candidate_pid,
            "adopt_reject_reason": self.adopt_reject_reason,
            "orphan_pid": self.orphan_pid,
            "orphan_identity": self.orphan_identity,
            "orphan_confidence": self.orphan_confidence,
            "orphan_observed_at": self.orphan_observed_at,
            "orphan_verify_after": self.orphan_verify_after,
            "destination_validation": self.destination_validation,
            "launch_intent_summary": dict(self.launch_intent_summary or {}),
            "runtime_generation": self.runtime_generation,
            "generation": self.generation,
            "recovery_generation": self.recovery_generation,
            "command_generation": self.command_generation,
            "retry_count": self.retry_count,
            "crash_count": self.crash_count,
            "fail_count": self.fail_count,
            "recovery_budget_count": self.recovery_budget_count,
            "cooldown_until": self.cooldown_until,
            "cooldown_left": cooldown_left,
            "recovery_status": self.recovery_status,
            "recovery_reason": self.recovery_reason,
            "recovery_inflight": self.recovery_inflight,
            "recovery_active": self.recovery_active,
            "liveness_state": self.liveness_state,
            "liveness_score": self.liveness_score,
            "last_heartbeat": self.last_heartbeat,
            "session_id": self.session_id,
            "launch_nonce": self.launch_nonce,
            "account_runtime_id": self.account_runtime_id,
            "rejoin_transaction_id": self.rejoin_transaction_id,
            "server_validation": self.server_validation,
            "observed_server_type": self.observed_server_type,
            "observed_private_server_id": self.observed_private_server_id,
            "observed_private_server_owner_id": self.observed_private_server_owner_id,
            "observed_place_id": self.observed_place_id,
            "observed_job_id": self.observed_job_id,
            "observed_universe_id": self.observed_universe_id,
            "observed_server_at": self.observed_server_at,
            "scheduler_slot": self.scheduler_slot,
            "supervisor_state": self.supervisor_state,
            "last_transaction_status": self.last_transaction_status,
            "last_transaction_step": self.last_transaction_step,
            "last_transaction_reason": self.last_transaction_reason,
            "last_transaction_started_at": self.last_transaction_started_at,
            "last_transaction_completed_at": self.last_transaction_completed_at,
            "last_transaction_failure_reason": self.last_transaction_failure_reason,
            "last_transition_at": self.last_transition_at,
            "last_transition_reason": self.last_transition_reason,
            "current_command": self.current_command,
            "command_inflight": self.command_inflight,
            "last_error": self.last_error,
        }
