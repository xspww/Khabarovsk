"""Explicit narrow seam for the recovery module.

The coordinator used to pass itself as ``recovery: Any`` into the signal
router and the evaluator, so both could reach into a dozen private members.
That seam was fictional: the interface was as wide as the implementation.

This module names exactly what each collaborator needs. The coordinator
already implements these members; the type hints only make the contract
explicit so the interface becomes the test surface. No runtime behavior
changes here.
"""

from __future__ import annotations

from typing import Any, Protocol


class RouterRecoveryActions(Protocol):
    """The seven recovery entry points the signal router may invoke."""

    def fail_account(self, acc: Any, reason: str, msg: str = "") -> Any: ...
    def report_crash(
        self,
        acc: Any,
        reason: str,
        msg: str = "",
        cooldown: Any = None,
        context: Any = None,
    ) -> Any: ...
    def report_launch_failure(self, acc: Any, detail: str = "") -> Any: ...
    def report_launch_success(
        self,
        acc: Any,
        trigger: str = "",
        count_rejoin: Any = None,
        lua_confirmed: Any = False,
    ) -> Any: ...
    def mark_network_lost(self, acc: Any, trigger: str = "") -> Any: ...
    def evaluate(self, acc: Any, trigger: str = "", **kwargs: Any) -> Any: ...
    def force_rejoin(self, acc: Any) -> Any: ...


class EvaluatorRecovery(Protocol):
    """What the evaluator needs from the coordinator.

    Wider than the router seam on purpose: evaluation touches slots,
    budget, executor-hold, queue and cooldown. Naming it here keeps the
    next deepening step honest — shrinking this list is the work.
    """

    _runtime_state: Any
    _state_mgr: Any
    _queue: Any
    _net: Any
    _cfg: dict
    _stop: Any
    slot_policy: Any

    def fail_account(self, acc: Any, reason: str, msg: str = "") -> Any: ...
    def mark_network_lost(self, acc: Any, trigger: str = "") -> Any: ...
    def _schedule(self, acc: Any, delay: float, reason: str) -> Any: ...
    def _schedule_cooldown(self, acc: Any, delay: float, *args: Any) -> Any: ...
    def _queue_account(self, acc: Any, reason: str) -> Any: ...
    def _retry_bucket_exceeded(self, acc: Any) -> Any: ...
    def _executor_held(self) -> bool: ...
    def _park_for_executor_hold(self, acc: Any, trigger: str) -> Any: ...
    def maybe_revive_transient_failed(self, acc: Any, trigger: str) -> bool: ...
