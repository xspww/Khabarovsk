from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from domain.runtime_signals import RuntimeSignal, is_recovery_signal, normalize_runtime_signal
from runtime.recovery_context import SESSION_CONFLICT
from runtime.recovery_policy import canonical_reason, context_from_signal
from runtime.recovery_support import _enrich_visual_disconnect_payload_with_log
from runtime.runtime_state_manager import RuntimeStateManager
from services.auth_gate import evaluate_account_auth_gate, mark_account_auth_quarantined
from services.captcha_guard import CAPTCHA_BLOCK_REASON, CAPTCHA_REASON, is_captcha_status_text, set_account_captcha_hold


class RecoverySignalRouter:
    """Routes worker/watchdog/maintenance signals into recovery actions."""

    def __init__(
        self,
        runtime_state: RuntimeStateManager,
        is_closed: Callable[[], bool],
        log_decision: Callable[..., None],
        active_recovery_blocks: Callable[..., bool],
        dedupe_recovery_context: Callable[..., bool],
        duplicate_window: float,
    ):
        self._runtime_state = runtime_state
        self._is_closed = is_closed
        self._log_decision = log_decision
        self._active_recovery_blocks = active_recovery_blocks
        self._dedupe_recovery_context = dedupe_recovery_context
        self._duplicate_window = max(1.0, float(duplicate_window or 1.0))
        self._recent_signals: Dict[Tuple[str, str, str, int], float] = {}
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._recent_signals.clear()

    def route(
        self,
        recovery: Any,
        acc: Any,
        signal: str,
        reason: str = "",
        payload: Optional[Dict[str, Any]] = None,
        expected_runtime_generation: Optional[int] = None,
        expected_session_id: str = "",
        expected_launch_nonce: str = "",
        expected_transaction_id: str = "",
    ) -> bool:
        payload = _enrich_visual_disconnect_payload_with_log(dict(payload or {}))
        raw_signal = str(signal or "").strip().lower()
        signal_name = (
            RuntimeSignal.REJOIN_REQUESTED.value
            if raw_signal == RuntimeSignal.REJOIN_REQUESTED.value
            else normalize_runtime_signal(signal)
        )
        reason_key = str(payload.get("reason_key") or reason or signal_name or "runtime_signal")
        context = context_from_signal(acc, signal_name, reason_key, payload)
        if context.category == SESSION_CONFLICT:
            reason_key = "session_conflict"
            payload.setdefault("reason_key", reason_key)
            payload.setdefault("disconnect_category", SESSION_CONFLICT)
        captcha_resume_reason = str(reason_key or "").strip().lower() in {"captcha_resume"}
        captcha_detection_allowed = signal_name != RuntimeSignal.LAUNCH_SUCCESS.value
        captcha_detected = captcha_detection_allowed and (not captcha_resume_reason) and is_captcha_status_text(
            signal_name,
            reason_key,
            payload.get("reason_msg"),
            payload.get("detail"),
            payload.get("msg"),
            payload.get("error"),
        )

        if self._is_closed():
            self._log_decision(
                "runtime_signal_rejected",
                acc,
                reason_key,
                signal=signal_name,
                reject="coordinator_closed",
                **context.to_dict(),
            )
            return False

        with acc._lock:
            if not self._runtime_state.guard_session_identity(
                acc,
                expected_generation=expected_runtime_generation,
                expected_session_id=expected_session_id,
                expected_launch_nonce=expected_launch_nonce,
                expected_transaction_id=expected_transaction_id,
                reason=f"runtime_signal:{signal_name}:{reason_key}",
            ):
                self._log_decision(
                    "runtime_signal_rejected",
                    acc,
                    reason_key,
                    signal=signal_name,
                    reject="stale_identity",
                    expected_runtime_generation=expected_runtime_generation,
                    expected_session_id=expected_session_id,
                    expected_transaction_id=expected_transaction_id,
                    **context.to_dict(),
                )
                return False
            current_recovery_generation = int(acc.recovery_generation or 0)
            account_key = acc._config_username

        if captcha_detected:
            detail = str(payload.get("detail") or payload.get("reason_msg") or reason_key or CAPTCHA_REASON)
            self._log_decision("captcha_hold", acc, CAPTCHA_REASON, signal=signal_name, captcha_detail=detail, **context.to_dict())
            set_account_captcha_hold(acc, detail, source=f"runtime_signal:{signal_name}", runtime_writer=self._runtime_state)
            recovery.fail_account(acc, CAPTCHA_REASON, CAPTCHA_BLOCK_REASON)
            return True

        if is_recovery_signal(signal_name):
            auth_gate = evaluate_account_auth_gate(acc)
            if auth_gate.blocked and not captcha_resume_reason:
                gate_fields = {key: value for key, value in auth_gate.to_dict().items() if key != "reason"}
                self._log_decision(
                    "auth_gate_hold",
                    acc,
                    auth_gate.reason_key,
                    signal=signal_name,
                    auth_gate_reason=auth_gate.reason,
                    **gate_fields,
                    **context.to_dict(),
                )
                mark_account_auth_quarantined(
                    acc,
                    auth_gate,
                    source=f"runtime_signal:{signal_name}",
                    runtime_writer=self._runtime_state,
                )
                recovery.fail_account(acc, auth_gate.reason_key, auth_gate.reason)
                return True
            if self._active_recovery_blocks(acc, context, reason_key):
                return True
            if self._dedupe_recovery_context(context, acc, reason_key):
                return True
            if self._suppress_duplicate_signal(acc, account_key, signal_name, reason_key, current_recovery_generation, context):
                return True

        self._log_decision(
            "runtime_signal_dispatch",
            acc,
            reason_key,
            signal=signal_name,
            **context.to_dict(),
        )
        if not self._dispatch(recovery, acc, signal_name, reason_key, payload, context, expected_runtime_generation, expected_session_id, expected_launch_nonce, expected_transaction_id):
            return False
        return True

    def _suppress_duplicate_signal(
        self,
        acc: Any,
        account_key: str,
        signal_name: str,
        reason_key: str,
        recovery_generation: int,
        context: Any,
    ) -> bool:
        signal_key = (account_key, signal_name, canonical_reason(reason_key), recovery_generation)
        now = time.time()
        with self._lock:
            last_seen = float(self._recent_signals.get(signal_key, 0.0) or 0.0)
            if last_seen and (now - last_seen) < self._duplicate_window:
                self._log_decision(
                    "recovery_duplicate_suppressed",
                    acc,
                    reason_key,
                    signal=signal_name,
                    recovery_generation=recovery_generation,
                    age=f"{now - last_seen:.2f}",
                    **context.to_dict(),
                )
                return True
            self._recent_signals[signal_key] = now
            if len(self._recent_signals) > 512:
                cutoff = now - max(self._duplicate_window * 4, 60.0)
                self._recent_signals = {key: ts for key, ts in self._recent_signals.items() if ts >= cutoff}
        return False

    def _dispatch(
        self,
        recovery: Any,
        acc: Any,
        signal_name: str,
        reason_key: str,
        payload: Dict[str, Any],
        context: Any,
        expected_runtime_generation: Optional[int],
        expected_session_id: str,
        expected_launch_nonce: str,
        expected_transaction_id: str,
    ) -> bool:
        captcha_resume_reason = str(reason_key or "").strip().lower() in {"captcha_resume"}
        if signal_name != RuntimeSignal.LAUNCH_SUCCESS.value and (not captcha_resume_reason) and is_captcha_status_text(
            signal_name,
            reason_key,
            payload.get("reason_msg"),
            payload.get("detail"),
            payload.get("msg"),
            payload.get("error"),
        ):
            detail = str(payload.get("detail") or payload.get("reason_msg") or reason_key or CAPTCHA_REASON)
            set_account_captcha_hold(acc, detail, source=f"runtime_signal:{signal_name}", runtime_writer=self._runtime_state)
            recovery.fail_account(acc, CAPTCHA_REASON, CAPTCHA_BLOCK_REASON)
            return True
        if signal_name in {
            RuntimeSignal.FAULT.value,
            RuntimeSignal.CRASH.value,
            RuntimeSignal.WATCHDOG_TIMEOUT.value,
            RuntimeSignal.PROCESS_LOST.value,
            RuntimeSignal.LOADING_FREEZE.value,
        }:
            reason_msg = str(payload.get("reason_msg") or payload.get("detail") or reason_key)
            recovery.report_crash(acc, reason_key, reason_msg, cooldown=payload.get("cooldown"), context=context)
        elif signal_name in {RuntimeSignal.LAUNCH_FAILURE.value, RuntimeSignal.LAUNCH_FAILED.value}:
            recovery.report_launch_failure(acc, str(payload.get("detail") or reason_key or "launch_failed"))
        elif signal_name == RuntimeSignal.LAUNCH_SUCCESS.value:
            count_rejoin = payload.get("count_rejoin") if "count_rejoin" in payload else None
            recovery.report_launch_success(
                acc,
                trigger=str(payload.get("trigger") or reason_key or "launch_success"),
                count_rejoin=count_rejoin,
            )
        elif signal_name in {RuntimeSignal.FATAL.value, RuntimeSignal.AUTH_FAILURE.value, RuntimeSignal.SESSION_FAILURE.value}:
            reason_msg = str(payload.get("reason_msg") or payload.get("detail") or reason_key)
            recovery.fail_account(acc, reason_key, reason_msg)
        elif signal_name in {RuntimeSignal.NETWORK_LOST.value, RuntimeSignal.NETWORK_DROP.value}:
            recovery.mark_network_lost(acc, trigger=str(payload.get("trigger") or reason_key or "network_lost"))
        elif signal_name == RuntimeSignal.EVALUATE.value:
            recovery.evaluate(
                acc,
                trigger=str(payload.get("trigger") or reason_key or "runtime_signal"),
                force_restart=bool(payload.get("force_restart", False)),
                expected_runtime_generation=expected_runtime_generation,
                expected_session_id=expected_session_id,
                expected_launch_nonce=expected_launch_nonce,
                expected_transaction_id=expected_transaction_id,
            )
        elif signal_name == RuntimeSignal.REJOIN_REQUESTED.value:
            recovery.force_rejoin(acc)
        else:
            self._log_decision(
                "runtime_signal_rejected",
                acc,
                reason_key,
                signal=signal_name,
                reject="unsupported_signal",
            )
            return False
        return True
