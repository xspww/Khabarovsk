from __future__ import annotations

from typing import Any, Dict, List, Optional

from core import Account, AccountState, flog_kv
from services.auth_gate import (
    AuthGateDecision,
    evaluate_account_auth_gate,
    mark_account_auth_quarantined,
)
from services.captcha_guard import (
    CAPTCHA_BLOCK_REASON,
    CAPTCHA_REASON,
    is_account_captcha_required,
    set_account_captcha_hold,
)


class AccountReconciliationError(ValueError):
    pass


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


def _validated_replacement_accounts(accounts: List[Account]) -> List[Account]:
    seen: Dict[str, str] = {}
    validated: List[Account] = []
    for index, account in enumerate(accounts):
        key = _runtime_account_key(account)
        label = str(getattr(account, "username", "") or getattr(account, "_config_username", "") or "").strip()
        if not key:
            raise AccountReconciliationError(f"Account at index {index} is missing a username")
        previous = seen.get(key)
        if previous is not None:
            current = label or key
            raise AccountReconciliationError(f"Duplicate account username '{current}' conflicts with '{previous}'")
        seen[key] = label or key
        validated.append(account)
    return validated


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


def _record_runtime_snapshot(farm: Any, account: Account) -> None:
    try:
        farm._runtime_store.record_account_snapshot(account._config_username, account.runtime_snapshot())
    except Exception as e:
        flog_kv("RUNTIME", "store_snapshot_failed", "warning", account=account.display_name, error=e)


def _sync_runtime_account_owners(farm: Any) -> None:
    if getattr(farm, "_recovery", None):
        farm._recovery._accounts = farm._accounts
    if getattr(farm, "_maintenance", None):
        farm._maintenance._accounts = farm._accounts
        farm._maintenance._workers = farm._workers
    dispatcher = getattr(farm, "_dispatcher", None)
    if dispatcher:
        dispatcher._workers = farm._workers
        launcher = getattr(dispatcher, "_launcher", None)
        if launcher is not None:
            launcher._accounts = farm._accounts


def _fail_runtime_block(farm: Any, account: Account, decision: AuthGateDecision) -> None:
    mark_account_auth_quarantined(account, decision, source="reload_cookies", runtime_writer=getattr(farm, "_runtime_state", None))
    if getattr(farm, "_recovery", None):
        farm._recovery.fail_account(account, decision.reason_key, decision.reason)


def _retire_removed_runtime_account(farm: Any, account: Account) -> None:
    key = _runtime_account_key(account)
    worker = getattr(farm, "_workers", {}).pop(key, None)
    if worker:
        try:
            worker.wake()
        except Exception:
            pass
    scheduler = getattr(farm, "_runtime_scheduler", None)
    if scheduler:
        try:
            scheduler.cancel(f"recovery:{account._config_username}", reason="reload_cookies_removed")
        except Exception:
            pass

    state_writer = getattr(farm, "_state_mgr", None) or getattr(farm, "_runtime_state", None)
    runtime_state = getattr(farm, "_runtime_state", None)
    with account._lock:
        pid = account.pid
        runtime_generation = account.runtime_generation
        if runtime_state:
            runtime_state.set_desired(account, AccountState.IDLE, reason="reload_cookies_removed", increment_generation=True)
            if hasattr(runtime_state, "clear_recovery"):
                runtime_state.clear_recovery(account, reason="reload_cookies_removed", inflight=False)
            runtime_state.set_cooldown(account, 0.0, reason="reload_cookies_removed")
        else:
            from runtime.runtime_state_manager import RuntimeStateManager

            RuntimeStateManager(logger=flog_kv).set_desired(
                account,
                AccountState.IDLE,
                reason="reload_cookies_removed",
                increment_generation=True,
            )

    if pid and state_writer:
        try:
            from services.process_service import ProcessService

            ProcessService.safe_kill_bound_process(
                account,
                state_writer,
                reason="reload_cookies_removed",
                expected_runtime_generation=runtime_generation,
            )
        except Exception as e:
            flog_kv("ACCOUNT_DATA", "removed_account_kill_failed", "warning", account=account.display_name, error=e)
    if getattr(farm, "_state_mgr", None):
        try:
            farm._state_mgr.transition(account, AccountState.IDLE, reason="reload_cookies_removed", force=True)
        except Exception:
            pass
    _record_runtime_snapshot(farm, account)
    if hasattr(farm, "_push_event"):
        farm._push_event(
            "cookie",
            f"Reload Cookies removed runtime account: {account.display_name}",
            account=account,
            severity="warning",
            reason="reload_cookies_removed",
        )


def _prepare_added_runtime_account(farm: Any, account: Account) -> bool:
    runtime_state = getattr(farm, "_runtime_state", None)
    if runtime_state:
        with account._lock:
            runtime_state.set_desired(account, AccountState.IN_GAME, reason="reload_cookies_added", increment_generation=False)
            runtime_state.set_cooldown(account, 0.0, reason="reload_cookies_added")
    orchestrator = getattr(farm, "_runtime_orchestrator", None)
    if orchestrator:
        try:
            orchestrator.request_start_epoch(account, reason="reload_cookies_added")
        except Exception:
            pass
    if account.vip_links:
        try:
            from services.vip_tracker import VipTracker

            account._vip_tracker = VipTracker(account.vip_links)
        except Exception:
            pass

    auth_gate = evaluate_account_auth_gate(account)
    if auth_gate.blocked:
        _fail_runtime_block(farm, account, auth_gate)
        return False
    return True


def _start_added_worker(farm: Any, account: Account) -> bool:
    if not getattr(farm, "_state_mgr", None) or not getattr(farm, "_recovery", None):
        return False
    workers = getattr(farm, "_workers", None)
    if workers is None:
        return False
    worker = workers.get(account._config_username)
    if worker and worker.is_alive():
        worker.wake()
        return True
    try:
        from runtime.account_worker import AccountWorker

        worker = AccountWorker(
            acc=account,
            state_mgr=farm._state_mgr,
            bus=farm.bus,
            cfg=farm.cfg_mgr.snapshot(),
            recovery=farm._recovery,
            stop=farm._stop,
            supervisor=getattr(farm, "_supervisor", None),
            accounts=farm._accounts,
        )
        workers[account._config_username] = worker
        worker.start()
        return True
    except Exception as e:
        flog_kv("ACCOUNT_DATA", "reload_added_worker_failed", "warning", account=account.display_name, error=e)
        return False


def _sync_running_farm_accounts(farm: Any, cfg_mgr: Any, new_accounts: List[Account]) -> int:
    runtime_accounts = {
        _runtime_account_key(account): account
        for account in farm._accounts
        if _runtime_account_key(account)
    }
    next_keys = {
        _runtime_account_key(account)
        for account in new_accounts
        if _runtime_account_key(account)
    }
    synced = 0
    added = 0
    removed = 0
    workers_started = 0
    captcha_synced = 0
    resumed = 0
    reconciled: List[Account] = []

    for account in list(farm._accounts):
        key = _runtime_account_key(account)
        if key and key not in next_keys:
            removed += 1
            _retire_removed_runtime_account(farm, account)

    for fresh in new_accounts:
        account = runtime_accounts.get(_runtime_account_key(fresh))
        if not account:
            account = fresh
            reconciled.append(account)
            added += 1
            if _prepare_added_runtime_account(farm, account) and _start_added_worker(farm, account):
                workers_started += 1
            _record_runtime_snapshot(farm, account)
            continue
        reconciled.append(account)
        was_captcha = is_account_captcha_required(account)
        now_captcha = is_account_captcha_required(fresh)
        _sync_existing_runtime_account(account, fresh)
        synced += 1
        if now_captcha:
            captcha_synced += 1
            set_account_captcha_hold(account, fresh.manual_status or CAPTCHA_BLOCK_REASON, source="reload_cookies", runtime_writer=farm._runtime_state)
            if farm._recovery:
                farm._recovery.fail_account(account, CAPTCHA_REASON, CAPTCHA_BLOCK_REASON)
        else:
            auth_gate = evaluate_account_auth_gate(account)
            if auth_gate.blocked:
                _fail_runtime_block(farm, account, auth_gate)
            elif was_captcha:
                ok, _ = farm.resume_captcha_account(account._config_username)
                if ok:
                    resumed += 1
        _record_runtime_snapshot(farm, account)

    farm._accounts[:] = reconciled
    _sync_runtime_account_owners(farm)
    cfg_mgr.save_accounts(farm._accounts)
    if hasattr(farm, "_bump_status_revision"):
        farm._bump_status_revision()
    flog_kv(
        "ACCOUNT_DATA",
        "reload_synced_running_farm",
        accounts=len(new_accounts),
        runtime_synced=synced,
        runtime_added=added,
        runtime_removed=removed,
        workers_started=workers_started,
        captcha=captcha_synced,
        resumed=resumed,
    )
    return len(new_accounts)


def replace_farm_accounts(farm: Any, cfg_mgr: Any, new_accounts: List[Account]) -> int:
    new_accounts = _validated_replacement_accounts(new_accounts)
    if farm.running:
        return _sync_running_farm_accounts(farm, cfg_mgr, new_accounts)
    farm.set_accounts(new_accounts)
    cfg_mgr.save_accounts(new_accounts)
    return len(new_accounts)
