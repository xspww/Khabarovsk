from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict

from runtime.recovery_policy import canonical_reason, policy_for


@dataclass(frozen=True)
class RecoveryPolicyInput:
    state: str
    desired_state: str = "IN_GAME"
    reason: str = ""
    network_online: bool = True
    cooldown_until: float = 0.0
    retry_count: int = 0
    max_retry: int = 10
    failed: bool = False
    stale: bool = False
    now: float = field(default_factory=time.time)


@dataclass(frozen=True)
class RecoveryDecision:
    action: str
    reason: str
    retry_after_seconds: float = 0.0
    fatal: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "retry_after_seconds": round(float(self.retry_after_seconds or 0.0), 1),
            "fatal": self.fatal,
        }


class RecoveryDecisionPolicy:
    """Pure recovery decision policy for shadow comparison before takeover."""

    def decide(self, request: RecoveryPolicyInput) -> RecoveryDecision:
        reason = canonical_reason(request.reason or "runtime_signal")
        if request.stale:
            return RecoveryDecision("IGNORE_STALE", reason)
        if request.failed or str(request.state or "").upper() == "FAILED":
            return RecoveryDecision("REQUIRE_MANUAL", reason, fatal=True)
        if str(request.desired_state or "").upper() != "IN_GAME":
            return RecoveryDecision("WAIT", "desired_state_not_in_game")
        policy = policy_for(reason)
        if bool(policy.get("fatal")):
            return RecoveryDecision("FAIL_ACCOUNT", reason, fatal=True)
        if not request.network_online and reason not in {"auth_failure", "captcha_required"}:
            return RecoveryDecision("WAIT", "network_not_online", retry_after_seconds=5.0)
        if int(request.retry_count or 0) >= int(request.max_retry or 0):
            return RecoveryDecision("FAIL_ACCOUNT", "max_retry_exceeded", fatal=True)
        if float(request.cooldown_until or 0.0) > float(request.now or 0.0):
            return RecoveryDecision(
                "WAIT",
                "cooldown_active",
                retry_after_seconds=max(0.0, float(request.cooldown_until) - float(request.now)),
            )
        return RecoveryDecision("QUEUE_RELAUNCH", reason)
