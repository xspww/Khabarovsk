"""Live moderation / ban lookups against Roblox endpoints."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

from infra import net_tls
from services.roblox_http import MODERATION_HEADERS, PUBLIC_HEADERS, USERMODERATION_BASE, USERS_BASE


def _fetch_moderation_raw(req: urllib.request.Request, timeout: float) -> tuple[str, str]:
    try:
        with net_tls.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return "ok", raw
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            # Cookie revoked/expired. Says nothing about moderation state.
            return "error", "moderation status unavailable (cookie not valid)"
        if exc.code == 429 or exc.code >= 500:
            return "error", f"moderation status transient (HTTP {exc.code})"
        return "error", f"moderation status HTTP {exc.code}"
    except Exception as exc:
        return "error", f"moderation status transient: {exc}"


def _moderation_payload(raw: str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def fetch_moderation_status(cookie: str, timeout: float = 8.0) -> tuple[bool | None, str, dict[str, Any]]:
    """Read Roblox account moderation (suspension / ban) for a live cookie.

    The public profile's isBanned only reports permanent terminations. A
    temporary suspension leaves isBanned=false, so it is invisible there.
    This endpoint is the only place Roblox exposes the active moderation
    action for the signed-in account, including its type and end date.

    Returns (is_moderated_or_None, detail, payload). None means the status
    could not be read â€” either the cookie is no longer valid (401, Roblox
    has already revoked it) or the request failed. A revoked cookie is NOT
    evidence of a ban, so callers must never treat None as banned.
    """
    token = str(cookie or "").strip()
    if not token:
        return None, "", {}
    url = f"{USERMODERATION_BASE}v1/not-approved"
    req = urllib.request.Request(
        url,
        headers={**MODERATION_HEADERS, "Cookie": f".ROBLOSECURITY={token};"},
    )
    kind, value = _fetch_moderation_raw(req, timeout)
    if kind == "error":
        return None, value, {}
    data = _moderation_payload(value)
    if data is None:
        return None, "moderation status parse failed", {}
    # An active intervention is the only marker; an empty object means clean.
    if data.get("interventionId") in (None, ""):
        return False, "", {}
    return True, _describe_moderation(data), data


def _describe_moderation(data: dict[str, Any]) -> str:
    """Summarize a not-approved payload into a short human reason."""
    kind = ""
    try:
        utterances = data.get("badUtterances")
        if isinstance(utterances, list) and utterances:
            first = utterances[0]
            if isinstance(first, dict):
                kind = str(first.get("utteranceText") or "").strip()
    except Exception:
        kind = ""
    end_raw = str(data.get("endDate") or "").strip()
    if end_raw:
        try:
            end = datetime.strptime(end_raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
            days = max(0, (end - datetime.now(UTC)).days)
            until = f" until {end:%Y-%m-%d} (~{days}d)"
        except Exception:
            until = ""
    else:
        until = " (no end date)"
    if kind:
        return f"Account suspended by Roblox: {kind}{until}"
    return f"Account suspended by Roblox{until}"


def _parse_public_payload(raw: str) -> tuple[str, Any]:
    try:
        data = json.loads(raw)
    except Exception:
        return "transient", "ban check parse failed"
    if isinstance(data, dict) and bool(data.get("isBanned", False)):
        return "ok", (True, "Account banned (Roblox isBanned=true)")
    return "ok", (False, "")


def _fetch_public_once(url: str, timeout: float) -> tuple[str, Any]:
    """Returns (kind, payload): kind is ok|http_error|transient."""
    try:
        req = urllib.request.Request(url, headers=dict(PUBLIC_HEADERS))
        with net_tls.urlopen(req, timeout=timeout) as resp:
            return _parse_public_payload(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return "http_error", (int(exc.code or 0), body)
    except Exception as exc:
        return "transient", f"ban check transient: {exc}"


def _ban_http_result(code: int, body: str) -> tuple[bool | None, str]:
    lowered = f"{code} {body}".lower()
    if code == 429 or code >= 500:
        return None, f"ban check transient (HTTP {code})"
    if "banned" in lowered or "terminat" in lowered:
        return True, f"Account banned ({body[:120].strip() or code})"
    return None, f"ban check HTTP {code}"


def _retry_404_result(fetch_once: Any) -> tuple[bool | None, str]:
    try:
        time.sleep(2.0)
    except Exception:
        "intentional: ignore exception"
    kind2, payload2 = fetch_once()
    if kind2 == "ok":
        return payload2
    if kind2 == "transient":
        return None, str(payload2)
    code2, body2 = payload2
    if code2 == 404:
        return True, "Account not found (deleted/terminated)"
    return _ban_http_result(code2, body2)


def fetch_user_ban_status(user_id: str, timeout: float = 8.0, trusted_id: bool = True) -> tuple[bool | None, str]:
    """Check Roblox account-level ban via public profile (isBanned).

    Returns (is_banned_or_None, detail). None = unknown/transient, caller
    must NOT mark banned on unknown to avoid false positives.

    trusted_id marks the user_id as coming from a real identity source
    (an authenticated cookie or a username lookup). Only then does a 404
    mean the account is gone. Roblox does not hand out every user_id, so a
    404 on an unverified id just means "no public profile" and must stay
    unknown instead of banning a healthy account.
    """
    uid = str(user_id or "").strip()
    if not uid.isdigit():
        return None, ""
    url = f"{USERS_BASE}v1/users/{uid}"
    kind, payload = _fetch_public_once(url, timeout)
    if kind == "ok":
        return payload
    if kind == "transient":
        return None, str(payload)
    code, body = payload
    if code == 404:
        if not trusted_id:
            return None, "ban check: no public profile for this id"
        # A numeric ID that just resolved via the username API 404ing once
        # is usually replication lag / rate-limit, not a deletion. Retry
        # once before concluding the account is gone so one flaky 404 can
        # never falsely mark a healthy account as banned.
        return _retry_404_result(lambda: _fetch_public_once(url, timeout))
    return _ban_http_result(code, body)


def _lookup_items(data: Any) -> list[Any]:
    items = data.get("data") if isinstance(data, dict) else []
    return items if isinstance(items, list) else []


def _match_user_item(items: list[Any], name: str) -> str:
    lowered = name.lower()
    for item in items:
        if not isinstance(item, dict):
            continue
        requested = str(item.get("requestedUsername") or "").lower()
        actual = str(item.get("name") or "").lower()
        if requested == lowered or actual == lowered:
            uid = str(item.get("id") or "").strip()
            if uid:
                return uid
    return ""


def fetch_user_id_by_username(username: str, timeout: float = 8.0) -> tuple[str, str]:
    """Resolve a Roblox user_id from a username via public API (no cookie).

    Returns (user_id_or_empty, detail). Empty user_id means unknown â€”
    either the user does not exist or the lookup failed transiently.
    """
    name = str(username or "").strip()
    if not name:
        return "", "empty username"
    body = json.dumps({"usernames": [name], "excludeBannedUsers": False}).encode("utf-8")
    req = urllib.request.Request(
        USERS_BASE + "v1/usernames/users",
        data=body,
        method="POST",
        headers={**PUBLIC_HEADERS, "Content-Type": "application/json"},
    )
    try:
        with net_tls.urlopen(req, timeout=timeout) as resp:
            try:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            except Exception:
                return "", "username lookup parse failed"
    except urllib.error.HTTPError as exc:
        if exc.code == 429 or exc.code >= 500:
            return "", f"username lookup transient (HTTP {exc.code})"
        return "", f"username lookup HTTP {exc.code}"
    except Exception as exc:
        return "", f"username lookup transient: {exc}"
    uid = _match_user_item(_lookup_items(data), name)
    if uid:
        return uid, ""
    return "", "user not found"


def fetch_ban_status_by_username(username: str, timeout: float = 8.0) -> tuple[bool | None, str, str]:
    """Check account-level ban by username (public API, no cookie needed).

    Returns (is_banned_or_None, user_id, detail). None = unknown (user not
    found or transient failure) â€” caller must NOT mark banned on unknown.
    """
    uid, detail = fetch_user_id_by_username(username, timeout=timeout)
    if not uid:
        return None, "", detail
    banned, ban_detail = fetch_user_ban_status(uid, timeout=timeout)
    return banned, uid, ban_detail
