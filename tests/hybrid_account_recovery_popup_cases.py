from tests.hybrid_account_fixture import *


class HybridAccountRecoveryPopupCases:
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

    def test_lua_waiting_verify_account_scans_captcha_popup(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {
            "watchdog_enabled": True,
            "popup_disconnected_enabled": True,
            "popup_scan_interval_seconds": 1,
            "popup_startup_grace_seconds": 0,
            "watchdog_hold_time": 60,
            "watchdog_activity_timeout": 180,
            "watchdog_loading_grace": 90,
            "watchdog_cpu_low": 0.9,
            "use_lua": True,
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
        acc = Account(username="lua_waiting_captcha_user")
        acc.state = AccountState.VERIFY
        acc.pid = 5432
        acc.recovery_status = "waiting_for_lua"
        acc.last_state_change_at = time.time() - 120
        acc.last_launch_at = time.time() - 120
        acc.liveness_state = "waiting_for_lua"
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
                "detail": "Roblox Security verification CAPTCHA visible",
                "popup_confidence": 1.5,
                "evidence_source": "text",
            },
        }
        with patch.object(ProcessManager, "assess_liveness", return_value=liveness) as assess, \
             patch.object(ProcessService, "safe_kill_bound_process", return_value={"ok": True, "killed": True, "pid": 5432, "reason": "killed"}):
            SystemMaintenance._scan_liveness_watchdog(maint)

        self.assertTrue(assess.called)
        self.assertTrue(assess.call_args.kwargs["inspect_ui"])
        self.assertTrue(is_account_captcha_required(acc))
        self.assertEqual(acc.recovery_status, CAPTCHA_REASON)
        self.assertEqual(recovery.failed, [("lua_waiting_captcha_user", CAPTCHA_REASON, CAPTCHA_BLOCK_REASON)])
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

    def test_lua_waiting_popup_scan_is_prioritized_over_ingame_accounts(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {
            "watchdog_enabled": True,
            "popup_disconnected_enabled": True,
            "popup_scan_interval_seconds": 30,
            "popup_scan_max_parallel": 1,
            "popup_startup_grace_seconds": 0,
            "watchdog_hold_time": 60,
            "watchdog_activity_timeout": 180,
            "watchdog_loading_grace": 90,
            "watchdog_cpu_low": 0.9,
            "use_lua": True,
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
        live = Account(username="popup_live_first")
        live.state = AccountState.IN_GAME
        live.pid = 4100
        live.in_game_since = time.time() - 120
        live.last_activity_at = time.time()
        waiting = Account(username="popup_lua_waiting_last")
        waiting.state = AccountState.VERIFY
        waiting.pid = 4101
        waiting.recovery_status = "waiting_for_lua"
        waiting.last_state_change_at = time.time() - 120
        waiting.last_launch_at = time.time() - 120
        maint._accounts = [live, waiting]

        inspect_by_pid = {}
        sample_by_pid = {}

        def _record_liveness(pid, *args, **kwargs):
            inspect_by_pid[pid] = bool(kwargs.get("inspect_ui"))
            sample_by_pid[pid] = kwargs.get("ui_sample_count")
            return {
                "state": "alive",
                "score": 8.0,
                "validation": {"cpu": 3.0, "ram_mb": 300.0, "windows": 1},
                "reason_key": "",
                "dialog": {},
            }

        with patch.object(ProcessManager, "assess_liveness", side_effect=_record_liveness):
            SystemMaintenance._scan_liveness_watchdog(maint)

        self.assertFalse(inspect_by_pid[4100])
        self.assertTrue(inspect_by_pid[4101])
        self.assertEqual(sample_by_pid[4101], 2)
        self.assertEqual(set(maint._last_popup_scan_at), {"popup_lua_waiting_last"})

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
