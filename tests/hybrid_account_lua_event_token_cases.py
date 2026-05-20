from tests.hybrid_account_fixture import *


class HybridAccountLuaEventTokenCases:
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
