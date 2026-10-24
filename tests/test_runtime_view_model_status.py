"""Regression tests for dashboard account status labels.

Bug: when the network dropped, ``mark_network_lost`` left the old
``last_crash_reason="game_unavailable"`` in place. The label helpers then
reported "Game Close" even though the account was offline.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from core import AccountState
from runtime.runtime_view_model import _game_closed, _hold_label


def _acc(**overrides):
    base = {
        "recovery_status": "",
        "last_recovery_reason": "",
        "last_crash_reason": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class NetworkLossLabelTests(unittest.TestCase):
    def test_stale_game_close_is_hidden_while_network_lost(self):
        acc = _acc(
            recovery_status="network_lost",
            last_recovery_reason="network_drop",
            last_crash_reason="game_unavailable",
        )
        self.assertFalse(_game_closed(acc))
        self.assertEqual(
            _hold_label(AccountState.NETWORK_LOST, False, False, True, True),
            ("Disconnected", "#f97316"),
        )

    def test_network_drop_reason_masks_stale_game_close(self):
        acc = _acc(
            recovery_status="cooldown",
            last_recovery_reason="network_drop",
            last_crash_reason="game_unavailable",
        )
        self.assertFalse(_game_closed(acc))

    def test_stale_crash_reason_alone_is_not_game_close(self):
        acc = _acc(last_crash_reason="game_unavailable")
        self.assertFalse(_game_closed(acc))

    def test_genuine_game_close_still_wins(self):
        acc = _acc(
            recovery_status="cooldown",
            last_recovery_reason="game_unavailable",
            last_crash_reason="game_unavailable",
        )
        self.assertTrue(_game_closed(acc))
        self.assertEqual(
            _hold_label(AccountState.COOLDOWN, False, False, True, False),
            ("Game Close", "#fcd34d"),
        )

    def test_in_game_account_is_not_relabeled_disconnected(self):
        self.assertIsNone(
            _hold_label(AccountState.IN_GAME, True, False, False, True)
        )


if __name__ == "__main__":
    unittest.main()
