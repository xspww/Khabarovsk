from tests.hybrid_account_fixture import *


class HybridAccountStatusCases:
    def test_status_step_maps_disconnect_check_to_simple_disconnected_label(self):
        controller = FarmController.__new__(FarmController)
        controller._net_mon = None
        account = Account("UserA")
        account.recovery_status = "checking_disconnect"
        account.liveness_state = "reconnecting"
        account.last_recovery_at = 123.0
        account.last_state_change_at = 100.0

        step, index, started_at = controller._recovery_step_for_account(account, AccountState.IN_GAME)

        self.assertEqual(step, "Disconnected")
        self.assertEqual(index, 4)
        self.assertEqual(started_at, 123.0)

    def test_status_step_treats_stale_disconnect_check_as_in_game_when_alive(self):
        controller = FarmController.__new__(FarmController)
        controller._net_mon = None
        account = Account("UserA")
        account.recovery_status = "checking_disconnect"
        account.last_recovery_reason = "connection_error"
        account.liveness_state = "alive"
        account.in_game_since = 456.0

        step, index, started_at = controller._recovery_step_for_account(account, AccountState.IN_GAME)

        self.assertEqual(step, "Recovery Complete")
        self.assertEqual(index, 8)
        self.assertEqual(started_at, 456.0)

    def test_status_view_model_does_not_show_stale_disconnect_for_alive_in_game(self):
        from config_store import ConfigManager
        from runtime.runtime_view_model import RuntimeViewModelBuilder

        controller = FarmController(ConfigManager())
        account = Account("UserA")
        account.state = AccountState.IN_GAME
        account.desired_state = AccountState.IN_GAME
        account.pid = 4321
        account.bound_process_identity = "RobloxPlayerBeta.exe|1|C:\\Roblox\\RobloxPlayerBeta.exe"
        account.process_binding_status = "verified"
        account.recovery_status = "checking_disconnect"
        account.last_recovery_reason = "connection_error"
        account.liveness_state = "alive"
        account.in_game_since = 456.0
        controller.set_accounts([account])
        controller._runtime_scheduler = type(
            "FakeScheduler",
            (),
            {"snapshot": lambda _self: {"pending_count": 2, "overdue_count": 0, "last_dispatch_latency_seconds": 0.25}},
        )()

        with patch("runtime.runtime_view_model.ProcessManager.is_bound_game_alive", return_value=True), \
             patch("runtime.runtime_view_model.ProcessManager.get_pid_owner", return_value="UserA"), \
             patch("runtime.runtime_view_model.ProcessManager.is_not_responding", return_value=False):
            status = controller.get_status()

        self.assertIn("runtime_health", status)
        self.assertIn("queue_snapshot", status)
        self.assertIn("scheduler_health", status)
        self.assertEqual(status["scheduler_health"]["pending_count"], 2)
        self.assertEqual(status["runtime_health"]["scheduler"]["pending_count"], 2)
        self.assertIn("accounts", status)
        row = status["accounts"][0]
        self.assertEqual(row["state"], "IN_GAME")
        self.assertEqual(row["state_label"], "In Game")
        self.assertEqual(row["recovery_step"], "Recovery Complete")
        self.assertNotEqual(row["state_label"], "Checking Disconnect")
        controller._runtime_store.close()

    def test_status_view_model_counts_blocked_live_captcha_out_of_online_total(self):
        from config_store import ConfigManager

        controller = FarmController(ConfigManager())
        account = Account("CaptchaLiveUser")
        account.state = AccountState.IN_GAME
        account.desired_state = AccountState.IN_GAME
        account.pid = 4321
        account.bound_process_identity = "RobloxPlayerBeta.exe|1|C:\\Roblox\\RobloxPlayerBeta.exe"
        account.process_binding_status = "verified"
        set_account_captcha_hold(account, "Roblox Security verification visible", source="unit")
        controller.set_accounts([account])

        with patch("runtime.runtime_view_model.ProcessManager.is_bound_game_alive", return_value=True), \
             patch("runtime.runtime_view_model.ProcessManager.get_pid_owner", return_value="CaptchaLiveUser"), \
             patch("runtime.runtime_view_model.ProcessManager.is_not_responding", return_value=False):
            status = controller.get_status()

        self.assertEqual(status["blocked_count"], 1)
        self.assertEqual(status["in_game"], 0)
        self.assertEqual(status["failed"], 1)
        row = status["accounts"][0]
        self.assertEqual(row["state_label"], "Captcha")
        self.assertTrue(row["captcha_required"])
        controller._runtime_store.close()

    def test_status_step_marks_in_game_complete_even_with_old_launch_reason(self):
        controller = FarmController.__new__(FarmController)
        controller._net_mon = None
        account = Account("UserA")
        account.recovery_status = "in_game"
        account.last_state_reason = "launch_sent"
        account.in_game_since = 456.0

        step, index, started_at = controller._recovery_step_for_account(account, AccountState.IN_GAME)

        self.assertEqual(step, "Recovery Complete")
        self.assertEqual(index, 8)
        self.assertEqual(started_at, 456.0)
