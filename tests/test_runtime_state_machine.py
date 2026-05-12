import unittest

from core import Account
from domain.account_state import AccountState, RuntimeState
from domain.runtime_lifecycle import (
    RuntimeLifecycleState,
    is_valid_lifecycle_transition,
    lifecycle_for_legacy_runtime,
    lifecycle_for_public,
)
from runtime.runtime_state_manager import RuntimeStateManager


class RuntimeStateMachineTests(unittest.TestCase):
    def test_public_states_map_to_canonical_lifecycle(self):
        cases = {
            AccountState.IDLE: RuntimeLifecycleState.IDLE,
            AccountState.QUEUED: RuntimeLifecycleState.QUEUED,
            AccountState.LAUNCHING: RuntimeLifecycleState.STARTING,
            AccountState.VERIFY: RuntimeLifecycleState.JOINING,
            AccountState.IN_GAME: RuntimeLifecycleState.IN_GAME,
            AccountState.NETWORK_LOST: RuntimeLifecycleState.CHECKING_DISCONNECT,
            AccountState.COOLDOWN: RuntimeLifecycleState.COOLDOWN,
            AccountState.FAILED: RuntimeLifecycleState.FAILED,
        }
        for public, lifecycle in cases.items():
            with self.subTest(public=public):
                self.assertEqual(lifecycle_for_public(public), lifecycle)

    def test_legacy_runtime_states_map_to_canonical_lifecycle(self):
        cases = {
            RuntimeState.STOPPED: RuntimeLifecycleState.STOPPED,
            RuntimeState.STARTING: RuntimeLifecycleState.STARTING,
            RuntimeState.JOINING: RuntimeLifecycleState.JOINING,
            RuntimeState.RUNNING: RuntimeLifecycleState.IN_GAME,
            RuntimeState.RECOVERING: RuntimeLifecycleState.RECOVERING,
            RuntimeState.BACKOFF: RuntimeLifecycleState.COOLDOWN,
            RuntimeState.FAILED: RuntimeLifecycleState.FAILED,
        }
        for legacy, lifecycle in cases.items():
            with self.subTest(legacy=legacy):
                self.assertEqual(lifecycle_for_legacy_runtime(legacy), lifecycle)

    def test_transition_table_rejects_invalid_runtime_jumps(self):
        self.assertTrue(is_valid_lifecycle_transition(RuntimeLifecycleState.STARTING, RuntimeLifecycleState.JOINING))
        self.assertTrue(is_valid_lifecycle_transition(RuntimeLifecycleState.JOINING, RuntimeLifecycleState.IN_GAME))
        self.assertTrue(is_valid_lifecycle_transition(RuntimeLifecycleState.IN_GAME, RuntimeLifecycleState.CHECKING_DISCONNECT))
        self.assertTrue(is_valid_lifecycle_transition(RuntimeLifecycleState.RECOVERING, RuntimeLifecycleState.COOLDOWN))
        self.assertFalse(is_valid_lifecycle_transition(RuntimeLifecycleState.FAILED, RuntimeLifecycleState.IN_GAME))
        self.assertFalse(is_valid_lifecycle_transition(RuntimeLifecycleState.STOPPED, RuntimeLifecycleState.RECOVERING))
        self.assertFalse(is_valid_lifecycle_transition(RuntimeLifecycleState.IDLE, RuntimeLifecycleState.IN_GAME))

    def test_runtime_snapshot_exposes_canonical_state_without_breaking_legacy_state(self):
        account = Account(username="state_machine_user")
        account.state = AccountState.IN_GAME
        snapshot = account.runtime_snapshot()
        self.assertEqual(snapshot["runtime_state"], RuntimeState.RUNNING.value)
        self.assertEqual(snapshot["canonical_runtime_state"], RuntimeLifecycleState.IN_GAME.value)

    def test_runtime_state_manager_rejects_invalid_transition(self):
        account = Account(username="invalid_transition_user")
        account.state = AccountState.FAILED

        manager = RuntimeStateManager()
        accepted = manager.transition_public(account, AccountState.IN_GAME, reason="invalid_test")

        self.assertFalse(accepted)
        self.assertEqual(account.state, AccountState.FAILED)


if __name__ == "__main__":
    unittest.main()
