from __future__ import annotations

from typing import Any, Mapping


CAPTCHA_REASON = "captcha_required"
CAPTCHA_LABEL = "Captcha"
CAPTCHA_BLOCK_REASON = "CAPTCHA required. Solve it manually, then click Resume or Reload Cookies."

CAPTCHA_KEYWORDS = (
    "captcha",
    "arkose",
    "funcaptcha",
    "challenge-required",
    "challenge required",
    "security challenge",
    "verification required",
    "prove you",
    "robot check",
)

CAPTCHA_UI_KEYWORDS = (
    "captcha",
    "arkose",
    "funcaptcha",
    "verifying you're not a bot",
    "verify you're not a bot",
    "not a bot",
    "start puzzle",
    "please solve this challenge",
    "real person",
    "security challenge",
    "verification",
)

CHALLENGE_HEADER_KEYS = (
    "rblx-challenge-id",
    "rblx-challenge-type",
    "rblx-challenge-metadata",
    "rblx-challenge-token",
)


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def challenge_header_summary(headers: Mapping[str, Any] | None = None) -> str:
    if not headers:
        return ""
    parts = []
    for key, value in headers.items():
        key_text = str(key or "").strip()
        if key_text.lower() in CHALLENGE_HEADER_KEYS:
            parts.append(f"{key_text}={str(value or '').strip()[:120]}")
    return " ".join(parts)


def is_captcha_text(*values: Any, headers: Mapping[str, Any] | None = None) -> bool:
    text = " ".join(_lower(value) for value in values if value is not None)
    header_text = _lower(challenge_header_summary(headers))
    combined = f"{text} {header_text}".strip()
    if not combined:
        return False
    if "rblx-challenge-type" in combined and "captcha" in combined:
        return True
    return any(keyword in combined for keyword in CAPTCHA_KEYWORDS)


def is_captcha_window_texts(values: Any) -> bool:
    if isinstance(values, (str, bytes)):
        parts = [values]
    else:
        try:
            parts = list(values or [])
        except TypeError:
            parts = [values]
    normalized = [_lower(part) for part in parts if _lower(part)]
    joined = " | ".join(normalized)
    if not joined:
        return False
    if is_captcha_text(joined):
        return True
    if any(keyword in joined for keyword in CAPTCHA_UI_KEYWORDS):
        return True
    if "security" in normalized and (
        "chrome legacy window" in normalized
        or any("roblox" in text for text in normalized)
    ):
        return True
    return False


def _is_cookie_auth_status_text(text: str) -> bool:
    return (
        "cookie identity mismatch" in text
        or "cookie belongs to" in text
        or ".roblosecurity" in text
        or "cookie invalid" in text
    )


def is_captcha_status_text(*values: Any) -> bool:
    for value in values:
        text = _lower(value)
        if not text or _is_cookie_auth_status_text(text):
            continue
        if is_captcha_text(text):
            return True
    return False


def captcha_detail(status: Any = "", body: Any = "", headers: Mapping[str, Any] | None = None) -> str:
    header_text = challenge_header_summary(headers)
    if not is_captcha_text(status, body, header_text):
        return ""
    status_text = f"HTTP {status}" if status not in ("", None) else "Roblox challenge"
    body_text = str(body or "").strip().replace("\r", " ").replace("\n", " ")
    if len(body_text) > 180:
        body_text = body_text[:180]
    extra = " ".join(part for part in (header_text, body_text) if part).strip()
    return f"{status_text} CAPTCHA challenge detected" + (f" ({extra})" if extra else "")


def is_account_captcha_required(account: Any) -> bool:
    fields = (
        getattr(account, "manual_status", ""),
        getattr(account, "last_error", ""),
        getattr(account, "last_crash_reason", ""),
        getattr(account, "last_recovery_reason", ""),
        getattr(account, "recovery_status", ""),
        getattr(account, "last_state_reason", ""),
        getattr(account, "import_status", ""),
    )
    if _lower(getattr(account, "last_crash_reason", "")) == CAPTCHA_REASON:
        return True
    if _lower(getattr(account, "recovery_status", "")) == CAPTCHA_REASON:
        return True
    return is_captcha_status_text(*fields)


def persist_account_captcha_status(account: Any, active: bool = True) -> None:
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
                "manual_status": CAPTCHA_BLOCK_REASON,
                "import_status": CAPTCHA_REASON,
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


def _apply_runtime_captcha_hold(account: Any, runtime_writer: Any = None, reason: str = "") -> None:
    if runtime_writer:
        if hasattr(runtime_writer, "set_recovery"):
            runtime_writer.set_recovery(account, status=CAPTCHA_REASON, reason=reason or CAPTCHA_REASON, inflight=False)
        if hasattr(runtime_writer, "set_cooldown"):
            runtime_writer.set_cooldown(account, 0.0, reason=reason or CAPTCHA_REASON)
        return
    try:
        account.sync_runtime(reason or CAPTCHA_REASON)
    except Exception:
        pass


def _apply_runtime_captcha_clear(account: Any, runtime_writer: Any = None, reason: str = "manual_resume") -> None:
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


def set_account_captcha_hold(account: Any, detail: str = "", source: str = "", runtime_writer: Any = None) -> None:
    reason = CAPTCHA_BLOCK_REASON
    lock = getattr(account, "_lock", None)

    def _set() -> None:
        account.session_checked = True
        account.session_valid = False
        account.session_wait_started_at = 0.0
        account.manual_status = reason
        account.last_error = detail or reason
        account.last_crash_reason = CAPTCHA_REASON
        account.last_recovery_reason = CAPTCHA_REASON
        account.recovery_scheduled_at = 0.0
        account.last_state_reason = source or CAPTCHA_REASON
        _apply_runtime_captcha_hold(account, runtime_writer, source or CAPTCHA_REASON)

    if lock:
        with lock:
            _set()
    else:
        _set()
    persist_account_captcha_status(account, active=True)


def clear_account_captcha_hold(account: Any, runtime_writer: Any = None) -> bool:
    was_captcha = is_account_captcha_required(account)
    lock = getattr(account, "_lock", None)

    def _clear() -> None:
        if is_captcha_status_text(getattr(account, "manual_status", "")):
            account.manual_status = ""
        if is_captcha_status_text(getattr(account, "last_error", "")):
            account.last_error = ""
        if _lower(getattr(account, "last_crash_reason", "")) == CAPTCHA_REASON:
            account.last_crash_reason = ""
        if _lower(getattr(account, "last_recovery_reason", "")) == CAPTCHA_REASON:
            account.last_recovery_reason = ""
        if is_captcha_status_text(getattr(account, "last_state_reason", "")):
            account.last_state_reason = ""
        if is_captcha_status_text(getattr(account, "import_status", "")):
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
        _apply_runtime_captcha_clear(account, runtime_writer, "manual_resume")
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
    persist_account_captcha_status(account, active=False)
    return was_captcha
