from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core import account_launch_block_reason, cookie_invalid_block_reason
from runtime.recovery_support import _set_account_cookie_block
from services.captcha_guard import (
    CAPTCHA_BLOCK_REASON,
    CAPTCHA_REASON,
    is_account_captcha_required,
    set_account_captcha_hold,
)
from services.verify_guard import (
    LOCKED_BLOCK_REASON,
    LOCKED_REASON,
    RECOVERY_BLOCK_REASON,
    RECOVERY_REASON,
    is_account_locked,
    is_account_recovery,
    set_account_locked_hold,
    set_account_recovery_hold,
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocked": self.blocked,
            "launchable": self.launchable,
            "reason_key": self.reason_key,
            "reason": self.reason,
            "category": self.category,
        }


def _finished_decision(account: Any) -> AuthGateDecision | None:
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
        "intentional: ignore exception"
    return None


def _moderation_decision(account: Any) -> AuthGateDecision | None:
    try:
        from services.ban_guard import (
            BANNED_BLOCK_REASON,
            BANNED_REASON,
            SUSPENDED_BLOCK_REASON,
            SUSPENDED_REASON,
            is_account_banned,
            is_account_suspended,
        )
    except Exception:
        return None

    # A suspension blocks the launch like a ban but keeps its own reason so
    # the dashboard can label it instead of reporting a permanent ban.
    if is_account_suspended(account):
        return AuthGateDecision(
            blocked=True,
            reason_key=SUSPENDED_REASON,
            reason=SUSPENDED_BLOCK_REASON,
            category="suspended",
        )
    if is_account_banned(account):
        return AuthGateDecision(
            blocked=True,
            reason_key=BANNED_REASON,
            reason=BANNED_BLOCK_REASON,
            category="banned",
        )
    return None


def _hold_decision(account: Any) -> AuthGateDecision | None:
    # Identity lock (face/ID) and account recovery (passkey/2FA) outrank
    # captcha everywhere else (Status payload, launch block reason,
    # dashboard). Check them first so a locked account never reports as
    # Captcha — notably LOCKED_BLOCK_REASON itself used to match a captcha
    # keyword, and lock screens share "prove you are human" wording.
    if is_account_locked(account):
        return AuthGateDecision(
            blocked=True,
            reason_key=LOCKED_REASON,
            reason=LOCKED_BLOCK_REASON,
            category="locked",
        )
    if is_account_recovery(account):
        return AuthGateDecision(
            blocked=True,
            reason_key=RECOVERY_REASON,
            reason=RECOVERY_BLOCK_REASON,
            category="account_recovery",
        )
    if is_account_captcha_required(account):
        return AuthGateDecision(
            blocked=True,
            reason_key=CAPTCHA_REASON,
            reason=CAPTCHA_BLOCK_REASON,
            category="captcha",
        )
    return None


def _banned_reason_decision(reason: str) -> AuthGateDecision | None:
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
        "intentional: ignore exception"
    return None


def _reason_decision(account: Any) -> AuthGateDecision:
    reason = account_launch_block_reason(account)
    if not reason:
        return AuthGateDecision(blocked=False)
    banned = _banned_reason_decision(reason)
    if banned:
        return banned
    if reason == CAPTCHA_BLOCK_REASON:
        return AuthGateDecision(
            blocked=True,
            reason_key=CAPTCHA_REASON,
            reason=CAPTCHA_BLOCK_REASON,
            category="captcha",
        )
    if reason == LOCKED_BLOCK_REASON:
        return AuthGateDecision(
            blocked=True,
            reason_key=LOCKED_REASON,
            reason=LOCKED_BLOCK_REASON,
            category="locked",
        )
    if reason == RECOVERY_BLOCK_REASON:
        return AuthGateDecision(
            blocked=True,
            reason_key=RECOVERY_REASON,
            reason=RECOVERY_BLOCK_REASON,
            category="account_recovery",
        )
    reason_key = "cookie_invalid" if cookie_invalid_block_reason(reason) else "cookie_mismatch"
    return AuthGateDecision(
        blocked=True,
        reason_key=reason_key,
        reason=reason,
        category="auth",
    )


def evaluate_account_auth_gate(account: Any) -> AuthGateDecision:
    for resolver in (_finished_decision, _moderation_decision, _hold_decision):
        decision = resolver(account)
        if decision:
            return decision
    return _reason_decision(account)


def _clear_hold_runtime(runtime_writer: Any, account: Any, status: str) -> None:
    if runtime_writer is None:
        return
    try:
        runtime_writer.set_recovery(account, status=status, reason=status, inflight=False)
        runtime_writer.set_cooldown(account, 0.0, reason=status)
    except Exception:
        "intentional: ignore exception"


def _apply_captcha_hold(account: Any, decision: AuthGateDecision, source: str, runtime_writer: Any) -> None:
    set_account_captcha_hold(
        account,
        decision.reason or CAPTCHA_BLOCK_REASON,
        source=source,
        runtime_writer=runtime_writer,
    )


def _apply_locked_hold(account: Any, decision: AuthGateDecision, source: str, runtime_writer: Any) -> None:
    set_account_locked_hold(
        account,
        decision.reason or LOCKED_BLOCK_REASON,
        source=source,
        runtime_writer=runtime_writer,
    )


def _apply_recovery_hold(account: Any, decision: AuthGateDecision, source: str, runtime_writer: Any) -> None:
    set_account_recovery_hold(
        account,
        decision.reason or RECOVERY_BLOCK_REASON,
        source=source,
        runtime_writer=runtime_writer,
    )


def _apply_banned_hold(account: Any, decision: AuthGateDecision, source: str, runtime_writer: Any) -> None:
    try:
        from services.ban_guard import set_account_banned_hold

        set_account_banned_hold(
            account,
            decision.reason or "",
            source=source,
            runtime_writer=runtime_writer,
        )
    except Exception:
        "intentional: ignore exception"
    _clear_hold_runtime(runtime_writer, account, "banned")


def _apply_suspended_hold(account: Any, decision: AuthGateDecision, source: str, runtime_writer: Any) -> None:
    try:
        from services.ban_guard import set_account_suspended_hold

        set_account_suspended_hold(
            account,
            decision.reason or "",
            source=source,
            runtime_writer=runtime_writer,
        )
    except Exception:
        "intentional: ignore exception"
    _clear_hold_runtime(runtime_writer, account, "suspended")


def _apply_cookie_hold(account: Any, decision: AuthGateDecision, source: str, runtime_writer: Any) -> None:
    _set_account_cookie_block(account, decision.reason)
    _clear_hold_runtime(runtime_writer, account, decision.reason_key)


def _hold_kind(decision: AuthGateDecision) -> str:
    if decision.reason_key == CAPTCHA_REASON:
        return "captcha"
    if decision.reason_key in (LOCKED_REASON, "locked") or decision.category == "locked":
        return "locked"
    if decision.reason_key == RECOVERY_REASON or decision.category == "account_recovery":
        return "recovery"
    if decision.reason_key == "banned" or decision.category == "banned":
        return "banned"
    if decision.reason_key == "suspended" or decision.category == "suspended":
        return "suspended"
    return "cookie"


_HOLD_APPLIERS = {
    "captcha": _apply_captcha_hold,
    "locked": _apply_locked_hold,
    "recovery": _apply_recovery_hold,
    "banned": _apply_banned_hold,
    "suspended": _apply_suspended_hold,
    "cookie": _apply_cookie_hold,
}


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
    _HOLD_APPLIERS[_hold_kind(decision)](account, decision, source or "auth_gate", runtime_writer)
    return decision
