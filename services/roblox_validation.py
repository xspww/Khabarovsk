"""Cookie validation entry points used by workers, routes, and detectors."""
from __future__ import annotations

from typing import Any

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
except Exception:
    _BANNED_MSG = "Account banned (Roblox). Unmark to allow rejoin."
    _BANNED_STATUS = "banned"
try:
    from services.verify_guard import LOCKED_BLOCK_REASON as _LOCKED_MSG
    from services.verify_guard import LOCKED_REASON as _LOCKED_STATUS
    from services.verify_guard import RECOVERY_BLOCK_REASON as _RECOVERY_MSG
    from services.verify_guard import RECOVERY_REASON as _RECOVERY_STATUS
except Exception:
    _LOCKED_MSG = "Account locked - face/ID verification required. Verify manually, then click Resume."
    _LOCKED_STATUS = "identity_locked"
    _RECOVERY_MSG = "Account recovery - passkey/2FA required. Complete recovery manually, then click Resume."
    _RECOVERY_STATUS = "account_recovery"


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
        return {
            "manual_status": _BANNED_MSG,
            "import_status": _BANNED_STATUS,
            "cookie_username": str(cookie_username or ""),
            "cookie_user_id": str(cookie_user_id or ""),
            "cookie_mismatch": False,
        }
    if kind == "locked":
        return {"manual_status": _LOCKED_MSG, "import_status": _LOCKED_STATUS}
    if kind == "recovery":
        return {"manual_status": _RECOVERY_MSG, "import_status": _RECOVERY_STATUS}
    if kind == "captcha":
        return {"manual_status": CAPTCHA_BLOCK_REASON, "import_status": CAPTCHA_REASON}
    if kind == "invalid":
        return {"cookie_mismatch": True, "import_status": "cookie_invalid"}
    if kind == "identity":
        mismatch = bool(fields.get("mismatch", False))
        return {
            "cookie_username": str(cookie_username or ""),
            "cookie_user_id": str(cookie_user_id or ""),
            "cookie_mismatch": mismatch,
            "import_status": "cookie_mismatch" if mismatch else "",
        }
    raise ValueError(f"unknown cookie verdict: {kind}")


def persist_cookie_verdict(username: str, kind: str, **fields: Any) -> bool:
    """Write one cookie verdict to the store. No-op on empty username."""
    if not str(username or "").strip():
        return False
    ACCOUNT_STORE.update_record(str(username), cookie_verdict_updates(kind, **fields))
    return True


def validate_cookie_details(cookie: str) -> tuple[bool, str, str, dict[str, Any]]:
    ok, data, detail = RobloxHTTP(cookie).authenticated_user()
    if not ok:
        # Owner fallback: a dead cookie still names its account inside the
        # token (uname/uid fields), so error paths can show *who* is blocked
        # even when the line carried no username prefix.
        try:
            _emb_name, _emb_uid = cookie_embedded_identity(cookie)
        except Exception:
            _emb_name, _emb_uid = "", ""
        # 403 "User is moderated" means the cookie is still good enough for
        # the moderation endpoint even though authenticated/ fails. Probe it
        # before giving up so a 6-month suspension reads as Suspended, not Invalid.
        try:
            _mod_banned, _mod_detail, _mod_payload = fetch_moderation_status(cookie)
        except Exception:
            _mod_banned, _mod_detail, _mod_payload = None, "", {}
        if _mod_banned is True and _mod_detail:
            try:
                from services.ban_guard import is_suspended_text as _is_susp_mod
            except Exception:
                _is_susp_mod = lambda *a, **k: False  # type: ignore
            _is_susp = False
            try:
                _is_susp = bool(_is_susp_mod(_mod_detail))
            except Exception:
                _is_susp = False
            _uid = ""
            try:
                _uid = str((_mod_payload or {}).get("punishedUserId") or "")
            except Exception:
                _uid = ""
            if _is_susp:
                return False, _emb_name, _mod_detail, {
                    "username": _emb_name,
                    "user_id": _uid or _emb_uid,
                    "is_banned": True,
                    "banned": True,
                    "is_suspended": True,
                    "suspended": True,
                }
            return False, _emb_name, _mod_detail, {
                "username": _emb_name,
                "user_id": _uid or _emb_uid,
                "is_banned": True,
                "banned": True,
                "is_suspended": False,
                "suspended": False,
            }
        try:
            from services.ban_guard import is_banned_text as _is_banned_text
        except Exception:
            _is_banned_text = None  # type: ignore
        if _is_banned_text is not None:
            try:
                if _is_banned_text(detail):
                    username_guess = ""
                    try:
                        username_guess = str(data.get("name") or "") if isinstance(data, dict) else ""
                    except Exception:
                        username_guess = ""
                    if not username_guess:
                        username_guess = _emb_name
                    return False, username_guess, detail, {"username": username_guess, "user_id": _emb_uid, "is_banned": True, "banned": True}
            except Exception:
                pass
        return False, _emb_name, detail, {"username": _emb_name, "user_id": _emb_uid}
    username = str(data.get("name") or data.get("displayName") or "")
    user_id = str(data.get("id") or "")
    # Cookie is valid. Check the live moderation action first: it is the only
    # signal that sees a temporary suspension, which isBanned reports as false.
    moderation_banned, moderation_detail, _payload = fetch_moderation_status(cookie)
    if moderation_banned is True:
        try:
            from services.ban_guard import (
                BANNED_BLOCK_REASON as _BANNED_MSG,
            )
            from services.ban_guard import (
                is_suspended_text as _is_suspended,
            )
        except Exception:
            _BANNED_MSG = "Account banned (Roblox). Unmark to allow rejoin."
            _is_suspended = lambda *a, **k: False  # type: ignore
        # A suspension blocks the account just as hard as a ban, but it is not
        # permanent, so it is tracked under its own mark and label.
        if _is_suspended(moderation_detail):
            try:
                from services.ban_guard import SUSPENDED_BLOCK_REASON as _SUSP_MSG
            except Exception:
                _SUSP_MSG = "Account suspended by Roblox. Unmark to allow rejoin."
            return False, username, moderation_detail or _SUSP_MSG, {
                "username": username,
                "user_id": user_id,
                "is_banned": True,
                "banned": True,
                "is_suspended": True,
                "suspended": True,
            }
        return False, username, moderation_detail or _BANNED_MSG, {
            "username": username,
            "user_id": user_id,
            "is_banned": True,
            "banned": True,
            "is_suspended": False,
            "suspended": False,
        }
    # No active intervention â€” fall back to the public profile, which still
    # owns permanent terminations (those can outlive the cookie).
    if user_id:
        banned, ban_detail = fetch_user_ban_status(user_id)
        if banned is True:
            try:
                from services.ban_guard import BANNED_BLOCK_REASON as _BANNED_MSG
            except Exception:
                _BANNED_MSG = "Account banned (Roblox). Unmark to allow rejoin."
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
    return True, username, "ok", {"username": username, "user_id": user_id, "is_banned": False, "banned": False}


def validate_record_cookie_identity(record: dict[str, Any], cookie: str, update_store: bool = True) -> dict[str, Any]:
    username = str((record or {}).get("username") or "").strip()
    ok, cookie_username, detail, meta = validate_cookie_details(cookie)
    try:
        from services.ban_guard import (
            BANNED_BLOCK_REASON as _BANNED_MSG,
        )
        from services.ban_guard import (
            BANNED_IMPORT_STATUS as _BANNED_STATUS,
        )
        from services.ban_guard import (
            is_banned_text as _is_banned_text,
        )
    except Exception:
        _BANNED_MSG = "Account banned (Roblox). Unmark to allow rejoin."
        _BANNED_STATUS = "banned"
        _is_banned_text = lambda *a, **k: False  # type: ignore
    try:
        from services.verify_guard import (
            LOCKED_BLOCK_REASON as _LOCKED_MSG,
        )
        from services.verify_guard import (
            LOCKED_REASON as _LOCKED_STATUS,
        )
        from services.verify_guard import (
            RECOVERY_BLOCK_REASON as _RECOVERY_MSG,
        )
        from services.verify_guard import (
            RECOVERY_REASON as _RECOVERY_STATUS,
        )
        from services.verify_guard import (
            is_locked_status_text as _is_locked_status,
        )
        from services.verify_guard import (
            is_locked_text as _is_locked_text,
        )
        from services.verify_guard import (
            is_recovery_status_text as _is_recovery_status,
        )
        from services.verify_guard import (
            is_recovery_text as _is_recovery_text,
        )
    except Exception:
        _LOCKED_MSG = "Account locked - face/ID verification required. Verify manually, then click Resume."
        _LOCKED_STATUS = "identity_locked"
        _RECOVERY_MSG = "Account recovery - passkey/2FA required. Complete recovery manually, then click Resume."
        _RECOVERY_STATUS = "account_recovery"
        _is_locked_status = lambda *a, **k: False  # type: ignore
        _is_locked_text = lambda *a, **k: False  # type: ignore
        _is_recovery_status = lambda *a, **k: False  # type: ignore
        _is_recovery_text = lambda *a, **k: False  # type: ignore
    is_banned = bool((meta or {}).get("is_banned") or (meta or {}).get("banned"))
    if not is_banned:
        try:
            is_banned = bool(_is_banned_text(detail))
        except Exception:
            is_banned = False
    if is_banned and moderated_only_detail(detail):
        # "User is moderated" alone fits a verification lock as well as a
        # ban. Resolve it before labeling so a locked account never reads
        # as banned.
        try:
            _mv, _md, _mn, _mu = resolve_moderated_dead_cookie(cookie, username)
        except Exception:
            _mv, _md, _mn, _mu = None, "", "", ""
        if _mv == "locked":
            is_banned = False
            detail = _md or detail
            meta = dict(meta or {})
            meta["is_banned"] = False
            meta["banned"] = False
            meta["is_locked"] = True
            meta["locked"] = True
            if _mn:
                meta["username"] = _mn
            if _mu:
                meta["user_id"] = _mu
    # Locked / recovery outrank captcha but never outrank a ban.
    is_locked = False
    is_recovery = False
    try:
        is_locked = bool(_is_locked_text(detail))
    except Exception:
        is_locked = False
    try:
        is_recovery = bool(_is_recovery_text(detail)) and not is_locked
    except Exception:
        is_recovery = False
    if not is_banned:
        # Cookie is dead so the ban API cannot confirm anything â€” but a
        # previous Banned mark must survive Reload instead of flipping to
        # Invalid (a terminated cookie stays dead forever).
        try:
            from services.ban_guard import is_banned_status_text as _is_banned_status2

            if _is_banned_status2(
                str((record or {}).get("manual_status") or ""),
                str((record or {}).get("import_status") or ""),
            ):
                # Re-verify via the public API first: a previous mark must
                # survive, but one old false mark must never stick forever.
                _recheck = None
                try:
                    _rb, _rn, _ru = resolve_ban_for_dead_cookie(cookie, username)
                    _recheck = _rb
                except Exception:
                    _recheck = None
                if _recheck is False:
                    # Public API says the account is alive: drop the stale
                    # mark instead of preserving it.
                    record = dict(record or {})
                    record["manual_status"] = ""
                    record["import_status"] = ""
                    if update_store and username:
                        persist_cookie_verdict(username, "cleared_ban")
                else:
                    is_banned = True
                    if not detail:
                        detail = "cookie expired, ban mark kept"
        except Exception:
            pass
    if not is_banned and not is_locked and not is_recovery:
        # A previous Locked/Recovery mark survives a dead cookie: the cookie
        # API cannot prove the verification is cleared, so keep the mark
        # instead of demoting to Invalid.
        try:
            if _is_locked_status(
                str((record or {}).get("manual_status") or ""),
                str((record or {}).get("import_status") or ""),
                str((record or {}).get("last_error") or ""),
            ):
                is_locked = True
                if not detail:
                    detail = "cookie expired, locked mark kept"
            elif _is_recovery_status(
                str((record or {}).get("manual_status") or ""),
                str((record or {}).get("import_status") or ""),
                str((record or {}).get("last_error") or ""),
            ):
                is_recovery = True
                if not detail:
                    detail = "cookie expired, recovery mark kept"
        except Exception:
            pass
    if not ok:
        if update_store and username:
            if is_banned:
                persist_cookie_verdict(
                    username,
                    "banned",
                    cookie_username=str(
                        cookie_username
                        or (meta or {}).get("username")
                        or (record or {}).get("cookie_username")
                        or ""
                    ),
                    cookie_user_id=str(
                        (meta or {}).get("user_id") or (record or {}).get("cookie_user_id") or ""
                    ),
                )
            elif is_locked:
                persist_cookie_verdict(username, "locked")
            elif is_recovery:
                persist_cookie_verdict(username, "recovery")
            elif is_captcha_text(detail):
                persist_cookie_verdict(username, "captcha")
            else:
                persist_cookie_verdict(username, "invalid")
        if is_banned:
            return {
                "ok": False,
                "fatal": True,
                "banned": True,
                "msg": _BANNED_MSG,
                "detail": detail or _BANNED_MSG,
                "cookie_username": str(
                    cookie_username
                    or (meta or {}).get("username")
                    or (record or {}).get("cookie_username")
                    or ""
                ),
                "cookie_user_id": str(
                    (meta or {}).get("user_id") or (record or {}).get("cookie_user_id") or ""
                ),
            }
        if is_locked:
            return {
                "ok": False,
                "fatal": True,
                "locked_required": True,
                "msg": _LOCKED_MSG,
                "detail": detail or _LOCKED_MSG,
                "cookie_username": "",
                "cookie_user_id": "",
            }
        if is_recovery:
            return {
                "ok": False,
                "fatal": True,
                "recovery_required": True,
                "msg": _RECOVERY_MSG,
                "detail": detail or _RECOVERY_MSG,
                "cookie_username": "",
                "cookie_user_id": "",
            }
        if is_captcha_text(detail):
            return {
                "ok": False,
                "fatal": True,
                "captcha_required": True,
                "msg": CAPTCHA_BLOCK_REASON,
                "detail": detail,
                "cookie_username": "",
                "cookie_user_id": "",
            }
        return {"ok": False, "msg": detail, "cookie_username": "", "cookie_user_id": ""}
    cookie_user_id = str(meta.get("user_id") or "")
    mismatch = bool(username and cookie_username and username.lower() != cookie_username.lower())
    updates = cookie_verdict_updates(
        "identity",
        cookie_username=cookie_username,
        cookie_user_id=cookie_user_id,
        mismatch=mismatch,
    )
    if not mismatch:
        # Cookie is valid and not banned â€” clear a stale banned mark so an
        # unbanned / replaced cookie rejoins again without manual unmark.
        # Locked/Recovery marks are NOT cleared here: a valid cookie does not
        # prove face-scan / passkey verification is done (cookie stays 200
        # while the lock screen persists). They clear only on manual Resume.
        try:
            prev_manual = str((record or {}).get("manual_status") or "")
            prev_import = str((record or {}).get("import_status") or "")
            from services.ban_guard import is_banned_status_text as _is_banned_status

            if _is_banned_status(prev_manual, prev_import):
                updates["manual_status"] = ""
            # Preserve a previous Locked/Recovery hold across a valid cookie.
            try:
                if _is_locked_status(prev_manual, prev_import) or _is_recovery_status(prev_manual, prev_import):
                    updates["manual_status"] = prev_manual
                    updates["import_status"] = prev_import
            except Exception:
                pass
        except Exception:
            pass
    if update_store and username:
        ACCOUNT_STORE.update_record(username, updates)
    if mismatch:
        return {
            "ok": False,
            "msg": f"Cookie belongs to {cookie_username}, not {username}. Reimport the correct .ROBLOSECURITY for this account.",
            "cookie_username": cookie_username,
            "cookie_user_id": cookie_user_id,
            "cookie_mismatch": True,
        }
    return {"ok": True, "msg": "ok", "cookie_username": cookie_username, "cookie_user_id": cookie_user_id, "cookie_mismatch": False}
