from __future__ import annotations

import time
import threading
from typing import Any, Callable, Dict, List, Tuple

from runtime.recovery_context import (
    RecoveryAttemptContext,
    SESSION_CONFLICT,
    normalize_disconnect_category,
    priority_for_signal,
)


REASON_ALIASES = {
    "pid_dead": "process_crash",
    "not_responding": "watchdog_timeout",
    "watchdog_low_resource": "watchdog_timeout",
    "security_kick": "connection_error",
    "unexpected_client_behavior": "connection_error",
    "idle_disconnect": "connection_error",
    "cookie_invalid": "auth_failure",
    "cookie_missing": "auth_failure",
    "cookie_mismatch": "auth_failure",
    "multi_roblox_guard_failed": "multi_roblox_guard_failed",
    "launch_verify_timeout": "watchdog_timeout",
}


RECOVERY_POLICIES = {
    "process_crash": {"bucket": "crash", "cap": 25.0, "fatal": False},
    "network_drop": {"bucket": "network", "cap": 12.0, "fatal": False},
    "connection_error": {"bucket": "network", "cap": 15.0, "fatal": False},
    "teleport_timeout": {"bucket": "crash", "cap": 30.0, "fatal": False},
    "loading_freeze": {"bucket": "crash", "cap": 45.0, "fatal": False},
    "watchdog_timeout": {"bucket": "crash", "cap": 35.0, "fatal": False},
    "auth_failure": {"bucket": "session", "cap": 0.0, "fatal": True},
    "account_launched_elsewhere": {"bucket": "session", "cap": 0.0, "fatal": True},
    "session_conflict": {"bucket": "session", "cap": 20.0, "fatal": False},
    "multi_roblox_guard_failed": {"bucket": "session", "cap": 0.0, "fatal": True},
    "server_full": {"bucket": "launch", "cap": 20.0, "fatal": False},
    "launch_fail": {"bucket": "launch", "cap": 45.0, "fatal": False},
}


def canonical_reason(reason_key: str) -> str:
    reason_key = str(reason_key or "")
    return REASON_ALIASES.get(reason_key, reason_key)


def policy_for(reason_key: str) -> Dict[str, Any]:
    return dict(RECOVERY_POLICIES.get(canonical_reason(reason_key), {"bucket": "crash", "cap": 30.0, "fatal": False}))


def build_recovery_log_payload(event: str, account: Any, reason: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    try:
        runtime_snapshot = account.runtime_snapshot()
    except Exception:
        runtime_snapshot = {}
    payload = {
        "event_type": event,
        "account": account.display_name,
        "account_id": account._config_username,
        "reason": reason,
        "runtime_generation": account.runtime_generation,
        "recovery_generation": account.recovery_generation,
        "command_generation": account.command_generation,
        "runtime_state": runtime_snapshot.get("runtime_state", ""),
        "public_state": runtime_snapshot.get("public_state", account.state.name),
        "session_id": account.session_id,
        "transaction_id": account.rejoin_transaction_id,
        "pid": account.pid or "",
        "PID": account.pid or "",
        "state": account.state.name,
        "recovery_status": account.recovery_status,
        "thread": threading.current_thread().name,
        "thread_name": threading.current_thread().name,
        "lifecycle_owner": "recovery_coordinator",
    }
    payload.update(fields)
    return payload


def adaptive_recovery_delay(
    cfg: Dict[str, Any],
    account: Any,
    reason_key: str,
    cooldown: float | None,
    session_conflict_attempts: int,
    compute_backoff: Callable[..., float],
) -> float:
    if cooldown is not None:
        return max(0.0, float(cooldown))
    canonical = canonical_reason(reason_key)
    policy = policy_for(canonical)
    rejoin_base = float(cfg.get("rejoin_delay", cfg.get("cooldown_after_crash", 5)) or 5)
    crash_base = float(cfg.get("cooldown_after_crash", rejoin_base) or rejoin_base)
    with account._lock:
        launch_retry = int(account.launch_fail_count or 0)
        crash_retry = int(account.crash_retry_count or 0)
        network_retry = int(account.network_retry_count or 0)
    if canonical == "session_conflict":
        return 3.0 if session_conflict_attempts <= 1 else min(20.0, 8.0 + (session_conflict_attempts * 4.0))
    if canonical == "network_drop":
        return min(12.0, 2.0 + (network_retry * 1.5))
    if canonical == "connection_error":
        return min(15.0, 3.0 + (network_retry * 1.5))
    if canonical == "launch_fail":
        return min(45.0, compute_backoff(max(1, launch_retry), base=4, cap=45))
    if canonical == "server_full":
        return min(20.0, rejoin_base + max(0, launch_retry - 1) * 3.0)
    if canonical in {"process_crash", "watchdog_timeout", "loading_freeze", "teleport_timeout"}:
        return min(float(policy.get("cap") or 35), crash_base + max(0, crash_retry - 1) * 4.0)
    return min(float(policy.get("cap") or 30), compute_backoff(max(1, crash_retry), base=int(rejoin_base), cap=int(policy.get("cap") or 30)))


def context_from_signal(account: Any, signal: str, reason_key: str, payload: Dict[str, Any]) -> RecoveryAttemptContext:
    payload = dict(payload or {})
    context = RecoveryAttemptContext.from_account_payload(account, signal, reason_key, payload)
    if payload.get("disconnect_category") or payload.get("category"):
        return context
    category = normalize_disconnect_category(reason=reason_key, popup_code=context.popup_code)
    priority = priority_for_signal(
        signal=signal,
        category=category,
        popup_code=context.popup_code,
        visual=bool(payload.get("visual_disconnect", False)),
    )
    return RecoveryAttemptContext(
        account_id=context.account_id,
        runtime_generation=context.runtime_generation,
        pid=context.pid,
        trigger=context.trigger,
        category=category,
        popup_code=context.popup_code,
        popup_confidence=context.popup_confidence,
        watchdog_reason=context.watchdog_reason,
        presence_state=context.presence_state,
        cooldown_reason=context.cooldown_reason,
        retry_count=context.retry_count,
        created_at=context.created_at,
        token=context.token,
        priority=priority,
        detail=context.detail,
    )


class RecoveryDedupeTracker:
    def __init__(self, window_seconds: float):
        self.window_seconds = max(1.0, float(window_seconds or 3.0))
        self._recent: Dict[Tuple[str, int, str], Tuple[float, int]] = {}
        self._lock = threading.Lock()

    def check_and_mark(self, ctx: RecoveryAttemptContext, now: float | None = None) -> Dict[str, Any]:
        now = time.time() if now is None else float(now)
        with self._lock:
            stale_cutoff = now - max(self.window_seconds * 4.0, 30.0)
            self._recent = {key: value for key, value in self._recent.items() if float(value[0]) >= stale_cutoff}
            previous = self._recent.get(ctx.signature)
            if previous:
                last_ts, last_priority = previous
                age = now - float(last_ts)
                if age <= self.window_seconds and int(last_priority) >= int(ctx.priority):
                    return {
                        "ignore": True,
                        "reason": "duplicate_recovery_signature",
                        "signature": "|".join(str(part) for part in ctx.signature),
                        "priority": ctx.priority,
                        "previous_priority": last_priority,
                        "age": f"{age:.2f}",
                    }
            self._recent[ctx.signature] = (now, int(ctx.priority))
        return {"ignore": False}


class SessionConflictTracker:
    def __init__(self):
        self._attempts: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def record(self, account_id: str, window_seconds: float) -> int:
        now = time.time()
        window = max(30.0, float(window_seconds or 90.0))
        with self._lock:
            attempts = [ts for ts in self._attempts.get(account_id, []) if (now - ts) <= window]
            attempts.append(now)
            self._attempts[account_id] = attempts
            return len(attempts)

    def clear(self, account_id: str) -> None:
        with self._lock:
            self._attempts.pop(account_id, None)

    def count(self, account_id: str, window_seconds: float) -> int:
        now = time.time()
        window = max(30.0, float(window_seconds or 90.0))
        with self._lock:
            attempts = [ts for ts in self._attempts.get(account_id, []) if (now - ts) <= window]
            self._attempts[account_id] = attempts
            return len(attempts)


def active_recovery_block_reason(owner: Dict[str, Any] | None, ctx: RecoveryAttemptContext) -> Dict[str, Any]:
    if not owner:
        return {"blocked": False}
    owner_priority = int(owner.get("priority", 0) or 0)
    if owner_priority <= int(ctx.priority):
        return {"blocked": False}
    return {
        "blocked": True,
        "ignore": "lower_priority_than_active_recovery",
        "active_reason": owner.get("reason", ""),
        "active_priority": owner_priority,
        "incoming_priority": ctx.priority,
    }


def kill_local_duplicate_for_session_conflict(
    account: Any,
    ctx: RecoveryAttemptContext,
    list_processes: Callable[[], List[Dict[str, Any]]],
    kill_pid: Callable[[int], bool],
    log_event: Callable[..., None],
) -> int:
    if ctx.category != SESSION_CONFLICT or (ctx.popup_code and ctx.popup_code != "273"):
        log_event(
            "session_conflict_duplicate_skipped",
            skip_reason="not_error_273_session_conflict",
            **ctx.to_dict(),
        )
        return 0
    killed = 0
    with account._lock:
        bound_pid = int(account.pid or 0)
        expected_owner = str(account._config_username or "")
        expected_tracker = str(getattr(account, "browser_tracker_id", "") or "")
    for entry in list_processes():
        pid = int(entry.get("pid") or 0)
        owner = str(entry.get("owner") or "")
        tracker = str(entry.get("browser_tracker_id") or "")
        if not pid or pid == bound_pid:
            continue
        owner_match = bool(expected_owner and owner and owner == expected_owner)
        tracker_match = bool(expected_tracker and tracker and tracker == expected_tracker)
        if expected_tracker and tracker and not tracker_match:
            continue
        if not owner_match and not tracker_match:
            continue
        if kill_pid(pid):
            killed += 1
            fields = ctx.to_dict()
            fields.update(
                duplicate_pid=pid,
                bound_pid=bound_pid,
                owner_match=owner_match,
                browser_tracker_match=tracker_match,
            )
            log_event(
                "session_conflict_duplicate_killed",
                **fields,
            )
    if not killed:
        log_event(
            "session_conflict_no_local_duplicate",
            bound_pid=bound_pid,
            browser_tracker_present=bool(expected_tracker),
            **ctx.to_dict(),
        )
    return killed
