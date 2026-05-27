from __future__ import annotations
import time
from typing import Any
from core import Account, flog
from services.process_service import ProcessManager, ProcessService
def handle_disconnect_checks(worker: Any, account: Account, runtime_seconds: float) -> bool:
    acc = account
    nr_timeout = worker.cfg.get("not_responding_timeout", 30)
    if runtime_seconds > 10 and worker.cfg.get("connection_error_rejoin", True) and worker.cfg.get("popup_disconnected_enabled", True):
        hold_sec = max(1.0, float(worker.cfg.get("connection_error_hold_time", 3) or 3))
        disconnect_info = ProcessManager.inspect_disconnect_dialog(acc.pid, sample_count=2)
        if disconnect_info.get("matched"):
            reason_key = str(disconnect_info.get("reason_key") or "connection_error")
            detail = str(disconnect_info.get("detail") or "")
            error_code = str(disconnect_info.get("error_code") or "")
            action = str(disconnect_info.get("action") or "rejoin")
            confidence = float(disconnect_info.get("popup_confidence", disconnect_info.get("confidence", 0.0)) or 0.0)
            effective_hold_sec = 1.0 if error_code in {"267", "268", "273", "277", "279"} else hold_sec
            evidence_note = f"source={disconnect_info.get('evidence_source', '')} confidence={confidence:.2f} samples={disconnect_info.get('positive_samples', 0)}/{disconnect_info.get('sample_count', 0)}"
            detail_suffix = f" code={error_code}" if error_code else ""
            if not disconnect_info.get("recovery_allowed"):
                flog(f"[WORKER] {acc.display_name} popup suspicious visual-only ignored ({evidence_note}: {detail})")
                worker._connection_error_since = None
                worker._wake.wait(timeout=min(1.0, max(0.2, hold_sec / 2.0)))
                worker._wake.clear()
                return True
            if action == "fail":
                flog(f"[WORKER] {acc.display_name} disconnect dialog requires stop (reason={reason_key}{detail_suffix} {evidence_note})")
                with acc._lock:
                    runtime_generation = acc.runtime_generation
                    session_id = acc.session_id
                    launch_nonce = acc.launch_nonce
                    transaction_id = acc.rejoin_transaction_id
                worker._connection_error_since = None
                reason_msg = worker.REASON_MESSAGES.get(reason_key, reason_key)
                worker.runtime_owner.handle_runtime_signal(
                    acc,
                    "fatal",
                    reason_key,
                    payload={
                        "reason_msg": f"{reason_msg} [PID={acc.pid} UI={detail}]",
                        "detail": detail,
                        "popup_code": error_code,
                        "popup_confidence": confidence,
                        "disconnect_category": disconnect_info.get("disconnect_category", ""),
                    },
                    expected_runtime_generation=runtime_generation,
                    expected_session_id=session_id,
                    expected_launch_nonce=launch_nonce,
                    expected_transaction_id=transaction_id,
                )
                return True
            if worker._connection_error_since is None:
                worker._connection_error_since = time.time()
                worker.runtime_owner.set_recovery_status(acc, status="disconnect_detected", reason=reason_key, inflight=False)
                flog(f"[WORKER] {acc.display_name} disconnect dialog detected - will recover in {effective_hold_sec:.0f}s ({reason_key}{detail_suffix} {evidence_note}: {detail})")
                worker._wake.wait(timeout=min(1.0, effective_hold_sec))
                worker._wake.clear()
                return True
            elif time.time() - worker._connection_error_since >= effective_hold_sec:
                flog(f"[WORKER] {acc.display_name} disconnect dialog held {effective_hold_sec:.0f}s -> force recover ({reason_key}{detail_suffix} {evidence_note})")
                with acc._lock:
                    runtime_generation = acc.runtime_generation
                    session_id = acc.session_id
                    launch_nonce = acc.launch_nonce
                    transaction_id = acc.rejoin_transaction_id
                worker._connection_error_since = None
                if reason_key == "network_drop" and not worker.recovery._net.is_online():
                    worker.runtime_owner.handle_runtime_signal(
                        acc,
                        "network_lost",
                        "network_drop",
                        payload={"trigger": "disconnect_dialog", "detail": detail},
                        expected_runtime_generation=runtime_generation,
                        expected_session_id=session_id,
                        expected_launch_nonce=launch_nonce,
                        expected_transaction_id=transaction_id,
                    )
                else:
                    worker.runtime_owner.handle_runtime_signal(
                        acc,
                        "disconnect_detected",
                        reason_key,
                        payload={
                            "trigger": "disconnect_dialog",
                            "detail": f"PID={acc.pid} UI={detail}",
                            "reason_msg": f"PID={acc.pid} UI={detail}",
                            "popup_code": error_code,
                            "popup_confidence": confidence,
                            "disconnect_category": disconnect_info.get("disconnect_category", ""),
                            "visual_disconnect": bool(disconnect_info.get("visual_disconnect", False)),
                        },
                        expected_runtime_generation=runtime_generation,
                        expected_session_id=session_id,
                        expected_launch_nonce=launch_nonce,
                        expected_transaction_id=transaction_id,
                    )
                return True
            else:
                worker._wake.wait(timeout=min(1.0, max(0.2, effective_hold_sec / 2.0)))
                worker._wake.clear()
                return True
        else:
            worker._connection_error_since = None
            if acc.recovery_status in {"checking_disconnect", "disconnect_detected"} and not acc.recovery_inflight:
                worker.runtime_owner.set_recovery_status(acc, status="in_game", reason="disconnect_check_clear", inflight=False)
    if runtime_seconds > 30:
        if ProcessManager.is_not_responding(acc.pid):
            if worker._not_responding_since is None:
                worker._not_responding_since = time.time()
                flog(f"[WORKER] {acc.display_name} Not Responding - will kill in {nr_timeout}s")
            elif time.time() - worker._not_responding_since >= nr_timeout:
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
                worker.report_fault("not_responding", f"PID={pid_was}", expected_runtime_generation=signal_generation)
                return True
        else:
            worker._not_responding_since = None
    return False
