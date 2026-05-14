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
    return "security" in normalized and "chrome legacy window" in normalized


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
    )
    if _lower(getattr(account, "last_crash_reason", "")) == CAPTCHA_REASON:
        return True
    if _lower(getattr(account, "recovery_status", "")) == CAPTCHA_REASON:
        return True
    return is_captcha_text(*fields)


def persist_account_captcha_status(account: Any, active: bool = True) -> None:
    try:
        from account_hybrid import ACCOUNT_STORE

        names = []
        for attr in ("_config_username", "username", "cookie_username", "display_name"):
            value = str(getattr(account, attr, "") or "").strip()
            if value and value.lower() not in {item.lower() for item in names}:
                names.append(value)
        if not names:
            return
        updates = (
            {
                "manual_status": CAPTCHA_BLOCK_REASON,
                "import_status": CAPTCHA_REASON,
                "cookie_mismatch": False,
            }
            if active
            else {
                "manual_status": "",
                "import_status": "",
                "cookie_mismatch": False,
            }
        )
        for username in names:
            ACCOUNT_STORE.update_record(username, updates)
    except Exception:
        pass


def set_account_captcha_hold(account: Any, detail: str = "", source: str = "") -> None:
    reason = CAPTCHA_BLOCK_REASON
    lock = getattr(account, "_lock", None)

    def _set() -> None:
        account.cookie_mismatch = False
        account.session_checked = True
        account.session_valid = False
        account.session_wait_started_at = 0.0
        account.manual_status = reason
        account.last_error = detail or reason
        account.last_crash_reason = CAPTCHA_REASON
        account.last_recovery_reason = CAPTCHA_REASON
        account.recovery_status = CAPTCHA_REASON
        account.recovery_inflight = False
        account.cooldown_until = 0.0
        account.recovery_scheduled_at = 0.0
        account.last_state_reason = source or CAPTCHA_REASON
        try:
            account.sync_runtime(source or CAPTCHA_REASON)
        except Exception:
            pass

    if lock:
        with lock:
            _set()
    else:
        _set()
    persist_account_captcha_status(account, active=True)


def clear_account_captcha_hold(account: Any) -> bool:
    was_captcha = is_account_captcha_required(account)
    lock = getattr(account, "_lock", None)

    def _clear() -> None:
        account.cookie_mismatch = False
        if is_captcha_text(getattr(account, "manual_status", "")):
            account.manual_status = ""
        if is_captcha_text(getattr(account, "last_error", "")):
            account.last_error = ""
        if _lower(getattr(account, "last_crash_reason", "")) == CAPTCHA_REASON:
            account.last_crash_reason = ""
        if _lower(getattr(account, "last_recovery_reason", "")) == CAPTCHA_REASON:
            account.last_recovery_reason = ""
        if _lower(getattr(account, "recovery_status", "")) == CAPTCHA_REASON:
            account.recovery_status = ""
        if is_captcha_text(getattr(account, "last_state_reason", "")):
            account.last_state_reason = ""
        if is_captcha_text(getattr(account, "import_status", "")):
            account.import_status = ""
        account.recovery_inflight = False
        account.recovery_scheduled_at = 0.0
        account.cooldown_until = 0.0
        account.session_checked = False
        account.session_valid = False
        account.session_wait_started_at = 0.0
        account.retry_count = 0
        account.fail_count = 0
        account.launch_fail_count = 0
        account.crash_retry_count = 0
        account.network_retry_count = 0
        account.session_retry_count = 0
        try:
            account.sync_runtime("manual_resume")
            account.runtime.last_error = ""
            account.runtime.recovery_status = ""
            account.runtime.recovery_reason = ""
            account.runtime.recovery_active = False
            account.runtime.recovery_inflight = False
        except Exception:
            pass

    if lock:
        with lock:
            _clear()
    else:
        _clear()
    persist_account_captcha_status(account, active=False)
    return was_captcha
