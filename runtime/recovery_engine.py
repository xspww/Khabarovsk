"""Backward-compatible facade for the recovery_engine split.

The 1488-line coordinator was divided by concern; new code should import
from the canonical modules directly:

- runtime.recovery_core (wiring, lifecycle, slots, public entry points)
- runtime.recovery_txn (budget, begin/clear, cooldown, scheduling)
- runtime.recovery_reports (crash, launch, fail, revive, force-rejoin)
- runtime.recovery_guard_net (guard self-heal, network, hold logging)
- runtime.recovery_seam (narrow RouterRecoveryActions / EvaluatorRecovery
  contracts — the interface that is the test surface)
- runtime.recovery_slots (slot counting — the evaluator leverages this one)

Everything below is a re-export so existing `from runtime.recovery_engine
import RecoveryEngine` sites keep working unchanged.
"""

from __future__ import annotations

from runtime.recovery_core import ACTIVE_SLOT_STATES, RecoveryCoreMixin
from runtime.recovery_guard_net import RecoveryGuardNetMixin
from runtime.recovery_reports import RecoveryReportsMixin
from runtime.recovery_txn import RecoveryTxnMixin


class RecoveryCoordinator(
    RecoveryCoreMixin,
    RecoveryTxnMixin,
    RecoveryReportsMixin,
    RecoveryGuardNetMixin,
):
    """Central recovery/rejoin controller. Every path that wants recovery reports here.

    Ownership map (one concept, one home file):
    - wiring/lifecycle/slots/park → recovery_core (+ recovery_slots)
    - budget/begin/clear/cooldown/schedule → recovery_txn
    - report_crash/report_launch_*/fail_account/revive/force_rejoin → recovery_reports
    - guard self-heal/network hold → recovery_guard_net
    - signal admission verdict → recovery_policy.RecoveryGate
    - narrow collaborator contracts → recovery_seam
    """


RecoveryEngine = RecoveryCoordinator

__all__ = [
    "ACTIVE_SLOT_STATES",
    "RecoveryCoordinator",
    "RecoveryEngine",
]
