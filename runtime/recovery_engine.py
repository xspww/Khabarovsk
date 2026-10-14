"""Backward-compatible facade for the recovery_engine split.

The 1488-line coordinator was divided by concern; new code should import
from the canonical modules directly:

- runtime.recovery_core (wiring, lifecycle, slots, public entry points)
- runtime.recovery_txn (budget, begin/clear, cooldown, scheduling)
- runtime.recovery_reports (crash, launch, fail, revive, force-rejoin)
- runtime.recovery_guard_net (guard self-heal, network, hold logging)

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
    """Central recovery/rejoin controller. Every path that wants recovery reports here."""


RecoveryEngine = RecoveryCoordinator

__all__ = [
    "ACTIVE_SLOT_STATES",
    "RecoveryCoordinator",
    "RecoveryEngine",
]
