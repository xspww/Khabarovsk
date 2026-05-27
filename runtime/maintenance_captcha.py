from __future__ import annotations

from typing import Any, Dict

from core import flog_kv
from services.captcha_guard import CAPTCHA_BLOCK_REASON, CAPTCHA_REASON, is_account_captcha_required, set_account_captcha_hold
from services.process_service import ProcessService


def handle_watchdog_captcha(owner: Any, acc: Any, pid: int, dialog: Dict[str, Any]) -> None:
    detail = str(dialog.get("detail") or "").strip() or "Roblox Security verification CAPTCHA visible"
    with acc._lock:
        captcha_pid = acc.pid
        captcha_runtime_generation = acc.runtime_generation
    if not is_account_captcha_required(acc):
        flog_kv(
            "WATCHDOG",
            "captcha_dialog_hold",
            "warning",
            account=acc.display_name,
            pid=pid,
            confidence=f"{float(dialog.get('popup_confidence', dialog.get('confidence', 0.0)) or 0.0):.2f}",
            source=dialog.get("evidence_source", ""),
            detail=detail,
        )
    set_account_captcha_hold(acc, detail, source="watchdog_popup", runtime_writer=owner._state_mgr)
    if captcha_pid and hasattr(owner._state_mgr, "clear_process_binding"):
        kill_result = ProcessService.safe_kill_bound_process(
            acc,
            owner._state_mgr,
            reason="captcha_hold",
            expected_runtime_generation=captcha_runtime_generation,
            increment_generation=False,
        )
        flog_kv(
            "CAPTCHA",
            "account_process_closed",
            "warning",
            account=acc.display_name,
            pid=captcha_pid,
            killed=bool(kill_result.get("killed")),
            kill_reason=kill_result.get("reason", ""),
        )
    else:
        flog_kv(
            "CAPTCHA",
            "account_process_close_skipped",
            "warning",
            account=acc.display_name,
            pid=captcha_pid or "",
            reason="missing_bound_pid" if not captcha_pid else "state_manager_unavailable",
        )
    if hasattr(owner._recovery, "fail_account"):
        owner._recovery.fail_account(acc, CAPTCHA_REASON, CAPTCHA_BLOCK_REASON)
