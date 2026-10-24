from __future__ import annotations

import time
from typing import Any

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

_VERIFY_HOLD_META = {
    LOCKED_REASON: (LOCKED_BLOCK_REASON, "LOCKED", "Roblox identity verification visible"),
    RECOVERY_REASON: (RECOVERY_BLOCK_REASON, "RECOVERY", "Roblox account recovery visible"),
}


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
            "intentional: ignore exception"

    if lock:
        with lock:
            _clear()
    else:
        _clear()


def _log_verify_probe_failure(acc: Any, pid: int, source: str, exc: Exception) -> None:
    flog_kv("VERIFY", "timeout_probe_failed", "warning", account=acc.display_name, pid=pid, source=source, error=str(exc))


def _probe_verify(acc: Any, pid: int, source: str) -> dict[str, Any] | None:
    try:
        from runtime.verify_detector import inspect_verify
    except Exception as exc:
        _log_verify_probe_failure(acc, pid, source, exc)
        return None
    try:
        cookie = str(getattr(acc, "cookie", "") or "")
        found = inspect_verify(pid, cookie=cookie, window=True, api=True)
    except Exception as exc:
        _log_verify_probe_failure(acc, pid, source, exc)
        return None
    if not found.get("matched"):
        return None
    reason = str(found.get("reason_key") or "")
    if reason not in (LOCKED_REASON, RECOVERY_REASON):
        return None
    return found


def _verify_dialog(found: dict[str, Any], source: str) -> dict[str, Any]:
    return {
        "detail": str(found.get("detail") or "Roblox verification required"),
        "evidence_source": str(found.get("evidence_source") or source),
        "popup_confidence": float(found.get("popup_confidence", found.get("confidence", 1.0)) or 0.0),
        "confidence": float(found.get("confidence", 1.0) or 0.0),
        "reason_key": str(found.get("reason_key") or ""),
    }


def detect_and_hold_verify(owner: Any, acc: Any, pid: int, source: str) -> bool:
    if not pid:
        return False
    found = _probe_verify(acc, pid, source)
    if found is None:
        return False
    return handle_watchdog_verify(owner, acc, pid, _verify_dialog(found, source), require_confirm=False)


def _infer_verify_reason(dialog: dict[str, Any]) -> str:
    reason_key = str(dialog.get("reason_key") or "")
    if reason_key in (LOCKED_REASON, RECOVERY_REASON):
        return reason_key
    # Infer from detail when caller only has window text.
    try:
        from services.verify_guard import is_locked_text, is_recovery_text

        detail_text = str(dialog.get("detail") or "")
        if is_locked_text(detail_text):
            return LOCKED_REASON
        if is_recovery_text(detail_text):
            return RECOVERY_REASON
    except Exception:
        return ""
    return ""


def _reset_verify_suspicion(acc: Any) -> None:
    with acc._lock:
        acc._verify_suspected_at = 0.0
        acc._verify_suspected_pid = 0
        acc._verify_suspected_reason = ""


def _mark_verify_suspicion(acc: Any, now: float, pid: int, reason_key: str) -> None:
    with acc._lock:
        acc._verify_suspected_at = now
        acc._verify_suspected_pid = int(pid or 0)
        acc._verify_suspected_reason = reason_key


def _verify_suspicion_state(acc: Any, pid: int, reason_key: str) -> float:
    suspected_at = float(getattr(acc, "_verify_suspected_at", 0.0) or 0.0)
    suspected_pid = int(getattr(acc, "_verify_suspected_pid", 0) or 0)
    suspected_reason = str(getattr(acc, "_verify_suspected_reason", "") or "")
    if suspected_pid and suspected_pid != int(pid or 0):
        suspected_at = 0.0
        _reset_verify_suspicion(acc)
    if suspected_reason and suspected_reason != reason_key:
        suspected_at = 0.0
    return suspected_at


def _verify_pending(
    acc: Any,
    pid: int,
    reason_key: str,
    detail: str,
    log_scope: str,
    now: float,
    confirm_seconds: float,
) -> bool:
    suspected_at = _verify_suspicion_state(acc, pid, reason_key)
    if not suspected_at:
        _mark_verify_suspicion(acc, now, pid, reason_key)
        flog_kv(log_scope, "verify_suspected_waiting_confirm", "warning", account=acc.display_name, pid=pid, confirm_seconds=f"{confirm_seconds:.1f}", detail=detail)
        return True
    if now - suspected_at < confirm_seconds:
        flog_kv(log_scope, "verify_confirm_pending", "warning", account=acc.display_name, pid=pid, elapsed=f"{now - suspected_at:.1f}", confirm_seconds=f"{confirm_seconds:.1f}", detail=detail)
        return True
    return False


def _close_verify_process(owner: Any, acc: Any, verify_pid: int, verify_runtime_generation: int, log_scope: str) -> None:
    if verify_pid and hasattr(owner._state_mgr, "clear_process_binding"):
        kill_result = ProcessService.safe_kill_bound_process(
            acc, owner._state_mgr, reason="verify_hold", expected_runtime_generation=verify_runtime_generation, increment_generation=False,
        )
        flog_kv(log_scope, "account_process_closed", "warning", account=acc.display_name, pid=verify_pid,
                killed=bool(kill_result.get("killed")), kill_reason=kill_result.get("reason", ""))
        return
    flog_kv(log_scope, "account_process_close_skipped", "warning", account=acc.display_name,
            pid=verify_pid or "", reason="missing_bound_pid" if not verify_pid else "state_manager_unavailable")


def _apply_verify_hold(
    owner: Any,
    acc: Any,
    pid: int,
    dialog: dict[str, Any],
    detail: str,
    reason_key: str,
    is_locked: bool,
    block_reason: str,
    log_scope: str,
    verify_pid: int,
    verify_runtime_generation: int,
) -> None:
    already = is_account_locked(acc) if is_locked else is_account_recovery(acc)
    if not already:
        flog_kv("WATCHDOG", "verify_dialog_hold", "warning", account=acc.display_name, pid=pid,
                confidence=f"{float(dialog.get('popup_confidence', dialog.get('confidence', 0.0)) or 0.0):.2f}",
                source=dialog.get("evidence_source", ""), detail=detail, reason=reason_key)
    if is_locked:
        set_account_locked_hold(acc, detail, source="watchdog_popup", runtime_writer=owner._state_mgr)
    else:
        set_account_recovery_hold(acc, detail, source="watchdog_popup", runtime_writer=owner._state_mgr)
    _close_verify_process(owner, acc, verify_pid, verify_runtime_generation, log_scope)
    if hasattr(owner._recovery, "fail_account"):
        owner._recovery.fail_account(acc, reason_key, block_reason)


def handle_watchdog_verify(owner: Any, acc: Any, pid: int, dialog: dict[str, Any], *, require_confirm: bool = True) -> bool:
    reason_key = _infer_verify_reason(dialog)
    if not reason_key:
        return False
    is_locked = reason_key == LOCKED_REASON
    block_reason, log_scope, default_detail = _VERIFY_HOLD_META[reason_key]
    detail = str(dialog.get("detail") or "").strip() or default_detail
    now = time.time()
    confirm_seconds = _confirm_seconds(owner)
    with acc._lock:
        verify_pid = acc.pid
        verify_runtime_generation = acc.runtime_generation

    if require_confirm and _verify_pending(acc, pid, reason_key, detail, log_scope, now, confirm_seconds):
        return False

    _apply_verify_hold(
        owner, acc, pid, dialog, detail, reason_key, is_locked, block_reason, log_scope, verify_pid, verify_runtime_generation,
    )
    return True
