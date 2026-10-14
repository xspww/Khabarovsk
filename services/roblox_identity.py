"""Offline cookie identity + dead-cookie disambiguation (no launching)."""
from __future__ import annotations

import base64
import re
import time

from services.account_hybrid import ACCOUNT_STORE
from services.roblox_http import USERS_BASE, RobloxHTTP
from services.roblox_moderation import fetch_ban_status_by_username


def cookie_embedded_identity(cookie: str) -> tuple[str, str]:
    """Read the (username, user_id) Roblox embeds inside the cookie token.

    `.ROBLOSECURITY` payloads carry protobuf fields `uname` and `uid`, so a
    cookie-only paste (no ``username:`` prefix) can still resolve its owner
    without network. Returns ("", "") when absent or unparseable.

    The identity is self-asserted by the token, so it is only ever used to
    *name* an account for display and public-API checks — never to prove a
    cookie is valid.
    """
    token = str(cookie or "").strip()
    if not token:
        return "", ""
    # The token separator is "|_" (pipe-underscore): "...items.|_CAEQ...".
    # A payload-only paste (no header) is used as-is.
    payload = token.rsplit("|_", 1)[-1].strip() if "|_" in token else token
    candidates = [payload]
    if payload.startswith("_"):
        # Tolerate a separator variant that leaves a stray leading "_".
        candidates.append(payload[1:])
    for candidate in candidates:
        if len(candidate) < 32 or not re.fullmatch(r"[A-Za-z0-9+/=_-]+", candidate):
            continue
        try:
            raw = base64.urlsafe_b64decode(candidate + "=" * (-len(candidate) % 4))
        except Exception:
            continue
        try:
            # `..` must span arbitrary tag/length bytes (0x0A is common), so
            # [\s\S] is used instead of `.` which skips newlines.
            name_match = re.search(rb"uname[\s\S]{2}([A-Za-z0-9_]{3,20})", raw)
            username = name_match.group(1).decode("ascii") if name_match else ""
        except Exception:
            username = ""
        try:
            # `duid` (device id) also contains "uid" — require a non-letter
            # before it so the device id is never mistaken for the user id.
            uid_match = re.search(rb"(?<![A-Za-z])uid[\s\S]{2}(\d{4,15})", raw)
            user_id = uid_match.group(1).decode("ascii") if uid_match else ""
        except Exception:
            user_id = ""
        if username and not re.fullmatch(r"[A-Za-z0-9_]{3,20}", username):
            username = ""
        if user_id and not user_id.isdigit():
            user_id = ""
        if username or user_id:
            return username, user_id
    return "", ""


def username_for_cookie_in_store(cookie: str) -> str:
    """Find the stored username that owns this cookie.

    Lets a cookie-only paste resolve an identity even when the line carries
    no "username:" prefix, so the ban check by name can still run.
    """
    token = str(cookie or "").strip()
    if not token:
        return ""
    try:
        for record in ACCOUNT_STORE.read_records(include_cookies=True):
            if str((record or {}).get("cookie") or "").strip() != token:
                continue
            name = str((record or {}).get("username") or "").strip()
            if name:
                return name
    except Exception:
        pass
    return ""


def resolve_ban_for_dead_cookie(cookie: str, username_hint: str = "", timeout: float = 8.0) -> tuple[bool | None, str, str]:
    """Check ban for a cookie that can no longer authenticate.

    The dead cookie cannot prove anything, so fall back to the public API by
    username. Returns (is_banned_or_None, username, user_id). None = unknown.
    """
    name = (
        str(username_hint or "").strip()
        or username_for_cookie_in_store(cookie)
        or cookie_embedded_identity(cookie)[0]
    )
    if not name:
        return None, "", ""
    banned, uid, _detail = fetch_ban_status_by_username(name, timeout=timeout)
    return banned, name, uid


def moderated_only_detail(detail: str) -> bool:
    """True when a failure detail claims moderation but names no explicit ban.

    A 403 "User is moderated" response is ambiguous on its own: it fits a
    banned account just as well as a verification-locked one. Callers must
    resolve it via resolve_moderated_dead_cookie instead of treating it as
    a ban outright.
    """
    t = str(detail or "").lower()
    if "moderat" not in t:
        return False
    if "suspend" in t:
        return False
    if "account banned" in t or "roblox ban" in t or "user is banned" in t:
        return False
    compact = t.replace(" ", "").replace("_", "").replace("-", "")
    if "isbanned" in compact or "terminat" in t:
        return False
    return True


def resolve_moderated_dead_cookie(
    cookie: str, username_hint: str = "", timeout: float = 8.0
) -> tuple[str | None, str, str, str]:
    """Disambiguate a dead cookie answered 403 "User is moderated".

    Returns (verdict, detail, username, user_id) with verdict in
    {"banned", "locked", None}. "locked" means the public profile proves
    the account is alive and not banned, so the moderation response comes
    from a non-ban hold (verification lock or similar). None = unknown.
    """
    name = (
        str(username_hint or "").strip()
        or username_for_cookie_in_store(cookie)
        or cookie_embedded_identity(cookie)[0]
    )
    if not name:
        return None, "", "", ""
    try:
        banned, uid, ban_detail = fetch_ban_status_by_username(name, timeout=timeout)
    except Exception:
        return None, "", name, ""
    if banned is True:
        return "banned", (ban_detail or "Account banned (Roblox isBanned=true)"), name, str(uid or "")
    if banned is False:
        try:
            from services.verify_guard import LOCKED_BLOCK_REASON as _LM
        except Exception:
            _LM = "Account locked - face/ID verification required. Verify manually, then click Resume."
        return "locked", _LM, name, str(uid or "")
    return None, "", name, str(uid or "")


def reprobe_moderation_response(cookie: str, attempts: int = 3, delay: float = 1.5, timeout: float = 8.0) -> tuple[int, str]:
    """Re-ask the authenticated endpoint a few times for a dead cookie.

    Roblox sometimes answers the same dead token with plain 401 and
    sometimes with 403 "User is moderated". Returns the first moderation
    indicating (status, body), else (0, ""). Only used when another signal
    (e.g. a gated password login) already suggests an account hold, so
    healthy expired cookies never pay the extra calls.
    """
    token = str(cookie or "").strip()
    if not token:
        return 0, ""
    tries = max(1, min(5, int(attempts or 3)))
    try:
        client = RobloxHTTP(token)
    except Exception:
        return 0, ""
    for _ in range(tries):
        try:
            status, body, _headers = client.request(USERS_BASE + "v1/users/authenticated", method="GET", retry_csrf=False)
        except Exception:
            status, body = 0, ""
        if int(status or 0) == 403 and "moderat" in str(body or "").lower() and "suspend" not in str(body or "").lower():
            return int(status), str(body or "")
        try:
            time.sleep(max(0.0, float(delay or 0.0)))
        except Exception:
            pass
    return 0, ""
