from __future__ import annotations

import re
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional


SECRET_KEY_RE = re.compile(r"(cookie|password|token|secret|roblosecurity|private.*link|accesscode|linkcode)", re.I)
COOKIE_RE = re.compile(r"(_\|WARNING:[^\s'\"<>]+|\.ROBLOSECURITY[^\s'\"<>]*)", re.I)
LINK_CODE_RE = re.compile(r"((?:privateServerLinkCode|linkCode|accessCode)=)[^&\s]+", re.I)

SAFE_CONFIG_KEYS = (
    "auto_rejoin",
    "rejoin_delay",
    "max_retry",
    "max_fail_count",
    "crash_timeout",
    "heartbeat_timeout",
    "launch_verify_window",
    "queue_delay_seconds",
    "queue_duration_seconds",
    "queue_timeout",
    "max_concurrent_accounts",
    "periodic_reconcile_interval",
    "recovery_budget_enabled",
    "recovery_budget_max_attempts",
    "recovery_budget_window_seconds",
    "recovery_confidence_threshold",
    "runtime_invariant_monitor_enabled",
    "runtime_invariant_suppress_seconds",
    "orphan_sweeper_enabled",
    "orphan_sweeper_kill_enabled",
    "orphan_sweeper_min_confidence",
    "popup_disconnected_enabled",
    "popup_scan_interval_seconds",
    "popup_scan_max_parallel",
    "multi_roblox_enabled",
)

ACCOUNT_KEYS = (
    "username",
    "account_id",
    "display",
    "state",
    "public_state",
    "desired_state",
    "state_label",
    "launchable",
    "blocked_reason",
    "captcha_required",
    "cookie_username",
    "cookie_user_id",
    "cookie_mismatch",
    "pid",
    "process_alive",
    "process_binding_status",
    "binding_decision",
    "process_binding_confidence",
    "process_reject_reason",
    "process_owner_claim",
    "unmanaged_live_process_count",
    "unmanaged_live_pids",
    "adopt_candidate_pid",
    "adopt_reject_reason",
    "orphan_confidence",
    "runtime_state",
    "runtime_generation",
    "recovery_generation",
    "command_generation",
    "recovery_active",
    "recovery_inflight",
    "recovery_status",
    "recovery_reason",
    "cooldown_left",
    "retry_count",
    "crash_count",
    "fail_count",
    "liveness_state",
    "liveness_score",
    "last_heartbeat",
    "server_validation",
    "destination_validation",
    "scheduler_slot",
    "supervisor_state",
    "last_transaction_status",
    "last_transaction_step",
    "last_transaction_reason",
    "health_flags",
    "command_inflight",
    "launch_intent_summary",
)


def _redact_value(key: str, value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    safe_identity_keys = {"cookie_username", "cookie_user_id", "cookie_mismatch"}
    if str(key or "").lower() not in safe_identity_keys and SECRET_KEY_RE.search(str(key or "")):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(item_key): _redact_value(str(item_key), item_value) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_redact_value(key, item) for item in value]
    text = str(value)
    text = COOKIE_RE.sub("[REDACTED_COOKIE]", text)
    text = LINK_CODE_RE.sub(r"\1[REDACTED]", text)
    return text


def _safe_config(cfg: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: _redact_value(key, cfg.get(key)) for key in SAFE_CONFIG_KEYS if key in cfg}


def _safe_account(row: Mapping[str, Any]) -> Dict[str, Any]:
    item = {key: _redact_value(key, row.get(key)) for key in ACCOUNT_KEYS if key in row}
    runtime = row.get("runtime") if isinstance(row.get("runtime"), Mapping) else {}
    for key in ("orphan_pid", "orphan_identity", "orphan_verify_after", "orphan_observed_at"):
        if key in runtime:
            item[key] = _redact_value(key, runtime.get(key))
    return item


def _safe_event(event: Mapping[str, Any]) -> Dict[str, Any]:
    return _redact_value("", dict(event))


def _matches_account(row: Mapping[str, Any], account_id: str) -> bool:
    if not account_id:
        return True
    target = str(account_id or "").strip().lower()
    values = (
        row.get("account_id"),
        row.get("username"),
        row.get("display"),
    )
    return any(str(value or "").strip().lower() == target for value in values)


def _recommendations(status: Mapping[str, Any], accounts: Iterable[Mapping[str, Any]], events: Iterable[Mapping[str, Any]]) -> List[str]:
    output: List[str] = []
    health = status.get("runtime_health") if isinstance(status.get("runtime_health"), Mapping) else {}
    warnings = [str(item) for item in health.get("warnings", [])] if isinstance(health, Mapping) else []
    if warnings:
        output.append(f"Investigate runtime health warnings: {', '.join(warnings[:5])}")
    blocked = [row for row in accounts if row.get("blocked_reason")]
    if blocked:
        output.append(f"Resolve blocked account gates before launch: {len(blocked)} account(s)")
    if any("process_binding_warning" in (row.get("health_flags") or []) for row in accounts):
        output.append("Review process binding warnings before force rejoin or kill actions")
    if any("invariant" in str(event.get("event_type", "")).lower() for event in events):
        output.append("Inspect runtime invariant violations; state may not match process truth")
    return output[:6]


def build_runtime_diagnostic_bundle(
    status: Mapping[str, Any],
    events: Iterable[Mapping[str, Any]],
    cfg: Mapping[str, Any],
    *,
    account_id: str = "",
    event_type: str = "",
    severity: str = "",
    limit: int = 200,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    generated_at = float(now if now is not None else time.time())
    safe_limit = max(1, min(int(limit or 200), 500))
    accounts = [
        _safe_account(row)
        for row in list(status.get("accounts") or [])
        if isinstance(row, Mapping) and _matches_account(row, account_id)
    ]
    safe_events = [_safe_event(event) for event in list(events or [])[:safe_limit] if isinstance(event, Mapping)]
    summary = {
        "running": bool(status.get("running", False)),
        "total_accounts": int(status.get("total_accounts", len(accounts)) or 0),
        "selected_accounts": len(accounts),
        "launchable_count": int(status.get("launchable_count", 0) or 0),
        "blocked_count": int(status.get("blocked_count", 0) or 0),
        "in_game": int(status.get("in_game", 0) or 0),
        "crash": int(status.get("crash", 0) or 0),
        "queued": int(status.get("queued", 0) or 0),
        "failed": int(status.get("failed", 0) or 0),
        "event_count": len(safe_events),
    }
    return {
        "ok": True,
        "generated_at": generated_at,
        "filters": {
            "account_id": str(account_id or ""),
            "event_type": str(event_type or ""),
            "severity": str(severity or ""),
            "limit": safe_limit,
        },
        "summary": summary,
        "runtime_health": _redact_value("runtime_health", status.get("runtime_health", {})),
        "queue_snapshot": _redact_value("queue_snapshot", status.get("queue_snapshot", {})),
        "command_inflight": _redact_value("command_inflight", status.get("command_inflight")),
        "supervisor": _redact_value("supervisor", status.get("supervisor", {})),
        "config": _safe_config(cfg),
        "accounts": accounts,
        "events": safe_events,
        "recommendations": _recommendations(status, accounts, safe_events),
    }
