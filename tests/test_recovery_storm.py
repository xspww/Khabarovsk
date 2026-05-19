import unittest

from env_bootstrap import ensure_test_user_root

ensure_test_user_root()

from core import Account, AccountState
from runtime.recovery_storm import RecoveryStormController


class FakeClock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


def _cfg(**overrides):
    cfg = {
        "recovery_storm_enabled": True,
        "recovery_storm_max_active": 3,
        "recovery_storm_min_spacing_seconds": 5,
        "recovery_storm_jitter_seconds": 0,
        "recovery_storm_outage_backoff_seconds": 30,
    }
    cfg.update(overrides)
    return cfg


class RecoveryStormControllerTests(unittest.TestCase):
    def test_disabled_preserves_requested_delay(self):
        acc = Account("DisabledUser")
        storm = RecoveryStormController({}, [acc], clock=FakeClock())

        decision = storm.reserve_delay(acc, 7.0, "disconnect")

        self.assertEqual(decision.delay_seconds, 7.0)
        self.assertEqual(decision.reason, "disabled")
        self.assertFalse(decision.delayed)

    def test_global_spacing_delays_second_immediate_recovery(self):
        clock = FakeClock()
        first = Account("FirstUser")
        second = Account("SecondUser")
        storm = RecoveryStormController(_cfg(recovery_storm_min_spacing_seconds=10), [first, second], clock=clock)

        first_decision = storm.reserve_delay(first, 0.0, "disconnect")
        second_decision = storm.reserve_delay(second, 0.0, "disconnect")

        self.assertEqual(first_decision.delay_seconds, 0.0)
        self.assertGreaterEqual(second_decision.delay_seconds, 10.0)
        self.assertEqual(second_decision.reason, "global_spacing")

    def test_network_outage_backoff_delays_recovery(self):
        acc = Account("OutageUser")
        storm = RecoveryStormController(_cfg(recovery_storm_min_spacing_seconds=0), [acc], clock=FakeClock())

        decision = storm.reserve_delay(acc, 0.0, "network_lost", net_online=False)

        self.assertGreaterEqual(decision.delay_seconds, 30.0)
        self.assertEqual(decision.reason, "network_outage_backoff")

    def test_active_recovery_limit_adds_delay(self):
        active = Account("ActiveUser")
        active.state = AccountState.COOLDOWN
        queued = Account("QueuedUser")
        storm = RecoveryStormController(
            _cfg(recovery_storm_max_active=1, recovery_storm_min_spacing_seconds=5),
            [active, queued],
            clock=FakeClock(),
        )

        decision = storm.reserve_delay(queued, 0.0, "crash")

        self.assertGreaterEqual(decision.delay_seconds, 5.0)
        self.assertEqual(decision.reason, "max_active_recovery")
        self.assertEqual(decision.active_recovery_count, 1)

    def test_snapshot_exposes_operational_truth(self):
        active = Account("SnapshotActive")
        active.recovery_inflight = True
        idle = Account("SnapshotIdle")
        storm = RecoveryStormController(_cfg(), [active, idle], clock=FakeClock())

        snapshot = storm.snapshot()

        self.assertTrue(snapshot["enabled"])
        self.assertEqual(snapshot["active_recovery_count"], 1)
        self.assertIn("last_decision", snapshot)


if __name__ == "__main__":
    unittest.main()
