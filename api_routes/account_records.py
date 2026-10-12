from __future__ import annotations
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from core import account_launch_block_reason, banned_block_reason, cookie_identity_block_reason, cookie_invalid_block_reason, flog_kv, locked_block_reason, recovery_block_reason, suspended_block_reason
from services.account_reload import load_accounts_from_store, mark_banned_cookie_record, mark_invalid_cookie_record, replace_farm_accounts
from services.captcha_guard import (
    CAPTCHA_BLOCK_REASON,
    CAPTCHA_REASON,
    is_captcha_status_text,
    is_captcha_text,
)
from services.verify_guard import (
    LOCKED_BLOCK_REASON,
    LOCKED_REASON,
    RECOVERY_BLOCK_REASON,
    RECOVERY_REASON,
    is_locked_status_text,
    is_locked_text,
    is_recovery_status_text,
    is_recovery_text,
)
from services.ban_guard import (
    BANNED_BLOCK_REASON,
    BANNED_IMPORT_STATUS,
    SUSPENDED_BLOCK_REASON,
    is_banned_status_text,
    is_banned_text,
    is_suspended_status_text,
    is_suspended_text,
)
from runtime.account_selection import runtime_account_allowlist
from .settings_state import _apply_game_defaults, persist_config
def account_data_records(store: Any, include_cookies: bool = False) -> List[Dict[str, Any]]:
    try:
        return store.read_records(include_cookies=include_cookies)
    except Exception as e:
        flog_kv("ACCOUNT_DATA", "read_failed", "warning", error=e)
        return []
def account_data_api_records(store: Any, farm: Any) -> List[Dict[str, Any]]:
    records = account_data_records(store, include_cookies=False)
    runtime_by_user = {
        str(account.username or "").strip().lower(): account
        for account in farm._accounts
        if str(account.username or "").strip()
    }
    result: List[Dict[str, Any]] = []
    for record in records:
        item = store.to_api_record(record)
        blocked_reason = cookie_identity_block_reason(
            str(item.get("username") or ""),
            str(item.get("cookie_username") or ""),
            bool(item.get("cookie_mismatch", False)),
        )
        invalid_reason = cookie_invalid_block_reason(item.get("manual_status"), item.get("import_status"))
        if invalid_reason:
            blocked_reason = invalid_reason
        suspended_reason = suspended_block_reason(
            item.get("manual_status"), item.get("import_status"),
        )
        if not suspended_reason:
            try:
                suspended_reason = suspended_block_reason(item.get("blocked_reason", ""))
            except Exception:
                suspended_reason = ""
        if suspended_reason:
            blocked_reason = SUSPENDED_BLOCK_REASON
        banned_reason = banned_block_reason(
            item.get("manual_status"), item.get("import_status"),
        )
        if not banned_reason:
            try:
                banned_reason = banned_block_reason(item.get("blocked_reason", ""))
            except Exception:
                banned_reason = ""
        if banned_reason and not suspended_reason:
            blocked_reason = banned_reason
        moderation_reason = suspended_reason or banned_reason
        # Locked (face/ID) and Recovery (passkey/2FA) outrank captcha.
        if is_locked_status_text(item.get("manual_status"), item.get("import_status"), item.get("blocked_reason", "")):
            if not moderation_reason:
                blocked_reason = LOCKED_BLOCK_REASON
        elif is_recovery_status_text(item.get("manual_status"), item.get("import_status"), item.get("blocked_reason", "")):
            if not moderation_reason:
                blocked_reason = RECOVERY_BLOCK_REASON
        if is_captcha_status_text(item.get("manual_status"), item.get("import_status")):
            # Suspended/Banned/Locked/Recovery outrank captcha.
            if not moderation_reason and not is_locked_status_text(item.get("manual_status"), item.get("import_status")) and not is_recovery_status_text(item.get("manual_status"), item.get("import_status")):
                blocked_reason = CAPTCHA_BLOCK_REASON
        runtime = runtime_by_user.get(str(item.get("username") or "").strip().lower())
        if runtime:
            runtime_snapshot = runtime.runtime_snapshot()
            runtime_blocked = account_launch_block_reason(runtime)
            if runtime_blocked:
                blocked_reason = runtime_blocked
            item["state"] = runtime.state.name
            item["pid"] = runtime.pid
            item["runtime_state"] = runtime_snapshot.get("runtime_state") or str(runtime.runtime.lifecycle_state)
            item["can_rejoin"] = bool(farm.running and runtime.state.name != "FAILED" and not blocked_reason)
            item["can_kill"] = bool(runtime.pid)
            item["cookie_username"] = runtime.cookie_username or item.get("cookie_username", "")
            item["cookie_user_id"] = runtime.cookie_user_id or item.get("cookie_user_id", "")
            item["cookie_mismatch"] = bool(runtime.cookie_mismatch or item.get("cookie_mismatch", False))
        item["blocked_reason"] = blocked_reason
        item["launchable"] = not bool(blocked_reason)
        result.append(item)
    return result
def replace_farm_accounts_from_store(ctx: Any, cfg_mgr: Any, farm: Any, store: Any) -> int:
    new_accounts = load_accounts_from_store(store)
    _apply_game_defaults(ctx, new_accounts, persist=False)
    return replace_farm_accounts(farm, cfg_mgr, new_accounts)
def clear_runtime_allowlist_after_reload(cfg_mgr: Any, farm: Any) -> Dict[str, Any]:
    current = runtime_account_allowlist({
        "runtime_account_allowlist": cfg_mgr.get("runtime_account_allowlist", [])
    })
    if not current:
        return {"allowlist_cleared": False, "allowlist_cleared_count": 0}
    persist_config(
        SimpleNamespace(cfg_mgr=cfg_mgr, farm=farm),
        {"runtime_account_allowlist": []},
        apply_snapshot=True,
        strict_snapshot=True,
    )
    if hasattr(farm, "_push_event"):
        farm._push_event(
            "system",
            f"Reload Cookies cleared account test lock: {len(current)} account(s)",
            severity="info",
            reason="reload_cookies_clear_allowlist",
            cleared_count=len(current),
        )
    return {"allowlist_cleared": True, "allowlist_cleared_count": len(current)}
def validate_cookie_records_from_store(store: Any, validate_cookie: Any, audit: Any) -> Dict[str, Any]:
    records = store.read_records(include_cookies=True)
    kept: List[Dict[str, Any]] = []
    invalid: List[Dict[str, str]] = []
    captcha: List[Dict[str, str]] = []
    banned: List[Dict[str, str]] = []
    suspended: List[Dict[str, str]] = []
    locked: List[Dict[str, str]] = []
    account_recovery: List[Dict[str, str]] = []
    valid: List[Dict[str, str]] = []
    for record in records:
        username = str(record.get("username") or "").strip()
        cookie = str(record.get("cookie") or "").strip()
        label = username or "Unknown"
        if not cookie:
            reason = "missing cookie"
            kept.append(mark_invalid_cookie_record(store, record, reason))
            invalid.append({"username": label, "reason": reason})
            continue
        try:
            ok, cookie_username, detail, meta = validate_cookie(cookie)
        except Exception as exc:
            raise RuntimeError(f"Cookie validation unavailable for {label}: {exc}") from exc
        meta = meta or {}
        is_suspended = bool(meta.get("is_suspended") or meta.get("suspended")) or bool(is_suspended_text(detail))
        is_banned = (
            bool(meta.get("is_banned") or meta.get("banned")) or bool(is_banned_text(detail))
        ) and not is_suspended
        # Locked / recovery from cookie API detail (outrank captcha, never ban).
        is_locked = bool(is_locked_text(detail)) and not is_banned and not is_suspended
        is_rec = bool(is_recovery_text(detail)) and not is_banned and not is_suspended and not is_locked
        if is_banned and not is_suspended:
            # Same disambiguation as the identity path: a bare
            # "User is moderated" must not label a locked account banned.
            try:
                from roblox_hybrid import moderated_only_detail as _mod_only
                from roblox_hybrid import resolve_moderated_dead_cookie as _resolve_mod
            except Exception:
                _mod_only = lambda *a, **k: False  # type: ignore
                _resolve_mod = lambda *a, **k: (None, "", "", "")  # type: ignore
            try:
                _mod_hit = bool(_mod_only(detail))
            except Exception:
                _mod_hit = False
            if _mod_hit:
                try:
                    _mv, _md, _mn, _mu = _resolve_mod(cookie, username)
                except Exception:
                    _mv, _md, _mn, _mu = None, "", "", ""
                if _mv == "locked":
                    is_banned = False
                    is_locked = True
                    detail = _md or detail
                    if _mn:
                        meta["username"] = _mn
                    if _mu:
                        meta["user_id"] = _mu
        if not ok:
            if is_locked:
                normalized = store.normalize_record(record)
                normalized["manual_status"] = LOCKED_BLOCK_REASON
                normalized["import_status"] = LOCKED_REASON
                kept.append(normalized)
                locked.append({"username": label, "reason": detail or LOCKED_REASON})
                continue
            if is_rec:
                normalized = store.normalize_record(record)
                normalized["manual_status"] = RECOVERY_BLOCK_REASON
                normalized["import_status"] = RECOVERY_REASON
                kept.append(normalized)
                account_recovery.append({"username": label, "reason": detail or RECOVERY_REASON})
                continue
            if is_captcha_text(detail) and not is_banned and not is_suspended and not is_locked and not is_rec:
                normalized = store.normalize_record(record)
                normalized["manual_status"] = CAPTCHA_BLOCK_REASON
                normalized["import_status"] = CAPTCHA_REASON
                kept.append(normalized)
                captcha.append({"username": label, "reason": detail or CAPTCHA_REASON})
                continue
            if is_banned or is_suspended:
                normalized = mark_banned_cookie_record(store, record, detail)
                # Preserve identity so the card still shows who is blocked.
                validated_username = str(meta.get("username") or cookie_username or "").strip()
                if validated_username:
                    normalized["cookie_username"] = validated_username
                    if not username:
                        normalized["username"] = validated_username
                        username = validated_username
                normalized["cookie_user_id"] = str(meta.get("user_id") or normalized.get("cookie_user_id") or "")
                kept.append(normalized)
                if is_suspended:
                    suspended.append({"username": username or label, "reason": detail or SUSPENDED_BLOCK_REASON})
                else:
                    banned.append({"username": username or label, "reason": detail or BANNED_BLOCK_REASON})
                continue
            # A suspension can never be re-checked on a dead cookie, so it
            # is always preserved as-is. A pure ban mark is re-verified
            # first so one old false mark can never stick forever.
            # (Note: the banned matcher also matches suspended text, so the
            # suspension check must come first.)
            if is_suspended_status_text(record.get("manual_status"), record.get("import_status")):
                normalized = mark_banned_cookie_record(store, record, detail or "cookie expired, mark kept")
                kept.append(normalized)
                if is_suspended_status_text(normalized.get("manual_status"), normalized.get("import_status")):
                    suspended.append({"username": username or label, "reason": detail or SUSPENDED_BLOCK_REASON})
                else:
                    banned.append({"username": username or label, "reason": detail or BANNED_BLOCK_REASON})
                continue
            if is_banned_status_text(record.get("manual_status"), record.get("import_status")):
                _recheck = None
                try:
                    from roblox_hybrid import resolve_ban_for_dead_cookie as _resolve_keep

                    _rb, _rn, _ru = _resolve_keep(cookie, username)
                    _recheck = _rb
                except Exception:
                    _recheck = None
                if _recheck is False:
                    # Public API says the account is alive: drop the stale
                    # ban mark instead of preserving it.
                    normalized = store.normalize_record(record)
                    normalized["manual_status"] = ""
                    normalized["import_status"] = "cookie_invalid"
                    normalized["cookie_mismatch"] = False
                    kept.append(normalized)
                    invalid.append({"username": username or label, "reason": detail or "invalid cookie"})
                    continue
                # Cookie is dead so the state cannot be re-confirmed — keep the
                # previous mark instead of flipping to Invalid.
                normalized = mark_banned_cookie_record(store, record, detail or "cookie expired, mark kept")
                kept.append(normalized)
                banned.append({"username": username or label, "reason": detail or BANNED_BLOCK_REASON})
                continue
            if is_locked_status_text(record.get("manual_status"), record.get("import_status")):
                # Dead cookie cannot prove verification cleared — keep Locked.
                normalized = store.normalize_record(record)
                normalized["manual_status"] = LOCKED_BLOCK_REASON
                normalized["import_status"] = LOCKED_REASON
                kept.append(normalized)
                locked.append({"username": username or label, "reason": detail or LOCKED_BLOCK_REASON})
                continue
            if is_recovery_status_text(record.get("manual_status"), record.get("import_status")):
                normalized = store.normalize_record(record)
                normalized["manual_status"] = RECOVERY_BLOCK_REASON
                normalized["import_status"] = RECOVERY_REASON
                kept.append(normalized)
                account_recovery.append({"username": username or label, "reason": detail or RECOVERY_BLOCK_REASON})
                continue
            # Cookie is dead and there is no previous Banned mark. The cookie
            # cannot confirm anything, but the public API still can — check
            # ban by username so a banned account reads as Banned instead of
            # being demoted to Invalid.
            banned_by_name = False
            ban_detail = ""
            if username:
                try:
                    from roblox_hybrid import resolve_ban_for_dead_cookie as _resolve_dead

                    _b, _name, _uid = _resolve_dead(cookie, username)
                    if _b is True:
                        banned_by_name = True
                        ban_detail = "Account banned (Roblox isBanned=true)"
                except Exception:
                    banned_by_name = False
            if banned_by_name:
                normalized = mark_banned_cookie_record(store, record, ban_detail)
                if not username:
                    username = label
                kept.append(normalized)
                banned.append({"username": username, "reason": ban_detail or BANNED_BLOCK_REASON})
                continue
            reason = detail or "invalid cookie"
            kept.append(mark_invalid_cookie_record(store, record, reason))
            invalid.append({"username": label, "reason": reason})
            continue
        normalized = store.normalize_record(record)
        validated_username = str(meta.get("username") or cookie_username or "").strip()
        if not username and validated_username:
            normalized["username"] = validated_username
            username = validated_username
        normalized["cookie_username"] = validated_username
        normalized["cookie_user_id"] = str(meta.get("user_id") or "")
        normalized["cookie_mismatch"] = bool(
            validated_username
            and username
            and username.lower() != validated_username.lower()
        )
        normalized["import_status"] = "cookie_mismatch" if normalized["cookie_mismatch"] else ""
        # Preserve Locked/Recovery across a valid cookie: cookie 200 does not
        # prove face-scan / passkey verification is done.
        prev_locked = is_locked_status_text(record.get("manual_status"), record.get("import_status"))
        prev_rec = is_recovery_status_text(record.get("manual_status"), record.get("import_status"))
        if (prev_locked or prev_rec) and not normalized["cookie_mismatch"]:
            normalized["manual_status"] = str(record.get("manual_status") or "")
            normalized["import_status"] = str(record.get("import_status") or "")
            kept.append(normalized)
            if prev_locked:
                locked.append({"username": username or label, "reason": "locked mark kept (cookie still valid)"})
            else:
                account_recovery.append({"username": username or label, "reason": "recovery mark kept (cookie still valid)"})
            valid.append({"username": username or label})
            continue
        if (
            is_captcha_status_text(normalized.get("manual_status"))
            or cookie_invalid_block_reason(normalized.get("manual_status"))
            or is_banned_status_text(normalized.get("manual_status"), normalized.get("import_status"))
        ) and not normalized["cookie_mismatch"]:
            normalized["manual_status"] = ""
            if is_banned_status_text(record.get("manual_status"), record.get("import_status")):
                normalized["import_status"] = ""
        kept.append(normalized)
        valid.append({"username": username or label})
    store.write_records(kept)
    for item in invalid:
        audit("reload_cookie_invalid", item.get("username", ""), False, reason=item.get("reason", ""))
    for item in banned:
        audit("reload_cookie_banned", item.get("username", ""), False, reason=item.get("reason", ""))
    for item in suspended:
        audit("reload_cookie_suspended", item.get("username", ""), False, reason=item.get("reason", ""))
    for item in locked:
        audit("reload_cookie_locked", item.get("username", ""), False, reason=item.get("reason", ""))
    for item in account_recovery:
        audit("reload_cookie_recovery", item.get("username", ""), False, reason=item.get("reason", ""))
    flog_kv(
        "ACCOUNT_DATA",
        "reload_cookie_validation",
        kept=len(kept),
        invalid=len(invalid),
        banned=len(banned),
        suspended=len(suspended),
        locked=len(locked),
        account_recovery=len(account_recovery),
        total=len(records),
    )
    return {
        "total": len(records),
        "kept": len(kept),
        "removed": 0,
        "invalid": len(invalid),
        "captcha": len(captcha),
        "banned": len(banned),
        "suspended": len(suspended),
        "locked": len(locked),
        "account_recovery": len(account_recovery),
        "valid_accounts": valid,
        "removed_accounts": [],
        "invalid_accounts": invalid,
        "captcha_accounts": captcha,
        "banned_accounts": banned,
        "suspended_accounts": suspended,
        "locked_accounts": locked,
        "recovery_accounts": account_recovery,
    }
def find_account_record(store: Any, username: str, include_cookie: bool = True) -> Optional[Dict[str, Any]]:
    wanted = str(username or "").strip().lower()
    for record in account_data_records(store, include_cookies=include_cookie):
        if str(record.get("username") or "").strip().lower() == wanted:
            return record
    return None
def import_cookie_validator(cookie: str, validate_cookie: Any):
    ok, username, detail, meta = validate_cookie(cookie)
    return ok, username, detail, meta
