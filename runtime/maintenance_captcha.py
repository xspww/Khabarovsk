from __future__ import annotations

import time
from typing import Any, Dict

from core import flog_kv
from services.captcha_guard import CAPTCHA_BLOCK_REASON, CAPTCHA_REASON, is_account_captcha_required, set_account_captcha_hold
from services.process_service import ProcessManager, ProcessService

# Roblox shows an auto-passing "Security Verification" page that looks like a
# captcha for a moment. A single sighting must never kill the game: only after
# the captcha evidence persists on the same bound process for this long do we
# hold the account and close the process.
CAPTCHA_CONFIRM_SECONDS = 15.0


def _confirm_seconds(owner: Any) -> float:
    try:
        cfg = getattr(owner, "_cfg", None) or {}
        return max(3.0, float(cfg.get("captcha_confirm_seconds", CAPTCHA_CONFIRM_SECONDS) or CAPTCHA_CONFIRM_SECONDS))
    except Exception:
        return CAPTCHA_CONFIRM_SECONDS


def clear_captcha_suspicion(acc: Any) -> None:
    lock = getattr(acc, "_lock", None)

    def _clear() -> None:
        try:
            acc._captcha_suspected_at = 0.0
            acc._captcha_suspected_pid = 0
        except Exception:
            pass

    if lock:
        with lock:
            _clear()
    else:
        _clear()


def detect_and_hold_captcha(owner: Any, acc: Any, pid: int, source: str) -> bool:
    if not pid:
        return False
    try:
        dialog = ProcessManager.inspect_disconnect_dialog(
            pid,
            prepare=True,
            process_idle=False,
            sample_count=2,
        )
    except Exception as exc:
        flog_kv(
            "CAPTCHA",
            "timeout_probe_failed",
            "warning",
            account=acc.display_name,
            pid=pid,
            source=source,
            error=str(exc),
        )
        return False
    if not dialog.get("matched") or str(dialog.get("reason_key") or "") != CAPTCHA_REASON:
        return False
    dialog = dict(dialog)
    dialog.setdefault("detail", "Roblox Security verification CAPTCHA visible")
    dialog.setdefault("evidence_source", source)
    # This path runs only for already-stuck accounts (Lua silent past the
    # timeout), where a captcha is the likely cause -> hold immediately.
    return handle_watchdog_captcha(owner, acc, pid, dialog, require_confirm=False)


def handle_watchdog_captcha(owner: Any, acc: Any, pid: int, dialog: Dict[str, Any], *, require_confirm: bool = True) -> bool:
    """Confirm a captcha sighting before holding the account and killing the game.

    Returns True when the account was put on captcha hold (process closed).
    A single sighting is a suspicion: the auto-passing Roblox "Security
    Verification" page looks identical to a challenge for a moment, and killing
    the game + holding the account there is the reported false-positive bug.
    The periodic watchdog (require_confirm=True, default) waits for the
    evidence to persist; stuck-account paths pass require_confirm=False to
    hold immediately.
    """
    detail = str(dialog.get("detail") or "").strip() or "Roblox Security verification CAPTCHA visible"
    now = time.time()
    confirm_seconds = _confirm_seconds(owner)
    with acc._lock:
        captcha_pid = acc.pid
        captcha_runtime_generation = acc.runtime_generation

    if require_confirm:
        suspected_at = float(getattr(acc, "_captcha_suspected_at", 0.0) or 0.0)
        suspected_pid = int(getattr(acc, "_captcha_suspected_pid", 0) or 0)
        if suspected_pid and suspected_pid != int(pid or 0):
            # bound process changed -> previous suspicion no longer applies
            suspected_at = 0.0
            with acc._lock:
                acc._captcha_suspected_at = 0.0
                acc._captcha_suspected_pid = 0

        if not suspected_at:
            with acc._lock:
                acc._captcha_suspected_at = now
                acc._captcha_suspected_pid = int(pid or 0)
            flog_kv(
                "CAPTCHA",
                "captcha_suspected_waiting_confirm",
                "warning",
                account=acc.display_name,
                pid=pid,
                confirm_seconds=f"{confirm_seconds:.1f}",
                detail=detail,
            )
            return False

        if now - suspected_at < confirm_seconds:
            flog_kv(
                "CAPTCHA",
                "captcha_confirm_pending",
                "warning",
                account=acc.display_name,
                pid=pid,
                elapsed=f"{now - suspected_at:.1f}",
                confirm_seconds=f"{confirm_seconds:.1f}",
                detail=detail,
            )
            return False

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
    return True
