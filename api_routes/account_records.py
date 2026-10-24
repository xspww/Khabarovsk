from __future__ import annotations

from types import SimpleNamespace
from typing import Any, NamedTuple

from core import (
    account_launch_block_reason,
    banned_block_reason,
    cookie_identity_block_reason,
    cookie_invalid_block_reason,
    flog_kv,
    suspended_block_reason,
)
from runtime.account_selection import runtime_account_allowlist
from services.account_hybrid import COOKIE_UNREADABLE_REASON
from services.account_reload import (
    load_accounts_from_store,
    mark_banned_cookie_record,
    mark_invalid_cookie_record,
    replace_farm_accounts,
)
from services.ban_guard import (
    BANNED_BLOCK_REASON,
    SUSPENDED_BLOCK_REASON,
    is_banned_status_text,
    is_banned_text,
    is_suspended_status_text,
    is_suspended_text,
)
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

from .settings_state import _apply_game_defaults, persist_config


def account_data_records(store: Any, include_cookies: bool = False) -> list[dict[str, Any]]:
    try:
        return store.read_records(include_cookies=include_cookies)
    except Exception as e:
        flog_kv("ACCOUNT_DATA", "read_failed", "warning", error=e)
        return []
def _runtime_accounts_by_user(farm: Any) -> dict[str, Any]:
    return {
        str(account.username or "").strip().lower(): account
        for account in farm._accounts
        if str(account.username or "").strip()
    }


def _status_reason(item: dict[str, Any], resolver: Any) -> str:
    reason = resolver(item.get("manual_status"), item.get("import_status"))
    if reason:
        return reason
    try:
        return resolver(item.get("blocked_reason", ""))
    except Exception:
        return ""


def _moderation_reasons(item: dict[str, Any]) -> tuple[str, str]:
    return (
        _status_reason(item, suspended_block_reason),
        _status_reason(item, banned_block_reason),
    )


def _cookie_identity_reason(item: dict[str, Any]) -> str:
    blocked_reason = cookie_identity_block_reason(
        str(item.get("username") or ""),
        str(item.get("cookie_username") or ""),
        bool(item.get("cookie_mismatch", False)),
    )
    invalid_reason = cookie_invalid_block_reason(
        item.get("manual_status"), item.get("import_status")
    )
    return invalid_reason or blocked_reason


def _locked_recovery_reason(item: dict[str, Any], moderation_reason: str) -> str:
    if is_locked_status_text(
        item.get("manual_status"),
        item.get("import_status"),
        item.get("blocked_reason", ""),
    ):
        if not moderation_reason:
            return LOCKED_BLOCK_REASON
    elif is_recovery_status_text(
        item.get("manual_status"),
        item.get("import_status"),
        item.get("blocked_reason", ""),
    ):
        if not moderation_reason:
            return RECOVERY_BLOCK_REASON
    return ""


def _captcha_reason(item: dict[str, Any], moderation_reason: str) -> str:
    if not is_captcha_status_text(
        item.get("manual_status"), item.get("import_status")
    ):
        return ""
    if moderation_reason:
        return ""
    if is_locked_status_text(item.get("manual_status"), item.get("import_status")):
        return ""
    if is_recovery_status_text(
        item.get("manual_status"), item.get("import_status")
    ):
        return ""
    return CAPTCHA_BLOCK_REASON


def _moderation_blocked_reason(item: dict[str, Any]) -> str:
    blocked_reason = _cookie_identity_reason(item)
    suspended_reason, banned_reason = _moderation_reasons(item)
    if suspended_reason:
        blocked_reason = SUSPENDED_BLOCK_REASON
    if banned_reason and not suspended_reason:
        blocked_reason = banned_reason
    moderation_reason = suspended_reason or banned_reason
    hold_reason = _locked_recovery_reason(item, moderation_reason)
    if hold_reason:
        blocked_reason = hold_reason
    captcha_reason = _captcha_reason(item, moderation_reason)
    if captcha_reason:
        blocked_reason = captcha_reason
    if not blocked_reason and item.get("cookie_state") == "unreadable":
        # Blob was sealed by another Windows profile/PC: the account is intact,
        # only the cookie has to be captured again.
        blocked_reason = COOKIE_UNREADABLE_REASON
    return blocked_reason


def _apply_runtime_record(
    farm: Any, item: dict[str, Any], runtime: Any, blocked_reason: str
) -> str:
    runtime_snapshot = runtime.runtime_snapshot()
    runtime_blocked = account_launch_block_reason(runtime)
    if runtime_blocked:
        blocked_reason = runtime_blocked
    item["state"] = runtime.state.name
    item["pid"] = runtime.pid
    item["runtime_state"] = runtime_snapshot.get("runtime_state") or str(
        runtime.runtime.lifecycle_state
    )
    item["can_rejoin"] = bool(
        farm.running
        and runtime.state.name != "FAILED"
        and not blocked_reason
    )
    item["can_kill"] = bool(runtime.pid)
    item["cookie_username"] = runtime.cookie_username or item.get(
        "cookie_username", ""
    )
    item["cookie_user_id"] = runtime.cookie_user_id or item.get(
        "cookie_user_id", ""
    )
    item["cookie_mismatch"] = bool(
        runtime.cookie_mismatch or item.get("cookie_mismatch", False)
    )
    return blocked_reason


def account_data_api_records(
    store: Any, farm: Any
) -> list[dict[str, Any]]:
    records = account_data_records(store, include_cookies=False)
    runtime_by_user = _runtime_accounts_by_user(farm)
    result: list[dict[str, Any]] = []
    for record in records:
        item = store.to_api_record(record)
        blocked_reason = _moderation_blocked_reason(item)
        runtime = runtime_by_user.get(
            str(item.get("username") or "").strip().lower()
        )
        if runtime:
            blocked_reason = _apply_runtime_record(
                farm, item, runtime, blocked_reason
            )
        item["blocked_reason"] = blocked_reason
        item["launchable"] = not bool(blocked_reason)
        result.append(item)
    return result
def replace_farm_accounts_from_store(ctx: Any, cfg_mgr: Any, farm: Any, store: Any) -> int:
    new_accounts = load_accounts_from_store(store)
    _apply_game_defaults(ctx, new_accounts, persist=False)
    return replace_farm_accounts(farm, cfg_mgr, new_accounts)
def clear_runtime_allowlist_after_reload(cfg_mgr: Any, farm: Any) -> dict[str, Any]:
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
def _resolve_moderated_cookie(
    cookie: str, username: str, detail: str, meta: dict[str, Any]
) -> tuple[bool, bool, str]:
    """Resolve a bare 'User is moderated' answer into locked-vs-banned.

    Returns (is_banned, is_locked, detail). Meta is updated in place with the
    resolved identity when the moderation probe returns one.
    """
    try:
        from services.roblox_identity import moderated_only_detail as _mod_only
        from services.roblox_identity import resolve_moderated_dead_cookie as _resolve_mod
    except Exception:
        _mod_only = lambda *a, **k: False
        _resolve_mod = lambda *a, **k: (None, "", "", "")
    try:
        _mod_hit = bool(_mod_only(detail))
    except Exception:
        _mod_hit = False
    if not _mod_hit:
        return False, False, detail
    try:
        _mv, _md, _mn, _mu = _resolve_mod(cookie, username)
    except Exception:
        _mv, _md, _mn, _mu = None, "", "", ""
    if _mv != "locked":
        return False, False, detail
    if _mn:
        meta["username"] = _mn
    if _mu:
        meta["user_id"] = _mu
    return True, True, _md or detail


class _CookieFlags(NamedTuple):
    suspended: bool
    banned: bool
    locked: bool
    recovery: bool


class _CookieScan:
    def __init__(self, store: Any, audit: Any) -> None:
        self.store = store
        self.audit = audit
        self.kept: list[dict[str, Any]] = []
        self.invalid: list[dict[str, str]] = []
        self.captcha: list[dict[str, str]] = []
        self.banned: list[dict[str, str]] = []
        self.suspended: list[dict[str, str]] = []
        self.locked: list[dict[str, str]] = []
        self.recovery: list[dict[str, str]] = []
        self.valid: list[dict[str, str]] = []

    def keep(self, record: dict[str, Any]) -> dict[str, Any]:
        self.kept.append(record)
        return record

    def reject_missing(self, record: dict[str, Any], label: str) -> None:
        reason = "missing cookie"
        self.keep(mark_invalid_cookie_record(self.store, record, reason))
        self.invalid.append({"username": label, "reason": reason})

    def keep_locked(self, record: Any, name: str, detail: str) -> None:
        normalized = self.store.normalize_record(record)
        normalized["manual_status"] = LOCKED_BLOCK_REASON
        normalized["import_status"] = LOCKED_REASON
        self.keep(normalized)
        self.locked.append(
            {"username": name, "reason": detail or LOCKED_REASON}
        )

    def keep_recovery(self, record: Any, name: str, detail: str) -> None:
        normalized = self.store.normalize_record(record)
        normalized["manual_status"] = RECOVERY_BLOCK_REASON
        normalized["import_status"] = RECOVERY_REASON
        self.keep(normalized)
        self.recovery.append(
            {"username": name, "reason": detail or RECOVERY_REASON}
        )

    def keep_captcha(self, record: Any, name: str, detail: str) -> None:
        normalized = self.store.normalize_record(record)
        normalized["manual_status"] = CAPTCHA_BLOCK_REASON
        normalized["import_status"] = CAPTCHA_REASON
        self.keep(normalized)
        self.captcha.append(
            {"username": name, "reason": detail or CAPTCHA_REASON}
        )

    def emit_audits(self) -> None:
        for event, items in (
            ("reload_cookie_invalid", self.invalid),
            ("reload_cookie_banned", self.banned),
            ("reload_cookie_suspended", self.suspended),
            ("reload_cookie_locked", self.locked),
            ("reload_cookie_recovery", self.recovery),
        ):
            for item in items:
                self.audit(
                    event,
                    item.get("username", ""),
                    False,
                    reason=item.get("reason", ""),
                )

    def summary(self, total: int) -> dict[str, Any]:
        return {
            "total": total,
            "kept": len(self.kept),
            "removed": 0,
            "invalid": len(self.invalid),
            "captcha": len(self.captcha),
            "banned": len(self.banned),
            "suspended": len(self.suspended),
            "locked": len(self.locked),
            "account_recovery": len(self.recovery),
            "valid_accounts": self.valid,
            "removed_accounts": [],
            "invalid_accounts": self.invalid,
            "captcha_accounts": self.captcha,
            "banned_accounts": self.banned,
            "suspended_accounts": self.suspended,
            "locked_accounts": self.locked,
            "recovery_accounts": self.recovery,
        }


def _record_identity(record: dict[str, Any]) -> tuple[str, str, str]:
    username = str(record.get("username") or "").strip()
    cookie = str(record.get("cookie") or "").strip()
    return username, cookie, username or "Unknown"


def _display_name(username: str, label: str) -> str:
    return username or label


def _meta_flag(meta: dict[str, Any], *keys: str) -> bool:
    return any(bool(meta.get(key)) for key in keys)


def _cookie_recovery_flag(
    detail: str, banned: bool, suspended: bool, locked: bool
) -> bool:
    return (
        bool(is_recovery_text(detail))
        and not banned
        and not suspended
        and not locked
    )


def _cookie_moderation_flags(
    meta: dict[str, Any], detail: str
) -> _CookieFlags:
    suspended = _meta_flag(meta, "is_suspended", "suspended") or bool(
        is_suspended_text(detail)
    )
    banned = (
        _meta_flag(meta, "is_banned", "banned") or bool(is_banned_text(detail))
    ) and not suspended
    locked = bool(is_locked_text(detail)) and not banned and not suspended
    recovery = _cookie_recovery_flag(detail, banned, suspended, locked)
    return _CookieFlags(suspended, banned, locked, recovery)


def _scan_cookie_record(
    scan: _CookieScan, record: dict[str, Any], validate_cookie: Any
) -> None:
    username, cookie, label = _record_identity(record)
    if not cookie:
        scan.reject_missing(record, label)
        return
    try:
        ok, cookie_username, detail, meta = validate_cookie(cookie)
    except Exception as exc:
        raise RuntimeError(
            f"Cookie validation unavailable for {label}: {exc}"
        ) from exc
    meta = meta or {}
    flags = _cookie_moderation_flags(meta, detail)
    if flags.banned and not flags.suspended:
        # Same disambiguation as the identity path: a bare
        # "User is moderated" must not label a locked account banned.
        banned, locked, detail = _resolve_moderated_cookie(
            cookie, username, detail, meta
        )
        flags = flags._replace(banned=banned, locked=locked)
    if not ok:
        _scan_dead_cookie(
            scan,
            record,
            username,
            cookie,
            label,
            detail,
            meta,
            cookie_username,
            flags,
        )
        return
    _scan_valid_cookie(scan, record, username, label, meta, cookie_username)


def _dead_captcha(flags: _CookieFlags, detail: str) -> bool:
    return (
        is_captcha_text(detail)
        and not flags.banned
        and not flags.suspended
        and not flags.locked
        and not flags.recovery
    )


def _preserve_blocked_identity(
    scan: _CookieScan,
    record: dict[str, Any],
    username: str,
    detail: str,
    meta: dict[str, Any],
    cookie_username: str,
) -> str:
    normalized = mark_banned_cookie_record(scan.store, record, detail)
    # Preserve identity so the card still shows who is blocked.
    validated_username = str(
        meta.get("username") or cookie_username or ""
    ).strip()
    if validated_username:
        normalized["cookie_username"] = validated_username
        normalized["username"] = username or validated_username
        username = username or validated_username
    normalized["cookie_user_id"] = str(
        meta.get("user_id") or normalized.get("cookie_user_id") or ""
    )
    scan.keep(normalized)
    return username


def _scan_moderated_cookie(
    scan: _CookieScan,
    record: dict[str, Any],
    username: str,
    label: str,
    detail: str,
    meta: dict[str, Any],
    cookie_username: str,
    flags: _CookieFlags,
) -> None:
    username = _preserve_blocked_identity(
        scan, record, username, detail, meta, cookie_username
    )
    if flags.suspended:
        scan.suspended.append(
            {
                "username": _display_name(username, label),
                "reason": detail or SUSPENDED_BLOCK_REASON,
            }
        )
    else:
        scan.banned.append(
            {
                "username": _display_name(username, label),
                "reason": detail or BANNED_BLOCK_REASON,
            }
        )


def _scan_kept_suspension(
    scan: _CookieScan,
    record: dict[str, Any],
    username: str,
    label: str,
    detail: str,
) -> None:
    normalized = mark_banned_cookie_record(
        scan.store, record, detail or "cookie expired, mark kept"
    )
    scan.keep(normalized)
    if is_suspended_status_text(
        normalized.get("manual_status"), normalized.get("import_status")
    ):
        scan.suspended.append(
            {
                "username": _display_name(username, label),
                "reason": detail or SUSPENDED_BLOCK_REASON,
            }
        )
    else:
        scan.banned.append(
            {
                "username": _display_name(username, label),
                "reason": detail or BANNED_BLOCK_REASON,
            }
        )


def _recheck_dead_ban(cookie: str, username: str) -> bool | None:
    try:
        from services.roblox_identity import resolve_ban_for_dead_cookie

        recheck, _name, _uid = resolve_ban_for_dead_cookie(cookie, username)
        return recheck
    except Exception:
        return None


def _scan_kept_ban(
    scan: _CookieScan,
    record: dict[str, Any],
    username: str,
    cookie: str,
    label: str,
    detail: str,
) -> None:
    recheck = _recheck_dead_ban(cookie, username)
    if recheck is False:
        # Public API says the account is alive: drop the stale
        # ban mark instead of preserving it.
        normalized = scan.store.normalize_record(record)
        normalized["manual_status"] = ""
        normalized["import_status"] = "cookie_invalid"
        normalized["cookie_mismatch"] = False
        scan.keep(normalized)
        scan.invalid.append(
            {
                "username": _display_name(username, label),
                "reason": detail or "invalid cookie",
            }
        )
        return
    # Cookie is dead so the state cannot be re-confirmed — keep the
    # previous mark instead of flipping to Invalid.
    normalized = mark_banned_cookie_record(
        scan.store, record, detail or "cookie expired, mark kept"
    )
    scan.keep(normalized)
    scan.banned.append(
        {
            "username": _display_name(username, label),
            "reason": detail or BANNED_BLOCK_REASON,
        }
    )


def _scan_kept_moderation_marks(
    scan: _CookieScan,
    record: dict[str, Any],
    username: str,
    cookie: str,
    label: str,
    detail: str,
) -> bool:
    if is_suspended_status_text(
        record.get("manual_status"), record.get("import_status")
    ):
        _scan_kept_suspension(scan, record, username, label, detail)
        return True
    if is_banned_status_text(
        record.get("manual_status"), record.get("import_status")
    ):
        _scan_kept_ban(scan, record, username, cookie, label, detail)
        return True
    if is_locked_status_text(
        record.get("manual_status"), record.get("import_status")
    ):
        scan.keep_locked(record, _display_name(username, label), detail)
        return True
    if is_recovery_status_text(
        record.get("manual_status"), record.get("import_status")
    ):
        scan.keep_recovery(record, _display_name(username, label), detail)
        return True
    return False


def _dead_ban_by_name(cookie: str, username: str) -> bool:
    try:
        from services.roblox_identity import resolve_ban_for_dead_cookie

        banned, _name, _uid = resolve_ban_for_dead_cookie(cookie, username)
        return banned is True
    except Exception:
        return False


def _scan_dead_by_name(
    scan: _CookieScan,
    record: dict[str, Any],
    username: str,
    cookie: str,
    label: str,
    detail: str,
) -> None:
    banned_by_name = False
    if username:
        banned_by_name = _dead_ban_by_name(cookie, username)
    if banned_by_name:
        ban_detail = "Account banned (Roblox isBanned=true)"
        normalized = mark_banned_cookie_record(scan.store, record, ban_detail)
        if not username:
            username = label
        scan.keep(normalized)
        scan.banned.append({"username": username, "reason": ban_detail})
        return
    reason = detail or "invalid cookie"
    scan.keep(mark_invalid_cookie_record(scan.store, record, reason))
    scan.invalid.append({"username": label, "reason": reason})


def _scan_dead_cookie(
    scan: _CookieScan,
    record: dict[str, Any],
    username: str,
    cookie: str,
    label: str,
    detail: str,
    meta: dict[str, Any],
    cookie_username: str,
    flags: _CookieFlags,
) -> None:
    if flags.locked:
        scan.keep_locked(record, label, detail)
        return
    if flags.recovery:
        scan.keep_recovery(record, label, detail)
        return
    if _dead_captcha(flags, detail):
        scan.keep_captcha(record, label, detail)
        return
    if flags.banned or flags.suspended:
        _scan_moderated_cookie(
            scan,
            record,
            username,
            label,
            detail,
            meta,
            cookie_username,
            flags,
        )
        return
    if _scan_kept_moderation_marks(scan, record, username, cookie, label, detail):
        return
    _scan_dead_by_name(scan, record, username, cookie, label, detail)


def _cookie_mismatch(username: str, validated_username: str) -> bool:
    return bool(
        validated_username
        and username
        and username.lower() != validated_username.lower()
    )


def _keep_locked_or_recovery(
    scan: _CookieScan,
    record: dict[str, Any],
    username: str,
    label: str,
    normalized: dict[str, Any],
) -> bool:
    prev_locked = is_locked_status_text(
        record.get("manual_status"), record.get("import_status")
    )
    prev_rec = is_recovery_status_text(
        record.get("manual_status"), record.get("import_status")
    )
    if not (prev_locked or prev_rec) or normalized["cookie_mismatch"]:
        return False
    name = _display_name(username, label)
    normalized["manual_status"] = str(record.get("manual_status") or "")
    normalized["import_status"] = str(record.get("import_status") or "")
    scan.keep(normalized)
    if prev_locked:
        scan.locked.append(
            {"username": name, "reason": "locked mark kept (cookie still valid)"}
        )
    else:
        scan.recovery.append(
            {
                "username": name,
                "reason": "recovery mark kept (cookie still valid)",
            }
        )
    scan.valid.append({"username": name})
    return True


def _stale_status_present(normalized: dict[str, Any]) -> bool:
    return (
        is_captcha_status_text(normalized.get("manual_status"))
        or cookie_invalid_block_reason(normalized.get("manual_status"))
        or is_banned_status_text(
            normalized.get("manual_status"), normalized.get("import_status")
        )
    )


def _clear_stale_status(
    normalized: dict[str, Any], record: dict[str, Any]
) -> None:
    if not _stale_status_present(normalized):
        return
    if normalized["cookie_mismatch"]:
        return
    normalized["manual_status"] = ""
    if is_banned_status_text(
        record.get("manual_status"), record.get("import_status")
    ):
        normalized["import_status"] = ""


def _scan_valid_cookie(
    scan: _CookieScan,
    record: dict[str, Any],
    username: str,
    label: str,
    meta: dict[str, Any],
    cookie_username: str,
) -> None:
    normalized = scan.store.normalize_record(record)
    validated_username = str(
        meta.get("username") or cookie_username or ""
    ).strip()
    if not username and validated_username:
        normalized["username"] = validated_username
        username = validated_username
    normalized["cookie_username"] = validated_username
    normalized["cookie_user_id"] = str(meta.get("user_id") or "")
    normalized["cookie_mismatch"] = _cookie_mismatch(
        username, validated_username
    )
    normalized["import_status"] = (
        "cookie_mismatch" if normalized["cookie_mismatch"] else ""
    )
    # Preserve Locked/Recovery across a valid cookie: cookie 200 does not
    # prove face-scan / passkey verification is done.
    if _keep_locked_or_recovery(scan, record, username, label, normalized):
        return
    _clear_stale_status(normalized, record)
    scan.keep(normalized)
    scan.valid.append({"username": _display_name(username, label)})


def validate_cookie_records_from_store(
    store: Any, validate_cookie: Any, audit: Any
) -> dict[str, Any]:
    records = store.read_records(include_cookies=True)
    scan = _CookieScan(store, audit)
    for record in records:
        _scan_cookie_record(scan, record, validate_cookie)
    store.write_records(scan.kept)
    scan.emit_audits()
    flog_kv(
        "ACCOUNT_DATA",
        "reload_cookie_validation",
        kept=len(scan.kept),
        invalid=len(scan.invalid),
        banned=len(scan.banned),
        suspended=len(scan.suspended),
        locked=len(scan.locked),
        account_recovery=len(scan.recovery),
        total=len(records),
    )
    return scan.summary(len(records))
def find_account_record(store: Any, username: str, include_cookie: bool = True) -> dict[str, Any] | None:
    wanted = str(username or "").strip().lower()
    for record in account_data_records(store, include_cookies=include_cookie):
        if str(record.get("username") or "").strip().lower() == wanted:
            return record
    return None
def import_cookie_validator(cookie: str, validate_cookie: Any):
    ok, username, detail, meta = validate_cookie(cookie)
    return ok, username, detail, meta
