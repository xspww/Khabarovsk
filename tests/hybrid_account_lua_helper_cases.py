from tests.hybrid_account_fixture import *


class HybridAccountLuaHelperCases:
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
