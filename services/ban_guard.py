from __future__ import annotations

from typing import Any

from services._util import _lower

BANNED_REASON = "banned"
BANNED_LABEL = "Banned"
BANNED_BLOCK_REASON = "Account banned (Roblox). Unmark to allow rejoin."
BANNED_IMPORT_STATUS = "banned"
SUSPENDED_REASON = "suspended"
SUSPENDED_LABEL = "Suspended"
SUSPENDED_BLOCK_REASON = "Account suspended by Roblox. Unmark to allow rejoin."
SUSPENDED_IMPORT_STATUS = "suspended"


def is_suspended_text(*values: Any) -> bool:
    """True only for a temporary suspension, never for a permanent ban.

    Matches the summary built in roblox_hybrid.fetch_moderation_status.
    """
    text = " ".join(_lower(v) for v in values if v is not None).strip()
    if not text:
        return False
    return "suspended by roblox" in text or "account suspended" in text


def _has_ban_phrase(text: str) -> bool:
    return (
        "account banned" in text
        or "roblox ban" in text
        or text.strip() == "banned"
        or "user is banned" in text
        or "user is moderated" in text
    )


def _is_terminated_account(text: str) -> bool:
    return "terminat" in text and ("account" in text or "deleted" in text)


def is_banned_text(*values: Any) -> bool:
    text = " ".join(_lower(v) for v in values if v is not None).strip()
    if not text:
        return False
    # A suspension is not a ban, but it blocks the account just as hard, so it
    # must hold the launch the same way. Callers that need to tell them apart
    # use is_suspended_text.
    if is_suspended_text(text):
        return True
    # "isBanned" from Roblox profile payload is the strongest signal.
    if "isbanned" in text.replace(" ", "").replace("_", "").replace("-", ""):
        return True
    if _has_ban_phrase(text):
        return True
    # "terminated" alone is too broad (e.g. "session terminated", "connection
    # terminated by user"). Only treat it as an account ban when the wording
    # names the account itself. A bare "user" mention is far too common
    # ("session terminated for user") and would ban healthy accounts.
    return _is_terminated_account(text)


def is_suspended_status_text(*values: Any) -> bool:
    for value in values:
        text = _lower(value)
        if not text:
            continue
        if text.strip() in (SUSPENDED_REASON, SUSPENDED_IMPORT_STATUS):
            return True
        if is_suspended_text(text):
            return True
    return False


def is_banned_status_text(*values: Any) -> bool:
    for value in values:
        text = _lower(value)
        if not text:
            continue
        # Don't confuse universe/place bans with account bans here;
        # account-level mark is import_status == "banned" or the block reason.
        if text.strip() == BANNED_REASON or text.strip() == BANNED_IMPORT_STATUS:
            return True
        if is_banned_text(text):
            # Cookie/captcha texts never contain ban markers, safe to accept.
            return True
    return False


def is_account_banned(account: Any) -> bool:
    try:
        fields = (
            getattr(account, "manual_status", ""),
            getattr(account, "last_error", ""),
            getattr(account, "last_crash_reason", ""),
            getattr(account, "last_recovery_reason", ""),
            getattr(account, "recovery_status", ""),
            getattr(account, "last_state_reason", ""),
            getattr(account, "import_status", ""),
        )
        if _lower(getattr(account, "last_crash_reason", "")) == BANNED_REASON:
            return True
        if _lower(getattr(account, "recovery_status", "")) == BANNED_REASON:
            return True
        if _lower(getattr(account, "import_status", "")) == BANNED_IMPORT_STATUS:
            return True
        return is_banned_status_text(*fields)
    except Exception:
        return False


def is_account_suspended(account: Any) -> bool:
    """True when the account carries a suspension mark (not a permanent ban)."""
    try:
        if _lower(getattr(account, "last_crash_reason", "")) == SUSPENDED_REASON:
            return True
        if _lower(getattr(account, "recovery_status", "")) == SUSPENDED_REASON:
            return True
        if _lower(getattr(account, "import_status", "")) == SUSPENDED_IMPORT_STATUS:
            return True
        return is_suspended_status_text(
            getattr(account, "manual_status", ""),
            getattr(account, "last_error", ""),
            getattr(account, "last_recovery_reason", ""),
            getattr(account, "last_state_reason", ""),
        )
    except Exception:
        return False


def is_account_permanently_banned(account: Any) -> bool:
    """True for a permanent ban only — a suspension is not one."""
    try:
        if is_account_suspended(account):
            return False
        return is_account_banned(account)
    except Exception:
        return False


def persist_account_banned_status(account: Any, active: bool = True) -> None:
    try:
        from services.account_hybrid import ACCOUNT_STORE

        username = ""
        for attr in ("_config_username", "username"):
            value = str(getattr(account, attr, "") or "").strip()
            if value:
                username = value
                break
        if not username:
            return
        suspended = is_account_suspended(account)
        if active:
            updates = (
                {
                    "manual_status": SUSPENDED_BLOCK_REASON,
                    "import_status": SUSPENDED_IMPORT_STATUS,
                }
                if suspended
                else {
                    "manual_status": BANNED_BLOCK_REASON,
                    "import_status": BANNED_IMPORT_STATUS,
                }
            )
        else:
            # Write back the surgically-cleared in-memory values so a
            # co-existing hold is preserved in the store.
            updates = {
                "manual_status": str(getattr(account, "manual_status", "") or ""),
                "import_status": str(getattr(account, "import_status", "") or ""),
            }
        ACCOUNT_STORE.update_record(username, updates)
    except Exception:
        "intentional: ignore exception"


def set_account_suspended_hold(account: Any, detail: str = "", source: str = "", runtime_writer: Any = None) -> None:
    _set_moderation_hold(
        account,
        detail=detail,
        source=source,
        runtime_writer=runtime_writer,
        reason=SUSPENDED_REASON,
        block_reason=SUSPENDED_BLOCK_REASON,
    )


def set_account_banned_hold(account: Any, detail: str = "", source: str = "", runtime_writer: Any = None) -> None:
    # A suspension already on the account must not be downgraded to a ban.
    if is_suspended_text(detail) or is_account_suspended(account):
        set_account_suspended_hold(account, detail=detail, source=source, runtime_writer=runtime_writer)
        return
    _set_moderation_hold(
        account,
        detail=detail,
        source=source,
        runtime_writer=runtime_writer,
        reason=BANNED_REASON,
        block_reason=BANNED_BLOCK_REASON,
    )


def _apply_moderation_writer(account: Any, runtime_writer: Any, reason: str) -> None:
    try:
        if hasattr(runtime_writer, "set_recovery"):
            runtime_writer.set_recovery(account, status=reason, reason=reason, inflight=False)
        if hasattr(runtime_writer, "set_cooldown"):
            runtime_writer.set_cooldown(account, 0.0, reason=reason)
    except Exception:
        "intentional: ignore exception"


def _notify_moderation_writer(account: Any, runtime_writer: Any, reason: str, sync_reason: str) -> None:
    if runtime_writer is not None:
        _apply_moderation_writer(account, runtime_writer, reason)
        return
    try:
        account.sync_runtime(sync_reason)
    except Exception:
        "intentional: ignore exception"


def _set_moderation_fields(account: Any, detail: str, reason: str, block_reason: str, source: str) -> None:
    account.session_checked = True
    account.session_valid = False
    account.session_wait_started_at = 0.0
    account.manual_status = block_reason
    account.last_error = detail or block_reason
    account.last_crash_reason = reason
    account.last_recovery_reason = reason
    account.recovery_scheduled_at = 0.0
    account.last_state_reason = source or reason
    try:
        account.import_status = SUSPENDED_IMPORT_STATUS if str(reason or "").lower() == SUSPENDED_REASON else BANNED_IMPORT_STATUS
    except Exception:
        "intentional: ignore exception"


def _apply_moderation_hold(
    account: Any,
    detail: str,
    source: str,
    runtime_writer: Any,
    reason: str,
    block_reason: str,
) -> None:
    _set_moderation_fields(account, detail, reason, block_reason, source)
    _notify_moderation_writer(account, runtime_writer, reason, source or reason)


def _set_moderation_hold(
    account: Any,
    detail: str = "",
    source: str = "",
    runtime_writer: Any = None,
    reason: str = BANNED_REASON,
    block_reason: str = BANNED_BLOCK_REASON,
) -> None:
    lock = getattr(account, "_lock", None)
    if lock:
        with lock:
            _apply_moderation_hold(account, detail, source, runtime_writer, reason, block_reason)
    else:
        _apply_moderation_hold(account, detail, source, runtime_writer, reason, block_reason)
    persist_account_banned_status(account, active=True)


def _is_moderation_status(value: Any) -> bool:
    return is_banned_status_text(value) or is_suspended_status_text(value)


def _clear_moderation_text_fields(account: Any) -> None:
    for attr in ("manual_status", "last_error", "last_state_reason", "import_status"):
        if _is_moderation_status(getattr(account, attr, "")):
            setattr(account, attr, "")
    for attr in ("last_crash_reason", "last_recovery_reason"):
        if _lower(getattr(account, attr, "")) in (BANNED_REASON, SUSPENDED_REASON):
            setattr(account, attr, "")


def _reset_moderation_counters(account: Any) -> None:
    account.recovery_scheduled_at = 0.0
    account.session_checked = False
    account.session_valid = False
    account.session_wait_started_at = 0.0
    account.retry_count = 0
    account.fail_count = 0
    account.launch_fail_count = 0
    account.crash_retry_count = 0
    account.network_retry_count = 0
    account.session_retry_count = 0


def _clear_recovery_writer(account: Any, runtime_writer: Any) -> None:
    if runtime_writer is None:
        try:
            account.sync_runtime("manual_unban")
        except Exception:
            "intentional: ignore exception"
        return
    try:
        if hasattr(runtime_writer, "clear_recovery"):
            runtime_writer.clear_recovery(account, reason="manual_unban", inflight=False)
        elif hasattr(runtime_writer, "set_recovery"):
            runtime_writer.set_recovery(account, reason="manual_unban", inflight=False)
        if hasattr(runtime_writer, "set_cooldown"):
            runtime_writer.set_cooldown(account, 0.0, reason="manual_unban")
    except Exception:
        "intentional: ignore exception"


def _clear_runtime_error_fields(account: Any) -> None:
    try:
        account.runtime.last_error = ""
        account.runtime.recovery_reason = ""
        account.runtime.recovery_active = False
    except Exception:
        "intentional: ignore exception"


def _clear_moderation_state(account: Any, runtime_writer: Any) -> None:
    _clear_moderation_text_fields(account)
    _reset_moderation_counters(account)
    _clear_recovery_writer(account, runtime_writer)
    _clear_runtime_error_fields(account)


def clear_account_banned_hold(account: Any, runtime_writer: Any = None) -> bool:
    was_banned = is_account_banned(account)
    try:
        was_suspended = is_account_suspended(account)
    except Exception:
        was_suspended = False
    was_moderated = bool(was_banned or was_suspended)
    lock = getattr(account, "_lock", None)
    if lock:
        with lock:
            _clear_moderation_state(account, runtime_writer)
    else:
        _clear_moderation_state(account, runtime_writer)
    if was_moderated:
        persist_account_banned_status(account, active=False)
    return was_moderated
