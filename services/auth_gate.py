from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from core import account_launch_block_reason, cookie_invalid_block_reason
from runtime.recovery_support import _set_account_cookie_block
from services.captcha_guard import (
    CAPTCHA_BLOCK_REASON,
    CAPTCHA_REASON,
    is_account_captcha_required,
    set_account_captcha_hold,
)


@dataclass(frozen=True)
class AuthGateDecision:
    blocked: bool
    reason_key: str = ""
    reason: str = ""
    category: str = ""

    @property
    def launchable(self) -> bool:
        return not self.blocked

    def to_dict(self) -> Dict[str, Any]:
        return {
            "blocked": self.blocked,
            "launchable": self.launchable,
            "reason_key": self.reason_key,
            "reason": self.reason,
            "category": self.category,
        }


def evaluate_account_auth_gate(account: Any) -> AuthGateDecision:
    try:
        from domain.account_model import FINISHED_STATUS, is_account_finished

        if is_account_finished(account):
            return AuthGateDecision(
                blocked=True,
                reason_key="finished",
                reason=FINISHED_STATUS,
                category="finished",
            )
    except Exception:
        pass
    try:
        from services.ban_guard import BANNED_BLOCK_REASON, BANNED_REASON, is_account_banned

        if is_account_banned(account):
            return AuthGateDecision(
                blocked=True,
                reason_key=BANNED_REASON,
                reason=BANNED_BLOCK_REASON,
                category="banned",
            )
    except Exception:
        pass
    if is_account_captcha_required(account):
        return AuthGateDecision(
            blocked=True,
            reason_key=CAPTCHA_REASON,
            reason=CAPTCHA_BLOCK_REASON,
            category="captcha",
        )
    reason = account_launch_block_reason(account)
    if not reason:
        return AuthGateDecision(blocked=False)
    try:
        from services.ban_guard import BANNED_BLOCK_REASON as _BANNED_MSG

        if reason == _BANNED_MSG:
            return AuthGateDecision(
                blocked=True,
                reason_key="banned",
                reason=_BANNED_MSG,
                category="banned",
            )
    except Exception:
        pass
    if reason == CAPTCHA_BLOCK_REASON:
        return AuthGateDecision(
            blocked=True,
            reason_key=CAPTCHA_REASON,
            reason=CAPTCHA_BLOCK_REASON,
            category="captcha",
        )
    reason_key = "cookie_invalid" if cookie_invalid_block_reason(reason) else "cookie_mismatch"
    return AuthGateDecision(
        blocked=True,
        reason_key=reason_key,
        reason=reason,
        category="auth",
    )


def mark_account_auth_quarantined(
    account: Any,
    decision: AuthGateDecision,
    *,
    source: str = "",
    runtime_writer: Any = None,
) -> AuthGateDecision:
    if not decision.blocked:
        return decision
    if decision.category == "finished" or decision.reason_key == "finished":
        # User-marked done: never touch cookie/captcha state, just stay out.
        return decision
    if decision.reason_key == CAPTCHA_REASON:
        set_account_captcha_hold(
            account,
            decision.reason or CAPTCHA_BLOCK_REASON,
            source=source or "auth_gate",
            runtime_writer=runtime_writer,
        )
    elif decision.reason_key == "banned" or decision.category == "banned":
        try:
            from services.ban_guard import set_account_banned_hold

            set_account_banned_hold(
                account,
                decision.reason or "",
                source=source or "auth_gate",
                runtime_writer=runtime_writer,
            )
        except Exception:
            pass
        if runtime_writer is not None:
            try:
                runtime_writer.set_recovery(account, status="banned", reason="banned", inflight=False)
                runtime_writer.set_cooldown(account, 0.0, reason="banned")
            except Exception:
                pass
    else:
        _set_account_cookie_block(account, decision.reason)
        if runtime_writer is not None:
            try:
                runtime_writer.set_recovery(account, status=decision.reason_key, reason=decision.reason_key, inflight=False)
                runtime_writer.set_cooldown(account, 0.0, reason=decision.reason_key)
            except Exception:
                pass
    return decision
