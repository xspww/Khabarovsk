from __future__ import annotations

from typing import Any, Mapping

# ── Locked: face / ID verification ─────────────────────────────────────
LOCKED_REASON = "identity_locked"
LOCKED_LABEL = "Locked"
LOCKED_BLOCK_REASON = "Account locked - face/ID verification required. Verify manually, then click Resume."

LOCKED_KEYWORDS = (
    "verify your identity",
    "verify it's you",
    "verify its you",
    "verify it is you",
    "verify you are",
    "verify you're",
    "verify you’re",
    "identity verification",
    "age verification",
    "id verification",
    "verify your age",
    "verify age",
    "face scan",
    "scan your face",
    "face verification",
    "selfie verification",
    "selfie check",
    "liveness verification",
    "liveness check",
    "government id",
    "government-issued",
    "upload id",
    "submit id",
    "id document",
    "provide id",
    "account locked",
    "account has been locked",
    "your account is locked",
    "temporarily locked",
    "locked for verification",
    "confirm your identity",
    "confirm identity",
    "proof of identity",
    "need to verify you",
    "complete verification",
    "account is locked",
    "your account has been locked",
    # Suspicious-activity lock ("verify you're human" screen)
    "suspicious activity",
    "unusual activity",
    "verify you are human",
    "verify you're human",
    "verify you're a human",
    "prove you're human",
    "prove you are human",
    "confirm you are human",
    "confirm you're human",
    "verify that you are human",
    "verify that you're human",
    # Thai Roblox client / user wording
    "ยืนยันตัวตน",
    "สแกนหน้า",
    "สแกนใบหน้า",
    "บัญชีถูกล็อค",
    "บัญชีติดล็อค",
    "ยืนยันอายุ",
    # Suspicious-activity lock screen (Thai)
    "บัญชีถูกล็อก",
    "บัญชีติดล็อก",
    "กิจกรรมที่น่าสงสัย",
    "กิจกรรมที่ผิดปกติ",
    "ยืนยันว่าคุณเป็นมนุษย์",
    "คุณเป็นมนุษย์",
    "ปลดล็อคบัญชี",
    "ปลดล็อกบัญชี",
)

LOCKED_UI_KEYWORDS = (
    "verify your identity",
    "verify it's you",
    "verify its you",
    "identity verification",
    "age verification",
    "id verification",
    "verify your age",
    "face scan",
    "scan your face",
    "face verification",
    "selfie",
    "government id",
    "upload id",
    "account locked",
    "account is locked",
    "confirm your identity",
    "suspicious activity",
    "verify you are human",
    "verify you're human",
    "prove you're human",
    "ยืนยันตัวตน",
    "สแกนหน้า",
    "สแกนใบหน้า",
    "บัญชีถูกล็อค",
    "บัญชีติดล็อค",
    "บัญชีถูกล็อก",
    "บัญชีติดล็อก",
    "กิจกรรมที่น่าสงสัย",
    "คุณเป็นมนุษย์",
    "ปลดล็อคบัญชี",
    "ปลดล็อกบัญชี",
)

# ── Recovery: passkey / 2FA / account recovery ─────────────────────────
RECOVERY_REASON = "account_recovery"
RECOVERY_LABEL = "Recovery"
RECOVERY_BLOCK_REASON = "Account recovery - passkey/2FA required. Complete recovery manually, then click Resume."

RECOVERY_KEYWORDS = (
    "passkey",
    "pass key",
    "webauthn",
    "security key",
    "recovery key",
    "recovery code",
    "recovery phrase",
    "backup code",
    "backup codes",
    "backup verification code",
    "account recovery",
    "recover your account",
    "recover account",
    "2-step verification",
    "2 step verification",
    "two-factor",
    "two factor",
    "2fa",
    "authenticator",
    "prove account ownership",
    "confirm account ownership",
    "verify account ownership",
    "add a passkey",
    "create a passkey",
    "use passkey",
    "sign in with passkey",
    "login with passkey",
    "recovery email",
    "recovery phone",
    # Thai wording
    "กู้กุญแจ",
    "กู้บัญชี",
    "รหัสกู้",
    "คีย์กู้",
    "ยืนยัน 2 ขั้น",
    "รหัสสำรอง",
)

RECOVERY_UI_KEYWORDS = (
    "passkey",
    "pass key",
    "webauthn",
    "security key",
    "recovery key",
    "recovery code",
    "account recovery",
    "recover your account",
    "2-step verification",
    "2 step verification",
    "two-factor",
    "two factor",
    "2fa",
    "authenticator",
    "prove account ownership",
    "กู้กุญแจ",
    "กู้บัญชี",
    "รหัสกู้",
)


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _is_cookie_auth_status_text(text: str) -> bool:
    return (
        "cookie identity mismatch" in text
        or "cookie belongs to" in text
        or ".roblosecurity" in text
        or "cookie invalid" in text
    )


# ── Locked helpers ─────────────────────────────────────────────────────

def is_locked_text(*values: Any, headers: Mapping[str, Any] | None = None) -> bool:
    text = " ".join(_lower(v) for v in values if v is not None).strip()
    if headers:
        try:
            extra = " ".join(f"{k}={v}" for k, v in dict(headers).items())
            text = f"{text} {_lower(extra)}".strip()
        except Exception:
            pass
    if not text:
        return False
    return any(k in text for k in LOCKED_KEYWORDS)


def is_locked_window_texts(values: Any) -> bool:
    if isinstance(values, (str, bytes)):
        parts = [values]
    else:
        try:
            parts = list(values or [])
        except TypeError:
            parts = [values]
    normalized = [_lower(p) for p in parts if _lower(p)]
    joined = " | ".join(normalized)
    if not joined:
        return False
    if is_locked_text(joined):
        return True
    return any(k in joined for k in LOCKED_UI_KEYWORDS)


def is_locked_status_text(*values: Any) -> bool:
    for value in values:
        text = _lower(value)
        if not text or _is_cookie_auth_status_text(text):
            continue
        if text.strip() in (LOCKED_REASON, "locked", "identity_locked"):
            return True
        if is_locked_text(text):
            return True
    return False


def is_account_locked(account: Any) -> bool:
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
        if _lower(getattr(account, "last_crash_reason", "")) == LOCKED_REASON:
            return True
        if _lower(getattr(account, "recovery_status", "")) == LOCKED_REASON:
            return True
        return is_locked_status_text(*fields)
    except Exception:
        return False


def persist_account_locked_status(account: Any, active: bool = True) -> None:
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
        if active:
            updates = {"manual_status": LOCKED_BLOCK_REASON, "import_status": LOCKED_REASON}
        else:
            # Write back the surgically-cleared in-memory values so a
            # co-existing hold is preserved in the store.
            updates = {
                "manual_status": str(getattr(account, "manual_status", "") or ""),
                "import_status": str(getattr(account, "import_status", "") or ""),
            }
        ACCOUNT_STORE.update_record(username, updates)
    except Exception:
        pass


def _apply_runtime_hold(account: Any, runtime_writer: Any, reason: str) -> None:
    if runtime_writer:
        if hasattr(runtime_writer, "set_recovery"):
            runtime_writer.set_recovery(account, status=reason, reason=reason, inflight=False)
        if hasattr(runtime_writer, "set_cooldown"):
            runtime_writer.set_cooldown(account, 0.0, reason=reason)
        return
    try:
        account.sync_runtime(reason)
    except Exception:
        pass


def _apply_runtime_clear(account: Any, runtime_writer: Any, reason: str = "manual_resume") -> None:
    if runtime_writer:
        if hasattr(runtime_writer, "clear_recovery"):
            runtime_writer.clear_recovery(account, reason=reason, inflight=False)
        elif hasattr(runtime_writer, "set_recovery"):
            runtime_writer.set_recovery(account, reason=reason, inflight=False)
        if hasattr(runtime_writer, "set_cooldown"):
            runtime_writer.set_cooldown(account, 0.0, reason=reason)
        return
    try:
        account.sync_runtime(reason)
    except Exception:
        pass


def set_account_locked_hold(account: Any, detail: str = "", source: str = "", runtime_writer: Any = None) -> None:
    reason = LOCKED_BLOCK_REASON
    lock = getattr(account, "_lock", None)

    def _set() -> None:
        account.session_checked = True
        account.session_valid = False
        account.session_wait_started_at = 0.0
        account.manual_status = reason
        account.import_status = LOCKED_REASON
        account.last_error = detail or reason
        account.last_crash_reason = LOCKED_REASON
        account.last_recovery_reason = LOCKED_REASON
        account.recovery_scheduled_at = 0.0
        account.last_state_reason = source or LOCKED_REASON
        _apply_runtime_hold(account, runtime_writer, source or LOCKED_REASON)

    if lock:
        with lock:
            _set()
    else:
        _set()
    persist_account_locked_status(account, active=True)


def clear_account_locked_hold(account: Any, runtime_writer: Any = None) -> bool:
    was_locked = is_account_locked(account)
    lock = getattr(account, "_lock", None)

    def _clear() -> None:
        if is_locked_status_text(getattr(account, "manual_status", "")):
            account.manual_status = ""
        if is_locked_status_text(getattr(account, "last_error", "")):
            account.last_error = ""
        if _lower(getattr(account, "last_crash_reason", "")) == LOCKED_REASON:
            account.last_crash_reason = ""
        if _lower(getattr(account, "last_recovery_reason", "")) == LOCKED_REASON:
            account.last_recovery_reason = ""
        if is_locked_status_text(getattr(account, "last_state_reason", "")):
            account.last_state_reason = ""
        if is_locked_status_text(getattr(account, "import_status", "")):
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
        _apply_runtime_clear(account, runtime_writer, "manual_resume")
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
    if was_locked:
        persist_account_locked_status(account, active=False)
    return was_locked


# ── Recovery helpers ───────────────────────────────────────────────────

def is_recovery_text(*values: Any, headers: Mapping[str, Any] | None = None) -> bool:
    text = " ".join(_lower(v) for v in values if v is not None).strip()
    if headers:
        try:
            extra = " ".join(f"{k}={v}" for k, v in dict(headers).items())
            text = f"{text} {_lower(extra)}".strip()
        except Exception:
            pass
    if not text:
        return False
    return any(k in text for k in RECOVERY_KEYWORDS)


def is_recovery_window_texts(values: Any) -> bool:
    if isinstance(values, (str, bytes)):
        parts = [values]
    else:
        try:
            parts = list(values or [])
        except TypeError:
            parts = [values]
    normalized = [_lower(p) for p in parts if _lower(p)]
    joined = " | ".join(normalized)
    if not joined:
        return False
    if is_recovery_text(joined):
        return True
    return any(k in joined for k in RECOVERY_UI_KEYWORDS)


def is_recovery_status_text(*values: Any) -> bool:
    for value in values:
        text = _lower(value)
        if not text or _is_cookie_auth_status_text(text):
            continue
        if text.strip() == RECOVERY_REASON:
            return True
        if is_recovery_text(text):
            return True
    return False


def is_account_recovery(account: Any) -> bool:
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
        if _lower(getattr(account, "last_crash_reason", "")) == RECOVERY_REASON:
            return True
        if _lower(getattr(account, "recovery_status", "")) == RECOVERY_REASON:
            return True
        return is_recovery_status_text(*fields)
    except Exception:
        return False


def persist_account_recovery_status(account: Any, active: bool = True) -> None:
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
        if active:
            updates = {"manual_status": RECOVERY_BLOCK_REASON, "import_status": RECOVERY_REASON}
        else:
            # Write back the surgically-cleared in-memory values so a
            # co-existing hold is preserved in the store.
            updates = {
                "manual_status": str(getattr(account, "manual_status", "") or ""),
                "import_status": str(getattr(account, "import_status", "") or ""),
            }
        ACCOUNT_STORE.update_record(username, updates)
    except Exception:
        pass


def set_account_recovery_hold(account: Any, detail: str = "", source: str = "", runtime_writer: Any = None) -> None:
    reason = RECOVERY_BLOCK_REASON
    lock = getattr(account, "_lock", None)

    def _set() -> None:
        account.session_checked = True
        account.session_valid = False
        account.session_wait_started_at = 0.0
        account.manual_status = reason
        account.import_status = RECOVERY_REASON
        account.last_error = detail or reason
        account.last_crash_reason = RECOVERY_REASON
        account.last_recovery_reason = RECOVERY_REASON
        account.recovery_scheduled_at = 0.0
        account.last_state_reason = source or RECOVERY_REASON
        _apply_runtime_hold(account, runtime_writer, source or RECOVERY_REASON)

    if lock:
        with lock:
            _set()
    else:
        _set()
    persist_account_recovery_status(account, active=True)


def clear_account_recovery_hold(account: Any, runtime_writer: Any = None) -> bool:
    was_recovery = is_account_recovery(account)
    lock = getattr(account, "_lock", None)

    def _clear() -> None:
        if is_recovery_status_text(getattr(account, "manual_status", "")):
            account.manual_status = ""
        if is_recovery_status_text(getattr(account, "last_error", "")):
            account.last_error = ""
        if _lower(getattr(account, "last_crash_reason", "")) == RECOVERY_REASON:
            account.last_crash_reason = ""
        if _lower(getattr(account, "last_recovery_reason", "")) == RECOVERY_REASON:
            account.last_recovery_reason = ""
        if is_recovery_status_text(getattr(account, "last_state_reason", "")):
            account.last_state_reason = ""
        if is_recovery_status_text(getattr(account, "import_status", "")):
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
        _apply_runtime_clear(account, runtime_writer, "manual_resume")
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
    if was_recovery:
        persist_account_recovery_status(account, active=False)
    return was_recovery
