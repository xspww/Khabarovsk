from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import Iterable
from ctypes import wintypes
from typing import Any, NamedTuple

from infra.app_paths import APP_DATA_DIR, LOG_DIR, move_app_data_file

ACCOUNT_DATA_FILE = os.path.join(APP_DATA_DIR, "AccountData.json")
move_app_data_file("account_tools_audit.jsonl", os.path.join("logs", "account_tools_audit.jsonl"))
ACCOUNT_AUDIT_FILE = os.path.join(LOG_DIR, "account_tools_audit.jsonl")
PENDING_IMPORT_FILE = os.path.join(APP_DATA_DIR, "account_import_pending.json")

COOKIE_RE = re.compile(
    r"(_\|WARNING:-DO-NOT-SHARE-THIS\.[^\s'\"<>]+|_\|WARNING:[^\s'\"<>]+)",
    re.IGNORECASE,
)
LINK_CODE_RE = re.compile(r"((?:privateServerLinkCode|linkCode|accessCode)=)[^&\s]+", re.IGNORECASE)

CRONUS_ENTROPY = b"Cronus Hybrid AccountData v1"
COOKIE_UNREADABLE_REASON = "Saved cookie was encrypted on another PC or Windows profile - sign in again"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _make_blob(data: bytes) -> tuple[DATA_BLOB, Any]:
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def dpapi_protect(data: bytes, entropy: bytes = CRONUS_ENTROPY) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI is only available on Windows")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, in_buffer = _make_blob(data)
    entropy_blob, entropy_buffer = _make_blob(entropy or b"")
    out_blob = DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    keepalive = (in_buffer, entropy_buffer)
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)
        _ = keepalive


def dpapi_unprotect(data: bytes, entropy: bytes = CRONUS_ENTROPY) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI is only available on Windows")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, in_buffer = _make_blob(data)
    entropy_blob, entropy_buffer = _make_blob(entropy or b"")
    out_blob = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    keepalive = (in_buffer, entropy_buffer)
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)
        _ = keepalive


def encrypt_cookie(cookie: str) -> str:
    cookie = str(cookie or "").strip()
    if not cookie:
        return ""
    protected = dpapi_protect(cookie.encode("utf-8"), CRONUS_ENTROPY)
    return "dpapi:v1:" + base64.b64encode(protected).decode("ascii")


def decrypt_cookie(value: str, *, strict: bool = True) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if value.startswith("dpapi:v1:"):
        try:
            raw = base64.b64decode(value.split(":", 2)[2].encode("ascii"))
            return dpapi_unprotect(raw, CRONUS_ENTROPY).decode("utf-8", errors="replace")
        except (OSError, ValueError):
            # WinError 13: the blob was protected by another Windows user/profile
            # (AccountData.json copied between PCs, recreated profile). Non-strict
            # callers keep the record usable and ask for a fresh login; the blob
            # itself is preserved so returning to the original machine still works.
            if strict:
                raise
            return ""
    if value.startswith("_|WARNING:"):
        return value
    return ""


def redact_secret(value: Any) -> Any:
    text = str(value or "")
    text = COOKIE_RE.sub("[ROBLOX_COOKIE_REDACTED]", text)
    text = LINK_CODE_RE.sub(r"\1[REDACTED]", text)
    return text


def _short_hash(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _coerce_private_server_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    return value


def _server_text(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value:
            return str(value).strip()
    return ""


def _server_float(item: dict[str, Any], *keys: str) -> float:
    for key in keys:
        value = item.get(key)
        if value:
            return float(value)
    return 0.0


def _server_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "private_server_id": _server_text(
            item,
            "private_server_id",
            "privateServerId",
            "vipServerId",
            "id",
        ),
        "owner_user_id": _server_text(item, "owner_user_id", "ownerId", "ownerUserId"),
        "place_id": _server_text(item, "place_id", "placeId"),
        "universe_id": _server_text(item, "universe_id", "universeId"),
        "name": _server_text(item, "name"),
        "active": bool(item.get("active", True)),
        "link": _server_text(item, "link", "join_link", "joinLink"),
        "join_code": _server_text(item, "join_code", "joinCode", "privateServerLinkCode"),
        "access_code": _server_text(item, "access_code", "accessCode"),
        "source": _server_text(item, "source"),
        "status": _server_text(item, "status"),
        "error": _server_text(item, "error"),
        "synced_at": _server_float(item, "synced_at", "syncedAt"),
    }


def _server_has_signal(server: dict[str, Any]) -> bool:
    return bool(
        server["private_server_id"]
        or server["link"]
        or server["join_code"]
        or server["access_code"]
        or server["status"]
        or server["error"]
    )


def _normalize_owned_private_servers(value: Any) -> list[dict[str, Any]]:
    clean: list[dict[str, Any]] = []
    for item in _coerce_private_server_list(value):
        if not isinstance(item, dict):
            continue
        server = _server_row(item)
        if _server_has_signal(server):
            clean.append(server)
    return clean


def _public_owned_private_servers(value: Any) -> list[dict[str, Any]]:
    public: list[dict[str, Any]] = []
    for item in _normalize_owned_private_servers(value):
        link = str(item.get("link") or "")
        join_code = str(item.get("join_code") or "")
        access_code = str(item.get("access_code") or "")
        public.append(
            {
                "private_server_id": item.get("private_server_id", ""),
                "owner_user_id": item.get("owner_user_id", ""),
                "place_id": item.get("place_id", ""),
                "universe_id": item.get("universe_id", ""),
                "name": item.get("name", ""),
                "active": bool(item.get("active", False)),
                "source": item.get("source", ""),
                "status": item.get("status", ""),
                "error": item.get("error", ""),
                "synced_at": item.get("synced_at", 0.0),
                "link_present": bool(link),
                "join_code_present": bool(join_code),
                "access_code_present": bool(access_code),
                "link_hash": _short_hash(link),
                "join_code_hash": _short_hash(join_code),
                "access_code_hash": _short_hash(access_code),
            }
        )
    return public


def _redact_vip_links_for_api(value: Any) -> tuple[list[str], list[str]]:
    if isinstance(value, str):
        links = [line.strip() for line in value.splitlines() if line.strip()]
    elif isinstance(value, list):
        links = [str(link).strip() for link in value if str(link).strip()]
    else:
        links = []
    return [redact_secret(link) for link in links], [_short_hash(link) for link in links]


def _link_list(value: Any) -> list[str] | None:
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    if isinstance(value, list):
        return [str(link).strip() for link in value if str(link).strip()]
    return None


def _merge_links(new_links: list[str], old_links: list[str]) -> list[str]:
    merged: list[str] = []
    for idx, link in enumerate(new_links):
        if "[REDACTED]" in link and idx < len(old_links):
            merged.append(old_links[idx])
        elif "[REDACTED]" not in link:
            merged.append(link)
    return merged


def _merge_redacted_vip_links(new_value: Any, old_value: Any) -> Any:
    new_links = _link_list(new_value)
    if new_links is None:
        return new_value
    if not any("[REDACTED]" in link for link in new_links):
        return new_value
    old_links = _link_list(old_value) if isinstance(old_value, list) else []
    if not old_links:
        return [link for link in new_links if "[REDACTED]" not in link]
    merged = _merge_links(new_links, old_links)
    if not merged:
        return old_links
    return merged


def parse_credential_line(line: str) -> tuple[str, str, str]:
    """Split a ``user:pass:cookie`` (or cookie-only) line.

    Returns (username, password, cookie). The password is only present on
    pasted credential lines; it must be used transiently and never stored.
    """
    text = str(line or "").strip()
    if not text:
        return "", "", ""
    match = COOKIE_RE.search(text)
    if match:
        before = text[: match.start()].strip(": \t")
        if before and ":" in before:
            username, password = before.split(":", 1)
            return username.strip(), password.strip(), match.group(1).strip()
        username = before.split(":", 1)[0].strip() if before else ""
        return username, "", match.group(1).strip()
    parts = text.split(":")
    if len(parts) >= 3 and parts[2].strip().startswith("_|WARNING:"):
        return parts[0].strip(), parts[1].strip(), ":".join(parts[2:]).strip()
    if text.startswith("_|WARNING:"):
        return "", "", text
    return "", "", ""


_GATED_HINT = (
    " (login was gated by a Roblox challenge — if the account shows a "
    "lock / verify-you-are-human screen, unlock it in the browser first)"
)


def _resolve_ban_by_name(cookie: str, username: str) -> tuple[bool, str, str]:
    """Resolve a dead cookie's ban state through the public API.

    Returns (banned, resolved_name, user_id); the name is filled even when the
    account turns out to be alive, so the caller can keep it visible.
    """
    try:
        from services.roblox_identity import resolve_ban_for_dead_cookie as _resolve_dead

        banned, name, uid = _resolve_dead(cookie, username)
    except Exception:
        return False, "", ""
    resolved_name = str(name or "")
    if banned is True:
        return True, resolved_name, str(uid or "")
    return False, resolved_name, ""


def _locked_import_mark(
    cookie: str,
    username: str,
    detail: str,
    validated: str,
    cookie_username: str,
) -> tuple[str, str, str, str]:
    """Return (user, user_id, block_message, import_status) for a moderated lock.

    An empty user means the detail is not a confirmed lock.
    """
    try:
        from services.roblox_identity import moderated_only_detail as _mod_only
        from services.roblox_identity import resolve_moderated_dead_cookie as _resolve_mod
    except Exception:
        return "", "", "", ""
    try:
        from services.verify_guard import LOCKED_BLOCK_REASON as _locked_msg
        from services.verify_guard import LOCKED_REASON as _locked_status
    except Exception:
        _locked_msg = "Account locked - face/ID verification required. Verify manually, then click Resume."
        _locked_status = "identity_locked"
    try:
        if not bool(_mod_only(detail)):
            return "", "", "", ""
    except Exception:
        return "", "", "", ""
    try:
        mv, _md, mn, mu = _resolve_mod(cookie, username)
    except Exception:
        mv, mn, mu = None, "", ""
    if mv != "locked":
        return "", "", "", ""
    user = next(
        (str(value).strip() for value in (username, mn, validated, cookie_username) if value),
        "",
    )
    return user, (str(mu) if mu else ""), _locked_msg, _locked_status


def _apply_fresh_cookie(
    validator: Any,
    cc: dict[str, Any] | None,
    cookie: str,
    ok: bool,
    validated: str,
    detail: str,
    meta: dict[str, Any],
    username: str,
) -> dict[str, Any]:
    """Revalidate a refreshed cookie; echoes the input state when it fails."""
    state: dict[str, Any] = {
        "cookie": cookie,
        "ok": ok,
        "validated": validated,
        "detail": detail,
        "meta": meta,
        "refreshed": 0,
        "errors": [],
    }
    fresh = str((cc or {}).get("fresh_cookie") or "")
    if not fresh:
        return state
    try:
        re_ = validator(fresh)
        rok = bool(re_[0])
        rvalidated = str(re_[1] or "")
        rdetail = str(re_[2] or "")
        rmeta = re_[3] if len(re_) >= 4 and isinstance(re_[3], dict) else {}
    except Exception:
        return state
    if not rok:
        return state
    state.update(cookie=fresh, ok=rok, validated=rvalidated, detail=rdetail, meta=rmeta, refreshed=1)
    state["errors"].append(f"{username} ok - cookie refreshed via login")
    return state


def _banned_login_record(username: str, cookie: str, cookie_user_id: str) -> dict[str, Any]:
    """Imported record for a ban confirmed by the login probe."""
    try:
        from services.ban_guard import BANNED_BLOCK_REASON as _msg
        from services.ban_guard import BANNED_IMPORT_STATUS as _status
    except Exception:
        _msg = "Account banned (Roblox). Unmark to allow rejoin."
        _status = "banned"
    return {
        "username": username,
        "cookie": cookie,
        "cookie_username": username,
        "cookie_user_id": cookie_user_id,
        "cookie_mismatch": False,
        "manual_status": _msg,
        "import_status": _status,
    }


def _verify_login_mark(
    outcome: str, username: str, cookie: str, cookie_user_id: str
) -> tuple[dict[str, Any], str, tuple[int, int, int]]:
    """Imported record for a locked/recovery/captcha verdict from the login probe."""
    try:
        from services.captcha_guard import CAPTCHA_BLOCK_REASON as cap_msg
        from services.captcha_guard import CAPTCHA_REASON as cap_status
        from services.credential_check import LOCKED as locked_outcome
        from services.credential_check import RECOVERY as recovery_outcome
        from services.verify_guard import LOCKED_BLOCK_REASON as lock_msg
        from services.verify_guard import LOCKED_REASON as lock_status
        from services.verify_guard import RECOVERY_BLOCK_REASON as rec_msg
        from services.verify_guard import RECOVERY_REASON as rec_status
    except Exception:
        cap_msg = "CAPTCHA required. Solve it manually, then click Resume or Reload Cookies."
        cap_status = "captcha_required"
        lock_msg = "Account locked - face/ID verification required. Verify manually, then click Resume."
        lock_status = "identity_locked"
        rec_msg = "Account recovery - passkey/2FA required. Complete recovery manually, then click Resume."
        rec_status = "account_recovery"
        locked_outcome, recovery_outcome = "locked", "recovery"
    if outcome == locked_outcome:
        msg, status, word = lock_msg, lock_status, "locked"
        deltas = (1, 0, 0)
    elif outcome == recovery_outcome:
        msg, status, word = rec_msg, rec_status, "recovery"
        deltas = (0, 1, 0)
    else:
        msg, status, word = cap_msg, cap_status, "captcha"
        deltas = (0, 0, 1)
    record = {
        "username": username,
        "cookie": cookie,
        "cookie_username": username,
        "cookie_user_id": cookie_user_id,
        "cookie_mismatch": False,
        "manual_status": msg,
        "import_status": status,
    }
    return record, word, deltas


def _challenge_login_hold(
    cookie: str,
    username: str,
    cookie_user_id: str,
    records: list[dict[str, Any]],
    errors: list[str],
) -> tuple[bool, int]:
    """Classify a challenge-gated login, appending a lock record when confirmed.

    Returns (login_gated, locked_delta). A confirmed lock appends its record
    and message here and reports a 1 so the caller skips the shared tail.
    """
    try:
        from services.roblox_identity import reprobe_moderation_response as _reprobe
        from services.roblox_identity import resolve_moderated_dead_cookie as _resolve_re
    except Exception:
        return True, 0
    try:
        rs, _rb = _reprobe(cookie)
    except Exception:
        return True, 0
    if (int(rs) if rs else 0) != 403:
        return True, 0
    try:
        mv, md, mn, mu = _resolve_re(cookie, username)
    except Exception:
        return True, 0
    if mv != "locked":
        return True, 0
    raw_user = next((v for v in (username, mn) if v), "")
    lock_user = str(raw_user).strip()
    records.append(
        {
            "username": lock_user or username,
            "cookie": cookie,
            "cookie_username": str(raw_user),
            "cookie_user_id": next((str(v) for v in (mu, cookie_user_id) if v), ""),
            "cookie_mismatch": False,
            "manual_status": md,
            "import_status": "identity_locked",
        }
    )
    errors.append(f"{username} locked (moderation) - imported with Locked mark")
    return False, 1


def _record_pick(
    item: dict[str, Any],
    fields: dict[str, Any],
    *keys: str,
) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    for key in keys:
        value = fields.get(key)
        if value not in (None, ""):
            return value
    return ""


def _record_pick_bool(
    item: dict[str, Any],
    fields: dict[str, Any],
    *keys: str,
) -> bool:
    value = _record_pick(item, fields, *keys)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _record_group(item: dict[str, Any], fields: dict[str, Any]) -> str:
    return str(_record_pick(item, fields, "group", "Group") or "Default").strip() or "Default"


def _record_encrypted_cookie(item: dict[str, Any], cookie: str) -> str:
    encrypted = str(
        item.get("encrypted_cookie") or item.get("EncryptedCookie") or ""
    ).strip()
    if encrypted:
        return encrypted
    return encrypt_cookie(cookie) if cookie else ""


def _normalize_vip_links(item: dict[str, Any]) -> list[Any]:
    vip_links = item.get("vip_links", item.get("VipLinks", item.get("vipLinks", [])))
    if isinstance(vip_links, str):
        vip_links = [line.strip() for line in vip_links.splitlines() if line.strip()]
    if not isinstance(vip_links, list):
        vip_links = []
    return vip_links


def _normalize_browser_tracker(
    item: dict[str, Any],
    fields: dict[str, Any],
) -> str:
    tracker = str(
        _record_pick(
            item,
            fields,
            "browser_tracker_id",
            "BrowserTrackerID",
            "BrowserTrackerId",
            "browserTrackerId",
        )
    ).strip()
    if not tracker:
        tracker = str(secrets.randbelow(75_000) + 100_000) + str(
            secrets.randbelow(800_000) + 100_000
        )
    return tracker


def _record_float(item: dict[str, Any], key: str, alternate: str | None = None) -> float:
    return float(item.get(key) or (item.get(alternate) if alternate else None) or 0.0)


def _record_priority(item: dict[str, Any]) -> int:
    return int(float(item.get("priority") or item.get("Priority") or 50))


def _record_text_field(item: dict[str, Any], key: str) -> str:
    return str(item.get(key) or "").strip()


def _existing_by_username(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(record.get("username") or "").strip().lower(): record
        for record in records
        if str(record.get("username") or "").strip()
    }


def _carry_forward_old(data: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
    if "cookie" not in data and old:
        data["encrypted_cookie"] = old.get("encrypted_cookie", "")
    if not data.get("browser_tracker_id") and old:
        data["browser_tracker_id"] = old.get("browser_tracker_id", "")
    if not data.get("fields") and old:
        data["fields"] = old.get("fields", {})
    if old and "vip_links" in data:
        data["vip_links"] = _merge_redacted_vip_links(
            data.get("vip_links"),
            old.get("vip_links", []),
        )
    return data


class _ImportState:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.banned = 0
        self.locked = 0
        self.account_recovery = 0
        self.captcha = 0
        self.refreshed = 0
        self.cred_check_blocked = False


class _ImportLine:
    def __init__(self, username: str, password: str, cookie: str) -> None:
        self.username = username
        self.password = password
        self.cookie = cookie
        self.ok: Any = False
        self.validated: Any = ""
        self.detail: Any = ""
        self.meta: dict[str, Any] = {}
        self.cookie_username = ""
        self.cookie_user_id = ""
        self.cookie_mismatch = False
        self.is_banned = False
        self.login_gated = False
        self.skip_line = 0


class _BanMarks(NamedTuple):
    msg: str
    status: str
    suspended_msg: str
    suspended_status: str
    is_suspended: Any


class _CredentialVerdicts(NamedTuple):
    bad: str
    banned: str
    captcha: str
    locked: str
    recovery: str
    transient: str
    unknown: str
    valid: str


def _import_owner_cookie(cookie: str) -> str:
    try:
        from services.roblox_identity import username_for_cookie_in_store as _owner_for_cookie

        username = _owner_for_cookie(cookie)
    except Exception:
        username = ""
    if username:
        return username
    try:
        from services.roblox_identity import cookie_embedded_identity as _embedded_identity

        return _embedded_identity(cookie)[0]
    except Exception:
        return ""


def _parse_validation(validation: Any) -> tuple[Any, Any, Any, dict[str, Any]]:
    ok, validated, detail = validation[:3]
    meta = validation[3] if len(validation) >= 4 and isinstance(validation[3], dict) else {}
    return ok, validated, detail, meta


def _apply_validation_ids(line: _ImportLine) -> None:
    line.cookie_user_id = str(line.meta.get("user_id") or "")
    line.cookie_username = str(line.meta.get("username") or line.validated or "")


def _import_ban_flag(meta: dict[str, Any], detail: Any) -> bool:
    try:
        from services.ban_guard import is_banned_text as _is_banned_text

        return bool(meta.get("is_banned") or meta.get("banned")) or bool(
            _is_banned_text(detail)
        )
    except Exception:
        return bool(meta.get("is_banned") or meta.get("banned"))


def _import_suspended_flag(meta: dict[str, Any]) -> bool:
    return bool(meta.get("is_suspended") or meta.get("suspended"))


def _import_ban_marks() -> _BanMarks:
    try:
        from services.ban_guard import BANNED_BLOCK_REASON as msg
        from services.ban_guard import BANNED_IMPORT_STATUS as status
        from services.ban_guard import SUSPENDED_BLOCK_REASON as susp_msg
        from services.ban_guard import SUSPENDED_IMPORT_STATUS as susp_status
        from services.ban_guard import is_suspended_text as is_suspended
    except Exception:
        return _BanMarks(
            "Account banned (Roblox). Unmark to allow rejoin.",
            "banned",
            "Account suspended by Roblox. Unmark to allow rejoin.",
            "suspended",
            lambda *a, **k: False,
        )
    return _BanMarks(msg, status, susp_msg, susp_status, is_suspended)


def _verify_detail_mark(detail: Any) -> tuple[str, str, str] | None:
    try:
        from services.captcha_guard import CAPTCHA_BLOCK_REASON as cap_msg
        from services.captcha_guard import CAPTCHA_REASON as cap_status
        from services.captcha_guard import is_captcha_text as is_cap_text
        from services.verify_guard import LOCKED_BLOCK_REASON as lock_msg
        from services.verify_guard import LOCKED_REASON as lock_status
        from services.verify_guard import RECOVERY_BLOCK_REASON as rec_msg
        from services.verify_guard import RECOVERY_REASON as rec_status
        from services.verify_guard import is_locked_text as is_locked_text
        from services.verify_guard import is_recovery_text as is_rec_text
    except Exception:
        return None
    try:
        if is_locked_text(detail):
            return lock_msg, lock_status, "locked"
        if is_rec_text(detail):
            return rec_msg, rec_status, "recovery"
        if is_cap_text(detail):
            return cap_msg, cap_status, "captcha"
    except Exception:
        return None
    return None


def _handle_moderated_lock(state: _ImportState, line: _ImportLine) -> bool:
    lock_user, lock_uid, lock_msg, lock_status = _locked_import_mark(
        line.cookie,
        line.username,
        line.detail,
        line.validated,
        line.cookie_username,
    )
    if not lock_user:
        return False
    state.records.append(
        {
            "username": lock_user,
            "cookie": line.cookie,
            "cookie_username": line.cookie_username or lock_user,
            "cookie_user_id": str(lock_uid or line.cookie_user_id or ""),
            "cookie_mismatch": False,
            "manual_status": lock_msg,
            "import_status": lock_status,
        }
    )
    state.locked += 1
    state.errors.append(f"{lock_user} locked (moderation) - imported with Locked mark")
    return True


def _handle_ban_import(state: _ImportState, line: _ImportLine) -> None:
    if not line.username:
        line.username = str(line.validated or line.cookie_username or "")
    if not line.username:
        state.errors.append("banned cookie missing username")
        return
    marks = _import_ban_marks()
    suspended = _import_suspended_flag(line.meta) or bool(marks.is_suspended(line.detail))
    mark_msg = marks.suspended_msg if suspended else marks.msg
    mark_status = marks.suspended_status if suspended else marks.status
    word = "suspended" if suspended else "banned"
    if line.cookie_username and line.username.strip().lower() != line.cookie_username.strip().lower():
        state.errors.append(
            f"{line.username} {word} — cookie belongs to {line.cookie_username}, "
            f"imported with {word.capitalize()} mark"
        )
    else:
        state.errors.append(f"{line.username} {word} — imported with {word.capitalize()} mark")
    state.records.append(
        {
            "username": line.username,
            "cookie": line.cookie,
            "cookie_username": line.cookie_username or line.username,
            "cookie_user_id": line.cookie_user_id,
            "cookie_mismatch": False,
            "manual_status": mark_msg,
            "import_status": mark_status,
        }
    )
    state.banned += 1


def _handle_verify_mark(state: _ImportState, line: _ImportLine) -> bool:
    mark = _verify_detail_mark(line.detail)
    if mark is None:
        return False
    msg, status, word = mark
    user = str(line.username or line.cookie_username or line.validated or "").strip()
    if not user:
        state.errors.append(f"{word} cookie missing username")
        return True
    state.records.append(
        {
            "username": user,
            "cookie": line.cookie,
            "cookie_username": line.cookie_username or user,
            "cookie_user_id": line.cookie_user_id,
            "cookie_mismatch": False,
            "manual_status": msg,
            "import_status": status,
        }
    )
    state.errors.append(f"{user} {word} — imported with {word.capitalize()} mark")
    return True


def _should_probe_credentials(line: _ImportLine, state: _ImportState) -> bool:
    return bool(
        not line.ok
        and not line.is_banned
        and line.username
        and line.password
        and not state.cred_check_blocked
    )


def _credential_verdicts() -> _CredentialVerdicts:
    from services.credential_check import (
        BAD_CREDENTIALS,
        BANNED,
        CAPTCHA,
        LOCKED,
        RECOVERY,
        TRANSIENT,
        UNKNOWN,
        VALID,
    )

    return _CredentialVerdicts(
        bad=BAD_CREDENTIALS,
        banned=BANNED,
        captcha=CAPTCHA,
        locked=LOCKED,
        recovery=RECOVERY,
        transient=TRANSIENT,
        unknown=UNKNOWN,
        valid=VALID,
    )


def _credential_probe(username: str, password: str) -> dict[str, Any] | None:
    try:
        from services.credential_check import paced_check_credentials

        return paced_check_credentials(username, password)
    except Exception:
        return None


def _log_credential_outcome(username: str, outcome: str) -> None:
    try:
        from core import flog_kv as _flog_cc

        _flog_cc("ACCOUNT_DATA", "credential_check", account=username, outcome=outcome)
    except Exception:
        "intentional: ignore exception"


def _login_was_gated(
    cc: dict[str, Any] | None,
    outcome: str,
    verdicts: _CredentialVerdicts,
    line: _ImportLine,
) -> bool:
    return bool(
        outcome == verdicts.unknown
        and str((cc or {}).get("challenge_type") or "")
        and (line.username or line.cookie)
    )


def _apply_fresh_state(
    state: _ImportState,
    line: _ImportLine,
    validator: Any,
    cc: dict[str, Any] | None,
) -> None:
    fresh_state = _apply_fresh_cookie(
        validator,
        cc,
        line.cookie,
        line.ok,
        line.validated,
        line.detail,
        line.meta,
        line.username,
    )
    line.cookie = fresh_state["cookie"]
    line.ok = fresh_state["ok"]
    line.validated = fresh_state["validated"]
    line.detail = fresh_state["detail"]
    line.meta = fresh_state["meta"]
    _apply_validation_ids(line)
    state.refreshed += fresh_state["refreshed"]
    state.errors.extend(fresh_state["errors"])


def _run_credential_probe(
    state: _ImportState,
    line: _ImportLine,
    validator: Any,
) -> None:
    verdicts = _credential_verdicts()
    cc = _credential_probe(line.username, line.password)
    outcome = str((cc or {}).get("outcome") or verdicts.unknown)
    _log_credential_outcome(line.username, outcome)
    if outcome == verdicts.transient:
        state.cred_check_blocked = True
    elif outcome == verdicts.valid:
        _apply_fresh_state(state, line, validator, cc)
    elif outcome == verdicts.banned:
        state.records.append(
            _banned_login_record(line.username, line.cookie, line.cookie_user_id)
        )
        state.banned += 1
        state.errors.append(f"{line.username} banned (login) - imported with Banned mark")
        line.skip_line = 1
    elif outcome in (verdicts.locked, verdicts.recovery, verdicts.captcha):
        record, word, (delta_l, delta_r, delta_c) = _verify_login_mark(
            outcome,
            line.username,
            line.cookie,
            line.cookie_user_id,
        )
        state.records.append(record)
        state.locked += delta_l
        state.account_recovery += delta_r
        state.captcha += delta_c
        state.errors.append(
            f"{line.username} {word} (login) - imported with {word.capitalize()} mark"
        )
        line.skip_line = 1
    elif outcome == verdicts.bad:
        state.records.append(
            {
                "username": line.username,
                "cookie": line.cookie,
                "cookie_username": line.username,
                "cookie_user_id": line.cookie_user_id,
                "cookie_mismatch": False,
                "manual_status": "Invalid Cookie: incorrect username or password",
                "import_status": "cookie_invalid",
            }
        )
        state.errors.append(
            f"{line.username} wrong password - fix the password and reimport"
        )
        line.skip_line = 1
    if _login_was_gated(cc, outcome, verdicts, line):
        line.login_gated, line.skip_line = _challenge_login_hold(
            line.cookie,
            line.username,
            line.cookie_user_id,
            state.records,
            state.errors,
        )
        state.locked += line.skip_line


def _handle_valid_import(line: _ImportLine, state: _ImportState) -> None:
    if not line.username:
        line.username = str(line.validated or "")
    elif line.cookie_username and line.username.strip().lower() != line.cookie_username.strip().lower():
        line.cookie_mismatch = True
        state.errors.append(f"cookie belongs to {line.cookie_username}, not {line.username}")


def _dead_lock_record(
    line: _ImportLine,
    lock_user: str,
    lock_uid: str,
    lock_msg: str,
    lock_status: str,
) -> dict[str, Any]:
    return {
        "username": lock_user,
        "cookie": line.cookie,
        "cookie_username": lock_user,
        "cookie_user_id": str(lock_uid or line.cookie_user_id or ""),
        "cookie_mismatch": False,
        "manual_status": lock_msg,
        "import_status": lock_status,
    }


def _handle_dead_cookie(state: _ImportState, line: _ImportLine) -> None:
    lock_user, lock_uid, lock_msg, lock_status = _locked_import_mark(
        line.cookie,
        line.username,
        line.detail,
        "",
        "",
    )
    if lock_user:
        state.records.append(_dead_lock_record(line, lock_user, lock_uid, lock_msg, lock_status))
        state.errors.append(f"{lock_user} locked (moderation) - imported with Locked mark")
        return
    banned_by_name, resolved_name, ban_uid = _resolve_ban_by_name(line.cookie, line.username)
    if banned_by_name:
        line.username = line.username or resolved_name
        state.records.append(_banned_login_record(line.username, line.cookie, ban_uid))
        state.banned += 1
        state.errors.append(
            f"{line.username} banned (cookie expired) — imported with Banned mark"
        )
        return
    if resolved_name:
        line.username = line.username or resolved_name
        state.records.append(
            {
                "username": line.username,
                "cookie": line.cookie,
                "cookie_username": resolved_name,
                "cookie_user_id": line.cookie_user_id or ban_uid,
                "cookie_mismatch": False,
                "manual_status": "",
                "import_status": "cookie_invalid",
            }
        )
        state.errors.append(
            f"{line.username} imported, but the cookie is not valid — paste a fresh cookie "
            "for this account" + (_GATED_HINT if line.login_gated else "")
        )
        return
    state.errors.append(str(line.detail or "") + (_GATED_HINT if line.login_gated else ""))


def _plain_import_record(line: _ImportLine) -> dict[str, Any]:
    return {
        "username": line.username,
        "cookie": line.cookie,
        "cookie_username": line.cookie_username,
        "cookie_user_id": line.cookie_user_id,
        "cookie_mismatch": line.cookie_mismatch,
        "import_status": "cookie_mismatch" if line.cookie_mismatch else "",
    }


def _import_result(
    state: _ImportState,
    changed: int,
    merged: list[dict[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": True,
        "imported": changed,
        "errors": state.errors,
        "count": len(merged),
    }
    if state.banned:
        result["banned"] = state.banned
    if state.locked:
        result["locked"] = state.locked
    if state.account_recovery:
        result["recovery"] = state.account_recovery
    if state.captcha:
        result["captcha"] = state.captcha
    if state.refreshed:
        result["refreshed"] = state.refreshed
    return result


def _validate_import_line(
    line: _ImportLine,
    validator: Any,
    state: _ImportState,
) -> bool:
    line.ok, line.validated, line.detail, line.meta = _parse_validation(validator(line.cookie))
    _apply_validation_ids(line)
    line.is_banned = _import_ban_flag(line.meta, line.detail)
    if line.is_banned and not _import_suspended_flag(line.meta):
        if _handle_moderated_lock(state, line):
            return True
    if line.is_banned:
        _handle_ban_import(state, line)
        return True
    if _handle_verify_mark(state, line):
        return True
    if _should_probe_credentials(line, state):
        _run_credential_probe(state, line, validator)
    if line.skip_line:
        return True
    if line.ok:
        _handle_valid_import(line, state)
        return False
    _handle_dead_cookie(state, line)
    return True


class AccountDataStore:
    def __init__(self, path: str = ACCOUNT_DATA_FILE):
        self.path = path

    def _read_bytes(self) -> bytes:
        if not os.path.exists(self.path):
            return b""
        with open(self.path, "rb") as f:
            return f.read()

    @staticmethod
    def _json_from_bytes(data: bytes) -> Any:
        return json.loads(data.decode("utf-8-sig"))

    @classmethod
    def decode_account_file_bytes(cls, data: bytes) -> list[dict[str, Any]]:
        if not data:
            return []
        stripped = data.lstrip()
        if stripped.startswith((b"{", b"[")):
            raw = cls._json_from_bytes(data)
        else:
            try:
                raw = cls._json_from_bytes(dpapi_unprotect(data, CRONUS_ENTROPY))
            except Exception as exc:
                raise ValueError(f"Unsupported or password-locked AccountData.json: {exc}") from exc
        if isinstance(raw, dict):
            accounts = raw.get("accounts", raw.get("Accounts", []))
        else:
            accounts = raw
        if not isinstance(accounts, list):
            raise ValueError("AccountData must contain an account array")  # noqa: TRY004 - public contract raises ValueError
        return [cls.normalize_record(item) for item in accounts if isinstance(item, dict)]

    def read_records(self, include_cookies: bool = False) -> list[dict[str, Any]]:
        records = self.decode_account_file_bytes(self._read_bytes())
        if include_cookies:
            for record in records:
                record["cookie"] = self.get_cookie_from_record(record)
        return records

    def write_records(self, records: Iterable[dict[str, Any]]) -> None:
        clean = [self.normalize_record(item) for item in records if isinstance(item, dict)]
        payload = {
            "schema": "cronus.accountdata.v1",
            "updated_at": time.time(),
            "accounts": clean,
        }
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = f"{self.path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.path)

    @staticmethod
    def get_cookie_from_record(record: dict[str, Any]) -> str:
        encrypted = str(record.get("encrypted_cookie") or record.get("EncryptedCookie") or "").strip()
        if encrypted:
            return decrypt_cookie(encrypted, strict=False)
        cookie = str(record.get("cookie") or record.get("SecurityToken") or record.get("securityToken") or "").strip()
        if cookie.startswith("_|WARNING:"):
            return cookie
        return ""

    @staticmethod
    def cookie_state(record: dict[str, Any], cookie: str | None = None) -> str:
        """Classify the stored cookie as ok | unreadable | missing. Never raises.

        Pass ``cookie`` when the caller already decrypted the record so the hot
        status path does not pay for a second DPAPI round-trip per account.
        """
        encrypted = str(record.get("encrypted_cookie") or record.get("EncryptedCookie") or "").strip()
        if encrypted:
            if cookie is None:
                try:
                    decrypt_cookie(encrypted)
                except Exception:
                    return "unreadable"
                return "ok"
            return "ok" if cookie else "unreadable"
        if cookie is None:
            cookie = str(record.get("cookie") or record.get("SecurityToken") or record.get("securityToken") or "").strip()
        return "ok" if cookie.startswith("_|WARNING:") else "missing"

    @classmethod
    def normalize_record(cls, item: dict[str, Any]) -> dict[str, Any]:
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else item.get("Fields")
        if not isinstance(fields, dict):
            fields = {}

        username = str(_record_pick(item, fields, "username", "Username", "Account", "account")).strip()
        cookie = cls.get_cookie_from_record(item)
        encrypted_cookie = _record_encrypted_cookie(item, cookie)

        vip_links = _normalize_vip_links(item)
        owned_private_servers = _normalize_owned_private_servers(
            item.get("owned_private_servers", item.get("ownedPrivateServers", item.get("private_servers", [])))
        )

        job_id = str(_record_pick(item, fields, "job_id", "JobId", "jobId", "SavedJobId")).strip()
        if job_id.startswith("http") and job_id not in vip_links:
            vip_links.append(job_id)

        browser_tracker_id = _normalize_browser_tracker(item, fields)
        normalized_fields = {str(k): str(v) for k, v in fields.items() if v not in (None, "")}

        return {
            "username": username,
            "user_id": str(_record_pick(item, fields, "user_id", "UserID", "userId")).strip(),
            "alias": str(_record_pick(item, fields, "alias", "Alias")).strip(),
            "group": _record_group(item, fields),
            "description": str(_record_pick(item, fields, "description", "Description")).strip(),
            "cookie_username": str(
                _record_pick(item, fields, "cookie_username", "CookieUsername", "cookieUsername")
            ).strip(),
            "cookie_user_id": str(
                _record_pick(item, fields, "cookie_user_id", "CookieUserID", "cookieUserId")
            ).strip(),
            "cookie_mismatch": _record_pick_bool(
                item,
                fields,
                "cookie_mismatch",
                "CookieMismatch",
                "cookieMismatch",
            ),
            "encrypted_cookie": encrypted_cookie,
            "place_id": str(
                _record_pick(item, fields, "place_id", "PlaceId", "placeId", "SavedPlaceId")
            ).strip(),
            "game_id": str(
                _record_pick(item, fields, "game_id", "GameId", "gameId", "SavedGameId")
            ).strip(),
            "job_id": job_id,
            "vip_links": [str(link).strip() for link in vip_links if str(link).strip()],
            "owned_private_servers": owned_private_servers,
            "browser_tracker_id": browser_tracker_id,
            "last_use": _record_float(item, "last_use", "LastUse"),
            "last_cookie_refresh": _record_float(
                item,
                "last_cookie_refresh",
                "LastAttemptedRefresh",
            ),
            "priority": _record_priority(item),
            "manual_status": _record_text_field(item, "manual_status"),
            "finished_at": _record_float(item, "finished_at"),
            "fields": normalized_fields,
            "import_status": _record_text_field(item, "import_status"),
        }

    @classmethod
    def to_api_record(cls, record: dict[str, Any], include_cookie: bool = False) -> dict[str, Any]:
        out = dict(cls.normalize_record(record))
        cookie = cls.get_cookie_from_record(record)
        out["cookie_present"] = bool(cookie)
        out["cookie_state"] = cls.cookie_state(record, cookie)
        redacted_vip_links, vip_link_hashes = _redact_vip_links_for_api(out.get("vip_links", []))
        out["vip_links"] = redacted_vip_links
        out["vip_link_hashes"] = vip_link_hashes
        out["vip_links_count"] = len(vip_link_hashes)
        out["owned_private_servers"] = _public_owned_private_servers(out.get("owned_private_servers", []))
        out.pop("encrypted_cookie", None)
        if include_cookie:
            out["cookie"] = cookie
        return out

    @classmethod
    def to_cronus_account(cls, record: dict[str, Any]) -> dict[str, Any]:
        normalized = cls.normalize_record(record)
        return {
            "username": normalized["username"],
            "user_id": normalized["user_id"],
            "priority": normalized["priority"],
            "place_id": normalized["place_id"],
            "game_id": str(normalized.get("game_id") or ""),
            "vip_links": list(normalized["vip_links"]),
            "owned_private_servers": list(normalized.get("owned_private_servers") or []),
            "alias": normalized["alias"],
            "cookie": cls.get_cookie_from_record(normalized),
            "browser_tracker_id": normalized["browser_tracker_id"],
            "cookie_username": normalized["cookie_username"],
            "cookie_user_id": normalized["cookie_user_id"],
            "cookie_mismatch": normalized["cookie_mismatch"],
            "description": normalized["description"],
            "manual_status": normalized["manual_status"],
            "import_status": normalized["import_status"],
            "finished_at": normalized["finished_at"],
        }

    def to_cronus_accounts(self) -> list[dict[str, Any]]:
        return [self.to_cronus_account(record) for record in self.read_records(include_cookies=True)]

    def replace_from_cronus_payload(self, payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
        existing = _existing_by_username(self.read_records(include_cookies=False))
        records: list[dict[str, Any]] = []
        for item in payload:
            data = dict(item)
            username = str(data.get("username") or data.get("Username") or "").strip()
            old = existing.get(username.lower(), {})
            records.append(self.normalize_record(_carry_forward_old(data, old)))
        self.write_records(records)
        return records

    def upsert_records(self, new_records: Iterable[dict[str, Any]]) -> tuple[int, list[dict[str, Any]]]:
        merged = {
            str(record.get("username") or "").strip().lower(): record
            for record in self.read_records(include_cookies=False)
            if str(record.get("username") or "").strip()
        }
        changed = 0
        for raw in new_records:
            record = self.normalize_record(raw)
            key = str(record.get("username") or "").strip().lower()
            if not key:
                continue
            old = merged.get(key, {})
            if not record.get("encrypted_cookie") and old.get("encrypted_cookie"):
                record["encrypted_cookie"] = old["encrypted_cookie"]
            merged[key] = {**old, **record}
            changed += 1
        records = list(merged.values())
        self.write_records(records)
        return changed, records

    def import_cookie_lines(self, lines: Iterable[str], validator=None) -> dict[str, Any]:
        state = _ImportState()
        for line_text in lines:
            line = _ImportLine(*parse_credential_line(line_text))
            if not line.cookie:
                continue
            if not line.username:
                # Cookie-only line (no "username:" prefix). The token itself
                # embeds its owner (uname/uid fields), so identity — and the
                # ban-by-name check — still work instead of skipping the line.
                line.username = _import_owner_cookie(line.cookie)
            if validator:
                try:
                    if _validate_import_line(line, validator, state):
                        continue
                except Exception as exc:
                    state.errors.append(str(exc))
                    continue
            if not line.username:
                state.errors.append("cookie line missing username")
                continue
            state.records.append(_plain_import_record(line))
        changed, merged = self.upsert_records(state.records)
        return _import_result(state, changed, merged)

    def update_record(self, username: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        records = self.read_records(include_cookies=False)
        key = str(username or "").strip().lower()
        updated: dict[str, Any] | None = None
        for idx, record in enumerate(records):
            if str(record.get("username") or "").strip().lower() == key:
                records[idx] = self.normalize_record({**record, **updates})
                updated = records[idx]
                break
        if updated is not None:
            self.write_records(records)
        return updated


def audit_event(action: str, username: str = "", ok: bool = True, **fields: Any) -> None:
    event = {
        "ts": round(time.time(), 3),
        "action": str(action or ""),
        "username": str(username or ""),
        "ok": bool(ok),
    }
    for key, value in fields.items():
        event[str(key)] = redact_secret(value)
    os.makedirs(os.path.dirname(ACCOUNT_AUDIT_FILE), exist_ok=True)
    with open(ACCOUNT_AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


ACCOUNT_STORE = AccountDataStore()
