import time
import unittest

from env_bootstrap import ensure_test_user_root

ensure_test_user_root()

from core import Account, AccountState, SmartQueue
from runtime.machine_supervisor import MachineSupervisor
from runtime.runtime_truth import TRUTH_CONFIRMED, TRUTH_QUARANTINED, TRUTH_SUSPECT, build_account_truth


def _probe_resource(cpu=10.0, memory=20.0):
    return lambda: {"cpu_percent": cpu, "memory_percent": memory}


class MachineSupervisorTests(unittest.TestCase):
    def test_blocks_launch_when_multi_roblox_guard_is_not_ready(self):
        acc = Account("BlockedUser")
        sup = MachineSupervisor(
            {"multi_roblox_enabled": True},
            [acc],
            resource_probe=_probe_resource(),
            guard_probe=lambda: {"state": "failed", "pid": 0},
            process_probe=lambda: [],
        )

        decision = sup.launch_decision(acc)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "multi_roblox_guard_not_ready")

    def test_blocks_next_launch_when_wave_is_busy(self):
        active = Account("LaunchingUser")
        active.state = AccountState.LAUNCHING
        queued = Account("QueuedUser")
        sup = MachineSupervisor(
            {"machine_supervisor_max_launching_accounts": 1, "multi_roblox_enabled": False},
            [active, queued],
            resource_probe=_probe_resource(),
            guard_probe=lambda: {"state": "disabled", "pid": 0},
            process_probe=lambda: [],
        )

        decision = sup.launch_decision(queued)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "launch_wave_busy")

    def test_allows_second_launch_when_wave_limit_is_raised(self):
        active = Account("LaunchingUser")
        active.state = AccountState.LAUNCHING
        queued = Account("QueuedUser")
        sup = MachineSupervisor(
            {"machine_supervisor_max_launching_accounts": 2, "multi_roblox_enabled": False},
            [active, queued],
            resource_probe=_probe_resource(),
            guard_probe=lambda: {"state": "disabled", "pid": 0},
            process_probe=lambda: [],
        )

        decision = sup.launch_decision(queued)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed")

    def test_blocks_launch_when_machine_is_over_pressure(self):
        acc = Account("PressureUser")
        sup = MachineSupervisor(
            {"machine_supervisor_cpu_high_percent": 80, "multi_roblox_enabled": False},
            [acc],
            resource_probe=_probe_resource(cpu=90.0),
            guard_probe=lambda: {"state": "disabled", "pid": 0},
            process_probe=lambda: [],
        )

        decision = sup.launch_decision(acc)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "cpu_pressure")

    def test_disabled_supervisor_allows_launch_under_pressure(self):
        acc = Account("DisabledSupervisorUser")
        sup = MachineSupervisor(
            {
                "machine_supervisor_enabled": False,
                "machine_supervisor_cpu_high_percent": 80,
                "multi_roblox_enabled": False,
            },
            [acc],
            resource_probe=_probe_resource(cpu=90.0),
            guard_probe=lambda: {"state": "disabled", "pid": 0},
            process_probe=lambda: [],
        )

        decision = sup.launch_decision(acc)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "disabled")

    def test_blocks_launch_when_live_process_capacity_is_reached(self):
        acc = Account("CapacityUser")
        sup = MachineSupervisor(
            {"max_concurrent_accounts": 1, "multi_roblox_enabled": False},
            [acc],
            resource_probe=_probe_resource(),
            guard_probe=lambda: {"state": "disabled", "pid": 0},
            process_probe=lambda: [{"pid": 1234}],
        )

        decision = sup.launch_decision(acc)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "live_process_capacity_reached")

    def test_queue_delay_prevents_immediate_repop(self):
        queue = SmartQueue()
        acc = Account("DelayUser")

        queue.push(acc, reason="machine_hold", delay_seconds=0.2)

        self.assertIsNone(queue.pop(timeout=0.01))
        self.assertIs(queue.pop(timeout=0.5), acc)

    def test_runtime_truth_quarantines_auth_or_captcha_accounts(self):
        acc = Account("CaptchaUser")
        acc.last_crash_reason = "captcha_required"

        truth = build_account_truth(acc, process_alive=True)

        self.assertEqual(truth.truth_state, TRUTH_QUARANTINED)
        self.assertIn("auth_or_captcha_quarantine", truth.reasons)

    def test_runtime_truth_confirms_only_with_multiple_live_signals(self):
        now = time.time()
        acc = Account("LiveUser")
        acc.state = AccountState.IN_GAME
        acc.pid = 1234
        acc.process_binding_status = "verified"
        acc.process_binding_confidence = 80.0
        acc.process_proof_level = "strong"
        acc.last_activity_at = now
        acc.observed_server_at = now

        truth = build_account_truth(acc, process_alive=True, window_count=1, now=now)

        self.assertEqual(truth.truth_state, TRUTH_CONFIRMED)
        self.assertGreaterEqual(truth.confidence, 70.0)

    def test_runtime_truth_marks_ingame_without_process_as_suspect(self):
        acc = Account("MissingPidUser")
        acc.state = AccountState.IN_GAME

        truth = build_account_truth(acc, process_alive=False, now=1000.0)

        self.assertEqual(truth.truth_state, TRUTH_SUSPECT)
        self.assertIn("in_game_without_live_process", truth.reasons)


if __name__ == "__main__":
    unittest.main()
