from tests.hybrid_account_fixture import *


class HybridAccountRecoveryCases:
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

    def test_alive_process_periodically_scans_popup_dialog(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {
            "watchdog_enabled": True,
            "popup_disconnected_enabled": True,
            "popup_scan_interval_seconds": 1,
            "watchdog_hold_time": 60,
            "watchdog_activity_timeout": 180,
            "watchdog_loading_grace": 90,
            "watchdog_cpu_low": 0.9,
        }
        maint._accounts = []
        maint._workers = {}
        maint._last_popup_scan_at = {}

        class Net:
            def is_online(self):
                return True

        class Recovery:
            _net = Net()

        maint._recovery = Recovery()
        acc = Account(username="periodic_popup_user")
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

        self.assertTrue(assess.call_args.kwargs["inspect_ui"])
        self.assertIn(acc._config_username, maint._last_popup_scan_at)

    def test_recovery_active_ingame_account_still_scans_captcha_popup(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {
            "watchdog_enabled": True,
            "popup_disconnected_enabled": True,
            "popup_scan_interval_seconds": 1,
            "watchdog_hold_time": 60,
            "watchdog_activity_timeout": 180,
            "watchdog_loading_grace": 90,
            "watchdog_cpu_low": 0.9,
        }
        maint._accounts = []
        maint._workers = {}
        maint._last_popup_scan_at = {}

        class Net:
            def is_online(self):
                return True

        class Recovery:
            _net = Net()

        class State:
            def __init__(self):
                self.runtime = RuntimeStateManager(logger=lambda *_args, **_kwargs: None)
                self.bound_status = []

            def set_recovery(self, account, status="", reason="", inflight=None):
                self.runtime.set_recovery(account, status=status, reason=reason, inflight=inflight)

            def set_cooldown(self, account, until_ts, reason=""):
                self.runtime.set_cooldown(account, until_ts, reason=reason)

            def set_binding_status(self, account, status, reason=""):
                self.bound_status.append((account.username, status, reason))

        maint._recovery = Recovery()
        maint._state_mgr = State()
        acc = Account(username="recovery_active_captcha_user")
        acc.state = AccountState.IN_GAME
        acc.pid = 4321
        acc.in_game_since = time.time() - 120
        acc.last_activity_at = time.time()
        acc.liveness_state = "alive"
        acc.recovery_inflight = True
        acc.recovery_status = "due"
        maint._accounts = [acc]

        liveness = {
            "state": "captcha",
            "score": 8.0,
            "validation": {"cpu": 3.0, "ram_mb": 300.0, "windows": 1},
            "reason_key": CAPTCHA_REASON,
            "dialog": {
                "matched": True,
                "action": "hold",
                "reason_key": CAPTCHA_REASON,
                "detail": "Roblox | R | recovery_active_captcha_user: 13+ | Security",
                "popup_confidence": 1.5,
                "evidence_source": "text",
            },
        }
        with patch.object(ProcessManager, "assess_liveness", return_value=liveness) as assess:
            SystemMaintenance._scan_liveness_watchdog(maint)

        self.assertTrue(assess.call_args.kwargs["inspect_ui"])
        self.assertTrue(is_account_captcha_required(acc))
        self.assertEqual(acc.recovery_status, CAPTCHA_REASON)
        self.assertIn(acc._config_username, maint._last_popup_scan_at)

    def test_captcha_popup_closes_only_bound_account_process(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {
            "watchdog_enabled": True,
            "popup_disconnected_enabled": True,
            "popup_scan_interval_seconds": 1,
            "popup_scan_max_parallel": 2,
            "watchdog_hold_time": 60,
            "watchdog_activity_timeout": 180,
            "watchdog_loading_grace": 90,
            "watchdog_cpu_low": 0.9,
        }
        maint._accounts = []
        maint._workers = {}
        maint._last_popup_scan_at = {}

        class Net:
            def is_online(self):
                return True

        class Recovery:
            _net = Net()

            def __init__(self):
                self.failed = []

            def fail_account(self, account, reason, reason_msg):
                self.failed.append((account.username, reason, reason_msg))

        class State:
            def __init__(self):
                self.runtime = RuntimeStateManager(logger=lambda *_args, **_kwargs: None)
                self.cleared = []

            def set_recovery(self, account, status="", reason="", inflight=None):
                self.runtime.set_recovery(account, status=status, reason=reason, inflight=inflight)

            def set_cooldown(self, account, until_ts, reason=""):
                self.runtime.set_cooldown(account, until_ts, reason=reason)

            def clear_process_binding(self, account, reason="", increment_generation=False):
                self.cleared.append((account.username, account.pid, reason, increment_generation))
                self.runtime.clear_process_binding(account, reason=reason, increment_generation=increment_generation)

        recovery = Recovery()
        state = State()
        maint._recovery = recovery
        maint._state_mgr = state
        now = time.time()
        captcha_acc = Account(username="Zuckmu")
        captcha_acc.state = AccountState.IN_GAME
        captcha_acc.pid = 3692
        captcha_acc.in_game_since = now - 120
        captcha_acc.last_activity_at = now
        captcha_acc.liveness_state = "alive"
        captcha_acc.runtime_generation = 11
        other_acc = Account(username="OtherAccount")
        other_acc.state = AccountState.IN_GAME
        other_acc.pid = 6504
        other_acc.in_game_since = now - 120
        other_acc.last_activity_at = now
        other_acc.liveness_state = "alive"
        maint._accounts = [captcha_acc, other_acc]

        def fake_liveness(pid, **_kwargs):
            if pid == 3692:
                return {
                    "state": "captcha",
                    "score": 8.0,
                    "validation": {"cpu": 3.0, "ram_mb": 300.0, "windows": 1},
                    "reason_key": CAPTCHA_REASON,
                    "dialog": {
                        "matched": True,
                        "action": "hold",
                        "reason_key": CAPTCHA_REASON,
                        "detail": "Roblox | Zuckmu | Security challenge",
                        "popup_confidence": 1.5,
                        "evidence_source": "text",
                    },
                }
            return {
                "state": "alive",
                "score": 8.0,
                "validation": {"cpu": 3.0, "ram_mb": 300.0, "windows": 1},
                "reason_key": "",
                "dialog": {},
            }

        killed = []

        def fake_kill(account, state_manager, **kwargs):
            killed.append((account.username, account.pid, kwargs))
            state_manager.clear_process_binding(
                account,
                reason=kwargs.get("reason", ""),
                increment_generation=kwargs.get("increment_generation", False),
            )
            return {"ok": True, "killed": True, "pid": 3692, "reason": "killed"}

        with patch.object(ProcessManager, "assess_liveness", side_effect=fake_liveness), \
             patch.object(ProcessService, "safe_kill_bound_process", side_effect=fake_kill):
            SystemMaintenance._scan_liveness_watchdog(maint)

        self.assertEqual(len(killed), 1)
        self.assertEqual(killed[0][0], "Zuckmu")
        self.assertEqual(killed[0][1], 3692)
        self.assertEqual(killed[0][2]["reason"], "captcha_hold")
        self.assertEqual(killed[0][2]["expected_runtime_generation"], 11)
        self.assertFalse(killed[0][2]["increment_generation"])
        self.assertIsNone(captcha_acc.pid)
        self.assertEqual(other_acc.pid, 6504)
        self.assertTrue(is_account_captcha_required(captcha_acc))
        self.assertEqual(recovery.failed, [("Zuckmu", CAPTCHA_REASON, CAPTCHA_BLOCK_REASON)])

    def test_popup_scan_max_parallel_limits_periodic_window_scans(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {
            "watchdog_enabled": True,
            "popup_disconnected_enabled": True,
            "popup_scan_interval_seconds": 5,
            "popup_scan_max_parallel": 1,
            "watchdog_hold_time": 60,
            "watchdog_activity_timeout": 180,
            "watchdog_loading_grace": 90,
            "watchdog_cpu_low": 0.9,
        }
        maint._accounts = []
        maint._workers = {}
        maint._last_popup_scan_at = {}

        class Net:
            def is_online(self):
                return True

        class Recovery:
            _net = Net()

        maint._recovery = Recovery()
        accounts = []
        for index in range(3):
            acc = Account(username=f"popup_budget_user_{index}")
            acc.state = AccountState.IN_GAME
            acc.pid = 2000 + index
            acc.in_game_since = time.time() - 120
            acc.last_activity_at = time.time()
            acc.liveness_state = "alive"
            accounts.append(acc)
        maint._accounts = accounts

        inspect_flags = []

        def _liveness(*args, **kwargs):
            inspect_flags.append(bool(kwargs.get("inspect_ui")))
            return {
                "state": "alive",
                "score": 8.0,
                "validation": {"cpu": 3.0, "ram_mb": 300.0, "windows": 1},
                "reason_key": "",
                "dialog": {},
            }

        with patch.object(ProcessManager, "assess_liveness", side_effect=_liveness):
            SystemMaintenance._scan_liveness_watchdog(maint)

        self.assertEqual(inspect_flags, [True, False, False])
        self.assertEqual(len(maint._last_popup_scan_at), 1)

    def test_popup_scan_queue_advances_by_account_order_after_interval(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {
            "watchdog_enabled": True,
            "popup_disconnected_enabled": True,
            "popup_scan_interval_seconds": 30,
            "popup_scan_max_parallel": 2,
            "watchdog_hold_time": 60,
            "watchdog_activity_timeout": 180,
            "watchdog_loading_grace": 90,
            "watchdog_cpu_low": 0.9,
        }
        maint._accounts = []
        maint._workers = {}
        maint._last_popup_scan_at = {}
        maint._last_popup_batch_at = 0.0
        maint._popup_scan_cursor = 0

        class Net:
            def is_online(self):
                return True

        class Recovery:
            _net = Net()

        maint._recovery = Recovery()
        accounts = []
        for index in range(4):
            acc = Account(username=f"popup_queue_user_{index}")
            acc.state = AccountState.IN_GAME
            acc.pid = 3000 + index
            acc.in_game_since = time.time() - 120
            acc.last_activity_at = time.time()
            acc.liveness_state = "alive"
            accounts.append(acc)
        maint._accounts = accounts

        def _liveness(*args, **kwargs):
            return {
                "state": "alive",
                "score": 8.0,
                "validation": {"cpu": 3.0, "ram_mb": 300.0, "windows": 1},
                "reason_key": "",
                "dialog": {},
            }

        inspect_flags = []

        def _record_liveness(*args, **kwargs):
            inspect_flags.append(bool(kwargs.get("inspect_ui")))
            return _liveness(*args, **kwargs)

        with patch.object(ProcessManager, "assess_liveness", side_effect=_record_liveness):
            SystemMaintenance._scan_liveness_watchdog(maint)

        self.assertEqual(inspect_flags, [True, True, False, False])
        self.assertEqual(maint._popup_scan_cursor, 2)
        self.assertEqual(set(maint._last_popup_scan_at), {"popup_queue_user_0", "popup_queue_user_1"})

        inspect_flags.clear()
        with patch.object(ProcessManager, "assess_liveness", side_effect=_record_liveness):
            SystemMaintenance._scan_liveness_watchdog(maint)

        self.assertEqual(inspect_flags, [False, False, False, False])
        self.assertEqual(maint._popup_scan_cursor, 2)

        maint._last_popup_batch_at = time.time() - 31
        inspect_flags.clear()
        with patch.object(ProcessManager, "assess_liveness", side_effect=_record_liveness):
            SystemMaintenance._scan_liveness_watchdog(maint)

        self.assertEqual(inspect_flags, [False, False, True, True])
        self.assertEqual(maint._popup_scan_cursor, 0)
        self.assertEqual(
            set(maint._last_popup_scan_at),
            {"popup_queue_user_0", "popup_queue_user_1", "popup_queue_user_2", "popup_queue_user_3"},
        )

    def test_popup_dialog_rejoin_signal_overrides_alive_process(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {
            "watchdog_enabled": True,
            "popup_disconnected_enabled": True,
            "popup_scan_interval_seconds": 1,
            "connection_error_hold_time": 1,
            "watchdog_hold_time": 60,
            "watchdog_activity_timeout": 180,
            "watchdog_loading_grace": 90,
            "watchdog_cpu_low": 0.9,
        }
        maint._accounts = []
        maint._workers = {}
        maint._last_popup_scan_at = {}

        class Net:
            def is_online(self):
                return True

        class Recovery:
            _net = Net()

            def __init__(self):
                self.calls = []

            def handle_runtime_signal(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        class State:
            def set_binding_status(self, *args, **kwargs):
                pass

        recovery = Recovery()
        maint._recovery = recovery
        maint._state_mgr = State()
        acc = Account(username="popup_rejoin_user")
        acc.state = AccountState.IN_GAME
        acc.pid = 1234
        acc.in_game_since = time.time() - 120
        acc.last_activity_at = time.time()
        acc.liveness_state = "alive"
        acc.liveness_suspect_since = time.time() - 2
        acc.runtime_generation = 7
        acc.session_id = "sess"
        acc.launch_nonce = "nonce"
        acc.rejoin_transaction_id = "tx"
        maint._accounts = [acc]

        liveness = {
            "state": "reconnecting",
            "score": 8.0,
            "validation": {"cpu": 3.0, "ram_mb": 300.0, "windows": 1},
            "reason_key": "session_conflict",
            "dialog": {
                "matched": True,
                "recovery_allowed": True,
                "action": "conditional_rejoin",
                "reason_key": "session_conflict",
                "detail": "Error Code 273",
                "error_code": "273",
                "popup_confidence": 1.5,
                "disconnect_category": "SESSION_CONFLICT",
            },
        }
        with patch.object(ProcessManager, "assess_liveness", return_value=liveness):
            SystemMaintenance._scan_liveness_watchdog(maint)

        self.assertEqual(len(recovery.calls), 1)
        args, kwargs = recovery.calls[0]
        self.assertEqual(args[1], "disconnect_detected")
        self.assertEqual(args[2], "session_conflict")
        self.assertEqual(kwargs["expected_runtime_generation"], 7)
        self.assertEqual(kwargs["payload"]["popup_code"], "273")

    def test_home_screen_without_job_evidence_triggers_rejoin_signal(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {
            "watchdog_enabled": True,
            "popup_disconnected_enabled": False,
            "watchdog_hold_time": 60,
            "watchdog_activity_timeout": 180,
            "watchdog_loading_grace": 90,
            "watchdog_cpu_low": 0.9,
            "launch_verify_window": 1,
            "home_rejoin_enabled": True,
            "home_rejoin_grace_seconds": 1,
            "home_rejoin_hold_seconds": 1,
            "home_rejoin_require_server_evidence": True,
        }
        maint._workers = {}
        maint._runtime_owner = None
        maint._runtime_state = RuntimeStateManager(logger=lambda *_args, **_kwargs: None)

        class Net:
            def is_online(self):
                return True

        class Recovery:
            _net = Net()

            def __init__(self):
                self.calls = []

            def handle_runtime_signal(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return True

        recovery = Recovery()
        maint._recovery = recovery
        acc = Account(username="home_stuck_user")
        acc.state = AccountState.IN_GAME
        acc.desired_state = AccountState.IN_GAME
        acc.pid = 1234
        now = time.time()
        acc.in_game_since = now - 120
        acc.last_launch_at = now - 120
        acc.last_activity_at = now - 120
        acc.launch_intent = {"place_id": "123456"}
        acc.observed_server_at = now - 119
        acc.observed_place_id = "123456"
        acc.observed_job_id = ""
        acc.liveness_state = "alive"
        acc.last_watchdog_classification = "home_screen_stuck"
        acc.liveness_suspect_since = now - 5
        acc.runtime_generation = 7
        acc.session_id = "sess"
        acc.launch_nonce = "nonce"
        acc.rejoin_transaction_id = "tx"
        maint._accounts = [acc]

        liveness = {
            "state": "alive",
            "score": 8.0,
            "validation": {"cpu": 3.0, "ram_mb": 300.0, "windows": 1},
            "reason_key": "",
            "dialog": {},
        }
        with patch.object(ProcessManager, "assess_liveness", return_value=liveness):
            SystemMaintenance._scan_liveness_watchdog(maint)

        self.assertEqual(len(recovery.calls), 1)
        args, kwargs = recovery.calls[0]
        self.assertEqual(args[1], "loading_freeze")
        self.assertEqual(args[2], "home_screen_no_job")
        self.assertEqual(kwargs["expected_runtime_generation"], 7)
        self.assertEqual(kwargs["expected_session_id"], "sess")
        self.assertEqual(kwargs["payload"]["trigger"], "home_screen_guard")
        self.assertEqual(acc.last_watchdog_classification, "home_screen_stuck")

    def test_home_rejoin_guard_allows_teleported_subplace_with_job(self):
        from runtime.home_rejoin_guard import detect_home_rejoin_issue

        acc = Account(username="teleport_user")
        now = time.time()
        acc.in_game_since = now - 120
        acc.last_launch_at = now - 120
        acc.launch_intent = {"place_id": "77747658251236"}
        acc.observed_server_at = now - 90
        acc.observed_place_id = "130167267952199"
        acc.observed_job_id = "a47501ca-e723-4f6b-be91-0937074f8635"

        issue = detect_home_rejoin_issue(
            acc,
            {
                "home_rejoin_enabled": True,
                "home_rejoin_grace_seconds": 60,
                "launch_verify_window": 25,
                "home_rejoin_require_server_evidence": True,
            },
            now,
            120,
        )

        self.assertIsNone(issue)

    def test_home_rejoin_guard_ignores_missing_server_evidence_alone(self):
        from runtime.home_rejoin_guard import detect_home_rejoin_issue

        acc = Account(username="no_evidence_user")
        now = time.time()
        acc.in_game_since = now - 180
        acc.last_launch_at = now - 180
        acc.launch_intent = {"place_id": "77747658251236"}
        acc.observed_server_at = 0.0
        acc.observed_place_id = ""
        acc.observed_job_id = ""

        issue = detect_home_rejoin_issue(
            acc,
            {
                "home_rejoin_enabled": True,
                "home_rejoin_grace_seconds": 60,
                "launch_verify_window": 25,
                "home_rejoin_require_server_evidence": True,
            },
            now,
            180,
        )

        self.assertIsNone(issue)

    def test_memory_pressure_guard_triggers_targeted_rejoin_signal(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {
            "watchdog_enabled": True,
            "popup_disconnected_enabled": False,
            "watchdog_hold_time": 60,
            "watchdog_activity_timeout": 180,
            "watchdog_loading_grace": 90,
            "watchdog_cpu_low": 0.9,
            "roblox_memory_guard_enabled": True,
            "roblox_memory_guard_mb": 1024,
            "roblox_memory_guard_hold_seconds": 1,
        }
        maint._workers = {}
        maint._runtime_owner = None
        maint._runtime_state = RuntimeStateManager(logger=lambda *_args, **_kwargs: None)
        maint._state_mgr = maint._runtime_state

        class Net:
            def is_online(self):
                return True

        class Recovery:
            _net = Net()

            def __init__(self):
                self.calls = []

            def handle_runtime_signal(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return True

        recovery = Recovery()
        maint._recovery = recovery
        acc = Account(username="memory_pressure_user")
        acc.state = AccountState.IN_GAME
        acc.desired_state = AccountState.IN_GAME
        acc.pid = 1234
        now = time.time()
        acc.in_game_since = now - 300
        acc.last_activity_at = now
        acc.resource_pressure_since = now - 5
        acc.resource_pressure_reason = "process_memory_pressure"
        acc.runtime_generation = 9
        acc.session_id = "sess"
        acc.launch_nonce = "nonce"
        acc.rejoin_transaction_id = "tx"
        maint._accounts = [acc]

        liveness = {
            "state": "alive",
            "score": 8.0,
            "validation": {"cpu": 8.0, "ram_mb": 2048.0, "windows": 1},
            "reason_key": "",
            "dialog": {},
        }
        with patch.object(ProcessManager, "assess_liveness", return_value=liveness):
            SystemMaintenance._scan_liveness_watchdog(maint)

        self.assertEqual(len(recovery.calls), 1)
        args, kwargs = recovery.calls[0]
        self.assertEqual(args[1], "watchdog_timeout")
        self.assertEqual(args[2], "process_memory_pressure")
        self.assertEqual(kwargs["expected_runtime_generation"], 9)
        self.assertEqual(kwargs["expected_session_id"], "sess")
        self.assertEqual(kwargs["payload"]["trigger"], "memory_guard")
        self.assertEqual(acc.last_watchdog_classification, "memory_pressure")
        self.assertEqual(acc.resource_pressure_since, 0.0)

    def test_visual_popup_is_enriched_with_recent_log_error_code(self):
        from services.roblox_liveness import assess_liveness

        class FakeProcessManager:
            @classmethod
            def validate_game_process(cls, pid, min_ram_mb=0.0):
                return {"ok": True, "cpu": 0.0, "ram_mb": 180.0, "windows": 1}

            @classmethod
            def is_not_responding(cls, pid):
                return False

            @classmethod
            def inspect_disconnect_dialog(cls, *args, **kwargs):
                return {
                    "matched": True,
                    "recovery_allowed": True,
                    "action": "rejoin",
                    "reason_key": "connection_error",
                    "detail": "visual_disconnect source=center_modal strength=strong",
                    "error_code": "",
                    "popup_confidence": 1.1,
                    "disconnect_category": "VISUAL_DISCONNECT",
                    "visual_disconnect": True,
                    "evidence_source": "visual_strong",
                }

            @classmethod
            def classify_disconnect_dialog_texts(cls, texts):
                return ProcessManager.classify_disconnect_dialog_texts(texts)

        with patch(
            "services.roblox_liveness.collect_recent_log_evidence",
            return_value={
                "matched": True,
                "source": "roblox_log",
                "error_code": "273",
                "keyword": "same account launched",
                "confidence": 1.2,
                "line": "Same account launched experience from different device. (Error Code: 273)",
            },
        ) as collect:
            result = assess_liveness(FakeProcessManager, 1234, inspect_ui=True)

        collect.assert_called_once()
        dialog = result["dialog"]
        self.assertEqual(result["state"], "reconnecting")
        self.assertEqual(result["reason_key"], "session_conflict")
        self.assertEqual(dialog["error_code"], "273")
        self.assertEqual(dialog["action"], "conditional_rejoin")
        self.assertEqual(dialog["disconnect_category"], "SESSION_CONFLICT")
        self.assertEqual(dialog["evidence_source"], "error_code")
        self.assertEqual(dialog["visual_evidence_source"], "visual_strong")
        self.assertTrue(dialog["visual_disconnect"])

    def test_visual_confirmed_popup_overrides_alive_process_without_log_evidence(self):
        from services.roblox_liveness import assess_liveness

        class FakeProcessManager:
            @classmethod
            def validate_game_process(cls, pid, min_ram_mb=0.0):
                return {"ok": True, "cpu": 5.0, "ram_mb": 220.0, "windows": 1}

            @classmethod
            def is_not_responding(cls, pid):
                return False

            @classmethod
            def inspect_disconnect_dialog(cls, *args, **kwargs):
                return {
                    "matched": True,
                    "recovery_allowed": True,
                    "action": "rejoin",
                    "reason_key": "connection_error",
                    "detail": "visual_disconnect source=center_modal strength=strong",
                    "error_code": "",
                    "popup_confidence": 1.1,
                    "disconnect_category": "VISUAL_DISCONNECT",
                    "visual_disconnect": True,
                    "evidence_source": "visual_strong",
                }

            @classmethod
            def classify_disconnect_dialog_texts(cls, texts):
                return ProcessManager.classify_disconnect_dialog_texts(texts)

        with patch(
            "services.roblox_liveness.collect_recent_log_evidence",
            return_value={"matched": False, "source": "roblox_log", "reason": "none"},
        ):
            result = assess_liveness(FakeProcessManager, 1234, inspect_ui=True)

        self.assertEqual(result["state"], "reconnecting")
        self.assertEqual(result["reason_key"], "connection_error")
        self.assertTrue(result["dialog"]["matched"])
        self.assertTrue(result["dialog"]["recovery_allowed"])
        self.assertEqual(result["dialog"]["evidence_source"], "visual_strong")

    def test_log_evidence_alone_does_not_create_popup_recovery(self):
        from services.roblox_liveness import assess_liveness

        class FakeProcessManager:
            @classmethod
            def validate_game_process(cls, pid, min_ram_mb=0.0):
                return {"ok": True, "cpu": 0.0, "ram_mb": 80.0, "windows": 0}

            @classmethod
            def is_not_responding(cls, pid):
                return False

            @classmethod
            def inspect_disconnect_dialog(cls, *args, **kwargs):
                return {
                    "matched": False,
                    "recovery_allowed": False,
                    "action": "",
                    "reason_key": "",
                    "detail": "",
                    "error_code": "",
                }

        with patch(
            "services.roblox_liveness.collect_recent_log_evidence",
            return_value={
                "matched": True,
                "source": "roblox_log",
                "error_code": "273",
                "confidence": 1.2,
                "line": "Same account launched experience from different device. (Error Code: 273)",
            },
        ):
            result = assess_liveness(FakeProcessManager, 1234, inspect_ui=True)

        self.assertNotEqual(result["state"], "reconnecting")
        self.assertFalse(result["dialog"].get("recovery_allowed", False))
        self.assertTrue(result["log_evidence"]["matched"])

    def test_window_resize_uses_interval_and_config(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {
            "roblox_window_resize_enabled": True,
            "roblox_window_width": 640,
            "roblox_window_height": 480,
            "roblox_window_resize_interval_seconds": 10,
        }
        maint._last_window_resize_at = time.time() - 5
        with patch.object(ProcessService, "resize_roblox_windows") as resize:
            SystemMaintenance._enforce_window_resize(maint)
        resize.assert_not_called()

        maint._last_window_resize_at = time.time() - 11
        with patch.object(ProcessService, "resize_roblox_windows", return_value={"resized": 2, "count": 2}) as resize:
            SystemMaintenance._enforce_window_resize(maint)
        resize.assert_called_once_with(640, 480, reason="auto_window_resize_cycle")

        maint._cfg["roblox_window_arrange_enabled"] = True
        maint._cfg["roblox_window_arrange_columns"] = 4
        maint._cfg["roblox_window_arrange_gap"] = 2
        maint._cfg["roblox_window_arrange_margin"] = 0
        maint._last_window_resize_at = time.time() - 11
        with patch.object(ProcessService, "arrange_roblox_windows", return_value={"arranged": 2, "count": 2}) as arrange:
            SystemMaintenance._enforce_window_resize(maint)
        arrange.assert_called_once_with(640, 480, 4, 2, 0, reason="auto_window_resize_cycle")
