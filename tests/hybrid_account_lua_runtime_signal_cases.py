from tests.hybrid_account_fixture import *


class HybridAccountLuaRuntimeSignalCases:
    def test_lua_event_rejects_oversized_string_payload_before_resolution(self):
        controller = FarmController.__new__(FarmController)
        controller._accounts = [Account("LuaUnit")]

        with patch("farm.resolve_lua_account") as resolver, patch("farm.flog_kv") as flog:
            result = controller.handle_lua_rejoin_event({
                "event": "heartbeat",
                "account": "LuaUnit",
                "detail": "x" * 513,
            })

        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 400)
        self.assertFalse(result["accepted"])
        resolver.assert_not_called()
        self.assertTrue(
            any(call.args[:2] == ("LUA_EVENT", "invalid_payload") for call in flog.call_args_list)
        )

    def test_lua_event_resolver_exception_returns_500(self):
        controller = FarmController.__new__(FarmController)
        controller._accounts = [Account("LuaUnit")]

        with patch("farm.resolve_lua_account", side_effect=RuntimeError("resolver failed")), \
             patch("farm.flog_kv") as flog:
            result = controller.handle_lua_rejoin_event({
                "event": "heartbeat",
                "account": "LuaUnit",
            })

        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 500)
        self.assertFalse(result["accepted"])
        self.assertIn("resolver failed", result["msg"].lower())
        self.assertTrue(
            any(call.args[:2] == ("LUA_EVENT", "resolver_exception") for call in flog.call_args_list)
        )

    def test_lua_event_handler_exception_returns_500_without_quarantine(self):
        controller = FarmController.__new__(FarmController)
        account = Account("LuaUnit")
        controller._accounts = [account]
        controller._workers = {}
        controller._push_event = lambda *_args, **_kwargs: None

        class FailingOrchestrator:
            def handle_runtime_signal(self, *_args, **_kwargs):
                raise RuntimeError("handler failed")

        controller._runtime_orchestrator = FailingOrchestrator()

        with patch("farm.flog_kv") as flog:
            result = controller.handle_lua_rejoin_event({
                "event": "disconnect",
                "account": "LuaUnit",
                "username": "LuaUnit",
                "reason_key": "lua_disconnect_unit",
            })

        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 500)
        self.assertFalse(result["accepted"])
        self.assertIn("retry later", result["msg"].lower())
        self.assertEqual(account.state, AccountState.IDLE)
        self.assertTrue(
            any(call.args[:2] == ("LUA_EVENT", "event_handler_exception") for call in flog.call_args_list)
        )

    def test_lua_loaded_event_records_vip_server_detection(self):
        controller = FarmController.__new__(FarmController)
        account = Account("LuaUnit")
        controller._accounts = [account]
        controller._workers = {}
        bumped = []
        pushed = []
        routed = []

        class FakeOrchestrator:
            def handle_runtime_signal(self, acc, signal, reason, payload=None):
                routed.append((acc, signal, reason, payload or {}))
                return True

        controller._runtime_orchestrator = FakeOrchestrator()
        controller._bump_status_revision = lambda: bumped.append(True)
        controller._push_event = lambda *args, **kwargs: pushed.append((args, kwargs))

        with patch("farm.flog_kv") as flog:
            result = controller.handle_lua_rejoin_event({
                "event": "loaded",
                "account": "LuaUnit",
                "username": "LuaUnit",
                "private_server_id": "3659f6a2-private",
                "private_server_owner_id": "42",
                "is_vip_server": "true",
                "server_type": "VIP",
                "place_id": "123456",
                "job_id": "job-1",
                "universe_id": "654321",
            })

        self.assertTrue(result["ok"])
        self.assertTrue(result["accepted"])
        self.assertEqual(result["observed_server_type"], "VIP")
        self.assertTrue(result["observed_is_vip"])
        self.assertEqual(account.observed_server_type, "VIP")
        self.assertEqual(account.observed_private_server_id, "3659f6a2-private")
        self.assertEqual(account.observed_private_server_owner_id, "42")
        self.assertEqual(routed[0][3]["observed_server_type"], "VIP")
        self.assertTrue(routed[0][3]["observed_is_vip"])
        self.assertTrue(bumped)
        self.assertTrue(pushed)
        flog.assert_any_call(
            "VIP",
            "server_detected",
            account="LuaUnit",
            pid="",
            is_vip=True,
            server_type="VIP",
            private_server_id="3659f6a2",
            place_id="123456",
            job_id="job-1",
        )

    def test_lua_loaded_without_pid_still_logs_bound_vip_server_detection(self):
        controller = FarmController.__new__(FarmController)
        account = Account("LuaUnit")
        account.pid = 555
        account.state = AccountState.IN_GAME
        controller._accounts = [account]
        controller._workers = {}
        bumped = []
        pushed = []
        controller._bump_status_revision = lambda: bumped.append(True)
        controller._push_event = lambda *args, **kwargs: pushed.append((args, kwargs))

        with patch("farm.flog_kv") as flog:
            result = controller.handle_lua_rejoin_event({
                "event": "loaded",
                "account": "LuaUnit",
                "username": "LuaUnit",
                "private_server_id": "3659f6a2-private",
                "private_server_owner_id": "42",
                "is_vip_server": "true",
                "server_type": "VIP",
                "place_id": "123456",
                "job_id": "job-1",
            })

        self.assertTrue(result["ok"])
        self.assertFalse(result["accepted"])
        self.assertEqual(result["matched_pid"], 555)
        self.assertEqual(result["lua_pid"], "")
        self.assertEqual(account.observed_server_type, "VIP")
        self.assertEqual(account.observed_private_server_id, "3659f6a2-private")
        self.assertTrue(bumped)
        self.assertTrue(pushed)
        flog.assert_any_call(
            "VIP",
            "server_detected",
            account="LuaUnit",
            pid="555",
            is_vip=True,
            server_type="VIP",
            private_server_id="3659f6a2",
            place_id="123456",
            job_id="job-1",
        )

    def test_lua_loaded_without_job_waits_for_in_game_evidence(self):
        controller = FarmController.__new__(FarmController)
        account = Account("LuaUnit")
        account.state = AccountState.VERIFY
        controller._accounts = [account]
        controller._workers = {}
        controller._bump_status_revision = lambda: None
        routed = []
        pushed = []

        class FakeOrchestrator:
            def handle_runtime_signal(self, acc, signal, reason, payload=None):
                routed.append((acc, signal, reason, payload or {}))
                return True

        controller._runtime_orchestrator = FakeOrchestrator()
        controller._push_event = lambda *args, **kwargs: pushed.append((args, kwargs))

        result = controller.handle_lua_rejoin_event({
            "event": "loaded",
            "account": "LuaUnit",
            "username": "LuaUnit",
            "server_type": "PUBLIC",
            "is_vip_server": "false",
            "place_id": "123456",
            "job_id": "",
        })

        self.assertTrue(result["ok"])
        self.assertTrue(result["accepted"])
        self.assertEqual(result["signal"], "")
        self.assertEqual(routed, [])
        self.assertEqual(account.last_watchdog_classification, "lua_loaded_waiting_server")
        self.assertTrue(pushed)

    def test_in_game_without_job_is_rejected_as_unverified(self):
        controller = FarmController.__new__(FarmController)
        account = Account("LuaUnit")
        account.state = AccountState.VERIFY
        controller._accounts = [account]
        controller._workers = {}
        controller._bump_status_revision = lambda: None
        routed = []

        class FakeOrchestrator:
            def handle_runtime_signal(self, acc, signal, reason, payload=None):
                routed.append((acc, signal, reason, payload or {}))
                return True

        controller._runtime_orchestrator = FakeOrchestrator()
        controller._push_event = lambda *args, **kwargs: None

        result = controller.handle_lua_rejoin_event({
            "event": "in_game",
            "account": "LuaUnit",
            "username": "LuaUnit",
            "server_type": "PUBLIC",
            "is_vip_server": "false",
            "place_id": "123456",
            "job_id": "",
        })

        self.assertTrue(result["ok"])
        self.assertFalse(result["accepted"])
        self.assertEqual(result["signal"], "")
        self.assertEqual(routed, [])
        self.assertEqual(account.last_watchdog_classification, "lua_in_game_missing_server_evidence")

    def test_lua_in_game_records_lua_online_evidence(self):
        controller = FarmController.__new__(FarmController)
        account = Account("LuaUnit")
        account.state = AccountState.VERIFY
        account.session_id = "session-a"
        account.launch_nonce = "nonce-a"
        controller._accounts = [account]
        controller._workers = {}
        controller._bump_status_revision = lambda: None

        class FakeOrchestrator:
            def handle_runtime_signal(self, acc, signal, reason, payload=None):
                return True

        controller._runtime_orchestrator = FakeOrchestrator()
        controller._push_event = lambda *args, **kwargs: None

        result = controller.handle_lua_rejoin_event({
            "event": "in_game",
            "account": "LuaUnit",
            "username": "LuaUnit",
            "server_type": "PUBLIC",
            "is_vip_server": "false",
            "place_id": "123456",
            "job_id": "job-1",
        })

        self.assertTrue(result["ok"])
        self.assertTrue(result["accepted"])
        self.assertEqual(account.lua_last_event, "in_game")
        self.assertGreater(account.lua_last_event_at, 0)
        self.assertGreater(account.lua_in_game_at, 0)
        self.assertEqual(account.lua_session_id, "session-a")
        self.assertEqual(account.lua_launch_nonce, "nonce-a")

    def test_lua_private_server_owner_id_counts_as_vip_detection(self):
        controller = FarmController.__new__(FarmController)
        account = Account("LuaUnit")
        controller._accounts = [account]
        controller._workers = {}
        controller._bump_status_revision = lambda: None
        routed = []

        class FakeOrchestrator:
            def handle_runtime_signal(self, acc, signal, reason, payload=None):
                routed.append((acc, signal, reason, payload or {}))
                return True

        controller._runtime_orchestrator = FakeOrchestrator()
        controller._push_event = lambda *args, **kwargs: None

        with patch("farm.flog_kv") as flog:
            result = controller.handle_lua_rejoin_event({
                "event": "loaded",
                "account": "LuaUnit",
                "username": "LuaUnit",
                "private_server_id": "",
                "private_server_owner_id": "42",
                "is_vip_server": "false",
                "server_type": "PUBLIC",
                "place_id": "123456",
            })

        self.assertTrue(result["ok"])
        self.assertEqual(result["observed_server_type"], "VIP")
        self.assertTrue(result["observed_is_vip"])
        self.assertEqual(account.observed_server_type, "VIP")
        self.assertEqual(account.observed_private_server_owner_id, "42")
        self.assertEqual(result["signal"], "")
        self.assertEqual(routed, [])
        flog.assert_any_call(
            "VIP",
            "server_detected",
            account="LuaUnit",
            pid="",
            is_vip=True,
            server_type="VIP",
            private_server_id="",
            place_id="123456",
            job_id="",
        )

    def test_lua_public_signal_uses_expected_vip_launch(self):
        controller = FarmController.__new__(FarmController)
        account = Account("LuaUnit")
        account.server_type = ServerType.VIP
        account.active_vip = "https://www.roblox.com/games/123456?privateServerLinkCode=secret"
        account.launch_intent = {"private_server_intent": True}
        controller._accounts = [account]
        controller._workers = {}
        controller._bump_status_revision = lambda: None

        class FakeOrchestrator:
            def handle_runtime_signal(self, acc, signal, reason, payload=None):
                return True

        controller._runtime_orchestrator = FakeOrchestrator()
        controller._push_event = lambda *args, **kwargs: None

        with patch("farm.flog_kv") as flog:
            result = controller.handle_lua_rejoin_event({
                "event": "loaded",
                "account": "LuaUnit",
                "username": "LuaUnit",
                "private_server_id": "",
                "private_server_owner_id": "0",
                "is_vip_server": "false",
                "server_type": "PUBLIC",
                "place_id": "123456",
            })

        self.assertTrue(result["ok"])
        self.assertEqual(result["observed_server_type"], "VIP")
        self.assertTrue(result["observed_is_vip"])
        self.assertEqual(account.observed_server_type, "VIP")
        self.assertEqual(account.observed_private_server_id, "")
        flog.assert_any_call(
            "VIP",
            "server_detected",
            account="LuaUnit",
            pid="",
            is_vip=True,
            server_type="VIP",
            private_server_id="",
            place_id="123456",
            job_id="",
        )

    def test_lua_public_signal_overrides_stale_private_observation_after_public_launch(self):
        controller = FarmController.__new__(FarmController)
        account = Account("LuaUnit")
        account.pid = 555
        account.server_type = ServerType.PUBLIC
        account.observed_server_type = "VIP"
        account.launch_intent = {"private_server_intent": True}
        controller._accounts = [account]
        controller._workers = {}
        controller._bump_status_revision = lambda: None

        class FakeOrchestrator:
            def handle_runtime_signal(self, acc, signal, reason, payload=None):
                return True

        controller._runtime_orchestrator = FakeOrchestrator()
        controller._push_event = lambda *args, **kwargs: None

        with patch("farm.flog_kv") as flog:
            result = controller.handle_lua_rejoin_event({
                "event": "loaded",
                "account": "LuaUnit",
                "username": "LuaUnit",
                "pid": "555",
                "private_server_id": "",
                "private_server_owner_id": "0",
                "is_vip_server": "false",
                "server_type": "PUBLIC",
                "place_id": "123456",
                "job_id": "job-1",
            })

        self.assertTrue(result["ok"])
        self.assertEqual(result["observed_server_type"], "PUBLIC")
        self.assertFalse(result["observed_is_vip"])
        self.assertEqual(account.observed_server_type, "PUBLIC")
        flog.assert_any_call(
            "VIP",
            "server_detected",
            account="LuaUnit",
            pid="555",
            is_vip=False,
            server_type="PUBLIC",
            private_server_id="",
            place_id="123456",
            job_id="job-1",
        )

    def test_lua_vip_detection_logs_once_per_process_and_job(self):
        controller = FarmController.__new__(FarmController)
        account = Account("LuaUnit")
        controller._accounts = [account]
        controller._workers = {}
        controller._bump_status_revision = lambda: None

        class FakeOrchestrator:
            def handle_runtime_signal(self, acc, signal, reason, payload=None):
                return True

        controller._runtime_orchestrator = FakeOrchestrator()
        controller._push_event = lambda *args, **kwargs: None
        payload = {
            "event": "loaded",
            "account": "LuaUnit",
            "username": "LuaUnit",
            "pid": "555",
            "private_server_id": "vip-1",
            "private_server_owner_id": "42",
            "is_vip_server": "true",
            "server_type": "VIP",
            "place_id": "123456",
            "job_id": "job-1",
        }

        with patch("farm.flog_kv") as flog:
            controller.handle_lua_rejoin_event(dict(payload))
            controller.handle_lua_rejoin_event(dict(payload))

        vip_logs = [call for call in flog.call_args_list if call.args[:2] == ("VIP", "server_detected")]
        self.assertEqual(len(vip_logs), 1)

    def test_lua_teleport_state_logs_terminal_event(self):
        controller = FarmController.__new__(FarmController)
        account = Account("LuaUnit")
        controller._accounts = [account]
        controller._workers = {}
        controller._bump_status_revision = lambda: None
        pushed = []
        controller._push_event = lambda *args, **kwargs: pushed.append((args, kwargs))

        with patch("farm.flog_kv") as flog:
            result = controller.handle_lua_rejoin_event({
                "event": "teleport_state",
                "account": "LuaUnit",
                "username": "LuaUnit",
                "teleport_state": "Enum.TeleportState.Started",
                "teleport_place_id": "123456",
                "job_id": "job-1",
            })

        self.assertTrue(result["ok"])
        self.assertTrue(result["accepted"])
        self.assertTrue(pushed)
        flog.assert_any_call(
            "LUA",
            "teleport_detected",
            account="LuaUnit",
            pid="",
            teleport_state="Enum.TeleportState.Started",
            place_id="123456",
            job_id="job-1",
        )

    def test_lua_description_event_updates_account_note_without_credentials(self):
        controller = FarmController.__new__(FarmController)
        account = Account("LuaUnit")
        controller._accounts = [account]
        controller._workers = {}
        controller._bump_status_revision = lambda: None
        pushed = []
        saved = []

        class FakeConfig:
            def save_accounts(self, accounts):
                saved.append(list(accounts))

        controller.cfg_mgr = FakeConfig()
        controller._push_event = lambda *args, **kwargs: pushed.append((args, kwargs))

        with patch("farm.ACCOUNT_STORE.update_record", return_value={"username": "LuaUnit", "description": "ready"}) as update_record, \
             patch("farm.audit_event") as audit:
            result = controller.handle_lua_rejoin_event({
                "event": "description",
                "account": "LuaUnit",
                "username": "LuaUnit",
                "description": "ready",
            })

        self.assertTrue(result["ok"])
        self.assertTrue(result["accepted"])
        self.assertEqual(result["signal"], "description_updated")
        self.assertEqual(account.description, "ready")
        self.assertTrue(result["persisted"])
        update_record.assert_called_once_with("LuaUnit", {"description": "ready"})
        audit.assert_called_once()
        self.assertTrue(saved)
        self.assertEqual(pushed[0][1]["lua_event"], "description")

    def test_lua_finished_event_marks_account_finished_through_runtime_orchestrator(self):
        controller = FarmController.__new__(FarmController)
        account = Account("LuaUnit")
        account.pid = 444
        controller._accounts = [account]
        controller._workers = {}
        controller._state_mgr = object()
        controller._runtime_state = None
        controller._bump_status_revision = lambda: None
        pushed = []
        saved = []
        calls = []

        class FakeConfig:
            def save_accounts(self, accounts):
                saved.append(list(accounts))

        class FakeOrchestrator:
            def request_verify_finished(self, acc, state_manager=None, reason=""):
                calls.append((acc, state_manager, reason))
                return {"ok": True, "killed": True, "finished_at": 123.5}

        controller.cfg_mgr = FakeConfig()
        controller._runtime_orchestrator = FakeOrchestrator()
        controller._push_event = lambda *args, **kwargs: pushed.append((args, kwargs))

        with patch("farm.ACCOUNT_STORE.update_record", return_value={"username": "LuaUnit", "description": "done"}):
            result = controller.handle_lua_rejoin_event({
                "event": "finished",
                "account": "LuaUnit",
                "username": "LuaUnit",
                "pid": "444",
                "reason_key": "lua_finished_unit",
                "description": "done",
            })

        self.assertTrue(result["ok"])
        self.assertTrue(result["accepted"])
        self.assertEqual(result["signal"], "verify_finished")
        self.assertTrue(result["killed"])
        self.assertEqual(result["finished_at"], 123.5)
        self.assertEqual(account.description, "done")
        self.assertEqual(calls, [(account, controller._state_mgr, "lua_finished_unit")])
        self.assertTrue(saved)
        self.assertEqual(pushed[0][1]["lua_event"], "finished")
