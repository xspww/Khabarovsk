from __future__ import annotations

from typing import Any, Dict, Tuple

from runtime.recovery_policy import policy_for


def recovery_budget_settings(cfg: Dict[str, Any]) -> Tuple[bool, int, float]:
    enabled = bool(cfg.get("recovery_budget_enabled", True))
    max_attempts = max(
        1,
        int(cfg.get("recovery_budget_max_attempts", cfg.get("max_retry", 10)) or 10),
    )
    window_seconds = max(1.0, float(cfg.get("recovery_budget_window_seconds", 300) or 300))
    return enabled, max_attempts, window_seconds


def record_recovery_budget_attempt(cfg: Dict[str, Any], account: Any, canonical: str, bucket: str, now: float) -> str:
    enabled, max_attempts, window_seconds = recovery_budget_settings(cfg)
    if not enabled or bucket == "manual" or bool(policy_for(canonical).get("fatal")):
        return ""
    attempts = [
        float(ts)
        for ts in list(getattr(account, "recovery_budget_attempts", []) or [])
        if (now - float(ts)) <= window_seconds
    ]
    if len(attempts) >= max_attempts:
        account.recovery_budget_attempts = attempts
        return (
            f"recovery budget exceeded: {len(attempts)}/{max_attempts} attempts "
            f"in {int(window_seconds)}s"
        )
    attempts.append(now)
    account.recovery_budget_attempts = attempts
    return ""

