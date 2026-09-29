from __future__ import annotations

from typing import Any

BANNED_REASON = "banned"
BANNED_LABEL = "Banned"
BANNED_BLOCK_REASON = "Account banned (Roblox). Unmark to allow rejoin."
BANNED_IMPORT_STATUS = "banned"
SUSPENDED_LABEL = "Suspended"
SUSPENDED_BLOCK_REASON = "Account suspended by Roblox. Unmark to allow rejoin."


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def is_banned_text(*values: Any) -> bool:
    text = " ".join(_lower(v) for v in values if v is not None).strip()
    if not text:
        return False
    # "isBanned" from Roblox profile payload is the strongest signal.
    if "isbanned" in text.replace(" ", "").replace("_", "").replace("-", ""):
        return True
    if (
        "account banned" in text
        or "roblox ban" in text
        or text.strip() == "banned"
        or "user is banned" in text
    ):
        return True
    # A temporary suspension is not a ban, but it blocks the account just as
    # hard, so it must hold the launch the same way. Phrased to match the
    # summary built in roblox_hybrid.fetch_moderation_status.
    if "suspended by roblox" in text or "account suspended" in text:
        return True
    # "terminated" alone is too broad (e.g. "session terminated", "connection
    # terminated by user"). Only treat it as an account ban when the wording
    # names the account itself. A bare "user" mention is far too common
    # ("session terminated for user") and would ban healthy accounts.
    if "terminat" in text and ("account" in text or "deleted" in text):
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


def persist_account_banned_status(account: Any, active: bool = True) -> None:
    try:
        from account_hybrid import ACCOUNT_STORE

        username = ""
        for attr in ("_config_username", "username"):
            value = str(getattr(account, attr, "") or "").strip()
            if value:
                username = value
                break
        if not username:
            return
        updates = (
            {
                "manual_status": BANNED_BLOCK_REASON,
                "import_status": BANNED_IMPORT_STATUS,
            }
            if active
            else {
                "manual_status": "",
                "import_status": "",
            }
        )
        ACCOUNT_STORE.update_record(username, updates)
    except Exception:
        pass


def set_account_banned_hold(account: Any, detail: str = "", source: str = "", runtime_writer: Any = None) -> None:
    reason = BANNED_BLOCK_REASON
    lock = getattr(account, "_lock", None)

    def _set() -> None:
        account.session_checked = True
        account.session_valid = False
        account.session_wait_started_at = 0.0
        account.manual_status = reason
        account.last_error = detail or reason
        account.last_crash_reason = BANNED_REASON
        account.last_recovery_reason = BANNED_REASON
        account.recovery_scheduled_at = 0.0
        account.last_state_reason = source or BANNED_REASON
        if runtime_writer is not None:
            try:
                if hasattr(runtime_writer, "set_recovery"):
                    runtime_writer.set_recovery(account, status=BANNED_REASON, reason=BANNED_REASON, inflight=False)
                if hasattr(runtime_writer, "set_cooldown"):
                    runtime_writer.set_cooldown(account, 0.0, reason=BANNED_REASON)
            except Exception:
                pass
        else:
            try:
                account.sync_runtime(source or BANNED_REASON)
            except Exception:
                pass

    if lock:
        with lock:
            _set()
    else:
        _set()
    persist_account_banned_status(account, active=True)


def clear_account_banned_hold(account: Any, runtime_writer: Any = None) -> bool:
    was_banned = is_account_banned(account)
    lock = getattr(account, "_lock", None)

    def _clear() -> None:
        if is_banned_status_text(getattr(account, "manual_status", "")):
            account.manual_status = ""
        if is_banned_status_text(getattr(account, "last_error", "")):
            account.last_error = ""
        if _lower(getattr(account, "last_crash_reason", "")) == BANNED_REASON:
            account.last_crash_reason = ""
        if _lower(getattr(account, "last_recovery_reason", "")) == BANNED_REASON:
            account.last_recovery_reason = ""
        if is_banned_status_text(getattr(account, "last_state_reason", "")):
            account.last_state_reason = ""
        if is_banned_status_text(getattr(account, "import_status", "")):
            account.import_status = ""
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
        if runtime_writer is not None:
            try:
                if hasattr(runtime_writer, "clear_recovery"):
                    runtime_writer.clear_recovery(account, reason="manual_unban", inflight=False)
                elif hasattr(runtime_writer, "set_recovery"):
                    runtime_writer.set_recovery(account, reason="manual_unban", inflight=False)
                if hasattr(runtime_writer, "set_cooldown"):
                    runtime_writer.set_cooldown(account, 0.0, reason="manual_unban")
            except Exception:
                pass
        else:
            try:
                account.sync_runtime("manual_unban")
            except Exception:
                pass
        try:
            account.runtime.last_error = ""
            account.runtime.recovery_reason = ""
            account.runtime.recovery_active = False
        except Exception:
            pass

    if lock:
        with lock:
            _clear()
    else:
        _clear()
    persist_account_banned_status(account, active=False)
    return was_banned
