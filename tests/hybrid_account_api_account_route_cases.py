from tests.hybrid_account_fixture import *


class HybridAccountApiAccountRouteCases:
    def test_cookie_refresh_routes_removed(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        self.assertEqual(auth_post(client, "/api/accounts/refresh-cookie", json={"usernames": ["UserA"]}).status_code, 404)
        self.assertEqual(auth_post(client, "/api/account/UserA/refresh-cookie", json={}).status_code, 404)
        self.assertEqual(auth_post(client, "/api/accounts/refresh-stale", json={}).status_code, 404)

    def test_accounts_cookie_import_route_reloads_without_game_default_name_error(self):
        from fastapi.testclient import TestClient
        import api_routes.accounts_routes as accounts_routes
        import main

        client = TestClient(main.app)
        with patch.object(
            accounts_routes.ACCOUNT_STORE,
            "import_cookie_lines",
            return_value={"ok": True, "imported": 1, "errors": []},
        ) as import_cookie_lines, patch.object(
            accounts_routes.ACCOUNT_STORE,
            "to_cronus_accounts",
            return_value=[],
        ), patch.object(main.farm, "running", False), patch.object(
            main.farm,
            "set_accounts",
        ) as set_accounts, patch.object(
            main.cfg_mgr,
            "save_accounts",
        ) as save_accounts:
            response = auth_post(client,
                "/api/accounts/import",
                json={"kind": "cookies", "lines": ["UnitUser:fake-cookie"]},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["imported"], 1)
        import_cookie_lines.assert_called_once()
        set_accounts.assert_called_once_with([])
        save_accounts.assert_called_once_with([])

    def test_accounts_userpass_import_route_is_removed(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        response = auth_post(client, "/api/accounts/import", json={"kind": "userpass", "lines": ["UnitUser:password"]})

        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported import kind", response.text)

    def test_manual_login_complete_endpoint_removed(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        response = auth_post(client,
            "/api/accounts/manual-login/complete",
            json={"request_id": "req1", "token": "bad", "cookie": "_|WARNING:secret-cookie"},
        )

        self.assertEqual(response.status_code, 404)

    def test_accounts_reload_route_reloads_store_into_farm(self):
        from fastapi.testclient import TestClient
        import api_routes.accounts_routes as accounts_routes
        import main

        client = TestClient(main.app)
        payload = [{"username": "ReloadUser", "cookie": "_|WARNING:reload"}]
        with patch.object(
            accounts_routes.ACCOUNT_STORE,
            "read_records",
            return_value=payload,
        ), patch.object(
            accounts_routes,
            "validate_cookie_details",
            return_value=(True, "ReloadUser", "ok", {"username": "ReloadUser", "user_id": "42"}),
        ), patch.object(
            accounts_routes.ACCOUNT_STORE,
            "write_records",
        ) as write_records, patch.object(
            accounts_routes.ACCOUNT_STORE,
            "to_cronus_accounts",
            return_value=payload,
        ), patch.object(main.farm, "running", False), patch.object(
            main.farm,
            "set_accounts",
        ) as set_accounts, patch.object(
            main.cfg_mgr,
            "save_accounts",
        ) as save_accounts:
            response = auth_post(client, "/api/accounts/reload", json={})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["kept"], 1)
        self.assertEqual(data["removed"], 0)
        written = write_records.call_args.args[0]
        self.assertEqual(written[0]["username"], "ReloadUser")
        self.assertEqual(written[0]["cookie_username"], "ReloadUser")
        self.assertEqual(written[0]["cookie_user_id"], "42")
        self.assertEqual(set_accounts.call_args.args[0][0].username, "ReloadUser")
        save_accounts.assert_called_once()

    def test_accounts_reload_route_removes_invalid_cookies_before_reloading(self):
        from fastapi.testclient import TestClient
        import api_routes.accounts_routes as accounts_routes
        import main

        client = TestClient(main.app)
        records = [
            {"username": "ValidUser", "cookie": "_|WARNING:valid"},
            {"username": "BadUser", "cookie": "_|WARNING:bad"},
            {"username": "EmptyUser", "cookie": ""},
            {"username": "CaptchaUser", "cookie": "_|WARNING:captcha", "cookie_mismatch": True},
        ]

        def validate(cookie):
            if cookie == "_|WARNING:valid":
                return True, "ValidUser", "ok", {"username": "ValidUser", "user_id": "77"}
            if cookie == "_|WARNING:captcha":
                return False, "", "CAPTCHA required", {}
            return False, "", "cookie validation failed (401)", {}

        with patch.object(
            accounts_routes.ACCOUNT_STORE,
            "read_records",
            return_value=records,
        ), patch.object(
            accounts_routes,
            "validate_cookie_details",
            side_effect=validate,
        ) as validator, patch.object(
            accounts_routes.ACCOUNT_STORE,
            "write_records",
        ) as write_records, patch.object(
            accounts_routes.ACCOUNT_STORE,
            "to_cronus_accounts",
            return_value=[
                {"username": "ValidUser", "cookie": "_|WARNING:valid"},
                {"username": "CaptchaUser", "cookie": "_|WARNING:captcha", "manual_status": CAPTCHA_BLOCK_REASON},
            ],
        ), patch.object(accounts_routes, "audit_event"), patch.object(main.farm, "running", False), patch.object(
            main.farm,
            "set_accounts",
        ) as set_accounts, patch.object(
            main.cfg_mgr,
            "save_accounts",
        ), patch.object(main.farm, "_push_event") as push_event:
            response = auth_post(client, "/api/accounts/reload", json={})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["kept"], 2)
        self.assertEqual(data["removed"], 2)
        self.assertEqual(data["captcha"], 1)
        self.assertEqual(data["count"], 2)
        self.assertEqual(validator.call_count, 3)
        written = write_records.call_args.args[0]
        self.assertEqual([item["username"] for item in written], ["ValidUser", "CaptchaUser"])
        self.assertTrue(written[1]["cookie_mismatch"])
        self.assertEqual(set_accounts.call_args.args[0][0].username, "ValidUser")
        messages = [str(call.args[1]) for call in push_event.call_args_list]
        self.assertIn("Reload Cookies checked: 1 valid, 1 CAPTCHA, 2 invalid", messages)
        self.assertIn("Reload Cookies OK: ValidUser", messages)
        self.assertIn("Reload Cookies CAPTCHA: CaptchaUser - solve manually", messages)
        self.assertIn("Reload Cookies invalid: BadUser - cookie validation failed (401)", messages)
        self.assertIn("Reload Cookies invalid: EmptyUser - missing cookie", messages)

    def test_accounts_reload_does_not_stop_running_farm(self):
        from fastapi.testclient import TestClient
        import api_routes.accounts_routes as accounts_routes
        import main

        client = TestClient(main.app)
        runtime_account = Account(username="ReloadUser", cookie="_|WARNING:old")
        records = [{"username": "ReloadUser", "cookie": "_|WARNING:reload"}]
        validated_records = [{
            "username": "ReloadUser",
            "cookie": "_|WARNING:reload",
            "cookie_username": "ReloadUser",
            "cookie_user_id": "42",
        }]

        with patch.object(
            accounts_routes.ACCOUNT_STORE,
            "read_records",
            return_value=records,
        ), patch.object(
            accounts_routes,
            "validate_cookie_details",
            return_value=(True, "ReloadUser", "ok", {"username": "ReloadUser", "user_id": "42"}),
        ), patch.object(
            accounts_routes.ACCOUNT_STORE,
            "write_records",
        ), patch.object(
            accounts_routes.ACCOUNT_STORE,
            "to_cronus_accounts",
            return_value=validated_records,
        ), patch.object(main.farm, "running", True), patch.object(
            main.farm,
            "_accounts",
            [runtime_account],
        ), patch.object(main.farm, "stop") as stop, patch.object(main.farm, "start") as start, patch.object(
            main.farm,
            "set_accounts",
        ) as set_accounts, patch.object(
            main.cfg_mgr,
            "save_accounts",
        ) as save_accounts, patch.object(
            main.farm._runtime_store,
            "record_account_snapshot",
        ):
            response = auth_post(client, "/api/accounts/reload", json={})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["count"], 1)
        stop.assert_not_called()
        start.assert_not_called()
        set_accounts.assert_not_called()
        save_accounts.assert_called()
        self.assertEqual(runtime_account.cookie, "_|WARNING:reload")
        self.assertEqual(runtime_account.cookie_username, "ReloadUser")
        self.assertEqual(runtime_account.cookie_user_id, "42")

    def test_accounts_reload_clears_runtime_account_allowlist_lock(self):
        from fastapi.testclient import TestClient
        import api_routes.accounts_routes as accounts_routes
        import main

        client = TestClient(main.app)
        records = [{"username": "UnlockedUser", "cookie": "_|WARNING:reload"}]
        validated_records = [{
            "username": "UnlockedUser",
            "cookie": "_|WARNING:reload",
            "cookie_username": "UnlockedUser",
            "cookie_user_id": "42",
        }]

        with patch.object(
            accounts_routes.ACCOUNT_STORE,
            "read_records",
            return_value=records,
        ), patch.object(
            accounts_routes,
            "validate_cookie_details",
            return_value=(True, "UnlockedUser", "ok", {"username": "UnlockedUser", "user_id": "42"}),
        ), patch.object(
            accounts_routes.ACCOUNT_STORE,
            "write_records",
        ), patch.object(
            accounts_routes.ACCOUNT_STORE,
            "to_cronus_accounts",
            return_value=validated_records,
        ), patch.object(main.farm, "running", False), patch.object(
            main.farm,
            "set_accounts",
        ), patch.object(
            main.cfg_mgr,
            "save_accounts",
        ), patch.object(
            main.cfg_mgr,
            "get",
            return_value=["LockedUser", "OtherUser"],
        ) as cfg_get, patch.object(
            main.cfg_mgr,
            "update",
        ) as cfg_update, patch.object(
            main.cfg_mgr,
            "save",
        ) as cfg_save, patch.object(
            main.farm,
            "apply_config_snapshot",
        ) as apply_config_snapshot, patch.object(main.farm, "_push_event") as push_event:
            response = auth_post(client, "/api/accounts/reload", json={})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["allowlist_cleared"])
        self.assertEqual(data["allowlist_cleared_count"], 2)
        self.assertIn("cleared account test lock", data["msg"])
        cfg_get.assert_any_call("runtime_account_allowlist", [])
        cfg_update.assert_called_once_with({"runtime_account_allowlist": []})
        cfg_save.assert_called_once()
        apply_config_snapshot.assert_called_once()
        messages = [str(call.args[1]) for call in push_event.call_args_list]
        self.assertIn("Reload Cookies cleared account test lock: 2 account(s)", messages)

    def test_running_account_reload_reconciles_added_and_removed_accounts(self):
        from services.account_reload import replace_farm_accounts

        class Store:
            def record_account_snapshot(self, *_args, **_kwargs):
                return None

        class Cfg:
            def __init__(self):
                self.saved = []

            def save_accounts(self, accounts):
                self.saved = [account.username for account in accounts]

        class Farm:
            running = True

            def __init__(self):
                self._accounts = [Account(username="OldUser")]
                self._workers = {}
                self._runtime_store = Store()
                self._runtime_state = RuntimeStateManager(logger=lambda *_args, **_kwargs: None)
                self._runtime_scheduler = None
                self._runtime_orchestrator = None
                self._state_mgr = None
                self._recovery = None
                self._maintenance = None
                self._dispatcher = None
                self.events = []
                self.revisions = 0

            def _push_event(self, *args, **kwargs):
                self.events.append((args, kwargs))

            def _bump_status_revision(self):
                self.revisions += 1

            def resume_captcha_account(self, _username):
                return True, "resumed"

        farm = Farm()
        cfg = Cfg()

        count = replace_farm_accounts(farm, cfg, [Account(username="NewUser")])

        self.assertEqual(count, 1)
        self.assertEqual([account.username for account in farm._accounts], ["NewUser"])
        self.assertEqual(cfg.saved, ["NewUser"])
        self.assertEqual(farm.revisions, 1)

    def test_account_reload_rejects_duplicate_runtime_keys(self):
        from services.account_reload import AccountReconciliationError, replace_farm_accounts

        class Farm:
            running = False

            def set_accounts(self, _accounts):
                raise AssertionError("duplicate account data must not be applied")

        with self.assertRaisesRegex(AccountReconciliationError, "Duplicate account username"):
            replace_farm_accounts(Farm(), object(), [Account(username="DupUser"), Account(username="dupuser")])
