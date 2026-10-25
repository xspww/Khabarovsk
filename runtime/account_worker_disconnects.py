from __future__ import annotations

import time
from typing import Any, NamedTuple

from core import Account, flog
from services.process_service import ProcessManager, ProcessService


class _RuntimeIds(NamedTuple):
    runtime_generation: Any
    session_id: Any
    launch_nonce: Any
    transaction_id: Any


class _DisconnectPopup(NamedTuple):
    reason_key: str
    detail: str
    error_code: str
    action: str
    confidence: float
    evidence_note: str
    detail_suffix: str
    hold_sec: float
    effective_hold_sec: float
    recovery_allowed: bool
    category: str
    visual_disconnect: bool


def _disconnect_checks_enabled(worker: Any, runtime_seconds: float) -> bool:
    return bool(
        runtime_seconds > 10
        and worker.cfg.get("connection_error_rejoin", True)
        and worker.cfg.get("popup_disconnected_enabled", True)
    )


def _wait_brief(worker: Any, timeout: float) -> None:
    worker._idle(timeout)


def _account_runtime_ids(acc: Account) -> _RuntimeIds:
    with acc._lock:
        return _RuntimeIds(
            acc.runtime_generation,
            acc.session_id,
            acc.launch_nonce,
            acc.rejoin_transaction_id,
        )


def _popup_from_info(info: dict[str, Any], hold_sec: float) -> _DisconnectPopup:
    reason_key = str(info.get("reason_key") or "connection_error")
    detail = str(info.get("detail") or "")
    error_code = str(info.get("error_code") or "")
    action = str(info.get("action") or "rejoin")
    confidence = float(
        info.get("popup_confidence", info.get("confidence", 0.0)) or 0.0
    )
    effective_hold_sec = (
        1.0 if error_code in {"267", "268", "273", "277", "279"} else hold_sec
    )
    evidence_note = (
        f"source={info.get('evidence_source', '')} confidence={confidence:.2f} "
        f"samples={info.get('positive_samples', 0)}/{info.get('sample_count', 0)}"
    )
    detail_suffix = f" code={error_code}" if error_code else ""
    return _DisconnectPopup(
        reason_key,
        detail,
        error_code,
        action,
        confidence,
        evidence_note,
        detail_suffix,
        hold_sec,
        effective_hold_sec,
        bool(info.get("recovery_allowed")),
        str(info.get("disconnect_category", "")),
        bool(info.get("visual_disconnect", False)),
    )


def _clear_disconnect_state(worker: Any, acc: Account) -> None:
    worker._connection_error_since = None
    if (
        acc.recovery_status in {"checking_disconnect", "disconnect_detected"}
        and not acc.recovery_inflight
    ):
        worker.runtime_owner.set_recovery_status(
            acc,
            status="in_game",
            reason="disconnect_check_clear",
            inflight=False,
        )


def _ignore_visual_only(worker: Any, acc: Account, popup: _DisconnectPopup) -> bool:
    flog(
        f"[WORKER] {acc.display_name} popup suspicious visual-only ignored "
        f"({popup.evidence_note}: {popup.detail})"
    )
    worker._connection_error_since = None
    _wait_brief(worker, min(1.0, max(0.2, popup.hold_sec / 2.0)))
    return True


def _fail_popup(worker: Any, acc: Account, popup: _DisconnectPopup) -> bool:
    flog(
        f"[WORKER] {acc.display_name} disconnect dialog requires stop "
        f"(reason={popup.reason_key}{popup.detail_suffix} {popup.evidence_note})"
    )
    ids = _account_runtime_ids(acc)
    worker._connection_error_since = None
    reason_msg = worker.REASON_MESSAGES.get(popup.reason_key, popup.reason_key)
    worker.runtime_owner.handle_runtime_signal(
        acc,
        "fatal",
        popup.reason_key,
        payload={
            "reason_msg": f"{reason_msg} [PID={acc.pid} UI={popup.detail}]",
            "detail": popup.detail,
            "popup_code": popup.error_code,
            "popup_confidence": popup.confidence,
            "disconnect_category": popup.category,
        },
        expected_runtime_generation=ids.runtime_generation,
        expected_session_id=ids.session_id,
        expected_launch_nonce=ids.launch_nonce,
        expected_transaction_id=ids.transaction_id,
    )
    return True


def _force_recover(worker: Any, acc: Account, popup: _DisconnectPopup) -> bool:
    flog(
        f"[WORKER] {acc.display_name} disconnect dialog held "
        f"{popup.effective_hold_sec:.0f}s -> force recover "
        f"({popup.reason_key}{popup.detail_suffix} {popup.evidence_note})"
    )
    ids = _account_runtime_ids(acc)
    worker._connection_error_since = None
    if popup.reason_key == "network_drop" and not worker.recovery._net.is_online():
        worker.runtime_owner.handle_runtime_signal(
            acc,
            "network_lost",
            "network_drop",
            payload={"trigger": "disconnect_dialog", "detail": popup.detail},
            expected_runtime_generation=ids.runtime_generation,
            expected_session_id=ids.session_id,
            expected_launch_nonce=ids.launch_nonce,
            expected_transaction_id=ids.transaction_id,
        )
        return True
    worker.runtime_owner.handle_runtime_signal(
        acc,
        "disconnect_detected",
        popup.reason_key,
        payload={
            "trigger": "disconnect_dialog",
            "detail": f"PID={acc.pid} UI={popup.detail}",
            "reason_msg": f"PID={acc.pid} UI={popup.detail}",
            "popup_code": popup.error_code,
            "popup_confidence": popup.confidence,
            "disconnect_category": popup.category,
            "visual_disconnect": popup.visual_disconnect,
        },
        expected_runtime_generation=ids.runtime_generation,
        expected_session_id=ids.session_id,
        expected_launch_nonce=ids.launch_nonce,
        expected_transaction_id=ids.transaction_id,
    )
    return True


def _hold_or_recover(worker: Any, acc: Account, popup: _DisconnectPopup) -> bool:
    if worker._connection_error_since is None:
        worker._connection_error_since = time.time()
        worker.runtime_owner.set_recovery_status(
            acc,
            status="disconnect_detected",
            reason=popup.reason_key,
            inflight=False,
        )
        flog(
            f"[WORKER] {acc.display_name} disconnect dialog detected - "
            f"will recover in {popup.effective_hold_sec:.0f}s "
            f"({popup.reason_key}{popup.detail_suffix} "
            f"{popup.evidence_note}: {popup.detail})"
        )
        _wait_brief(worker, min(1.0, popup.effective_hold_sec))
        return True
    if time.time() - worker._connection_error_since >= popup.effective_hold_sec:
        return _force_recover(worker, acc, popup)
    _wait_brief(worker, min(1.0, max(0.2, popup.effective_hold_sec / 2.0)))
    return True


def _handle_matched_popup(
    worker: Any,
    acc: Account,
    popup: _DisconnectPopup,
) -> bool:
    if not popup.recovery_allowed:
        return _ignore_visual_only(worker, acc, popup)
    if popup.action == "fail":
        return _fail_popup(worker, acc, popup)
    return _hold_or_recover(worker, acc, popup)


def _handle_disconnect_popup(worker: Any, acc: Account) -> bool:
    hold_sec = max(
        1.0,
        float(worker.cfg.get("connection_error_hold_time", 3) or 3),
    )
    disconnect_info = ProcessManager.inspect_disconnect_dialog(
        acc.pid,
        sample_count=2,
    )
    if not disconnect_info.get("matched"):
        _clear_disconnect_state(worker, acc)
        return False
    popup = _popup_from_info(disconnect_info, hold_sec)
    return _handle_matched_popup(worker, acc, popup)


def _kill_not_responding(worker: Any, acc: Account, nr_timeout: Any) -> bool:
    flog(f"[WORKER] {acc.display_name} Not Responding for {nr_timeout}s -> force kill")
    pid_was = acc.pid
    with acc._lock:
        runtime_generation = acc.runtime_generation
    kill_result = ProcessService.safe_kill_bound_process(
        acc,
        worker.state_mgr,
        reason="not_responding_recover",
        expected_runtime_generation=runtime_generation,
    )
    if kill_result.get("reason") == "stale_runtime_generation":
        return True
    worker._not_responding_since = None
    with acc._lock:
        signal_generation = acc.runtime_generation
    worker.report_fault(
        "not_responding",
        f"PID={pid_was}",
        expected_runtime_generation=signal_generation,
    )
    return True


def _handle_not_responding(
    worker: Any,
    acc: Account,
    runtime_seconds: float,
    nr_timeout: Any,
) -> bool:
    if runtime_seconds <= 30:
        return False
    if not ProcessManager.is_not_responding(acc.pid):
        worker._not_responding_since = None
        return False
    if worker._not_responding_since is None:
        worker._not_responding_since = time.time()
        flog(
            f"[WORKER] {acc.display_name} Not Responding - will kill in {nr_timeout}s"
        )
        return False
    if time.time() - worker._not_responding_since < nr_timeout:
        return False
    return _kill_not_responding(worker, acc, nr_timeout)


def handle_disconnect_checks(worker: Any, account: Account, runtime_seconds: float) -> bool:
    acc = account
    nr_timeout = worker.cfg.get("not_responding_timeout", 30)
    if _disconnect_checks_enabled(worker, runtime_seconds) and _handle_disconnect_popup(
        worker, acc
    ):
        return True
    if _handle_not_responding(worker, acc, runtime_seconds, nr_timeout):
        return True
    return False
