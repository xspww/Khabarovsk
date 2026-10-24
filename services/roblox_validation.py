"""Cookie validation entry points used by workers, routes, and detectors."""
from __future__ import annotations

from typing import Any, NamedTuple

from services.account_hybrid import ACCOUNT_STORE
from services.captcha_guard import CAPTCHA_BLOCK_REASON, CAPTCHA_REASON, is_captcha_text
from services.roblox_http import RobloxHTTP
from services.roblox_identity import (
    cookie_embedded_identity,
    moderated_only_detail,
    resolve_ban_for_dead_cookie,
    resolve_moderated_dead_cookie,
)
from services.roblox_moderation import fetch_moderation_status, fetch_user_ban_status

try:
    from services.ban_guard import BANNED_BLOCK_REASON as _BANNED_MSG
    from services.ban_guard import BANNED_IMPORT_STATUS as _BANNED_STATUS
    from services.ban_guard import is_banned_status_text as _is_banned_status_text
    from services.ban_guard import is_banned_text as _is_banned_text
    from services.ban_guard import is_suspended_text as _is_suspended_text
except Exception:
    _BANNED_MSG = "Account banned (Roblox). Unmark to allow rejoin."
    _BANNED_STATUS = "banned"
    _is_banned_status_text = lambda *a, **k: False
    _is_banned_text = lambda *a, **k: False
    _is_suspended_text = lambda *a, **k: False
try:
    from services.ban_guard import SUSPENDED_BLOCK_REASON as _SUSP_MSG
except Exception:
    _SUSP_MSG = "Account suspended by Roblox. Unmark to allow rejoin."
try:
    from services.verify_guard import LOCKED_BLOCK_REASON as _LOCKED_MSG
    from services.verify_guard import LOCKED_REASON as _LOCKED_STATUS
    from services.verify_guard import RECOVERY_BLOCK_REASON as _RECOVERY_MSG
    from services.verify_guard import RECOVERY_REASON as _RECOVERY_STATUS
    from services.verify_guard import is_locked_status_text as _is_locked_status
    from services.verify_guard import is_locked_text as _is_locked_text
    from services.verify_guard import is_recovery_status_text as _is_recovery_status
    from services.verify_guard import is_recovery_text as _is_recovery_text
except Exception:
    _LOCKED_MSG = "Account locked - face/ID verification required. Verify manually, then click Resume."
    _LOCKED_STATUS = "identity_locked"
    _RECOVERY_MSG = "Account recovery - passkey/2FA required. Complete recovery manually, then click Resume."
    _RECOVERY_STATUS = "account_recovery"
    _is_locked_status = lambda *a, **k: False
    _is_locked_text = lambda *a, **k: False
    _is_recovery_status = lambda *a, **k: False
    _is_recovery_text = lambda *a, **k: False


class _RecordVerdict(NamedTuple):
    record: dict[str, Any]
    username: str
    cookie_username: str
    detail: str
    meta: dict[str, Any]
    is_banned: bool
    is_locked: bool
    is_recovery: bool
    update_store: bool


def _banned_updates(cookie_username: str, cookie_user_id: str) -> dict[str, Any]:
    return {
        "manual_status": _BANNED_MSG,
        "import_status": _BANNED_STATUS,
        "cookie_username": str(cookie_username or ""),
        "cookie_user_id": str(cookie_user_id or ""),
        "cookie_mismatch": False,
    }


def _identity_updates(
    cookie_username: str,
    cookie_user_id: str,
    mismatch: bool,
) -> dict[str, Any]:
    return {
        "cookie_username": str(cookie_username or ""),
        "cookie_user_id": str(cookie_user_id or ""),
        "cookie_mismatch": mismatch,
        "import_status": "cookie_mismatch" if mismatch else "",
    }


def cookie_verdict_updates(
    kind: str,
    *,
    cookie_username: str = "",
    cookie_user_id: str = "",
    **fields: Any,
) -> dict[str, Any]:
    """Single owner for the cookie-verdict → store-write mapping.

    Validation used to build these dicts inline at seven sites; every
    verdict decision now leverages this one. Pure: callers still gate on
    `update_store and username` and write via persist_cookie_verdict.
    """
    if kind == "cleared_ban":
        return {"manual_status": "", "import_status": ""}
    if kind == "banned":
        return _banned_updates(cookie_username, cookie_user_id)
    if kind == "locked":
        return {"manual_status": _LOCKED_MSG, "import_status": _LOCKED_STATUS}
    if kind == "recovery":
        return {"manual_status": _RECOVERY_MSG, "import_status": _RECOVERY_STATUS}
    if kind == "captcha":
        return {"manual_status": CAPTCHA_BLOCK_REASON, "import_status": CAPTCHA_REASON}
    if kind == "invalid":
        return {"cookie_mismatch": True, "import_status": "cookie_invalid"}
    if kind == "identity":
        return _identity_updates(
            cookie_username,
            cookie_user_id,
            bool(fields.get("mismatch", False)),
        )
    raise ValueError(f"unknown cookie verdict: {kind}")


def persist_cookie_verdict(username: str, kind: str, **fields: Any) -> bool:
    """Write one cookie verdict to the store. No-op on empty username."""
    if not str(username or "").strip():
        return False
    ACCOUNT_STORE.update_record(str(username), cookie_verdict_updates(kind, **fields))
    return True


def _embedded_identity(cookie: str) -> tuple[str, str]:
    try:
        return cookie_embedded_identity(cookie)
    except Exception:
        return "", ""


def _cookie_moderation_probe(cookie: str) -> tuple[bool | None, str, dict[str, Any]]:
    try:
        return fetch_moderation_status(cookie)
    except Exception:
        return None, "", {}


def _moderation_meta(
    emb_name: str,
    emb_uid: str,
    mod_payload: dict[str, Any],
    suspended: bool,
) -> dict[str, Any]:
    uid = ""
    try:
        uid = str((mod_payload or {}).get("punishedUserId") or "")
    except Exception:
        uid = ""
    return {
        "username": emb_name,
        "user_id": uid or emb_uid,
        "is_banned": True,
        "banned": True,
        "is_suspended": suspended,
        "suspended": suspended,
    }


def _moderated_failure(
    emb_name: str,
    emb_uid: str,
    mod_detail: str,
    mod_payload: dict[str, Any],
) -> tuple[bool, str, str, dict[str, Any]]:
    try:
        suspended = bool(_is_suspended_text(mod_detail))
    except Exception:
        suspended = False
    return False, emb_name, mod_detail, _moderation_meta(
        emb_name,
        emb_uid,
        mod_payload,
        suspended,
    )


def _data_username(data: Any) -> str:
    try:
        return str(data.get("name") or "") if isinstance(data, dict) else ""
    except Exception:
        return ""


def _banned_dead_cookie(
    emb_name: str,
    emb_uid: str,
    detail: str,
    data: Any,
) -> tuple[bool, str, str, dict[str, Any]] | None:
    try:
        banned = bool(_is_banned_text(detail))
    except Exception:
        banned = False
    if not banned:
        return None
    username_guess = _data_username(data)
    if not username_guess:
        username_guess = emb_name
    return False, username_guess, detail, {
        "username": username_guess,
        "user_id": emb_uid,
        "is_banned": True,
        "banned": True,
    }


def _dead_cookie_result(
    cookie: str,
    data: Any,
    detail: str,
) -> tuple[bool, str, str, dict[str, Any]]:
    # Owner fallback: a dead cookie still names its account inside the
    # token (uname/uid fields), so error paths can show *who* is blocked
    # even when the line carried no username prefix.
    emb_name, emb_uid = _embedded_identity(cookie)
    # 403 "User is moderated" means the cookie is still good enough for
    # the moderation endpoint even though authenticated/ fails. Probe it
    # before giving up so a 6-month suspension reads as Suspended, not Invalid.
    mod_banned, mod_detail, mod_payload = _cookie_moderation_probe(cookie)
    if mod_banned is True and mod_detail:
        return _moderated_failure(emb_name, emb_uid, mod_detail, mod_payload)
    banned = _banned_dead_cookie(emb_name, emb_uid, detail, data)
    if banned is not None:
        return banned
    return False, emb_name, detail, {"username": emb_name, "user_id": emb_uid}


def _moderation_failure(
    username: str,
    user_id: str,
    moderation_detail: str,
) -> tuple[bool, str, str, dict[str, Any]]:
    try:
        suspended = bool(_is_suspended_text(moderation_detail))
    except Exception:
        suspended = False
    msg = _SUSP_MSG if suspended else _BANNED_MSG
    # A suspension blocks the account just as hard as a ban, but it is not
    # permanent, so it is tracked under its own mark and label.
    return False, username, moderation_detail or msg, {
        "username": username,
        "user_id": user_id,
        "is_banned": True,
        "banned": True,
        "is_suspended": suspended,
        "suspended": suspended,
    }


def _valid_cookie_result(
    cookie: str,
    username: str,
    user_id: str,
) -> tuple[bool, str, str, dict[str, Any]]:
    # Cookie is valid. Check the live moderation action first: it is the only
    # signal that sees a temporary suspension, which isBanned reports as false.
    moderation_banned, moderation_detail, _payload = fetch_moderation_status(cookie)
    if moderation_banned is True:
        return _moderation_failure(username, user_id, moderation_detail)
    # No active intervention â€” fall back to the public profile, which still
    # owns permanent terminations (those can outlive the cookie).
    if user_id:
        banned, ban_detail = fetch_user_ban_status(user_id)
        if banned is True:
            return False, username, ban_detail or _BANNED_MSG, {
                "username": username,
                "user_id": user_id,
                "is_banned": True,
                "banned": True,
            }
        if banned is None:
            # Transient (rate-limit/network): don't false-positive as banned.
            return True, username, "ok", {
                "username": username,
                "user_id": user_id,
                "is_banned": False,
                "banned": False,
                "ban_check": "unknown",
                "ban_detail": ban_detail,
            }
    return True, username, "ok", {
        "username": username,
        "user_id": user_id,
        "is_banned": False,
        "banned": False,
    }


def validate_cookie_details(cookie: str) -> tuple[bool, str, str, dict[str, Any]]:
    ok, data, detail = RobloxHTTP(cookie).authenticated_user()
    if not ok:
        return _dead_cookie_result(cookie, data, detail)
    username = str(data.get("name") or data.get("displayName") or "")
    user_id = str(data.get("id") or "")
    return _valid_cookie_result(cookie, username, user_id)


def _verdict_is_banned(meta: dict[str, Any], detail: str) -> bool:
    if bool((meta or {}).get("is_banned") or (meta or {}).get("banned")):
        return True
    try:
        return bool(_is_banned_text(detail))
    except Exception:
        return False


def _resolve_moderated(cookie: str, username: str) -> tuple[Any, str, str, str]:
    try:
        return resolve_moderated_dead_cookie(cookie, username)
    except Exception:
        return None, "", "", ""


def _locked_override(
    cookie: str,
    username: str,
    detail: str,
    meta: dict[str, Any],
) -> tuple[bool, str, dict[str, Any]]:
    mv, md, mn, mu = _resolve_moderated(cookie, username)
    if mv != "locked":
        return False, detail, meta
    updated = dict(meta or {})
    updated["is_banned"] = False
    updated["banned"] = False
    updated["is_locked"] = True
    updated["locked"] = True
    if mn:
        updated["username"] = mn
    if mu:
        updated["user_id"] = mu
    return True, md or detail, updated


def _lock_recovery_flags(detail: str) -> tuple[bool, bool]:
    try:
        is_locked = bool(_is_locked_text(detail))
    except Exception:
        is_locked = False
    try:
        is_recovery = bool(_is_recovery_text(detail)) and not is_locked
    except Exception:
        is_recovery = False
    return is_locked, is_recovery


def _record_text(record: dict[str, Any], key: str) -> str:
    return str((record or {}).get(key) or "")


def _recheck_ban(cookie: str, username: str) -> bool | None:
    try:
        banned, _name, _uid = resolve_ban_for_dead_cookie(cookie, username)
        return banned
    except Exception:
        return None


def _keep_ban_mark(
    record: dict[str, Any],
    username: str,
    cookie: str,
    detail: str,
    update_store: bool,
) -> tuple[bool, str, dict[str, Any]]:
    manual = _record_text(record, "manual_status")
    import_status = _record_text(record, "import_status")
    try:
        keep = bool(_is_banned_status_text(manual, import_status))
    except Exception:
        keep = False
    if not keep:
        return False, detail, record
    # Re-verify via the public API first: a previous mark must survive, but
    # one old false mark must never stick forever.
    recheck = _recheck_ban(cookie, username)
    if recheck is False:
        # Public API says the account is alive: drop the stale mark instead
        # of preserving it.
        updated = dict(record or {})
        updated["manual_status"] = ""
        updated["import_status"] = ""
        if update_store and username:
            persist_cookie_verdict(username, "cleared_ban")
        return False, detail, updated
    if not detail:
        detail = "cookie expired, ban mark kept"
    return True, detail, record


def _keep_lock_marks(record: dict[str, Any], detail: str) -> tuple[bool, bool, str]:
    manual = _record_text(record, "manual_status")
    import_status = _record_text(record, "import_status")
    last_error = _record_text(record, "last_error")
    # A previous Locked/Recovery mark survives a dead cookie: the cookie API
    # cannot prove the verification is cleared, so keep the mark instead of
    # demoting to Invalid.
    try:
        if _is_locked_status(manual, import_status, last_error):
            return True, False, detail or "cookie expired, locked mark kept"
        if _is_recovery_status(manual, import_status, last_error):
            return False, True, detail or "cookie expired, recovery mark kept"
    except Exception:
        "intentional: ignore exception"
    return False, False, detail


def _failure_cookie_username(
    record: dict[str, Any],
    meta: dict[str, Any],
    cookie_username: str,
) -> str:
    return str(
        cookie_username
        or (meta or {}).get("username")
        or (record or {}).get("cookie_username")
        or ""
    )


def _failure_cookie_user_id(record: dict[str, Any], meta: dict[str, Any]) -> str:
    return str((meta or {}).get("user_id") or (record or {}).get("cookie_user_id") or "")


def _persist_failure(
    verdict: _RecordVerdict,
    cookie_username: str,
    cookie_user_id: str,
) -> None:
    if verdict.is_banned:
        persist_cookie_verdict(
            verdict.username,
            "banned",
            cookie_username=cookie_username,
            cookie_user_id=cookie_user_id,
        )
    elif verdict.is_locked:
        persist_cookie_verdict(verdict.username, "locked")
    elif verdict.is_recovery:
        persist_cookie_verdict(verdict.username, "recovery")
    elif is_captcha_text(verdict.detail):
        persist_cookie_verdict(verdict.username, "captcha")
    else:
        persist_cookie_verdict(verdict.username, "invalid")


def _detail_or(detail: str, fallback: str) -> str:
    return detail or fallback


def _record_failure(verdict: _RecordVerdict) -> dict[str, Any]:
    cookie_username = _failure_cookie_username(
        verdict.record,
        verdict.meta,
        verdict.cookie_username,
    )
    cookie_user_id = _failure_cookie_user_id(verdict.record, verdict.meta)
    if verdict.update_store and verdict.username:
        _persist_failure(verdict, cookie_username, cookie_user_id)
    if verdict.is_banned:
        return {
            "ok": False,
            "fatal": True,
            "banned": True,
            "msg": _BANNED_MSG,
            "detail": _detail_or(verdict.detail, _BANNED_MSG),
            "cookie_username": cookie_username,
            "cookie_user_id": cookie_user_id,
        }
    if verdict.is_locked:
        return {
            "ok": False,
            "fatal": True,
            "locked_required": True,
            "msg": _LOCKED_MSG,
            "detail": _detail_or(verdict.detail, _LOCKED_MSG),
            "cookie_username": "",
            "cookie_user_id": "",
        }
    if verdict.is_recovery:
        return {
            "ok": False,
            "fatal": True,
            "recovery_required": True,
            "msg": _RECOVERY_MSG,
            "detail": _detail_or(verdict.detail, _RECOVERY_MSG),
            "cookie_username": "",
            "cookie_user_id": "",
        }
    if is_captcha_text(verdict.detail):
        return {
            "ok": False,
            "fatal": True,
            "captcha_required": True,
            "msg": CAPTCHA_BLOCK_REASON,
            "detail": verdict.detail,
            "cookie_username": "",
            "cookie_user_id": "",
        }
    return {
        "ok": False,
        "msg": verdict.detail,
        "cookie_username": "",
        "cookie_user_id": "",
    }


def _preserve_marks(updates: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    prev_manual = _record_text(record, "manual_status")
    prev_import = _record_text(record, "import_status")
    # Cookie is valid and not banned — clear a stale banned mark so an
    # unbanned / replaced cookie rejoins again without manual unmark.
    try:
        if _is_banned_status_text(prev_manual, prev_import):
            updates["manual_status"] = ""
    except Exception:
        "intentional: ignore exception"
    # Locked/Recovery marks are NOT cleared here: a valid cookie does not
    # prove face-scan / passkey verification is done (cookie stays 200
    # while the lock screen persists). They clear only on manual Resume.
    try:
        if _is_locked_status(prev_manual, prev_import) or _is_recovery_status(
            prev_manual,
            prev_import,
        ):
            updates["manual_status"] = prev_manual
            updates["import_status"] = prev_import
    except Exception:
        "intentional: ignore exception"
    return updates


def _record_success(verdict: _RecordVerdict) -> dict[str, Any]:
    cookie_user_id = str(verdict.meta.get("user_id") or "")
    mismatch = bool(
        verdict.username
        and verdict.cookie_username
        and verdict.username.lower() != verdict.cookie_username.lower()
    )
    updates = cookie_verdict_updates(
        "identity",
        cookie_username=verdict.cookie_username,
        cookie_user_id=cookie_user_id,
        mismatch=mismatch,
    )
    if not mismatch:
        updates = _preserve_marks(updates, verdict.record)
    if verdict.update_store and verdict.username:
        ACCOUNT_STORE.update_record(verdict.username, updates)
    if mismatch:
        return {
            "ok": False,
            "msg": (
                f"Cookie belongs to {verdict.cookie_username}, not "
                f"{verdict.username}. Reimport the correct .ROBLOSECURITY "
                "for this account."
            ),
            "cookie_username": verdict.cookie_username,
            "cookie_user_id": cookie_user_id,
            "cookie_mismatch": True,
        }
    return {
        "ok": True,
        "msg": "ok",
        "cookie_username": verdict.cookie_username,
        "cookie_user_id": cookie_user_id,
        "cookie_mismatch": False,
    }


def _prepare_record_verdict(
    record: dict[str, Any],
    cookie: str,
    username: str,
    detail: str,
    meta: dict[str, Any],
    update_store: bool,
) -> tuple[bool, bool, bool, str, dict[str, Any], dict[str, Any]]:
    is_banned = _verdict_is_banned(meta, detail)
    if is_banned and moderated_only_detail(detail):
        # "User is moderated" alone fits a verification lock as well as a
        # ban. Resolve it before labeling so a locked account never reads
        # as banned.
        is_banned, detail, meta = _locked_override(cookie, username, detail, meta)
    # Locked / recovery outrank captcha but never outrank a ban.
    is_locked, is_recovery = _lock_recovery_flags(detail)
    if not is_banned:
        is_banned, detail, record = _keep_ban_mark(
            record,
            username,
            cookie,
            detail,
            update_store,
        )
    if not is_banned and not is_locked and not is_recovery:
        is_locked, is_recovery, detail = _keep_lock_marks(record, detail)
    return is_banned, is_locked, is_recovery, detail, meta, record


def validate_record_cookie_identity(
    record: dict[str, Any],
    cookie: str,
    update_store: bool = True,
) -> dict[str, Any]:
    username = str((record or {}).get("username") or "").strip()
    ok, cookie_username, detail, meta = validate_cookie_details(cookie)
    is_banned, is_locked, is_recovery, detail, meta, record = _prepare_record_verdict(
        record,
        cookie,
        username,
        detail,
        meta,
        update_store,
    )
    verdict = _RecordVerdict(
        record=record,
        username=username,
        cookie_username=cookie_username,
        detail=detail,
        meta=meta,
        is_banned=is_banned,
        is_locked=is_locked,
        is_recovery=is_recovery,
        update_store=update_store,
    )
    if not ok:
        return _record_failure(verdict)
    return _record_success(verdict)
