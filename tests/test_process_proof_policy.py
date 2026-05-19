import atexit
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch


_TEST_USER_ROOT = tempfile.mkdtemp(prefix="cronus-test-user-root-")
if "CRONUS_USER_ROOT" not in os.environ:
    os.environ["CRONUS_USER_ROOT"] = _TEST_USER_ROOT
    atexit.register(shutil.rmtree, _TEST_USER_ROOT, ignore_errors=True)
else:
    shutil.rmtree(_TEST_USER_ROOT, ignore_errors=True)

from core import Account, AccountState, EventBus, StateManager
from runtime.runtime_state_manager import RuntimeStateManager
from services.process_service import ProcessService


def _medium_validation(pid=4321):
    return {
        "ok": True,
        "pid": pid,
        "reason": "ok",
        "confidence": 58.0,
        "identity": "robloxplayerbeta.exe|100.000000|c:\\roblox\\versions\\robloxplayerbeta.exe",
        "name": "RobloxPlayerBeta.exe",
        "created": 100.0,
        "windows": 1,
        "hwnd": 123,
        "cpu": 0.2,
        "ram_mb": 140.0,
        "owner": "",
        "browser_tracker_id": "",
    }


class ProcessProofPolicyTests(unittest.TestCase):
    def test_classifies_launch_only_process_proof_as_medium_and_tracker_match_as_strong(self):
        from services.process_proof_policy import classify_process_proof

        medium = classify_process_proof(
            _medium_validation(),
            owner_key="UserA",
            launched_after=98.0,
        )
        self.assertEqual(medium["process_proof_level"], "medium")
        self.assertEqual(medium["process_proof_reason"], "launched_after")

        strong_validation = dict(_medium_validation())
        strong_validation["browser_tracker_id"] = "tracker-1"
        strong = classify_process_proof(
            strong_validation,
            owner_key="UserA",
            expected_browser_tracker_id="tracker-1",
            launched_after=98.0,
        )
        self.assertEqual(strong["process_proof_level"], "strong")
        self.assertEqual(strong["process_proof_reason"], "browser_tracker_match")

    def test_runtime_state_rejects_in_game_transition_with_only_medium_process_proof(self):
        acc = Account(username="ProofUser")
        acc.state = AccountState.VERIFY
        acc.pid = 4321
        acc.bound_process_identity = "robloxplayerbeta.exe|100.000000|c:\\roblox\\versions\\robloxplayerbeta.exe"
        acc.process_binding_status = "verified"
        acc.process_binding_confidence = 58.0
        acc.process_proof_level = "medium"
        acc.sync_runtime("seed_medium_proof")
        state = RuntimeStateManager(logger=lambda *args, **kwargs: None)

        changed = state.transition_public(acc, AccountState.IN_GAME, reason="unit_medium_proof", force=True)

        self.assertFalse(changed)
        self.assertEqual(acc.state, AccountState.VERIFY)
        self.assertEqual(acc.process_reject_reason, "process_proof_insufficient")

    def test_process_bind_allows_medium_proof_only_while_launching_or_verify(self):
        acc = Account(username="ProofUser")
        acc.state = AccountState.VERIFY
        state = StateManager(EventBus())

        with patch("services.process_service._LegacyProcessManager.validate_game_process", return_value=_medium_validation()), \
             patch("services.process_service._LegacyProcessManager.claim_pid_owner"), \
             patch("services.process_service.get_rt_monitor") as monitor:
            monitor.return_value.register = lambda _pid: None
            result = ProcessService.bind_account_process(
                acc,
                4321,
                state,
                reason="unit_verify_bind",
                launched_after=98.0,
                increment_generation=False,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(acc.process_proof_level, "medium")
        self.assertEqual(result["validation"]["process_proof_level"], "medium")

        ready_acc = Account(username="ReadyProofUser")
        ready_acc.state = AccountState.READY
        ready_state = StateManager(EventBus())
        with patch("services.process_service._LegacyProcessManager.validate_game_process", return_value=_medium_validation()):
            rejected = ProcessService.bind_account_process(
                ready_acc,
                4321,
                ready_state,
                reason="unit_ready_bind",
                launched_after=98.0,
                increment_generation=False,
            )

        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["reason"], "process_proof_insufficient")
        self.assertEqual(ready_acc.process_binding_status, "process_proof_quarantine")
        self.assertEqual(ready_acc.process_proof_level, "medium")

    def test_safe_kill_bound_process_rejects_medium_process_proof(self):
        acc = Account(username="KillProofUser")
        acc.state = AccountState.IN_GAME
        acc.pid = 4321
        acc.bound_process_identity = "robloxplayerbeta.exe|100.000000|c:\\roblox\\versions\\robloxplayerbeta.exe"
        acc.process_binding_status = "verified"
        acc.process_binding_confidence = 58.0
        acc.process_proof_level = "medium"
        acc.sync_runtime("seed_medium_kill")
        state = StateManager(EventBus())

        with patch("services.process_service._LegacyProcessManager.validate_game_process", return_value=_medium_validation()), \
             patch("services.process_service._LegacyProcessManager.kill_pid") as kill_pid:
            result = ProcessService.safe_kill_bound_process(acc, state, reason="unit_medium_kill")

        self.assertFalse(result["ok"])
        self.assertFalse(result["killed"])
        self.assertEqual(result["reason"], "process_proof_insufficient")
        kill_pid.assert_not_called()


if __name__ == "__main__":
    unittest.main()
