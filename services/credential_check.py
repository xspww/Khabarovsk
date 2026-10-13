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

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Tuple

import net_tls

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


def extract_security_cookie(set_cookies: List[str]) -> str:
    for entry in set_cookies or []:
        match = re.search(r"\.ROBLOSECURITY=([^;,\s]+)", str(entry or ""))
        if match and match.group(1).strip():
            return match.group(1).strip()
    return ""


def challenge_type_of(headers: Dict[str, str]) -> str:
    """Read the Roblox challenge type header ("" when none)."""
    try:
        for key, value in dict(headers or {}).items():
            if str(key or "").strip().lower() == "rblx-challenge-type":
                return str(value or "").strip()[:120]
    except Exception:
        pass
    return ""


def has_challenge_headers(headers: Dict[str, str]) -> bool:
    try:
        for key in dict(headers or {}):
            if str(key or "").strip().lower().startswith("rblx-challenge-"):
                return True
    except Exception:
        pass
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
        pass


def _header_names(headers: Dict[str, str]) -> str:
    try:
        return ",".join(sorted(str(k or "").strip().lower() for k in dict(headers or {}) if str(k or "").strip()))
    except Exception:
        return ""


def login_with_credentials(
    username: str,
    password: str,
    timeout: float = 12.0,
) -> Tuple[int, str, Dict[str, str], List[str]]:
    """Attempt a password login. Returns (status, body, headers, set_cookies).

    Never raises: network failures come back as status 0 (transient).
    The password is only sent to Roblox over TLS, never returned.
    """
    name = str(username or "").strip()
    secret = str(password or "")
    if not name or not secret:
        return 0, "missing username or password", {}, []
    payload = json.dumps({"ctype": "Username", "cvalue": name, "password": secret}).encode("utf-8")
    csrf_token = ""
    for attempt in (0, 1):
        headers = dict(BROWSER_HEADERS)
        if csrf_token:
            headers["X-CSRF-TOKEN"] = csrf_token
        req = urllib.request.Request(LOGIN_URL, data=payload, method="POST", headers=headers)
        try:
            with net_tls.urlopen(req, timeout=timeout) as resp:
                status = int(getattr(resp, "status", 200) or 200)
                try:
                    raw_headers = dict(resp.headers.items())
                except Exception:
                    raw_headers = {}
                try:
                    set_cookies = list(resp.headers.get_all("Set-Cookie") or [])
                except Exception:
                    single = raw_headers.get("Set-Cookie") or raw_headers.get("set-cookie") or ""
                    set_cookies = [single] if single else []
                try:
                    body = resp.read().decode("utf-8", errors="replace")
                except Exception:
                    body = ""
                return status, body, {str(k): str(v) for k, v in raw_headers.items()}, [str(c) for c in set_cookies]
        except urllib.error.HTTPError as exc:
            try:
                response_headers = dict(exc.headers.items())
            except Exception:
                response_headers = {}
            token = response_headers.get("x-csrf-token") or response_headers.get("X-CSRF-TOKEN")
            if token and attempt == 0:
                csrf_token = str(token)
                continue
            try:
                body_text = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body_text = ""
            return int(exc.code or 0), body_text, {str(k): str(v) for k, v in response_headers.items()}, []
        except Exception as exc:
            return 0, f"login transient: {exc}", {}, []
    return 0, "login transient: csrf retry exhausted", {}, []


def check_credentials(
    username: str,
    password: str,
    timeout: float = 12.0,
) -> Dict[str, Any]:
    """Classify what an account needs via a real login attempt.

    Returns {"outcome": ..., "detail": ..., "fresh_cookie": ...}.
    `fresh_cookie` is set only on a fully successful login.
    """
    name = str(username or "").strip()
    if not name or not str(password or ""):
        return {"outcome": UNKNOWN, "detail": "missing username or password", "fresh_cookie": ""}
    status, body, headers, set_cookies = login_with_credentials(name, password, timeout=timeout)
    body_text = str(body or "")
    lowered = body_text.lower()

    if status == 0 or status == 429 or status >= 500:
        return {"outcome": TRANSIENT, "detail": (body_text.strip()[:180] or f"HTTP {status}").strip(), "fresh_cookie": ""}

    try:
        from services.captcha_guard import captcha_detail as _captcha_detail
        from services.captcha_guard import is_captcha_text as _is_captcha
    except Exception:
        _captcha_detail = lambda *a, **k: ""  # type: ignore
        _is_captcha = lambda *a, **k: False  # type: ignore

    # Roblox challenge headers (captcha / device verification, ...).
    challenge = ""
    try:
        challenge = _captcha_detail(status, body_text, headers)
    except Exception:
        challenge = ""
    # An account-level lock screen ("Account Locked ... verify you're
    # human") outranks the captcha mechanism used to unlock it — this
    # matches the Status priority (Locked > Captcha) everywhere else.
    _lock_markers = (
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
    if any(marker in lowered for marker in _lock_markers):
        return {"outcome": LOCKED, "detail": body_text.strip()[:220] or "account locked", "fresh_cookie": ""}
    if challenge and _is_captcha(body_text, headers=headers):
        return {"outcome": CAPTCHA, "detail": challenge[:220], "fresh_cookie": ""}
    # A non-captcha challenge type names what Roblox wants next. Classify it
    # by its own wording instead of falling through to unknown.
    challenge_type = challenge_type_of(headers)
    challenge_headers = has_challenge_headers(headers)
    if challenge_headers and not _is_captcha(body_text, headers=headers):
        lowered_type = str(challenge_type or "").lower()
        if "captcha" in lowered_type or "arkose" in lowered_type or "funcaptcha" in lowered_type:
            return {"outcome": CAPTCHA, "detail": (challenge or f"Roblox challenge required ({challenge_type})")[:220], "fresh_cookie": ""}
        if any(k in lowered_type for k in ("identity", "face", "selfie", "id-verify", "idverify")):
            return {"outcome": LOCKED, "detail": f"Roblox challenge required ({challenge_type})", "fresh_cookie": ""}
        if any(k in lowered_type for k in ("passkey", "webauthn", "twostep", "two-step", "two step", "2fa", "authenticator", "verification")):
            return {"outcome": RECOVERY, "detail": f"Roblox challenge required ({challenge_type or 'verification'})", "fresh_cookie": ""}
        if challenge_type:
            _log_unknown_login(name, status, challenge_type, body_text.strip()[:180], _header_names(headers))
            return {"outcome": UNKNOWN, "detail": f"Roblox challenge required ({challenge_type})", "fresh_cookie": "", "challenge_type": challenge_type}

    if status == 200:
        if _looks_like_twostep(body_text):
            return {"outcome": RECOVERY, "detail": "login requires two-step verification", "fresh_cookie": ""}
        try:
            from services.verify_guard import is_locked_text as _is_locked
            from services.verify_guard import is_recovery_text as _is_recovery
        except Exception:
            _is_locked = lambda *a, **k: False  # type: ignore
            _is_recovery = lambda *a, **k: False  # type: ignore
        if _is_recovery(body_text):
            return {"outcome": RECOVERY, "detail": body_text.strip()[:220], "fresh_cookie": ""}
        if _is_locked(body_text):
            return {"outcome": LOCKED, "detail": body_text.strip()[:220], "fresh_cookie": ""}
        fresh = extract_security_cookie(set_cookies)
        if fresh:
            return {"outcome": VALID, "detail": "login ok", "fresh_cookie": fresh}
        _log_unknown_login(name, status, challenge_type_of(headers), body_text.strip()[:180], _header_names(headers))
        return {"outcome": UNKNOWN, "detail": (body_text.strip()[:180] or "login ok without session cookie"), "fresh_cookie": "", "challenge_type": challenge_type_of(headers)}

    # Error statuses: classify by explicit wording only (never guess). A ban
    # mark is only ever set with public-API confirmation — an unconfirmed
    # "deleted" message stays unknown so one flaky response can never mark
    # a healthy account as banned.
    try:
        from services.ban_guard import is_banned_text as _is_banned
    except Exception:
        _is_banned = lambda *a, **k: False  # type: ignore
    if _is_banned(body_text) or _looks_like_deleted(body_text):
        confirmed = False
        try:
            from services.roblox_hybrid import fetch_ban_status_by_username as _ban_by_name

            _b, _uid, _detail = _ban_by_name(name, timeout=timeout)
            confirmed = _b is True
        except Exception:
            confirmed = False
        if confirmed:
            return {"outcome": BANNED, "detail": "Account banned (Roblox isBanned=true)", "fresh_cookie": ""}
        _log_unknown_login(name, status, challenge_type_of(headers), body_text.strip()[:180], _header_names(headers))
        return {"outcome": UNKNOWN, "detail": (body_text.strip()[:180] or f"HTTP {status}").strip(), "fresh_cookie": "", "challenge_type": challenge_type_of(headers)}

    if _looks_like_wrong_password(body_text):
        return {"outcome": BAD_CREDENTIALS, "detail": "incorrect username or password", "fresh_cookie": ""}

    try:
        from services.verify_guard import is_locked_text as _is_locked2
        from services.verify_guard import is_recovery_text as _is_recovery2
    except Exception:
        _is_locked2 = lambda *a, **k: False  # type: ignore
        _is_recovery2 = lambda *a, **k: False  # type: ignore
    if _is_locked2(body_text):
        return {"outcome": LOCKED, "detail": body_text.strip()[:220], "fresh_cookie": ""}
    if _is_recovery2(body_text) or _looks_like_twostep(body_text):
        return {"outcome": RECOVERY, "detail": body_text.strip()[:220] or "login requires verification", "fresh_cookie": ""}
    if _is_captcha(body_text, headers=headers):
        return {"outcome": CAPTCHA, "detail": (challenge or body_text.strip())[:220], "fresh_cookie": ""}

    _log_unknown_login(name, status, challenge_type_of(headers), body_text.strip()[:180], _header_names(headers))
    return {"outcome": UNKNOWN, "detail": (body_text.strip()[:180] or f"HTTP {status}").strip(), "fresh_cookie": "", "challenge_type": challenge_type_of(headers)}


def paced_check_credentials(username: str, password: str, timeout: float = 12.0, pace_seconds: float = 1.0) -> Dict[str, Any]:
    """check_credentials with a small delay to avoid hammering Roblox."""
    try:
        if pace_seconds > 0:
            time.sleep(max(0.0, float(pace_seconds)))
    except Exception:
        pass
    return check_credentials(username, password, timeout=timeout)
