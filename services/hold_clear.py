"""Shared helpers for surgically clearing per-account hold fields.

Locked, captcha and recovery holds all clear the same bookkeeping fields but
use different status predicates, reason constants and runtime writers. Keeping
the shared sequence here prevents the three clear paths from drifting apart.
"""

from __future__ import annotations

from typing import Any

from services._util import _lower


def clear_status_field(account: Any, attr: str, predicate: Any) -> None:
    if predicate(getattr(account, attr, "")):
        setattr(account, attr, "")


def clear_exact_reason(account: Any, attr: str, reason: str) -> None:
    if _lower(getattr(account, attr, "")) == reason:
        setattr(account, attr, "")


def reset_session_fields(account: Any) -> None:
    account.recovery_scheduled_at = 0.0
    account.session_checked = False
    account.session_valid = False
    account.session_wait_started_at = 0.0


def reset_retry_counters(account: Any) -> None:
    account.retry_count = 0
    account.fail_count = 0
    account.launch_fail_count = 0
    account.crash_retry_count = 0
    account.network_retry_count = 0
    account.session_retry_count = 0


def clear_runtime_failure(account: Any) -> None:
    try:
        account.runtime.last_error = ""
        account.runtime.recovery_reason = ""
        account.runtime.recovery_active = False
    except Exception:
        "intentional: ignore exception"


def run_under_account_lock(account: Any, action: Any) -> None:
    lock = getattr(account, "_lock", None)
    if lock:
        with lock:
            action()
    else:
        action()


def clear_hold_fields(account: Any, status_predicate: Any, reason: str) -> None:
    clear_status_field(account, "manual_status", status_predicate)
    clear_status_field(account, "last_error", status_predicate)
    clear_exact_reason(account, "last_crash_reason", reason)
    clear_exact_reason(account, "last_recovery_reason", reason)
    clear_status_field(account, "last_state_reason", status_predicate)
    clear_status_field(account, "import_status", status_predicate)


def clear_account_hold(
    account: Any,
    runtime_writer: Any,
    was_held: bool,
    persist: Any,
    status_predicate: Any,
    reason: str,
    apply_runtime_clear: Any,
    runtime_reason: str,
    extra_clear: Any = None,
) -> bool:
    def _clear() -> None:
        clear_hold_fields(account, status_predicate, reason)
        reset_session_fields(account)
        if extra_clear is not None:
            extra_clear(account)
        reset_retry_counters(account)
        apply_runtime_clear(account, runtime_writer, runtime_reason)
        clear_runtime_failure(account)

    run_under_account_lock(account, _clear)
    if was_held:
        persist(account, active=False)
    return was_held
