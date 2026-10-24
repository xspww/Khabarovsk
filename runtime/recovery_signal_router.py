from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple

from domain.states import RuntimeSignal, is_recovery_signal, normalize_runtime_signal
from runtime.lua_liveness_policy import lua_event_source
from runtime.recovery_context import SESSION_CONFLICT
from runtime.recovery_policy import context_from_signal
from runtime.recovery_seam import RouterRecoveryActions
from runtime.recovery_support import _enrich_visual_disconnect_payload_with_log
from runtime.runtime_state_manager import RuntimeStateManager
from services.auth_gate import evaluate_account_auth_gate, mark_account_auth_quarantined
from services.captcha_guard import (
    CAPTCHA_BLOCK_REASON,
    CAPTCHA_REASON,
    is_captcha_status_text,
    set_account_captcha_hold,
)


class _ExpectedIdentity(NamedTuple):
    runtime_generation: int | None
    session_id: str
    launch_nonce: str
    transaction_id: str


class _DispatchContext(NamedTuple):
    recovery: RouterRecoveryActions
    acc: Any
    signal_name: str
    reason_key: str
    payload: dict[str, Any]
    context: Any
    expected: _ExpectedIdentity


def _first_signal_text(
    payload: dict[str, Any],
    keys: tuple[str, ...],
    reason_key: str,
    default: str,
) -> str:
    for key in keys:
        value = payload.get(key)
        if value:
            return str(value)
    return str(reason_key or default)


def _captcha_detail(payload: dict[str, Any], reason_key: str) -> str:
    return _first_signal_text(
        payload,
        ("detail", "reason_msg"),
        reason_key,
        CAPTCHA_REASON,
    )


def _normalized_signal(signal: str) -> str:
    raw_signal = str(signal or "").strip().lower()
    if raw_signal == RuntimeSignal.REJOIN_REQUESTED.value:
        return RuntimeSignal.REJOIN_REQUESTED.value
    return normalize_runtime_signal(signal)


def _reason_key(
    payload: dict[str, Any],
    reason: str,
    signal_name: str,
) -> str:
    return str(
        payload.get("reason_key") or reason or signal_name or "runtime_signal"
    )


def _session_reason(
    context: Any,
    payload: dict[str, Any],
    reason_key: str,
) -> str:
    if context.category == SESSION_CONFLICT:
        reason_key = "session_conflict"
        payload.setdefault("reason_key", reason_key)
        payload.setdefault("disconnect_category", SESSION_CONFLICT)
    return reason_key


class RecoverySignalRouter:
    """Routes worker/watchdog/maintenance signals into recovery actions."""

    def __init__(
        self,
        runtime_state: RuntimeStateManager,
        is_closed: Callable[[], bool],
        log_decision: Callable[..., None],
        recovery_gate: Any,
    ):
        self._runtime_state = runtime_state
        self._is_closed = is_closed
        self._log_decision = log_decision
        self._recovery_gate = recovery_gate

    @staticmethod
    def _is_captcha_hold(
        signal_name: str,
        reason_key: str,
        payload: dict[str, Any],
        captcha_resume_reason: bool,
    ) -> bool:
        """Single owner for the captcha-detect predicate.

        Both the route path and the dispatch path used to repeat this
        check inline; the recovery decision now leverages this one.
        """
        if captcha_resume_reason:
            return False
        if signal_name == RuntimeSignal.LAUNCH_SUCCESS.value:
            return False
        return bool(
            is_captcha_status_text(
                signal_name,
                reason_key,
                payload.get("reason_msg"),
                payload.get("detail"),
                payload.get("msg"),
                payload.get("error"),
            )
        )

    def clear(self) -> None:
        self._recovery_gate.clear()

    def _read_route_state(
        self,
        acc: Any,
        expected: _ExpectedIdentity,
        signal_name: str,
        reason_key: str,
        context: Any,
    ) -> tuple[int, str] | None:
        with acc._lock:
            if not self._runtime_state.guard_session_identity(
                acc,
                expected_generation=expected.runtime_generation,
                expected_session_id=expected.session_id,
                expected_launch_nonce=expected.launch_nonce,
                expected_transaction_id=expected.transaction_id,
                reason=f"runtime_signal:{signal_name}:{reason_key}",
            ):
                self._log_decision(
                    "runtime_signal_rejected",
                    acc,
                    reason_key,
                    signal=signal_name,
                    reject="stale_identity",
                    expected_runtime_generation=expected.runtime_generation,
                    expected_session_id=expected.session_id,
                    expected_transaction_id=expected.transaction_id,
                    **context.to_dict(),
                )
                return None
            return int(acc.recovery_generation or 0), acc._config_username

    def _apply_captcha_hold(
        self,
        recovery: RouterRecoveryActions,
        acc: Any,
        signal_name: str,
        payload: dict[str, Any],
        reason_key: str,
    ) -> None:
        detail = _captcha_detail(payload, reason_key)
        set_account_captcha_hold(
            acc,
            detail,
            source=f"runtime_signal:{signal_name}",
            runtime_writer=self._runtime_state,
        )
        recovery.fail_account(acc, CAPTCHA_REASON, CAPTCHA_BLOCK_REASON)

    def _handle_captcha_hold(
        self,
        recovery: RouterRecoveryActions,
        acc: Any,
        signal_name: str,
        payload: dict[str, Any],
        reason_key: str,
        context: Any,
    ) -> bool:
        detail = _captcha_detail(payload, reason_key)
        self._log_decision(
            "captcha_hold",
            acc,
            CAPTCHA_REASON,
            signal=signal_name,
            captcha_detail=detail,
            **context.to_dict(),
        )
        self._apply_captcha_hold(recovery, acc, signal_name, payload, reason_key)
        return True

    def _handle_auth_gate(
        self,
        recovery: RouterRecoveryActions,
        acc: Any,
        signal_name: str,
        context: Any,
        auth_gate: Any,
    ) -> bool:
        gate_fields = {
            ("auth_gate_category" if key == "category" else key): value
            for key, value in auth_gate.to_dict().items()
            if key != "reason"
        }
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

    def _handle_gate_verdict(
        self,
        acc: Any,
        context: Any,
        reason_key: str,
        verdict: dict[str, Any],
    ) -> bool:
        if not verdict.get("ignored"):
            return False
        self._log_decision(
            verdict.get("event", "recovery_ignored"),
            acc,
            reason_key,
            **verdict.get("fields", {}),
            **context.to_dict(),
        )
        return True

    def _handled_result(self, signal_name: str) -> bool:
        """Report whether a handled signal counts as an accepted rejoin.

        Auth, captcha and recovery-gate suppression all mean "handled, but
        no rejoin ran". Returning True for ``rejoin_requested`` in those
        paths used to make the caller report a successful rejoin.
        """
        return signal_name != RuntimeSignal.REJOIN_REQUESTED.value

    def route(
        self,
        recovery: RouterRecoveryActions,
        acc: Any,
        signal: str,
        reason: str = "",
        payload: dict[str, Any] | None = None,
        expected_runtime_generation: int | None = None,
        expected_session_id: str = "",
        expected_launch_nonce: str = "",
        expected_transaction_id: str = "",
    ) -> bool:
        payload = _enrich_visual_disconnect_payload_with_log(dict(payload or {}))
        signal_name = _normalized_signal(signal)
        reason_key = _reason_key(payload, reason, signal_name)
        context = context_from_signal(acc, signal_name, reason_key, payload)
        reason_key = _session_reason(context, payload, reason_key)
        captcha_resume_reason = (
            str(reason_key or "").strip().lower() == "captcha_resume"
        )
        captcha_detected = self._is_captcha_hold(
            signal_name,
            reason_key,
            payload,
            captcha_resume_reason,
        )
        expected = _ExpectedIdentity(
            expected_runtime_generation,
            expected_session_id,
            expected_launch_nonce,
            expected_transaction_id,
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

        state = self._read_route_state(
            acc,
            expected,
            signal_name,
            reason_key,
            context,
        )
        if state is None:
            return False
        current_recovery_generation, account_key = state

        if captcha_detected:
            self._handle_captcha_hold(
                recovery,
                acc,
                signal_name,
                payload,
                reason_key,
                context,
            )
            return self._handled_result(signal_name)

        if is_recovery_signal(signal_name):
            auth_gate = evaluate_account_auth_gate(acc)
            if auth_gate.blocked and not captcha_resume_reason:
                self._handle_auth_gate(
                    recovery,
                    acc,
                    signal_name,
                    context,
                    auth_gate,
                )
                return self._handled_result(signal_name)
            verdict = self._recovery_gate.check(
                account_key=account_key,
                ctx=context,
                signal_name=signal_name,
                reason_key=reason_key,
                recovery_generation=current_recovery_generation,
            )
            if self._handle_gate_verdict(acc, context, reason_key, verdict):
                return self._handled_result(signal_name)

        self._log_decision(
            "runtime_signal_dispatch",
            acc,
            reason_key,
            signal=signal_name,
            **context.to_dict(),
        )
        return self._dispatch(
            recovery,
            acc,
            signal_name,
            reason_key,
            payload,
            context,
            expected_runtime_generation,
            expected_session_id,
            expected_launch_nonce,
            expected_transaction_id,
        )

    def _dispatch(
        self,
        recovery: RouterRecoveryActions,
        acc: Any,
        signal_name: str,
        reason_key: str,
        payload: dict[str, Any],
        context: Any,
        expected_runtime_generation: int | None,
        expected_session_id: str,
        expected_launch_nonce: str,
        expected_transaction_id: str,
    ) -> bool:
        expected = _ExpectedIdentity(
            expected_runtime_generation,
            expected_session_id,
            expected_launch_nonce,
            expected_transaction_id,
        )
        return self._dispatch_context(
            _DispatchContext(
                recovery,
                acc,
                signal_name,
                reason_key,
                payload,
                context,
                expected,
            )
        )

    def _dispatch_context(self, ctx: _DispatchContext) -> bool:
        captcha_resume_reason = (
            str(ctx.reason_key or "").strip().lower() == "captcha_resume"
        )
        if self._is_captcha_hold(
            ctx.signal_name,
            ctx.reason_key,
            ctx.payload,
            captcha_resume_reason,
        ):
            self._apply_captcha_hold(
                ctx.recovery,
                ctx.acc,
                ctx.signal_name,
                ctx.payload,
                ctx.reason_key,
            )
            return True
        if ctx.signal_name in {
            RuntimeSignal.FAULT.value,
            RuntimeSignal.CRASH.value,
            RuntimeSignal.WATCHDOG_TIMEOUT.value,
            RuntimeSignal.PROCESS_LOST.value,
            RuntimeSignal.LOADING_FREEZE.value,
        }:
            self._dispatch_crash(ctx)
            return True
        if ctx.signal_name in {
            RuntimeSignal.LAUNCH_FAILURE.value,
            RuntimeSignal.LAUNCH_FAILED.value,
        }:
            self._dispatch_launch_failure(ctx)
            return True
        if ctx.signal_name == RuntimeSignal.LAUNCH_SUCCESS.value:
            self._dispatch_launch_success(ctx)
            return True
        return self._dispatch_remaining(ctx)

    def _dispatch_remaining(self, ctx: _DispatchContext) -> bool:
        if ctx.signal_name in {
            RuntimeSignal.FATAL.value,
            RuntimeSignal.AUTH_FAILURE.value,
            RuntimeSignal.SESSION_FAILURE.value,
        }:
            self._dispatch_fatal(ctx)
            return True
        if ctx.signal_name in {
            RuntimeSignal.NETWORK_LOST.value,
            RuntimeSignal.NETWORK_DROP.value,
        }:
            self._dispatch_network(ctx)
            return True
        if ctx.signal_name == RuntimeSignal.EVALUATE.value:
            self._dispatch_evaluate(ctx)
            return True
        if ctx.signal_name == RuntimeSignal.REJOIN_REQUESTED.value:
            return bool(ctx.recovery.force_rejoin(ctx.acc))
        self._log_decision(
            "runtime_signal_rejected",
            ctx.acc,
            ctx.reason_key,
            signal=ctx.signal_name,
            reject="unsupported_signal",
        )
        return False

    def _dispatch_crash(self, ctx: _DispatchContext) -> None:
        ctx.recovery.report_crash(
            ctx.acc,
            ctx.reason_key,
            _first_signal_text(
                ctx.payload,
                ("reason_msg", "detail"),
                ctx.reason_key,
                "",
            ),
            cooldown=ctx.payload.get("cooldown"),
            context=ctx.context,
        )

    def _dispatch_launch_failure(self, ctx: _DispatchContext) -> None:
        ctx.recovery.report_launch_failure(
            ctx.acc,
            _first_signal_text(
                ctx.payload,
                ("detail",),
                ctx.reason_key,
                "launch_failed",
            ),
        )

    def _dispatch_launch_success(self, ctx: _DispatchContext) -> None:
        count_rejoin = (
            ctx.payload.get("count_rejoin")
            if "count_rejoin" in ctx.payload
            else None
        )
        ctx.recovery.report_launch_success(
            ctx.acc,
            trigger=_first_signal_text(
                ctx.payload,
                ("trigger",),
                ctx.reason_key,
                "launch_success",
            ),
            count_rejoin=count_rejoin,
            lua_confirmed=lua_event_source(ctx.reason_key, ctx.payload),
        )

    def _dispatch_fatal(self, ctx: _DispatchContext) -> None:
        ctx.recovery.fail_account(
            ctx.acc,
            ctx.reason_key,
            _first_signal_text(
                ctx.payload,
                ("reason_msg", "detail"),
                ctx.reason_key,
                "",
            ),
        )

    def _dispatch_network(self, ctx: _DispatchContext) -> None:
        ctx.recovery.mark_network_lost(
            ctx.acc,
            trigger=_first_signal_text(
                ctx.payload,
                ("trigger",),
                ctx.reason_key,
                "network_lost",
            ),
        )

    def _dispatch_evaluate(self, ctx: _DispatchContext) -> None:
        ctx.recovery.evaluate(
            ctx.acc,
            trigger=_first_signal_text(
                ctx.payload,
                ("trigger",),
                ctx.reason_key,
                "runtime_signal",
            ),
            force_restart=bool(ctx.payload.get("force_restart", False)),
            expected_runtime_generation=ctx.expected.runtime_generation,
            expected_session_id=ctx.expected.session_id,
            expected_launch_nonce=ctx.expected.launch_nonce,
            expected_transaction_id=ctx.expected.transaction_id,
        )
