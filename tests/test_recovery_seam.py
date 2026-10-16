"""Proves the recovery seam is narrow: the router works through 7 actions only.

No coordinator, no farm, no network. If this passes, the interface is the
test surface — deleting the coordinator still leaves a usable contract.
"""
import threading
import unittest

from runtime.recovery_seam import EvaluatorRecovery, RouterRecoveryActions
from runtime.recovery_signal_router import RecoverySignalRouter
from runtime.recovery_support import should_suppress_recovery_schedule
from runtime.runtime_state_manager import RuntimeStateManager
from services.captcha_guard import CAPTCHA_REASON
from types import SimpleNamespace


class _FakeRecovery:
    def __init__(self):
        self.failed = []

    def fail_account(self, acc, reason, msg=""):
        self.failed.append((reason, msg))
        return True

    def report_crash(self, acc, reason, msg="", cooldown=None, context=None):
        return True

    def report_launch_failure(self, acc, detail=""):
        return True

    def report_launch_success(self, acc, trigger="", count_rejoin=None, lua_confirmed=False):
        return True

    def mark_network_lost(self, acc, trigger=""):
        return True

    def evaluate(self, acc, trigger="", **kwargs):
        return True

    def force_rejoin(self, acc):
        return True


def _acc():
    ns = SimpleNamespace(
        _lock=threading.Lock(),
        _config_username="",
        display_name="fake",
        session_checked=False,
        session_valid=False,
        session_wait_started_at=0.0,
        manual_status="",
        import_status="",
        last_error="",
        last_crash_reason="",
        last_recovery_reason="",
        last_state_reason="",
        recovery_scheduled_at=0.0,
    )
    ns.sync_runtime = lambda *a, **k: None
    return ns


class TestNarrowSeam(unittest.TestCase):
    def test_dispatch_captcha_needs_only_fail_account(self):
        state = RuntimeStateManager(logger=lambda *a, **k: None)
        router = RecoverySignalRouter(
            state,
            is_closed=lambda: False,
            log_decision=lambda *a, **k: None,
            recovery_gate=SimpleNamespace(check=lambda **k: {"ignored": False}),
        )
        fake = _FakeRecovery()
        ok = router._dispatch(
            fake,
            _acc(),
            "fault",
            "captcha_required",
            {"detail": "captcha required, please solve this challenge"},
            None,
            None,
            "",
            "",
            "",
        )
        self.assertTrue(ok)
        self.assertTrue(fake.failed)
        self.assertEqual(fake.failed[0][0], CAPTCHA_REASON)

    def test_schedule_suppress_single_owner(self):
        job = SimpleNamespace(
            due_at=10.0,
            recovery_generation=3,
            runtime_generation=7,
            command_generation=1,
        )
        self.assertTrue(should_suppress_recovery_schedule(job, 12.0, 3, 7, 1))
        self.assertFalse(should_suppress_recovery_schedule(job, 9.0, 3, 7, 1))
        self.assertFalse(should_suppress_recovery_schedule(job, 12.0, 4, 7, 1))
        self.assertFalse(should_suppress_recovery_schedule(None, 12.0, 3, 7, 1))

    def test_protocols_listed(self):
        self.assertTrue(hasattr(RouterRecoveryActions, "fail_account"))
        self.assertIn("slot_policy", EvaluatorRecovery.__annotations__)


class TestSlotPolicy(unittest.TestCase):
    def test_counts_only_desired_in_game_slots(self):
        import threading

        from core import AccountState
        from runtime.recovery_slots import RecoverySlotPolicy, describe_slot_full

        def _item(desired, state):
            return SimpleNamespace(
                _lock=threading.Lock(),
                desired_state=desired,
                state=state,
            )

        policy = RecoverySlotPolicy(
            cfg_getter=lambda: {"max_concurrent_accounts": 2},
            accounts_getter=lambda: [
                _item(AccountState.IN_GAME, AccountState.IN_GAME),
                _item(AccountState.IN_GAME, AccountState.QUEUED),
                _item(AccountState.READY, AccountState.IN_GAME),
            ],
        )
        self.assertEqual(policy.active_slot_count(), 2)
        self.assertEqual(policy.max_concurrent_accounts(), 2)
        self.assertFalse(policy.slot_available(_item(AccountState.IN_GAME, AccountState.READY)))
        self.assertIn("max=2", describe_slot_full(policy, None))


if __name__ == "__main__":
    unittest.main()
