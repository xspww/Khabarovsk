from tests.hybrid_account_fixture import *


class HybridAccountRecoverySignalCases:
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

    def test_home_rejoin_guard_uses_missing_lua_evidence_when_lua_required(self):
        from runtime.home_rejoin_guard import detect_home_rejoin_issue

        acc = Account(username="lua_home_user")
        now = time.time()
        acc.in_game_since = now - 180
        acc.last_launch_at = now - 180
        acc.launch_intent = {"place_id": "77747658251236"}
        acc.observed_server_at = 0.0
        acc.observed_place_id = ""
        acc.observed_job_id = ""
        acc.lua_in_game_at = 0.0
        acc.lua_last_event_at = 0.0

        issue = detect_home_rejoin_issue(
            acc,
            {
                "use_lua": True,
                "home_rejoin_enabled": True,
                "home_rejoin_grace_seconds": 60,
                "launch_verify_window": 25,
            },
            now,
            180,
        )

        self.assertIsNotNone(issue)
        self.assertEqual(issue["reason_key"], "home_screen_no_server_evidence")

    def test_waiting_for_lua_timeout_triggers_rejoin_signal(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {
            "use_lua": True,
            "lua_wait_timeout": 1,
            "launch_verify_window": 1,
            "queue_timeout": 90,
        }
        maint._accounts = []

        calls = []

        def runtime_signal(*args, **kwargs):
            calls.append((args, kwargs))
            return True

        maint._runtime_signal = runtime_signal

        acc = Account(username="lua_timeout_user")
        now = time.time()
        acc.state = AccountState.VERIFY
        acc.desired_state = AccountState.IN_GAME
        acc.pid = 1234
        acc.bound_process_identity = "RobloxPlayerBeta.exe|1|C:\\Roblox\\RobloxPlayerBeta.exe"
        acc.browser_tracker_id = "browser-1"
        acc.recovery_status = "waiting_for_lua"
        acc.last_state_change_at = now - 30
        acc.runtime_generation = 7
        acc.session_id = "sess"
        acc.launch_nonce = "nonce"
        acc.rejoin_transaction_id = "tx"
        maint._accounts = [acc]

        with patch("runtime.maintenance_liveness.ProcessManager.is_bound_game_alive", return_value=True):
            SystemMaintenance._recover_stale_joining_states(maint)

        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(args[1], "loading_freeze")
        self.assertEqual(args[2], "lua_wait_timeout")
        self.assertEqual(kwargs["expected_runtime_generation"], 7)
        self.assertEqual(kwargs["expected_session_id"], "sess")
        self.assertEqual(kwargs["payload"]["trigger"], "lua_wait_timeout")

    def test_waiting_for_lua_keeps_waiting_before_timeout_even_when_pid_is_live(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {
            "use_lua": True,
            "lua_wait_timeout": 60,
            "launch_verify_window": 25,
            "queue_timeout": 90,
        }
        calls = []
        maint._runtime_signal = lambda *args, **kwargs: calls.append((args, kwargs)) or True

        acc = Account(username="lua_waiting_user")
        now = time.time()
        acc.state = AccountState.VERIFY
        acc.desired_state = AccountState.IN_GAME
        acc.pid = 1234
        acc.bound_process_identity = "RobloxPlayerBeta.exe|1|C:\\Roblox\\RobloxPlayerBeta.exe"
        acc.browser_tracker_id = "browser-1"
        acc.recovery_status = "waiting_for_lua"
        acc.last_state_change_at = now - 40
        maint._accounts = [acc]

        with patch("runtime.maintenance_liveness.ProcessManager.is_bound_game_alive", return_value=True):
            SystemMaintenance._recover_stale_joining_states(maint)

        self.assertEqual(calls, [])

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
