from __future__ import annotations

from typing import Any, Dict, List, Optional

from core import Account, flog_kv
from services.captcha_guard import (
    CAPTCHA_BLOCK_REASON,
    CAPTCHA_REASON,
    is_account_captcha_required,
    is_captcha_text,
    set_account_captcha_hold,
)


def load_accounts_from_store(account_store: Any) -> List[Account]:
    return [Account.from_dict(item) for item in account_store.to_roboguard_accounts()]


def _runtime_account_key(account: Account) -> str:
    return str(account._config_username or account.username or "").strip().lower()


def _find_runtime_account(farm: Any, username: str) -> Optional[Account]:
    wanted = str(username or "").strip().lower()
    if not wanted:
        return None
    for account in farm._accounts:
        names = (
            account._config_username,
            account.username,
            account.cookie_username,
            account.display_name,
        )
        if any(str(name or "").strip().lower() == wanted for name in names):
            return account
    return None


def emit_reload_cookie_events(farm: Any, validation: Dict[str, Any]) -> None:
    valid_accounts = list(validation.get("valid_accounts") or [])
    captcha_accounts = list(validation.get("captcha_accounts") or [])
    removed_accounts = list(validation.get("removed_accounts") or [])
    valid_count = len(valid_accounts)
    removed = int(validation.get("removed") or 0)
    captcha = int(validation.get("captcha") or 0)
    summary_level = "warning" if (removed or captcha) else "success"
    farm._push_event(
        "cookie",
        f"Reload Cookies checked: {valid_count} valid, {captcha} CAPTCHA, {removed} invalid",
        severity=summary_level,
        reason="reload_cookies",
        valid=valid_count,
        captcha=captcha,
        invalid=removed,
    )
    for item in valid_accounts:
        username = str(item.get("username") or "Unknown")
        farm._push_event(
            "cookie",
            f"Reload Cookies OK: {username}",
            account=_find_runtime_account(farm, username),
            severity="success",
            reason="cookie_valid",
        )
    for item in captcha_accounts:
        username = str(item.get("username") or "Unknown")
        detail = str(item.get("reason") or CAPTCHA_REASON)
        farm._push_event(
            "captcha",
            f"Reload Cookies CAPTCHA: {username} - solve manually",
            account=_find_runtime_account(farm, username),
            severity="warn",
            reason=CAPTCHA_REASON,
            detail=detail,
        )
    for item in removed_accounts:
        username = str(item.get("username") or "Unknown")
        reason = str(item.get("reason") or "invalid cookie")
        farm._push_event(
            "cookie",
            f"Reload Cookies invalid: {username} - {reason}",
            account=_find_runtime_account(farm, username),
            severity="error",
            reason="cookie_invalid",
            detail=reason,
        )


def _sync_existing_runtime_account(target: Account, source: Account) -> None:
    with target._lock:
        target.user_id = source.user_id
        target.priority = source.priority
        target.place_id = source.place_id
        target.vip_links = list(source.vip_links or [])
        target.alias = source.alias
        target.cookie = source.cookie
        target.browser_tracker_id = source.browser_tracker_id
        target.cookie_username = source.cookie_username
        target.cookie_user_id = source.cookie_user_id
        target.cookie_mismatch = source.cookie_mismatch
        target.description = source.description
        target.manual_status = source.manual_status
        target.finished_at = source.finished_at
        target.sync_runtime("reload_cookies_sync")


def _sync_running_farm_accounts(farm: Any, cfg_mgr: Any, new_accounts: List[Account]) -> int:
    runtime_accounts = {
        _runtime_account_key(account): account
        for account in farm._accounts
        if _runtime_account_key(account)
    }
    synced = 0
    captcha_synced = 0
    resumed = 0
    for fresh in new_accounts:
        account = runtime_accounts.get(_runtime_account_key(fresh))
        if not account:
            continue
        was_captcha = is_account_captcha_required(account)
        now_captcha = is_captcha_text(fresh.manual_status)
        _sync_existing_runtime_account(account, fresh)
        synced += 1
        if now_captcha:
            captcha_synced += 1
            set_account_captcha_hold(account, fresh.manual_status or CAPTCHA_BLOCK_REASON, source="reload_cookies", runtime_writer=farm._runtime_state)
            if farm._recovery:
                farm._recovery.fail_account(account, CAPTCHA_REASON, CAPTCHA_BLOCK_REASON)
        elif was_captcha:
            ok, _ = farm.resume_captcha_account(account._config_username)
            if ok:
                resumed += 1
        try:
            farm._runtime_store.record_account_snapshot(account._config_username, account.runtime_snapshot())
        except Exception as e:
            flog_kv("RUNTIME", "store_snapshot_failed", "warning", account=account.display_name, error=e)

    cfg_mgr.save_accounts(farm._accounts)
    if hasattr(farm, "_bump_status_revision"):
        farm._bump_status_revision()
    flog_kv(
        "ACCOUNT_DATA",
        "reload_synced_running_farm",
        accounts=len(new_accounts),
        runtime_synced=synced,
        captcha=captcha_synced,
        resumed=resumed,
    )
    return len(new_accounts)


def replace_farm_accounts(farm: Any, cfg_mgr: Any, new_accounts: List[Account]) -> int:
    if farm.running:
        return _sync_running_farm_accounts(farm, cfg_mgr, new_accounts)
    farm.set_accounts(new_accounts)
    cfg_mgr.save_accounts(new_accounts)
    return len(new_accounts)
