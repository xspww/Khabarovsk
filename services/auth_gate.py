from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core import account_launch_block_reason, cookie_invalid_block_reason
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
        from services.ban_guard import (
            BANNED_BLOCK_REASON,
            BANNED_REASON,
            SUSPENDED_BLOCK_REASON,
            SUSPENDED_REASON,
            is_account_banned,
            is_account_suspended,
        )

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
    except Exception:
        pass
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
    elif decision.reason_key in (LOCKED_REASON, "locked") or decision.category == "locked":
        set_account_locked_hold(
            account,
            decision.reason or LOCKED_BLOCK_REASON,
            source=source or "auth_gate",
            runtime_writer=runtime_writer,
        )
    elif decision.reason_key == RECOVERY_REASON or decision.category == "account_recovery":
        set_account_recovery_hold(
            account,
            decision.reason or RECOVERY_BLOCK_REASON,
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
    elif decision.reason_key == "suspended" or decision.category == "suspended":
        try:
            from services.ban_guard import set_account_suspended_hold

            set_account_suspended_hold(
                account,
                decision.reason or "",
                source=source or "auth_gate",
                runtime_writer=runtime_writer,
            )
        except Exception:
            pass
        if runtime_writer is not None:
            try:
                runtime_writer.set_recovery(account, status="suspended", reason="suspended", inflight=False)
                runtime_writer.set_cooldown(account, 0.0, reason="suspended")
            except Exception:
                pass
    else:
        try:
            from runtime.recovery_support import _set_account_cookie_block as _cookie_block
        except ImportError:  # pragma: no cover - keeps services->runtime out of top level
            def _cookie_block(acc: Any, reason: str, cookie_username: str = "") -> None:
                # Degraded mirror of runtime.recovery_support._set_account_cookie_block:
                # same fields, same lock, best-effort persist (no recovery import).
                lock = getattr(acc, "_lock", None)

                def _write() -> None:
                    if cookie_username:
                        acc.cookie_username = str(cookie_username)
                    acc.cookie_mismatch = True
                    acc.session_checked = True
                    acc.session_valid = False
                    acc.manual_status = reason
                    acc.last_error = reason
                    acc.last_crash_reason = "cookie_mismatch"

                if lock is not None:
                    with lock:
                        _write()
                else:
                    _write()
                try:
                    from core import flog_kv as _flog

                    _flog(
                        "ACCOUNT_DATA",
                        "cookie_identity_status_persist_degraded",
                        "warning",
                        account=getattr(acc, "display_name", ""),
                    )
                except Exception:
                    pass

        _cookie_block(account, decision.reason)
        if runtime_writer is not None:
            try:
                runtime_writer.set_recovery(account, status=decision.reason_key, reason=decision.reason_key, inflight=False)
                runtime_writer.set_cooldown(account, 0.0, reason=decision.reason_key)
            except Exception:
                pass
    return decision
