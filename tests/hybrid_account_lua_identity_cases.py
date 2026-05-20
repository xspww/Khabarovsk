from tests.hybrid_account_fixture import *


class HybridAccountLuaIdentityCases:
    def test_lua_disconnect_event_maps_to_recovery_signal(self):
        controller = FarmController.__new__(FarmController)
        account = Account("LuaUnit")
        controller._accounts = [account]
        controller._workers = {}
        pushed = []
        routed = []

        class FakeOrchestrator:
            def handle_runtime_signal(self, acc, signal, reason, payload=None):
                routed.append((acc, signal, reason, payload or {}))
                return True

        controller._runtime_orchestrator = FakeOrchestrator()
        controller._push_event = lambda *args, **kwargs: pushed.append((args, kwargs))

        result = controller.handle_lua_rejoin_event({
            "event": "disconnect",
            "account": "LuaUnit",
            "error_code": "277",
            "reason_key": "lua_disconnect_test",
            "detail": "manual disconnect sensor test",
            "visual_disconnect": "true",
            "evidence_source": "lua_manual_test",
        })

        self.assertTrue(result["ok"])
        self.assertTrue(result["accepted"])
        self.assertEqual(result["signal"], "disconnect_detected")
        self.assertEqual(routed[0][0], account)
        self.assertEqual(routed[0][1], "disconnect_detected")
        self.assertEqual(routed[0][2], "lua_disconnect_test")
        self.assertEqual(routed[0][3]["error_code"], "277")
        self.assertTrue(routed[0][3]["visual_disconnect"])
        self.assertEqual(routed[0][3]["evidence_source"], "lua_manual_test")
        self.assertEqual(pushed[0][1]["lua_event"], "disconnect")

    def test_lua_identity_uses_actual_username_over_configured_account_hint(self):
        controller = FarmController.__new__(FarmController)
        configured = Account("ConfiguredAccount")
        actual = Account("RealRobloxUser")
        controller._accounts = [configured, actual]
        controller._workers = {}
        pushed = []
        routed = []

        class FakeOrchestrator:
            def handle_runtime_signal(self, acc, signal, reason, payload=None):
                routed.append((acc, signal, reason, payload or {}))
                return True

        controller._runtime_orchestrator = FakeOrchestrator()
        controller._push_event = lambda *args, **kwargs: pushed.append((args, kwargs))

        result = controller.handle_lua_rejoin_event({
            "event": "disconnect",
            "account": "ConfiguredAccount",
            "configured_account": "ConfiguredAccount",
            "username": "RealRobloxUser",
            "error_code": "277",
            "reason_key": "lua_disconnect_identity",
        })

        self.assertTrue(result["ok"])
        self.assertTrue(result["accepted"])
        self.assertEqual(result["account"], "RealRobloxUser")
        self.assertEqual(result["identity_match"], "username")
        self.assertEqual(routed[0][0], actual)
        self.assertEqual(routed[0][3]["lua_username"], "RealRobloxUser")
        self.assertEqual(routed[0][3]["configured_account"], "ConfiguredAccount")

    def test_lua_identity_can_match_cookie_user_id(self):
        controller = FarmController.__new__(FarmController)
        account = Account("ConfigOnlyName")
        account.cookie_user_id = "123456"
        controller._accounts = [account]
        controller._workers = {}
        routed = []

        class FakeOrchestrator:
            def handle_runtime_signal(self, acc, signal, reason, payload=None):
                routed.append((acc, signal, reason, payload or {}))
                return True

        controller._runtime_orchestrator = FakeOrchestrator()
        controller._push_event = lambda *args, **kwargs: None

        result = controller.handle_lua_rejoin_event({
            "event": "disconnect",
            "account": "RuntimeUsername",
            "username": "RuntimeUsername",
            "user_id": "123456",
            "error_code": "277",
        })

        self.assertTrue(result["ok"])
        self.assertTrue(result["accepted"])
        self.assertEqual(result["account"], "ConfigOnlyName")
        self.assertEqual(result["identity_match"], "user_id")
        self.assertEqual(routed[0][0], account)

    def test_lua_disconnect_with_pid_mismatch_is_not_routed(self):
        controller = FarmController.__new__(FarmController)
        account = Account("LuaUnit")
        account.pid = 111
        controller._accounts = [account]
        controller._workers = {}
        pushed = []
        routed = []

        class FakeOrchestrator:
            def handle_runtime_signal(self, acc, signal, reason, payload=None):
                routed.append((acc, signal, reason, payload or {}))
                return True

        controller._runtime_orchestrator = FakeOrchestrator()
        controller._push_event = lambda *args, **kwargs: pushed.append((args, kwargs))

        result = controller.handle_lua_rejoin_event({
            "event": "disconnect",
            "account": "LuaUnit",
            "username": "LuaUnit",
            "pid": "222",
            "error_code": "277",
        })

        self.assertTrue(result["ok"])
        self.assertFalse(result["accepted"])
        self.assertEqual(result["matched_pid"], 111)
        self.assertEqual(result["lua_pid"], 222)
        self.assertEqual(routed, [])
        self.assertEqual(pushed[0][1]["reason"], "lua_pid_mismatch")

    def test_lua_disconnect_without_pid_is_not_routed_when_account_has_bound_pid(self):
        controller = FarmController.__new__(FarmController)
        account = Account("LuaUnit")
        account.pid = 111
        controller._accounts = [account]
        controller._workers = {}
        pushed = []
        routed = []

        class FakeOrchestrator:
            def handle_runtime_signal(self, acc, signal, reason, payload=None):
                routed.append((acc, signal, reason, payload or {}))
                return True

        controller._runtime_orchestrator = FakeOrchestrator()
        controller._push_event = lambda *args, **kwargs: pushed.append((args, kwargs))

        result = controller.handle_lua_rejoin_event({
            "event": "disconnect",
            "account": "LuaUnit",
            "username": "LuaUnit",
            "error_code": "277",
        })

        self.assertTrue(result["ok"])
        self.assertFalse(result["accepted"])
        self.assertEqual(result["matched_pid"], 111)
        self.assertEqual(result["lua_pid"], "")
        self.assertEqual(routed, [])
        self.assertEqual(pushed[0][1]["reason"], "lua_pid_missing")

    def test_lua_disconnect_with_matching_pid_routes_targeted_rejoin(self):
        controller = FarmController.__new__(FarmController)
        account = Account("LuaUnit")
        account.pid = 333
        controller._accounts = [account]
        controller._workers = {}
        routed = []

        class FakeOrchestrator:
            def handle_runtime_signal(self, acc, signal, reason, payload=None):
                routed.append((acc, signal, reason, payload or {}))
                return True

        controller._runtime_orchestrator = FakeOrchestrator()
        controller._push_event = lambda *args, **kwargs: None

        result = controller.handle_lua_rejoin_event({
            "event": "disconnect",
            "account": "LuaUnit",
            "username": "LuaUnit",
            "pid": "333",
            "error_code": "277",
        })

        self.assertTrue(result["ok"])
        self.assertTrue(result["accepted"])
        self.assertEqual(result["matched_pid"], 333)
        self.assertEqual(routed[0][0], account)
        self.assertEqual(routed[0][3]["matched_pid"], 333)
        self.assertEqual(routed[0][3]["lua_pid"], 333)
