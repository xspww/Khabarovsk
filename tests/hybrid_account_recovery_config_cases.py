from tests.hybrid_account_fixture import *


class HybridAccountRecoveryConfigCases:
    def test_single_instance_detection_is_scoped_to_current_folder(self):
        import main

        self.assertTrue(main._cmdline_targets_this_app(["python.exe", "main.py"], main.BASE_DIR))
        self.assertTrue(main._cmdline_targets_this_app(["python.exe", "ops\\run_backend.py"], main.BASE_DIR))
        self.assertTrue(
            main._cmdline_targets_this_app(
                ["python.exe", os.path.join(main.BASE_DIR, "ops", "run_backend.py")],
                tempfile.gettempdir(),
            )
        )
        self.assertTrue(main._cmdline_targets_this_app(["python.exe", "-m", "ops.run_backend"], main.BASE_DIR))
        self.assertFalse(main._cmdline_targets_this_app(["python.exe", "main.py"], tempfile.gettempdir()))
        self.assertFalse(main._cmdline_targets_this_app(["python.exe", "ops\\run_backend.py"], tempfile.gettempdir()))
        self.assertFalse(main._cmdline_targets_this_app(["python.exe", "-m", "ops.run_backend"], tempfile.gettempdir()))
        self.assertFalse(main._cmdline_targets_this_app(["python.exe", "main.py"], ""))
        self.assertFalse(main._cmdline_targets_this_app(["python.exe", "script.py"], main.BASE_DIR))

    def test_auto_close_uses_minutes_not_seconds(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {"auto_close_enabled": True, "auto_close_minutes": 2}
        maint._last_auto_close_at = time.time() - 90
        maint._accounts = []
        maint._state_mgr = None
        maint._recovery = None
        maint._workers = {}
        with patch.object(ProcessService, "kill_all_roblox_clients") as kill_all:
            SystemMaintenance._enforce_auto_close(maint)
        kill_all.assert_not_called()

        maint._last_auto_close_at = time.time() - 121
        with patch.object(ProcessService, "kill_all_roblox_clients", return_value=0) as kill_all:
            SystemMaintenance._enforce_auto_close(maint)
        kill_all.assert_called_once()

    def test_auto_minimize_runtime_path_removed(self):
        self.assertFalse(hasattr(SystemMaintenance, "_enforce_auto_minimize"))

    def test_disabled_popup_setting_avoids_popup_inspection(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {
            "watchdog_enabled": True,
            "popup_disconnected_enabled": False,
            "watchdog_hold_time": 60,
            "watchdog_activity_timeout": 180,
            "watchdog_loading_grace": 90,
            "watchdog_cpu_low": 0.9,
        }
        maint._accounts = []
        maint._workers = {}

        class Net:
            def is_online(self):
                return True

        class Recovery:
            _net = Net()

        maint._recovery = Recovery()
        acc = Account(username="popup_scan_user")
        acc.state = AccountState.IN_GAME
        acc.pid = 1234
        acc.in_game_since = time.time() - 120
        acc.last_activity_at = time.time()
        acc.liveness_state = "alive"
        maint._accounts = [acc]

        liveness = {
            "state": "alive",
            "score": 8.0,
            "validation": {"cpu": 3.0, "ram_mb": 300.0, "windows": 1},
            "reason_key": "",
            "dialog": {},
        }
        with patch.object(ProcessManager, "assess_liveness", return_value=liveness) as assess:
            SystemMaintenance._scan_liveness_watchdog(maint)

        self.assertFalse(assess.call_args.kwargs["inspect_ui"])
