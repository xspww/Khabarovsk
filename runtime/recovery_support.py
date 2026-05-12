from __future__ import annotations

import random
import sys
from typing import Any, Dict

from core import Account, flog_kv
from runtime.recovery_context import SESSION_CONFLICT
from services.roblox_log_evidence import collect_recent_log_evidence


RECOVERY_REASON_MESSAGES = {
    "pid_dead": "à¸«à¸¥à¸¸à¸” - Process à¸«à¸²à¸¢à¹„à¸› (game crashed/closed)",
    "not_responding": "à¸«à¸¥à¸¸à¸” - Not Responding (à¹€à¸à¸¡à¸„à¹‰à¸²à¸‡, à¸•à¸£à¸§à¸ˆà¸žà¸šà¸ˆà¸²à¸ Task Manager)",
    "network_drop": "à¸«à¸¥à¸¸à¸” - à¹€à¸™à¹‡à¸•à¸«à¸¥à¸¸à¸” (network dropped)",
    "launch_fail": "à¸«à¸¥à¸¸à¸” - Launch à¸¥à¹‰à¸¡à¹€à¸«à¸¥à¸§ (à¹„à¸¡à¹ˆà¸ªà¸²à¸¡à¸²à¸£à¸–à¹€à¸›à¸´à¸”à¹€à¸à¸¡à¹„à¸”à¹‰)",
    "cookie_invalid": "à¸«à¸¢à¸¸à¸” - Cookie à¹„à¸¡à¹ˆà¸–à¸¹à¸à¸•à¹‰à¸­à¸‡ (session expired)",
    "cookie_missing": "à¸«à¸¢à¸¸à¸” - à¹„à¸¡à¹ˆà¸¡à¸µ cookie login à¸ˆà¸²à¸ Roblox Account Manager",
    "max_fail": "à¸«à¸¢à¸¸à¸” - à¹€à¸à¸´à¸™ fail limit (FAILED state)",
    "relaunch_loop": "à¸«à¸¢à¸¸à¸” - Roblox à¹€à¸”à¹‰à¸‡à¹€à¸£à¹‡à¸§à¸«à¸¥à¸²à¸¢à¸£à¸­à¸šà¸•à¸´à¸”à¸à¸±à¸™ à¸ˆà¸¶à¸‡à¸«à¸¢à¸¸à¸” auto rejoin",
    "watchdog_low_resource": "à¸«à¸¥à¸¸à¸” - CPU/RAM à¸•à¹ˆà¸³à¸œà¸´à¸”à¸›à¸à¸•à¸´ (Watchdog kill)",
    "cookie_mismatch": "Stopped - cookie belongs to a different Roblox account. Reimport the correct cookie.",
    "process_crash": "Process crashed or disappeared",
    "watchdog_timeout": "Watchdog timeout - no process activity",
    "loading_freeze": "Loading freeze - no heartbeat during loading",
    "teleport_timeout": "Teleport timeout",
    "auth_failure": "Authentication failure",
    "server_full": "Server full",
    "connection_error": "Connection Error / Disconnected",
    "account_launched_elsewhere": "Session conflict (Error 273)",
    "session_conflict": "Session conflict (Error 273)",
    "unexpected_client_behavior": "Rejoining - Roblox disconnected (Error 268)",
    "idle_disconnect": "Rejoining - Roblox idle disconnect (Error 278)",
    "security_kick": "Rejoining - Roblox data session ended (Error 267)",
    "multi_roblox_guard_failed": "Stopped - Multi Roblox guard failed. Roblox closed another account while launching; restart RT after the guard is ready.",
}


def _enrich_visual_disconnect_payload_with_log(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Attach late Roblox log evidence when visual popup recovery wins the race."""
    if not payload:
        return payload
    if payload.get("popup_code") or payload.get("error_code"):
        return payload
    if not payload.get("visual_disconnect"):
        return payload
    evidence_source = str(payload.get("evidence_source") or "")
    if evidence_source not in {"visual_strong", "center_modal", "visual"}:
        return payload

    collector = collect_recent_log_evidence
    farm_module = sys.modules.get("farm")
    if farm_module is not None and hasattr(farm_module, "collect_recent_log_evidence"):
        collector = getattr(farm_module, "collect_recent_log_evidence")
    evidence = collector(since_seconds=180.0, max_files=8, max_lines=1200)
    code = str(evidence.get("error_code") or "").strip()
    if not evidence.get("matched") or not code:
        return payload

    enriched = dict(payload)
    detail = str(enriched.get("detail") or enriched.get("reason_msg") or "").strip()
    log_line = str(evidence.get("line") or "").strip()
    if log_line and log_line not in detail:
        detail = f"{detail}; roblox_log={log_line}" if detail else f"roblox_log={log_line}"
    enriched.update({
        "popup_code": code,
        "error_code": code,
        "detail": detail,
        "reason_msg": detail or str(enriched.get("reason_msg") or ""),
        "evidence_source": "roblox_log",
        "visual_evidence_source": evidence_source,
        "log_evidence": dict(evidence),
    })
    if code == "273":
        enriched["reason_key"] = "session_conflict"
        enriched["disconnect_category"] = SESSION_CONFLICT
    return enriched


def compute_backoff(attempt: int, base: int = 5, cap: int = 120) -> float:
    exp = base * (2 ** max(0, attempt - 1))
    jitter = random.uniform(0, 3)
    return min(exp + jitter, float(cap))


def _persist_cookie_identity_status(
    acc: Account,
    cookie_username: str = "",
    cookie_user_id: str = "",
    cookie_mismatch: bool = True,
):
    try:
        from account_hybrid import ACCOUNT_STORE

        ACCOUNT_STORE.update_record(
            acc.username,
            {
                "cookie_username": str(cookie_username or getattr(acc, "cookie_username", "") or ""),
                "cookie_user_id": str(cookie_user_id or getattr(acc, "cookie_user_id", "") or ""),
                "cookie_mismatch": bool(cookie_mismatch),
                "import_status": "cookie_mismatch" if cookie_mismatch else "",
            },
        )
    except Exception as e:
        flog_kv("ACCOUNT_DATA", "cookie_identity_status_persist_failed", "warning", account=acc.display_name, error=e)


def _set_account_cookie_block(acc: Account, reason: str, cookie_username: str = ""):
    with acc._lock:
        if cookie_username:
            acc.cookie_username = str(cookie_username)
        acc.cookie_mismatch = True
        acc.session_checked = True
        acc.session_valid = False
        acc.manual_status = reason
        acc.last_error = reason
        acc.last_crash_reason = "cookie_mismatch"
    _persist_cookie_identity_status(acc, cookie_username=cookie_username or acc.cookie_username, cookie_mismatch=True)


def _clear_account_cookie_block(acc: Account):
    with acc._lock:
        acc.cookie_mismatch = False
        if acc.last_crash_reason == "cookie_mismatch":
            acc.last_crash_reason = ""
        if "cookie" in str(acc.manual_status or "").lower():
            acc.manual_status = ""
        if "cookie" in str(acc.last_error or "").lower():
            acc.last_error = ""
    _persist_cookie_identity_status(acc, cookie_username=acc.cookie_username, cookie_user_id=acc.cookie_user_id, cookie_mismatch=False)
