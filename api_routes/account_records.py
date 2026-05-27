from __future__ import annotations
from typing import Any, Dict, List, Optional
from core import account_launch_block_reason, cookie_identity_block_reason, cookie_invalid_block_reason, flog_kv
from services.account_reload import load_accounts_from_store, mark_invalid_cookie_record, replace_farm_accounts
from services.captcha_guard import (
    CAPTCHA_BLOCK_REASON,
    CAPTCHA_REASON,
    is_captcha_status_text,
    is_captcha_text,
)
from runtime.account_selection import runtime_account_allowlist
from .settings_state import _apply_game_defaults
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
        if is_captcha_status_text(item.get("manual_status"), item.get("import_status")):
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
    cfg_mgr.update({"runtime_account_allowlist": []})
    cfg_mgr.save()
    if hasattr(farm, "apply_config_snapshot"):
        farm.apply_config_snapshot()
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
        if not ok:
            if is_captcha_text(detail):
                normalized = store.normalize_record(record)
                normalized["manual_status"] = CAPTCHA_BLOCK_REASON
                normalized["import_status"] = CAPTCHA_REASON
                kept.append(normalized)
                captcha.append({"username": label, "reason": detail or CAPTCHA_REASON})
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
        if (
            is_captcha_status_text(normalized.get("manual_status"))
            or cookie_invalid_block_reason(normalized.get("manual_status"))
        ) and not normalized["cookie_mismatch"]:
            normalized["manual_status"] = ""
        kept.append(normalized)
        valid.append({"username": username or label})
    store.write_records(kept)
    for item in invalid:
        audit("reload_cookie_invalid", item.get("username", ""), False, reason=item.get("reason", ""))
    flog_kv(
        "ACCOUNT_DATA",
        "reload_cookie_validation",
        kept=len(kept),
        invalid=len(invalid),
        total=len(records),
    )
    return {
        "total": len(records),
        "kept": len(kept),
        "removed": 0,
        "invalid": len(invalid),
        "captcha": len(captcha),
        "valid_accounts": valid,
        "removed_accounts": [],
        "invalid_accounts": invalid,
        "captcha_accounts": captcha,
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
