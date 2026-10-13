from __future__ import annotations

import time
from typing import Any, Dict

from core import flog_kv
from services.process_service import ProcessService
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

VERIFY_CONFIRM_SECONDS = 15.0


def _confirm_seconds(owner: Any) -> float:
    try:
        cfg = getattr(owner, "_cfg", None) or {}
        return max(3.0, float(cfg.get("verify_confirm_seconds", cfg.get("captcha_confirm_seconds", VERIFY_CONFIRM_SECONDS)) or VERIFY_CONFIRM_SECONDS))
    except Exception:
        return VERIFY_CONFIRM_SECONDS


def clear_verify_suspicion(acc: Any) -> None:
    lock = getattr(acc, "_lock", None)

    def _clear() -> None:
        try:
            acc._verify_suspected_at = 0.0
            acc._verify_suspected_pid = 0
            acc._verify_suspected_reason = ""
        except Exception:
            pass

    if lock:
        with lock:
            _clear()
    else:
        _clear()


def detect_and_hold_verify(owner: Any, acc: Any, pid: int, source: str) -> bool:
    if not pid:
        return False
    try:
        from runtime.verify_detector import inspect_verify
    except Exception as exc:
        flog_kv("VERIFY", "timeout_probe_failed", "warning", account=acc.display_name, pid=pid, source=source, error=str(exc))
        return False
    try:
        cookie = str(getattr(acc, "cookie", "") or "")
        found = inspect_verify(pid, cookie=cookie, window=True, api=True)
    except Exception as exc:
        flog_kv("VERIFY", "timeout_probe_failed", "warning", account=acc.display_name, pid=pid, source=source, error=str(exc))
        return False
    if not found.get("matched"):
        return False
    reason = str(found.get("reason_key") or "")
    if reason not in (LOCKED_REASON, RECOVERY_REASON):
        return False
    dialog = {
        "detail": str(found.get("detail") or "Roblox verification required"),
        "evidence_source": str(found.get("evidence_source") or source),
        "popup_confidence": float(found.get("popup_confidence", found.get("confidence", 1.0)) or 0.0),
        "confidence": float(found.get("confidence", 1.0) or 0.0),
        "reason_key": reason,
    }
    return handle_watchdog_verify(owner, acc, pid, dialog, require_confirm=False)


def handle_watchdog_verify(owner: Any, acc: Any, pid: int, dialog: Dict[str, Any], *, require_confirm: bool = True) -> bool:
    reason_key = str(dialog.get("reason_key") or "")
    if reason_key not in (LOCKED_REASON, RECOVERY_REASON):
        # Infer from detail when caller only has window text.
        try:
            from services.verify_guard import is_locked_text, is_recovery_text

            detail_text = str(dialog.get("detail") or "")
            if is_locked_text(detail_text):
                reason_key = LOCKED_REASON
            elif is_recovery_text(detail_text):
                reason_key = RECOVERY_REASON
            else:
                return False
        except Exception:
            return False
    is_locked = reason_key == LOCKED_REASON
    block_reason = LOCKED_BLOCK_REASON if is_locked else RECOVERY_BLOCK_REASON
    default_detail = "Roblox identity verification visible" if is_locked else "Roblox account recovery visible"
    detail = str(dialog.get("detail") or "").strip() or default_detail
    log_scope = "LOCKED" if is_locked else "RECOVERY"
    now = time.time()
    confirm_seconds = _confirm_seconds(owner)
    with acc._lock:
        verify_pid = acc.pid
        verify_runtime_generation = acc.runtime_generation

    if require_confirm:
        suspected_at = float(getattr(acc, "_verify_suspected_at", 0.0) or 0.0)
        suspected_pid = int(getattr(acc, "_verify_suspected_pid", 0) or 0)
        suspected_reason = str(getattr(acc, "_verify_suspected_reason", "") or "")
        if suspected_pid and suspected_pid != int(pid or 0):
            suspected_at = 0.0
            with acc._lock:
                acc._verify_suspected_at = 0.0
                acc._verify_suspected_pid = 0
                acc._verify_suspected_reason = ""
        if suspected_reason and suspected_reason != reason_key:
            suspected_at = 0.0

        if not suspected_at:
            with acc._lock:
                acc._verify_suspected_at = now
                acc._verify_suspected_pid = int(pid or 0)
                acc._verify_suspected_reason = reason_key
            flog_kv(log_scope, "verify_suspected_waiting_confirm", "warning", account=acc.display_name, pid=pid, confirm_seconds=f"{confirm_seconds:.1f}", detail=detail)
            return False

        if now - suspected_at < confirm_seconds:
            flog_kv(log_scope, "verify_confirm_pending", "warning", account=acc.display_name, pid=pid, elapsed=f"{now - suspected_at:.1f}", confirm_seconds=f"{confirm_seconds:.1f}", detail=detail)
            return False

    already = is_account_locked(acc) if is_locked else is_account_recovery(acc)
    if not already:
        flog_kv("WATCHDOG", "verify_dialog_hold", "warning", account=acc.display_name, pid=pid,
                confidence=f"{float(dialog.get('popup_confidence', dialog.get('confidence', 0.0)) or 0.0):.2f}",
                source=dialog.get("evidence_source", ""), detail=detail, reason=reason_key)
    if is_locked:
        set_account_locked_hold(acc, detail, source="watchdog_popup", runtime_writer=owner._state_mgr)
    else:
        set_account_recovery_hold(acc, detail, source="watchdog_popup", runtime_writer=owner._state_mgr)
    if verify_pid and hasattr(owner._state_mgr, "clear_process_binding"):
        kill_result = ProcessService.safe_kill_bound_process(
            acc, owner._state_mgr, reason="verify_hold", expected_runtime_generation=verify_runtime_generation, increment_generation=False,
        )
        flog_kv(log_scope, "account_process_closed", "warning", account=acc.display_name, pid=verify_pid,
                killed=bool(kill_result.get("killed")), kill_reason=kill_result.get("reason", ""))
    else:
        flog_kv(log_scope, "account_process_close_skipped", "warning", account=acc.display_name,
                pid=verify_pid or "", reason="missing_bound_pid" if not verify_pid else "state_manager_unavailable")
    if hasattr(owner._recovery, "fail_account"):
        owner._recovery.fail_account(acc, reason_key, block_reason)
    return True
