from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NamedTuple

from core import Account, AccountState, flog_kv
from services.auth_gate import (
    AuthGateDecision,
    evaluate_account_auth_gate,
    mark_account_auth_quarantined,
)
from services.ban_guard import (
    BANNED_BLOCK_REASON,
    BANNED_REASON,
    SUSPENDED_BLOCK_REASON,
    SUSPENDED_REASON,
    is_account_banned,
    is_account_suspended,
)
from services.captcha_guard import (
    CAPTCHA_BLOCK_REASON,
    CAPTCHA_REASON,
    is_account_captcha_required,
    set_account_captcha_hold,
)
from services.verify_guard import (
    LOCKED_BLOCK_REASON,
    LOCKED_REASON,
    RECOVERY_BLOCK_REASON,
    RECOVERY_REASON,
    is_account_locked,
    is_account_recovery,
    set_account_locked_hold,
    set_account_recovery_hold,
)


class AccountReconciliationError(ValueError):
    pass


COOKIE_INVALID_IMPORT_STATUS = "cookie_invalid"


def mark_banned_cookie_record(account_store: Any, record: dict[str, Any], detail: str = "") -> dict[str, Any]:
    from services.ban_guard import (
        BANNED_BLOCK_REASON,
        BANNED_IMPORT_STATUS,
        SUSPENDED_BLOCK_REASON,
        SUSPENDED_IMPORT_STATUS,
        is_suspended_status_text,
        is_suspended_text,
    )

    normalized = account_store.normalize_record(record)
    # Keep an existing suspension a suspension — a dead cookie cannot tell the
    # two apart, so never silently promote it to a permanent ban.
    suspended = is_suspended_text(detail) or is_suspended_status_text(
        (record or {}).get("manual_status"), (record or {}).get("import_status")
    )
    normalized["manual_status"] = SUSPENDED_BLOCK_REASON if suspended else BANNED_BLOCK_REASON
    normalized["import_status"] = SUSPENDED_IMPORT_STATUS if suspended else BANNED_IMPORT_STATUS
    normalized["cookie_mismatch"] = False
    return normalized


def mark_invalid_cookie_record(account_store: Any, record: dict[str, Any], reason: str) -> dict[str, Any]:
    normalized = account_store.normalize_record(record)
    normalized["manual_status"] = f"Invalid Cookie: {reason or 'invalid cookie'}"
    normalized["import_status"] = COOKIE_INVALID_IMPORT_STATUS
    normalized["cookie_username"] = ""
    normalized["cookie_user_id"] = ""
    normalized["cookie_mismatch"] = False
    return normalized


def load_accounts_from_store(account_store: Any) -> list[Account]:
    return [Account.from_dict(item) for item in account_store.to_cronus_accounts()]


def _runtime_account_key(account: Account) -> str:
    return str(account._config_username or account.username or "").strip().lower()


def _find_runtime_account(farm: Any, username: str) -> Account | None:
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


def _validated_replacement_accounts(accounts: list[Account]) -> list[Account]:
    seen: dict[str, str] = {}
    validated: list[Account] = []
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


class _ReloadBatches(NamedTuple):
    valid: list[dict[str, Any]]
    captcha: list[dict[str, Any]]
    banned: list[dict[str, Any]]
    suspended: list[dict[str, Any]]
    locked: list[dict[str, Any]]
    recovery: list[dict[str, Any]]
    invalid: list[dict[str, Any]]


class _ReloadCounts(NamedTuple):
    valid: int
    invalid: int
    captcha: int
    banned: int
    suspended: int
    locked: int
    account_recovery: int


def _reload_batches(validation: dict[str, Any]) -> _ReloadBatches:
    return _ReloadBatches(
        valid=list(validation.get("valid_accounts") or []),
        captcha=list(validation.get("captcha_accounts") or []),
        banned=list(validation.get("banned_accounts") or []),
        suspended=list(validation.get("suspended_accounts") or []),
        locked=list(validation.get("locked_accounts") or []),
        recovery=list(
            validation.get("recovery_accounts")
            or validation.get("account_recovery_accounts")
            or []
        ),
        invalid=list(
            validation.get("invalid_accounts")
            or validation.get("removed_accounts")
            or []
        ),
    )


def _or_count(value: Any, fallback: int = 0) -> int:
    return int(value or fallback or 0)


def _invalid_count(validation: dict[str, Any]) -> int:
    raw = validation.get("invalid")
    if raw is None:
        raw = validation.get("removed") or 0
    return int(raw)


def _reload_counts(
    validation: dict[str, Any],
    batches: _ReloadBatches,
) -> _ReloadCounts:
    return _ReloadCounts(
        valid=len(batches.valid),
        invalid=_invalid_count(validation),
        captcha=_or_count(validation.get("captcha")),
        banned=_or_count(validation.get("banned"), len(batches.banned)),
        suspended=_or_count(validation.get("suspended"), len(batches.suspended)),
        locked=_or_count(validation.get("locked"), len(batches.locked)),
        account_recovery=_or_count(
            validation.get("account_recovery"),
            _or_count(validation.get("recovery"), len(batches.recovery)),
        ),
    )


def _reload_summary_level(counts: _ReloadCounts) -> str:
    blocked = (
        counts.invalid
        or counts.captcha
        or counts.banned
        or counts.suspended
        or counts.locked
        or counts.account_recovery
    )
    return "warning" if blocked else "success"


def _reload_summary_text(counts: _ReloadCounts) -> str:
    summary = (
        f"Reload Cookies checked: {counts.valid} valid, "
        f"{counts.captcha} CAPTCHA, {counts.invalid} invalid"
    )
    if counts.locked:
        summary += f", {counts.locked} locked"
    if counts.account_recovery:
        summary += f", {counts.account_recovery} recovery"
    if counts.suspended:
        summary += f", {counts.suspended} suspended"
    if counts.banned:
        summary += f", {counts.banned} banned"
    return summary


def _push_reload_summary(farm: Any, counts: _ReloadCounts) -> None:
    summary_level = _reload_summary_level(counts)
    summary = _reload_summary_text(counts)
    farm._push_event(
        "cookie",
        summary,
        severity=summary_level,
        reason="reload_cookies",
        valid=counts.valid,
        captcha=counts.captcha,
        invalid=counts.invalid,
        banned=counts.banned,
        suspended=counts.suspended,
        locked=counts.locked,
        account_recovery=counts.account_recovery,
    )


def _push_reload_event(
    farm: Any,
    item: dict[str, Any],
    channel: str,
    message: str,
    severity: str,
    reason: str,
    default_reason: str | None,
) -> None:
    username = str(item.get("username") or "Unknown")
    if default_reason is None:
        farm._push_event(
            channel,
            message.format(username=username),
            account=_find_runtime_account(farm, username),
            severity=severity,
            reason=reason,
        )
        return
    detail = str(item.get("reason") or default_reason)
    farm._push_event(
        channel,
        message.format(username=username, reason=detail),
        account=_find_runtime_account(farm, username),
        severity=severity,
        reason=reason,
        detail=detail,
    )


def _push_reload_batch(
    farm: Any,
    items: list[dict[str, Any]],
    channel: str,
    message: str,
    severity: str,
    reason: str,
    default_reason: str | None = None,
) -> None:
    for item in items:
        _push_reload_event(
            farm,
            item,
            channel,
            message,
            severity,
            reason,
            default_reason,
        )


def emit_reload_cookie_events(farm: Any, validation: dict[str, Any]) -> None:
    batches = _reload_batches(validation)
    counts = _reload_counts(validation, batches)
    _push_reload_summary(farm, counts)
    _push_reload_batch(
        farm,
        batches.valid,
        "cookie",
        "Reload Cookies OK: {username}",
        "success",
        "cookie_valid",
    )
    _push_reload_batch(
        farm,
        batches.captcha,
        "captcha",
        "Reload Cookies CAPTCHA: {username} - solve manually",
        "warn",
        CAPTCHA_REASON,
        CAPTCHA_REASON,
    )
    _push_reload_batch(
        farm,
        batches.locked,
        "verify",
        "Reload Cookies Locked: {username} - verify face/ID manually",
        "warn",
        LOCKED_REASON,
        LOCKED_REASON,
    )
    _push_reload_batch(
        farm,
        batches.recovery,
        "verify",
        "Reload Cookies Recovery: {username} - complete passkey/2FA manually",
        "warn",
        RECOVERY_REASON,
        RECOVERY_REASON,
    )
    _push_reload_batch(
        farm,
        batches.banned,
        "cookie",
        "Reload Cookies banned: {username} - {reason}",
        "error",
        "banned",
        "banned",
    )
    _push_reload_batch(
        farm,
        batches.suspended,
        "cookie",
        "Reload Cookies suspended: {username} - {reason}",
        "error",
        "suspended",
        "suspended",
    )
    _push_reload_batch(
        farm,
        batches.invalid,
        "cookie",
        "Reload Cookies invalid: {username} - {reason}",
        "error",
        "cookie_invalid",
        "invalid cookie",
    )


def _sync_existing_runtime_account(target: Account, source: Account) -> None:
    with target._lock:
        target.user_id = source.user_id
        target.priority = source.priority
        target.place_id = source.place_id
        target.game_id = source.game_id
        target.vip_links = list(source.vip_links or [])
        target.alias = source.alias
        target.cookie = source.cookie
        target.browser_tracker_id = source.browser_tracker_id
        target.cookie_username = source.cookie_username
        target.cookie_user_id = source.cookie_user_id
        target.cookie_mismatch = source.cookie_mismatch
        target.description = source.description
        target.manual_status = source.manual_status
        target.import_status = source.import_status
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


def _wake_removed_worker(farm: Any, key: str) -> None:
    worker = getattr(farm, "_workers", {}).pop(key, None)
    if worker:
        try:
            worker.wake()
        except Exception:
            "intentional: ignore exception"


def _cancel_removed_scheduler(farm: Any, account: Account) -> None:
    scheduler = getattr(farm, "_runtime_scheduler", None)
    if scheduler:
        try:
            scheduler.cancel(
                f"recovery:{account._config_username}",
                reason="reload_cookies_removed",
            )
        except Exception:
            "intentional: ignore exception"


def _reset_removed_account_state(farm: Any, account: Account) -> None:
    state_writer = getattr(farm, "_state_mgr", None) or getattr(
        farm,
        "_runtime_state",
        None,
    )
    runtime_state = getattr(farm, "_runtime_state", None)
    with account._lock:
        pid = account.pid
        runtime_generation = account.runtime_generation
        if runtime_state:
            runtime_state.set_desired(
                account,
                AccountState.IDLE,
                reason="reload_cookies_removed",
                increment_generation=True,
            )
            if hasattr(runtime_state, "clear_recovery"):
                runtime_state.clear_recovery(
                    account,
                    reason="reload_cookies_removed",
                    inflight=False,
                )
            runtime_state.set_cooldown(
                account,
                0.0,
                reason="reload_cookies_removed",
            )
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
            flog_kv(
                "ACCOUNT_DATA",
                "removed_account_kill_failed",
                "warning",
                account=account.display_name,
                error=e,
            )


def _transition_removed_account(farm: Any, account: Account) -> None:
    if not getattr(farm, "_state_mgr", None):
        return
    try:
        farm._state_mgr.transition(
            account,
            AccountState.IDLE,
            reason="reload_cookies_removed",
            force=True,
        )
    except Exception:
        "intentional: ignore exception"


def _retire_removed_runtime_account(farm: Any, account: Account) -> None:
    key = _runtime_account_key(account)
    _wake_removed_worker(farm, key)
    _cancel_removed_scheduler(farm, account)
    _reset_removed_account_state(farm, account)
    _transition_removed_account(farm, account)
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
            "intentional: ignore exception"
    if account.vip_links:
        try:
            from services.vip_tracker import VipTracker

            account._vip_tracker = VipTracker(account.vip_links)
        except Exception:
            "intentional: ignore exception"

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


def _restart_cleared_worker(farm: Any, account: Any) -> tuple[int, int]:
    """Restart a dead worker (or nudge a live one) after a ban/suspend cleared.

    Returns (workers_started, resumed) deltas.
    """
    try:
        workers = getattr(farm, "_workers", None)
        worker = workers.get(account._config_username) if workers else None
        alive = bool(worker.is_alive()) if worker is not None else False
    except Exception:
        worker = None
        alive = False
    if not alive:
        if _start_added_worker(farm, account):
            return 1, 1
        try:
            orchestrator = getattr(farm, "_runtime_orchestrator", None)
            request_evaluate = getattr(orchestrator, "request_evaluate", None)
            if request_evaluate is not None:
                request_evaluate(account, trigger="banned_cleared")
                return 0, 1
        except Exception:
            "intentional: ignore exception"
        return 0, 0
    try:
        worker.wake()
    except Exception:
        "intentional: ignore exception"
    try:
        orchestrator = getattr(farm, "_runtime_orchestrator", None)
        request_evaluate = getattr(orchestrator, "request_evaluate", None)
        if request_evaluate is not None:
            request_evaluate(account, trigger="banned_cleared")
    except Exception:
        "intentional: ignore exception"
    return 0, 1


def _resume_after_hold_cleared(
    farm: Any, account: Any, was_captcha: bool, was_verify: bool, was_moderated: bool
) -> tuple[int, int]:
    """Resume or restart one account whose hold cleared; returns (started, resumed)."""
    auth_gate = evaluate_account_auth_gate(account)
    if auth_gate.blocked:
        _fail_runtime_block(farm, account, auth_gate)
        return 0, 0
    if was_captcha:
        ok, _ = farm.resume_captcha_account(account._config_username)
        return 0, 1 if ok else 0
    if was_verify:
        try:
            ok, _ = farm.resume_verify_account(account._config_username)
        except Exception:
            ok = False
        return 0, 1 if ok else 0
    if was_moderated:
        return _restart_cleared_worker(farm, account)
    return 0, 0


class _HoldFlags(NamedTuple):
    was_captcha: bool
    was_verify: bool
    was_moderated: bool
    now_suspended: bool
    now_banned: bool
    now_locked: bool
    now_recovery: bool
    now_captcha: bool


class _HoldDeltas(NamedTuple):
    banned: int = 0
    verify: int = 0
    captcha: int = 0
    workers_started: int = 0
    resumed: int = 0


@dataclass
class _SyncCounts:
    synced: int = 0
    added: int = 0
    removed: int = 0
    workers_started: int = 0
    captcha: int = 0
    banned: int = 0
    verify: int = 0
    resumed: int = 0


def _runtime_account_map(farm: Any) -> dict[str, Account]:
    return {
        _runtime_account_key(account): account
        for account in farm._accounts
        if _runtime_account_key(account)
    }


def _next_account_keys(new_accounts: list[Account]) -> set[str]:
    return {
        _runtime_account_key(account)
        for account in new_accounts
        if _runtime_account_key(account)
    }


def _hold_flags(account: Account, fresh: Account) -> _HoldFlags:
    was_captcha = is_account_captcha_required(account)
    now_captcha = is_account_captcha_required(fresh)
    was_locked = is_account_locked(account)
    now_locked = is_account_locked(fresh)
    was_recovery = is_account_recovery(account)
    now_recovery = is_account_recovery(fresh)
    was_verify = was_locked or was_recovery
    was_banned = is_account_banned(account)
    was_suspended = is_account_suspended(account)
    was_moderated = was_banned or was_suspended
    now_suspended = is_account_suspended(fresh)
    now_banned = is_account_banned(fresh) and not now_suspended
    return _HoldFlags(
        was_captcha=was_captcha,
        was_verify=was_verify,
        was_moderated=was_moderated,
        now_suspended=now_suspended,
        now_banned=now_banned,
        now_locked=now_locked,
        now_recovery=now_recovery,
        now_captcha=now_captcha,
    )


def _kill_account_for_hold(farm: Any, account: Account, reason: str) -> None:
    try:
        with account._lock:
            pid = account.pid
            generation = account.runtime_generation
    except Exception:
        pid, generation = None, 0
    if pid:
        try:
            from services.process_service import ProcessService

            ProcessService.safe_kill_bound_process(
                account,
                farm._state_mgr,
                reason=reason,
                expected_runtime_generation=generation,
            )
        except Exception:
            "intentional: ignore exception"


def _fail_account_hold(
    farm: Any,
    account: Account,
    reason_key: str,
    block_msg: str,
) -> None:
    if farm._recovery:
        farm._recovery.fail_account(account, reason_key, block_msg)


def _apply_hold(
    farm: Any,
    account: Account,
    fresh: Account,
    *,
    setter: Any,
    block_reason: str,
    kill_reason: str,
    fail_reason: str,
    fail_block: str,
    kill: bool = True,
) -> None:
    setter(
        account,
        fresh.manual_status or block_reason,
        source="reload_cookies",
        runtime_writer=farm._runtime_state,
    )
    if kill:
        _kill_account_for_hold(farm, account, kill_reason)
    _fail_account_hold(farm, account, fail_reason, fail_block)


def _apply_existing_hold(
    farm: Any,
    account: Account,
    fresh: Account,
    flags: _HoldFlags,
) -> _HoldDeltas:
    if flags.now_suspended:
        from services.ban_guard import set_account_suspended_hold

        _apply_hold(
            farm,
            account,
            fresh,
            setter=set_account_suspended_hold,
            block_reason=SUSPENDED_BLOCK_REASON,
            kill_reason="reload_suspended",
            fail_reason=SUSPENDED_REASON,
            fail_block=SUSPENDED_BLOCK_REASON,
        )
        return _HoldDeltas(banned=1)
    if flags.now_banned:
        from services.ban_guard import set_account_banned_hold

        _apply_hold(
            farm,
            account,
            fresh,
            setter=set_account_banned_hold,
            block_reason=BANNED_BLOCK_REASON,
            kill_reason="reload_banned",
            fail_reason=BANNED_REASON,
            fail_block=BANNED_BLOCK_REASON,
        )
        return _HoldDeltas(banned=1)
    if flags.now_locked:
        _apply_hold(
            farm,
            account,
            fresh,
            setter=set_account_locked_hold,
            block_reason=LOCKED_BLOCK_REASON,
            kill_reason="reload_locked",
            fail_reason=LOCKED_REASON,
            fail_block=LOCKED_BLOCK_REASON,
        )
        return _HoldDeltas(verify=1)
    if flags.now_recovery:
        _apply_hold(
            farm,
            account,
            fresh,
            setter=set_account_recovery_hold,
            block_reason=RECOVERY_BLOCK_REASON,
            kill_reason="reload_recovery",
            fail_reason=RECOVERY_REASON,
            fail_block=RECOVERY_BLOCK_REASON,
        )
        return _HoldDeltas(verify=1)
    if flags.now_captcha:
        _apply_hold(
            farm,
            account,
            fresh,
            setter=set_account_captcha_hold,
            block_reason=CAPTCHA_BLOCK_REASON,
            kill_reason="reload_captcha",
            fail_reason=CAPTCHA_REASON,
            fail_block=CAPTCHA_BLOCK_REASON,
            kill=False,
        )
        return _HoldDeltas(captcha=1)
    workers_started, resumed = _resume_after_hold_cleared(
        farm,
        account,
        flags.was_captcha,
        flags.was_verify,
        flags.was_moderated,
    )
    return _HoldDeltas(
        workers_started=workers_started,
        resumed=resumed,
    )


def _reconcile_removed_accounts(farm: Any, next_keys: set[str]) -> int:
    removed = 0
    for account in list(farm._accounts):
        key = _runtime_account_key(account)
        if key and key not in next_keys:
            removed += 1
            _retire_removed_runtime_account(farm, account)
    return removed


def _reconcile_added_account(
    farm: Any,
    account: Account,
    reconciled: list[Account],
) -> int:
    reconciled.append(account)
    workers_started = 0
    if _prepare_added_runtime_account(farm, account) and _start_added_worker(
        farm,
        account,
    ):
        workers_started = 1
    _record_runtime_snapshot(farm, account)
    return workers_started


def _reconcile_existing_account(
    farm: Any,
    account: Account,
    fresh: Account,
    reconciled: list[Account],
) -> _HoldDeltas:
    reconciled.append(account)
    flags = _hold_flags(account, fresh)
    _sync_existing_runtime_account(account, fresh)
    deltas = _apply_existing_hold(farm, account, fresh, flags)
    _record_runtime_snapshot(farm, account)
    return deltas


def _log_reload_sync(new_count: int, counts: _SyncCounts) -> None:
    flog_kv(
        "ACCOUNT_DATA",
        "reload_synced_running_farm",
        accounts=new_count,
        runtime_synced=counts.synced,
        runtime_added=counts.added,
        runtime_removed=counts.removed,
        workers_started=counts.workers_started,
        captcha=counts.captcha,
        banned=counts.banned,
        verify=counts.verify,
        resumed=counts.resumed,
    )


def _sync_running_farm_accounts(
    farm: Any,
    cfg_mgr: Any,
    new_accounts: list[Account],
) -> int:
    runtime_accounts = _runtime_account_map(farm)
    next_keys = _next_account_keys(new_accounts)
    counts = _SyncCounts()
    counts.removed = _reconcile_removed_accounts(farm, next_keys)
    reconciled: list[Account] = []

    for fresh in new_accounts:
        account = runtime_accounts.get(_runtime_account_key(fresh))
        if not account:
            counts.added += 1
            counts.workers_started += _reconcile_added_account(
                farm,
                fresh,
                reconciled,
            )
            continue
        counts.synced += 1
        deltas = _reconcile_existing_account(
            farm,
            account,
            fresh,
            reconciled,
        )
        counts.banned += deltas.banned
        counts.verify += deltas.verify
        counts.captcha += deltas.captcha
        counts.workers_started += deltas.workers_started
        counts.resumed += deltas.resumed

    farm._accounts[:] = reconciled
    _sync_runtime_account_owners(farm)
    cfg_mgr.save_accounts(farm._accounts)
    if hasattr(farm, "_bump_status_revision"):
        farm._bump_status_revision()
    _log_reload_sync(len(new_accounts), counts)
    return len(new_accounts)


def replace_farm_accounts(farm: Any, cfg_mgr: Any, new_accounts: list[Account]) -> int:
    new_accounts = _validated_replacement_accounts(new_accounts)
    if farm.running:
        return _sync_running_farm_accounts(farm, cfg_mgr, new_accounts)
    farm.set_accounts(new_accounts)
    cfg_mgr.save_accounts(new_accounts)
    return len(new_accounts)
