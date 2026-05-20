from tests.hybrid_account_fixture import *


class HybridAccountLuaCases:
    def test_lua_rejoin_helper_is_served_with_scoped_token_and_local_endpoint(self):
        from fastapi.testclient import TestClient
        import main

        account = Account("LuaUnit")
        account.session_id = "session-unit"
        account.launch_nonce = "nonce-unit"
        old_accounts = main.farm._accounts
        main.farm._accounts = [account]
        client = TestClient(main.app)
        try:
            response = auth_get(client, "/api/lua/rejoin-helper?account=LuaUnit&port=7777&shutdown_delay=2.5")
        finally:
            main.farm._accounts = old_accounts

        self.assertEqual(response.status_code, 200)
        script = response.text
        self.assertIn('Host = "127.0.0.1"', script)
        self.assertIn("Port = 7777", script)
        self.assertNotIn(main.INSTANCE_TOKEN, script)
        self.assertIn('Token = "lua1.', script)
        self.assertIn('SessionId = "session-unit"', script)
        self.assertIn('LaunchNonce = "nonce-unit"', script)
        self.assertIn('Account = "LuaUnit"', script)
        self.assertIn('account = safeString(LocalPlayer.Name)', script)
        self.assertIn('configured_account = safeString(self.Account)', script)
        self.assertIn("pid = getProcessId()", script)
        self.assertIn("ShutdownDelay = 2.50", script)
        self.assertIn('Version = "1.7.1"', script)
        self.assertIn('RequeueSource = "local Request', script)
        self.assertIn('token = safeString(self.Token)', script)
        self.assertIn('cronus_token = safeString(self.Token)', script)
        self.assertIn('api_token = safeString(self.Token)', script)
        self.assertIn('_cronus_token = safeString(self.Token)', script)
        self.assertIn("function CronusRejoin:QueueOnTeleport", script)
        self.assertIn("function CronusRejoin:IsTeleportTransitionActive", script)
        self.assertIn('function CronusRejoin:EndpointWithToken', script)
        self.assertIn('function CronusRejoin:QueryEndpoint', script)
        self.assertIn('function CronusRejoin:GetFallback', script)
        self.assertIn('FallbackEvents = {', script)
        self.assertIn('function CronusRejoin:CanUseGetFallback', script)
        self.assertIn('if self:CanUseGetFallback(eventName) then', script)
        self.assertIn('["User-Agent"] = "CronusLuaRejoin/1.7"', script)
        self.assertIn('Headers = requestHeaders', script)
        self.assertIn('headers = requestHeaders', script)
        self.assertIn('body = body', script)
        self.assertIn('Data = body', script)
        self.assertIn('return self:FallbackOrFail(eventName, payload, status)', script)
        self.assertIn('game:HttpGet(url)', script)
        self.assertIn('["X-Cronus-Token"] = self.Token', script)
        self.assertIn("/api/lua/rejoin-event", script)
        self.assertIn('"http://" .. host .. ":" .. port .. "/api/lua/rejoin-event"', script)
        self.assertNotIn('("http://%s:%s/api/lua/rejoin-event"):format', script)
        self.assertIn("GuiService.ErrorMessageChanged", script)
        self.assertIn("function CronusRejoin:PostAsync", script)
        self.assertIn("function CronusRejoin:ClientRecoveryFallback", script)
        self.assertIn('log("post begin"', script)
        self.assertIn('log("post async"', script)
        self.assertIn('log("post task error"', script)
        self.assertIn('log("json encode failed"', script)
        self.assertIn('log("client fallback start"', script)
        self.assertIn("TeleportService:Teleport(game.PlaceId, LocalPlayer)", script)
        self.assertIn('LocalPlayer:Kick("Cronus recovery fallback")', script)
        self.assertIn("disconnect ignored during teleport", script)
        self.assertIn('reportDisconnect("poll")', script)
        self.assertIn("local function hasServerEvidence()", script)
        self.assertIn("local function reportInGame()", script)
        self.assertIn('CronusRejoin:PostAsync("in_game"', script)
        self.assertIn('CronusRejoin:PostAsync("heartbeat"', script)
        self.assertIn("task.wait(0.5)", script)
        self.assertIn("shutdown fallback after disconnect", script)
        self.assertIn("TeleportService.TeleportInitFailed", script)
        self.assertIn("teleport_state = stateText", script)
        self.assertIn("teleport_place_id = safeString(game.PlaceId)", script)
        self.assertIn("universe_id = safeString(game.GameId)", script)
        self.assertIn("private_server_id = serverInfo.private_server_id", script)
        self.assertIn("private_server_owner_id = serverInfo.private_server_owner_id", script)
        self.assertIn("is_vip_server = serverInfo.is_vip_server", script)
        self.assertIn("server_type = serverInfo.server_type", script)
        self.assertIn("local ownerNumber = tonumber(privateServerOwnerId) or 0", script)
        self.assertIn('local isPrivate = privateServerId ~= "" or ownerNumber > 0', script)
        self.assertNotIn("TeleportToPlaceInstance", script)
        self.assertIn('G.CronusRejoin = CronusRejoin', script)
        self.assertNotIn("__CRONUS_", script)
        loader = (Path(__file__).resolve().parents[1] / "lua" / "run_in_executor.lua").read_text(encoding="utf-8")
        self.assertIn("/api/lua/rejoin-helper", loader)
        self.assertIn("local Load = loadstring or load", loader)
        self.assertIn("queueOnTeleport(source)", loader)
        self.assertIn('log("helper queued for teleport")', loader)
        self.assertIn("Load(source)", loader)

    def test_lua_rejoin_helper_rejects_unauthenticated_token_mint(self):
        from fastapi.testclient import TestClient
        import main

        account = Account("LuaUnit")
        account.session_id = "session-unit"
        account.launch_nonce = "nonce-unit"
        old_accounts = main.farm._accounts
        main.farm._accounts = [account]
        client = TestClient(main.app)
        try:
            response = client.get("/api/lua/rejoin-helper?account=LuaUnit&port=7777&shutdown_delay=2.5")
        finally:
            main.farm._accounts = old_accounts

        self.assertEqual(response.status_code, 403)
        self.assertNotIn('Token = "lua1.', response.text)

    def test_lua_rejoin_helper_reuses_existing_scoped_token_without_renewal(self):
        from fastapi.testclient import TestClient
        import main
        from services.lua_session_tokens import issue_lua_session_token

        account = Account("LuaUnit")
        account.session_id = "session-unit"
        account.launch_nonce = "nonce-unit"
        token = issue_lua_session_token(
            main.INSTANCE_TOKEN,
            account="LuaUnit",
            session_id="session-unit",
            launch_nonce="nonce-unit",
            ttl_seconds=900,
        )
        old_accounts = main.farm._accounts
        main.farm._accounts = [account]
        client = TestClient(main.app)
        try:
            response = client.get(
                f"/api/lua/rejoin-helper?account=LuaUnit&port=7777&shutdown_delay=2.5&cronus_token={token}"
            )
        finally:
            main.farm._accounts = old_accounts

        self.assertEqual(response.status_code, 200)
        script = response.text
        self.assertIn(f'Token = "{token}"', script)
        self.assertEqual(script.count("Token = \"lua1."), 1)

    def test_lua_account_module_is_served_with_scoped_token_and_safe_api_contract(self):
        from fastapi.testclient import TestClient
        import main

        account = Account("LuaUnit")
        account.session_id = "session-unit"
        account.launch_nonce = "nonce-unit"
        old_accounts = main.farm._accounts
        main.farm._accounts = [account]
        client = TestClient(main.app)
        try:
            response = auth_get(client, "/api/lua/account-module?account=LuaUnit&port=7777")
        finally:
            main.farm._accounts = old_accounts

        self.assertEqual(response.status_code, 200)
        script = response.text
        self.assertIn('Host = "127.0.0.1"', script)
        self.assertIn("Port = 7777", script)
        self.assertNotIn(main.INSTANCE_TOKEN, script)
        self.assertIn('Token = "lua1.', script)
        self.assertIn('SessionId = "session-unit"', script)
        self.assertIn('LaunchNonce = "nonce-unit"', script)
        self.assertIn('Account = "LuaUnit"', script)
        self.assertIn('Version = "account-1.0.0"', script)
        self.assertIn("function Account.new", script)
        self.assertIn("function Account.SetKey", script)
        self.assertIn("function Account:Send", script)
        self.assertIn("function Account:SetDescription", script)
        self.assertIn("function Account:MarkFinished", script)
        self.assertIn("universe_id = safeString(game.GameId)", script)
        self.assertIn("teleport_state = stateText", script)
        self.assertIn("teleport_place_id = safeString(game.PlaceId)", script)
        self.assertIn("private_server_id = serverInfo.private_server_id", script)
        self.assertIn("private_server_owner_id = serverInfo.private_server_owner_id", script)
        self.assertIn("is_vip_server = serverInfo.is_vip_server", script)
        self.assertIn("server_type = serverInfo.server_type", script)
        self.assertIn("local ownerNumber = tonumber(privateServerOwnerId) or 0", script)
        self.assertIn('local isPrivate = privateServerId ~= "" or ownerNumber > 0', script)
        self.assertIn("/api/lua/rejoin-event", script)
        self.assertIn('["X-Cronus-Token"] = self.Token', script)
        self.assertIn('return self:Send("finished"', script)
        self.assertIn('client:Loaded("CronusAccount module loaded")', script)
        self.assertNotIn("__CRONUS_", script)
        self.assertNotIn("GetCookie", script)
        self.assertNotIn("GetCSRFToken", script)
        self.assertNotIn("Password", script)
        loader = (Path(__file__).resolve().parents[1] / "lua" / "internal" / "load_account_status.lua").read_text(encoding="utf-8")
        self.assertIn("/api/lua/account-module", loader)
        self.assertIn("local Load = loadstring or load", loader)
        self.assertIn("Load(source)", loader)

    def test_lua_account_module_rejects_unauthenticated_token_mint(self):
        from fastapi.testclient import TestClient
        import main

        account = Account("LuaUnit")
        account.session_id = "session-unit"
        account.launch_nonce = "nonce-unit"
        old_accounts = main.farm._accounts
        main.farm._accounts = [account]
        client = TestClient(main.app)
        try:
            response = client.get("/api/lua/account-module?account=LuaUnit&port=7777")
        finally:
            main.farm._accounts = old_accounts

        self.assertEqual(response.status_code, 403)
        self.assertNotIn('Token = "lua1.', response.text)

    def test_lua_rejoin_event_requires_api_token(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        response = client.post("/api/lua/rejoin-event", json={"event": "heartbeat", "account": "LuaUnit"})

        self.assertEqual(response.status_code, 403)

    def test_lua_rejoin_event_accepts_body_token_for_executor_requests(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        calls = []

        def fake_lua_event(payload):
            calls.append(dict(payload))
            return {
                "ok": True,
                "accepted": True,
                "event": payload.get("event", ""),
                "account": payload.get("account", ""),
                "signal": "",
                "msg": "Lua event accepted",
            }

        with patch.object(main.farm, "handle_lua_rejoin_event", side_effect=fake_lua_event):
            response = client.post(
                "/api/lua/rejoin-event",
                json={
                    "event": "heartbeat",
                    "account": "LuaUnit",
                    "username": "LuaUnit",
                    "token": main.INSTANCE_TOKEN,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["accepted"], True)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("token", calls[0])

    def test_lua_rejoin_event_accepts_scoped_lua_session_token_once(self):
        from fastapi.testclient import TestClient
        import main
        from services.lua_session_tokens import issue_lua_session_token

        account = Account("LuaUnit")
        account.session_id = "session-unit"
        account.launch_nonce = "nonce-unit"
        old_accounts = main.farm._accounts
        main.farm._accounts = [account]
        client = TestClient(main.app)
        calls = []
        token = issue_lua_session_token(
            main.INSTANCE_TOKEN,
            account="LuaUnit",
            session_id="session-unit",
            launch_nonce="nonce-unit",
            ttl_seconds=900,
        )

        def fake_lua_event(payload):
            calls.append(dict(payload))
            return {
                "ok": True,
                "accepted": True,
                "event": payload.get("event", ""),
                "account": payload.get("account", ""),
                "signal": "",
                "msg": "Lua event accepted",
            }

        try:
            with patch.object(main.farm, "handle_lua_rejoin_event", side_effect=fake_lua_event):
                first = client.post(
                    "/api/lua/rejoin-event",
                    json={
                        "event": "heartbeat",
                        "account": "LuaUnit",
                        "username": "LuaUnit",
                        "session_id": "session-unit",
                        "launch_nonce": "nonce-unit",
                        "event_id": "unit-event-1",
                        "ts": str(int(time.time())),
                        "token": token,
                    },
                )
                replay = client.post(
                    "/api/lua/rejoin-event",
                    json={
                        "event": "heartbeat",
                        "account": "LuaUnit",
                        "username": "LuaUnit",
                        "session_id": "session-unit",
                        "launch_nonce": "nonce-unit",
                        "event_id": "unit-event-1",
                        "ts": str(int(time.time())),
                        "token": token,
                    },
                )
        finally:
            main.farm._accounts = old_accounts

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("token", calls[0])

    def test_lua_rejoin_event_rejects_scoped_token_for_wrong_launch_nonce(self):
        from fastapi.testclient import TestClient
        import main
        from services.lua_session_tokens import issue_lua_session_token

        account = Account("LuaUnit")
        account.session_id = "session-unit"
        account.launch_nonce = "nonce-unit"
        old_accounts = main.farm._accounts
        main.farm._accounts = [account]
        client = TestClient(main.app)
        token = issue_lua_session_token(
            main.INSTANCE_TOKEN,
            account="LuaUnit",
            session_id="session-unit",
            launch_nonce="other-nonce",
            ttl_seconds=900,
        )

        try:
            response = client.post(
                "/api/lua/rejoin-event",
                json={
                    "event": "heartbeat",
                    "account": "LuaUnit",
                    "username": "LuaUnit",
                    "session_id": "session-unit",
                    "launch_nonce": "nonce-unit",
                    "event_id": "unit-event-wrong-nonce",
                    "ts": str(int(time.time())),
                    "token": token,
                },
            )
        finally:
            main.farm._accounts = old_accounts

        self.assertEqual(response.status_code, 403)

    def test_lua_rejoin_event_accepts_cronus_token_alias(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        calls = []

        def fake_lua_event(payload):
            calls.append(dict(payload))
            return {
                "ok": True,
                "accepted": True,
                "event": payload.get("event", ""),
                "account": payload.get("account", ""),
                "signal": "",
                "msg": "Lua event accepted",
            }

        with patch.object(main.farm, "handle_lua_rejoin_event", side_effect=fake_lua_event):
            response = client.post(
                "/api/lua/rejoin-event",
                json={
                    "event": "heartbeat",
                    "account": "LuaUnit",
                    "username": "LuaUnit",
                    "cronus_token": main.INSTANCE_TOKEN,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["accepted"], True)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("cronus_token", calls[0])

    def test_lua_rejoin_event_accepts_legacy_argus_token_aliases_during_migration(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        calls = []

        def fake_lua_event(payload):
            calls.append(dict(payload))
            return {
                "ok": True,
                "accepted": True,
                "event": payload.get("event", ""),
                "account": payload.get("account", ""),
                "signal": "",
                "msg": "Lua event accepted",
            }

        with patch.object(main.farm, "handle_lua_rejoin_event", side_effect=fake_lua_event):
            body_response = client.post(
                "/api/lua/rejoin-event",
                json={
                    "event": "heartbeat",
                    "account": "LuaUnit",
                    "username": "LuaUnit",
                    "argus_token": main.INSTANCE_TOKEN,
                },
            )
            query_response = client.get(
                f"/api/lua/rejoin-event?argus_token={main.INSTANCE_TOKEN}&event=heartbeat&account=LuaUnit&username=LuaUnit"
            )

        self.assertEqual(body_response.status_code, 200)
        self.assertEqual(query_response.status_code, 200)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("argus_token", calls[0])
        self.assertNotIn("argus_token", calls[1])

    def test_lua_rejoin_event_accepts_query_token_fallback(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        calls = []

        def fake_lua_event(payload):
            calls.append(dict(payload))
            return {
                "ok": True,
                "accepted": True,
                "event": payload.get("event", ""),
                "account": payload.get("account", ""),
                "signal": "",
                "msg": "Lua event accepted",
            }

        with patch.object(main.farm, "handle_lua_rejoin_event", side_effect=fake_lua_event):
            response = client.post(
                f"/api/lua/rejoin-event?cronus_token={main.INSTANCE_TOKEN}",
                json={"event": "heartbeat", "account": "LuaUnit", "username": "LuaUnit"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["accepted"], True)
        self.assertEqual(len(calls), 1)

    def test_lua_rejoin_event_rejects_state_changing_get_fallback_when_executor_token_is_mangled(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        calls = []

        def fake_lua_event(payload):
            calls.append(dict(payload))
            return {
                "ok": True,
                "accepted": True,
                "event": payload.get("event", ""),
                "account": payload.get("account", ""),
                "signal": "disconnect_detected",
                "msg": "Lua event accepted",
            }

        with patch.object(main.farm, "handle_lua_rejoin_event", side_effect=fake_lua_event):
            response = client.get(
                "/api/lua/rejoin-event?"
                "cronus_token=bad-token&event=disconnect&account=LuaUnit&username=LuaUnit&"
                "helper_version=1.7.0&error_code=273&reason_key=lua_disconnect_error"
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(calls, [])

    def test_lua_rejoin_event_rejects_unauthenticated_get_without_helper_version(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        response = client.get(
            "/api/lua/rejoin-event?event=disconnect&account=LuaUnit&username=LuaUnit&error_code=273"
        )

        self.assertEqual(response.status_code, 403)

    def test_lua_rejoin_event_routes_to_farm_boundary(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        calls = []

        def fake_lua_event(payload):
            calls.append(dict(payload))
            return {
                "ok": True,
                "accepted": True,
                "event": payload.get("event", ""),
                "account": payload.get("account", ""),
                "signal": "disconnect_detected",
                "msg": "Lua event accepted",
            }

        with patch.object(main.farm, "handle_lua_rejoin_event", side_effect=fake_lua_event):
            response = auth_post(
                client,
                "/api/lua/rejoin-event",
                json={
                    "event": "disconnect",
                    "account": "LuaUnit",
                    "error_code": "277",
                    "reason_key": "lua_disconnect_error",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])
        self.assertEqual(calls[0]["event"], "disconnect")
        self.assertEqual(calls[0]["error_code"], "277")

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
            is_vip=True,
            server_type="VIP",
            private_server_id="",
            place_id="123456",
            job_id="",
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
