"""Real credential checking via the Roblox Auth v2 login API.

Import lines carry ``user:pass:cookie``. When the pasted cookie is dead,
the cookie alone cannot tell a Locked / Recovery / Captcha account apart
from a plain expired cookie — so this module asks Roblox directly with
the username + password from the same line and classifies what the
account actually needs:

- valid            -> login OK, fresh .ROBLOSECURITY captured
- bad_credentials  -> wrong username / password
- banned           -> deleted / terminated (confirmed via public API)
- locked           -> face / ID verification required
- recovery         -> passkey / 2FA / two-step verification required
- captcha          -> Arkose / captcha challenge required
- unknown          -> unrecognized response (caller keeps old flow)
- transient        -> rate-limited / network error (caller keeps old flow)

The password is used transiently for the single login attempt and is
NEVER logged, audited, or persisted. Every log/audit call in this file
carries the username only.
"""
from __future__ import annotations

import importlib
import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, NamedTuple

from infra import net_tls

LOGIN_URL = "https://auth.roblox.com/v2/login"

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Accept": "application/json",
    "Referer": "https://www.roblox.com/",
    "Content-Type": "application/json",
}

VALID = "valid"
BAD_CREDENTIALS = "bad_credentials"
BANNED = "banned"
LOCKED = "locked"
RECOVERY = "recovery"
CAPTCHA = "captcha"
UNKNOWN = "unknown"
TRANSIENT = "transient"

WRONG_PASSWORD_PHRASES = (
    "incorrect username or password",
    "incorrect password",
    "wrong password",
    "invalid password",
    "invalid username or password",
)

DELETED_PHRASES = (
    "account has been deleted",
    "account is deleted",
    "account deleted",
    "user does not exist",
    "username doesn't exist",
    "username does not exist",
)

TWOSTEP_MARKERS = (
    "twostepverification",
    "two-stepverification",
    "two-step verification",
    "two step verification",
    "2-step verification",
    "2 step verification",
    "two-factor",
    "two factor",
)

LOCK_MARKERS = (
    "account locked",
    "account is locked",
    "your account has been locked",
    "temporarily locked",
    "locked for verification",
    "บัญชีถูกล็อค",
    "บัญชีติดล็อค",
    "บัญชีถูกล็อก",
    "บัญชีติดล็อก",
)


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _looks_like_wrong_password(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in WRONG_PASSWORD_PHRASES)


def _looks_like_deleted(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in DELETED_PHRASES)


def _looks_like_twostep(text: str) -> bool:
    lowered = text.lower()
    if any(marker in lowered for marker in TWOSTEP_MARKERS):
        return True
    compacted = _compact(text)
    return "twostepverification" in compacted


def extract_security_cookie(set_cookies: list[str]) -> str:
    for entry in set_cookies or []:
        match = re.search(r"\.ROBLOSECURITY=([^;,\s]+)", str(entry or ""))
        if match and match.group(1).strip():
            return match.group(1).strip()
    return ""


def challenge_type_of(headers: dict[str, str]) -> str:
    """Read the Roblox challenge type header ("" when none)."""
    try:
        for key, value in dict(headers or {}).items():
            if str(key or "").strip().lower() == "rblx-challenge-type":
                return str(value or "").strip()[:120]
    except Exception:
        "intentional: ignore exception"
    return ""


def has_challenge_headers(headers: dict[str, str]) -> bool:
    try:
        for key in dict(headers or {}):
            if str(key or "").strip().lower().startswith("rblx-challenge-"):
                return True
    except Exception:
        "intentional: ignore exception"
    return False


def _log_unknown_login(username: str, status: int, challenge_type: str, body_head: str, header_keys: str = "") -> None:
    # Debug aid for unrecognized login responses. Carries the username,
    # HTTP status, challenge type, header names and Roblox's message only.
    # Never any credential.
    try:
        from core import flog_kv as _flog

        _flog(
            "ACCOUNT_DATA",
            "credential_check_unknown",
            account=str(username or ""),
            http_status=int(status or 0),
            challenge_type=str(challenge_type or ""),
            header_keys=str(header_keys or "")[:200],
            detail=str(body_head or "")[:180],
        )
    except Exception:
        "intentional: ignore exception"


def _header_names(headers: dict[str, str]) -> str:
    try:
        return ",".join(sorted(str(k or "").strip().lower() for k in dict(headers or {}) if str(k or "").strip()))
    except Exception:
        return ""


class _LoginAttempt(NamedTuple):
    status: int
    body: str
    headers: dict[str, str]
    set_cookies: list[str]


def _login_credentials(
    username: Any,
    password: Any,
) -> tuple[str, str] | None:
    name = str(username or "").strip()
    secret = str(password or "")
    if not name or not secret:
        return None
    return name, secret


def _login_headers(csrf_token: str) -> dict[str, str]:
    headers = dict(BROWSER_HEADERS)
    if csrf_token:
        headers["X-CSRF-TOKEN"] = csrf_token
    return headers


def _login_request(
    payload: bytes,
    headers: dict[str, str],
) -> urllib.request.Request:
    return urllib.request.Request(
        LOGIN_URL,
        data=payload,
        method="POST",
        headers=headers,
    )


def _response_headers(response: Any) -> dict[str, str]:
    try:
        raw_headers = dict(response.headers.items())
    except Exception:
        raw_headers = {}
    return {str(k): str(v) for k, v in raw_headers.items()}


def _response_status(response: Any) -> int:
    try:
        return int(getattr(response, "status", 200) or 200)
    except Exception:
        return 200


def _response_body(response: Any) -> str:
    try:
        return response.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _response_set_cookies(
    response: Any,
    raw_headers: dict[str, str],
) -> list[str]:
    try:
        return [
            str(cookie)
            for cookie in (response.headers.get_all("Set-Cookie") or [])
        ]
    except Exception:
        single = (
            raw_headers.get("Set-Cookie")
            or raw_headers.get("set-cookie")
            or ""
        )
        return [str(single)] if single else []


def _login_success(response: Any) -> _LoginAttempt:
    status = _response_status(response)
    raw_headers = _response_headers(response)
    set_cookies = _response_set_cookies(response, raw_headers)
    body = _response_body(response)
    return _LoginAttempt(
        status=status,
        body=body,
        headers=raw_headers,
        set_cookies=set_cookies,
    )


def _login_retry_token(exc: Any) -> str:
    headers = _response_headers(exc)
    return str(
        headers.get("x-csrf-token")
        or headers.get("X-CSRF-TOKEN")
        or ""
    )


def _http_error_response(exc: Any) -> _LoginAttempt:
    headers = _response_headers(exc)
    body = _response_body(exc)
    return _LoginAttempt(
        status=int(exc.code or 0),
        body=body,
        headers=headers,
        set_cookies=[],
    )


def _login_result(
    attempt: _LoginAttempt,
) -> tuple[int, str, dict[str, str], list[str]]:
    return (
        attempt.status,
        attempt.body,
        attempt.headers,
        attempt.set_cookies,
    )


def login_with_credentials(
    username: str,
    password: str,
    timeout: float = 12.0,
) -> tuple[int, str, dict[str, str], list[str]]:
    """Attempt a password login. Returns (status, body, headers, set_cookies).

    Never raises: network failures come back as status 0 (transient).
    The password is only sent to Roblox over TLS, never returned.
    """
    credentials = _login_credentials(username, password)
    if credentials is None:
        return 0, "missing username or password", {}, []
    name, secret = credentials
    payload = json.dumps(
        {
            "ctype": "Username",
            "cvalue": name,
            "password": secret,
        }
    ).encode("utf-8")
    csrf_token = ""
    for attempt in (0, 1):
        headers = _login_headers(csrf_token)
        request = _login_request(payload, headers)
        try:
            with net_tls.urlopen(request, timeout=timeout) as response:
                return _login_result(_login_success(response))
        except urllib.error.HTTPError as exc:
            token = _login_retry_token(exc)
            if token and attempt == 0:
                csrf_token = token
                continue
            return _login_result(_http_error_response(exc))
        except Exception as exc:
            return 0, f"login transient: {exc}", {}, []
    return 0, "login transient: csrf retry exhausted", {}, []


class _CheckContext(NamedTuple):
    name: str
    status: int
    body_text: str
    headers: dict[str, str]
    challenge: str
    is_captcha: Any
    timeout: float
    set_cookies: list[str]


def _import_guards(
    module: str,
    names: tuple[str, ...],
    fallbacks: tuple[Any, ...],
) -> tuple[Any, ...]:
    try:
        imported = importlib.import_module(module)
        return tuple(getattr(imported, name) for name in names)
    except Exception:
        return fallbacks


def _captcha_guards() -> tuple[Any, ...]:
    return _import_guards(
        "services.captcha_guard",
        ("captcha_detail", "is_captcha_text"),
        (lambda *a, **k: "", lambda *a, **k: False),
    )


def _verify_guards() -> tuple[Any, ...]:
    return _import_guards(
        "services.verify_guard",
        ("is_locked_text", "is_recovery_text"),
        (lambda *a, **k: False, lambda *a, **k: False),
    )


def _ban_guard() -> Any:
    return _import_guards(
        "services.ban_guard",
        ("is_banned_text",),
        (lambda *a, **k: False,),
    )[0]


def _outcome(
    outcome: str,
    detail: str,
    fresh_cookie: str = "",
    challenge_type: str | None = None,
) -> dict[str, Any]:
    result = {
        "outcome": outcome,
        "detail": detail,
        "fresh_cookie": fresh_cookie,
    }
    if challenge_type is not None:
        result["challenge_type"] = challenge_type
    return result


def _transient_result(
    status: int,
    body_text: str,
) -> dict[str, Any] | None:
    if status == 0 or status == 429 or status >= 500:
        detail = (
            body_text.strip()[:180] or f"HTTP {status}"
        ).strip()
        return _outcome(TRANSIENT, detail)
    return None


def _locked_result(
    lowered: str,
    body_text: str,
) -> dict[str, Any] | None:
    if any(marker in lowered for marker in LOCK_MARKERS):
        return _outcome(
            LOCKED,
            body_text.strip()[:220] or "account locked",
        )
    return None


def _safe_captcha_detail(
    captcha_detail: Any,
    status: int,
    body_text: str,
    headers: dict[str, str],
) -> str:
    try:
        return captcha_detail(status, body_text, headers)
    except Exception:
        return ""


def _challenge_kind(lowered_type: str) -> str:
    if (
        "captcha" in lowered_type
        or "arkose" in lowered_type
        or "funcaptcha" in lowered_type
    ):
        return CAPTCHA
    if any(
        key in lowered_type
        for key in ("identity", "face", "selfie", "id-verify", "idverify")
    ):
        return LOCKED
    if any(
        key in lowered_type
        for key in (
            "passkey",
            "webauthn",
            "twostep",
            "two-step",
            "two step",
            "2fa",
            "authenticator",
            "verification",
        )
    ):
        return RECOVERY
    return ""


def _challenge_outcome(
    ctx: _CheckContext,
) -> dict[str, Any] | None:
    challenge_type = challenge_type_of(ctx.headers)
    if not has_challenge_headers(ctx.headers) or ctx.is_captcha(
        ctx.body_text,
        headers=ctx.headers,
    ):
        return None
    kind = _challenge_kind(str(challenge_type or "").lower())
    if kind == CAPTCHA:
        detail = (
            ctx.challenge
            or f"Roblox challenge required ({challenge_type})"
        )[:220]
        return _outcome(CAPTCHA, detail)
    if kind == LOCKED:
        return _outcome(
            LOCKED,
            f"Roblox challenge required ({challenge_type})",
        )
    if kind == RECOVERY:
        return _outcome(
            RECOVERY,
            "Roblox challenge required "
            f"({challenge_type or 'verification'})",
        )
    if challenge_type:
        _log_unknown_login(
            ctx.name,
            ctx.status,
            challenge_type,
            ctx.body_text.strip()[:180],
            _header_names(ctx.headers),
        )
        return _outcome(
            UNKNOWN,
            f"Roblox challenge required ({challenge_type})",
            challenge_type=challenge_type,
        )
    return None


def _unknown_response(
    ctx: _CheckContext,
    fallback: str,
) -> dict[str, Any]:
    challenge_type = challenge_type_of(ctx.headers)
    _log_unknown_login(
        ctx.name,
        ctx.status,
        challenge_type,
        ctx.body_text.strip()[:180],
        _header_names(ctx.headers),
    )
    detail = (ctx.body_text.strip()[:180] or fallback).strip()
    return _outcome(
        UNKNOWN,
        detail,
        challenge_type=challenge_type,
    )


def _confirm_ban_by_username(name: str, timeout: float) -> bool:
    fetch_status = _import_guards(
        "services.roblox_moderation",
        ("fetch_ban_status_by_username",),
        (None,),
    )[0]
    if fetch_status is None:
        return False
    try:
        confirmed, _user_id, _detail = fetch_status(
            name,
            timeout=timeout,
        )
        return confirmed is True
    except Exception:
        return False


def _recovery_detail(body_text: str, fallback: str) -> str:
    return body_text.strip()[:220] or fallback


def _classify_ok(ctx: _CheckContext) -> dict[str, Any]:
    if _looks_like_twostep(ctx.body_text):
        return _outcome(
            RECOVERY,
            "login requires two-step verification",
        )
    is_locked, is_recovery = _verify_guards()
    if is_recovery(ctx.body_text):
        return _outcome(RECOVERY, ctx.body_text.strip()[:220])
    if is_locked(ctx.body_text):
        return _outcome(LOCKED, ctx.body_text.strip()[:220])
    fresh = extract_security_cookie(ctx.set_cookies)
    if fresh:
        return _outcome(VALID, "login ok", fresh_cookie=fresh)
    return _unknown_response(
        ctx,
        "login ok without session cookie",
    )


def _classify_error(ctx: _CheckContext) -> dict[str, Any]:
    if _ban_guard()(ctx.body_text) or _looks_like_deleted(
        ctx.body_text
    ):
        if _confirm_ban_by_username(ctx.name, ctx.timeout):
            return _outcome(
                BANNED,
                "Account banned (Roblox isBanned=true)",
            )
        return _unknown_response(ctx, f"HTTP {ctx.status}")
    if _looks_like_wrong_password(ctx.body_text):
        return _outcome(
            BAD_CREDENTIALS,
            "incorrect username or password",
        )
    is_locked, is_recovery = _verify_guards()
    if is_locked(ctx.body_text):
        return _outcome(LOCKED, ctx.body_text.strip()[:220])
    if is_recovery(ctx.body_text) or _looks_like_twostep(
        ctx.body_text
    ):
        return _outcome(
            RECOVERY,
            _recovery_detail(
                ctx.body_text,
                "login requires verification",
            ),
        )
    if ctx.is_captcha(ctx.body_text, headers=ctx.headers):
        detail = (ctx.challenge or ctx.body_text.strip())[:220]
        return _outcome(CAPTCHA, detail)
    return _unknown_response(ctx, f"HTTP {ctx.status}")


def check_credentials(
    username: str,
    password: str,
    timeout: float = 12.0,
) -> dict[str, Any]:
    """Classify what an account needs via a real login attempt.

    Returns {"outcome": ..., "detail": ..., "fresh_cookie": ...}.
    `fresh_cookie` is set only on a fully successful login.
    """
    credentials = _login_credentials(username, password)
    if credentials is None:
        return _outcome(
            UNKNOWN,
            "missing username or password",
        )
    name, secret = credentials
    status, body, headers, set_cookies = login_with_credentials(
        name,
        secret,
        timeout=timeout,
    )
    body_text = str(body or "")
    lowered = body_text.lower()
    transient = _transient_result(status, body_text)
    if transient is not None:
        return transient

    captcha_detail, is_captcha = _captcha_guards()
    challenge = _safe_captcha_detail(
        captcha_detail,
        status,
        body_text,
        headers,
    )
    ctx = _CheckContext(
        name=name,
        status=status,
        body_text=body_text,
        headers=headers,
        challenge=challenge,
        is_captcha=is_captcha,
        timeout=timeout,
        set_cookies=set_cookies,
    )
    locked = _locked_result(lowered, body_text)
    if locked is not None:
        return locked
    if challenge and is_captcha(body_text, headers=headers):
        return _outcome(CAPTCHA, challenge[:220])
    challenge_result = _challenge_outcome(ctx)
    if challenge_result is not None:
        return challenge_result
    if status == 200:
        return _classify_ok(ctx)
    return _classify_error(ctx)


def paced_check_credentials(username: str, password: str, timeout: float = 12.0, pace_seconds: float = 1.0) -> dict[str, Any]:
    """check_credentials with a small delay to avoid hammering Roblox."""
    try:
        if pace_seconds > 0:
            time.sleep(max(0.0, float(pace_seconds)))
    except Exception:
        "intentional: ignore exception"
    return check_credentials(username, password, timeout=timeout)
