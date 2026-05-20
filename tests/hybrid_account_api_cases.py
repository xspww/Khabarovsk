from tests.hybrid_account_fixture import *


class HybridAccountApiCases:
    def test_queue_popup_disconnected_toggle_persists(self):
        from fastapi.testclient import TestClient
        import main

        original = main.cfg_mgr.snapshot()
        try:
            client = TestClient(main.app)
            html = (
                client.get("/").text
                + "\n" + client.get("/ui/dashboard.js").text
                + "\n" + client.get("/ui/panels/settingsPanels.js").text
            )
            self.assertIn("Popup Detector", html)
            self.assertNotIn("Use Popup Disconnected", html)
            self.assertIn('id="popup-disconnected-enabled"', html)
            self.assertIn('id="popup-scan-interval"', html)
            self.assertIn('id="popup-scan-max-parallel"', html)
            self.assertIn("popup_disconnected_enabled:$('popup-disconnected-enabled').checked", html)
            self.assertIn("popup_scan_interval_seconds:Number($('popup-scan-interval').value)||30", html)
            self.assertIn("popup_scan_max_parallel:Number($('popup-scan-max-parallel').value)||2", html)
            self.assertNotIn('id="presence-enabled"', html)
            self.assertNotIn("Use Presence API", html)
            self.assertNotIn("$('presence-interval')", html)
            self.assertNotIn("$('presence-ttl')", html)
            self.assertNotIn("$('presence-assist')", html)

            response = auth_post(client,
                "/api/config",
                json={
                    "popup_disconnected_enabled": False,
                    "popup_scan_interval_seconds": 45,
                    "popup_scan_max_parallel": 3,
                },
            )
            self.assertEqual(response.status_code, 200)
            config = client.get("/api/config").json()
            self.assertFalse(config["popup_disconnected_enabled"])
            self.assertEqual(config["popup_scan_interval_seconds"], 45)
            self.assertEqual(config["popup_scan_max_parallel"], 3)
        finally:
            main.cfg_mgr.update(original)
            main.cfg_mgr.save()

    def test_popup_disconnected_config_gates_popup_scans(self):
        import inspect
        from farm import AccountWorker, SystemMaintenance

        worker_source = inspect.getsource(AccountWorker.run)
        maintenance_source = inspect.getsource(SystemMaintenance._scan_liveness_watchdog)
        self.assertIn('self.cfg.get("popup_disconnected_enabled", True)', worker_source)
        self.assertIn("popup_scan_interval_seconds", worker_source)
        self.assertIn("effective_hold_sec", worker_source)
        self.assertIn("disconnect_detected", worker_source)
        self.assertIn('"279"', worker_source)
        self.assertIn("popup_enabled", maintenance_source)
        self.assertIn("popup_scan_max_parallel", maintenance_source)
        self.assertIn("_popup_periodic_scan_batch", maintenance_source)
        self.assertIn("inspect_ui = popup_enabled", maintenance_source)
        self.assertIn("state == \"reconnecting\" and popup_enabled", maintenance_source)

    def test_clear_logs_endpoint_truncates_runtime_log(self):
        from fastapi.testclient import TestClient
        import main

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cronus.log")
            with open(path, "w", encoding="utf-8") as f:
                f.write("line one\nline two\n")
            with patch.object(main, "LOG_FILE", path):
                client = TestClient(main.app)
                response = auth_post(client, "/api/logs/clear")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["lines"], [])
                with open(path, encoding="utf-8") as f:
                    self.assertEqual(f.read(), "")
                self.assertEqual(client.get("/api/logs").status_code, 403)
                self.assertEqual(client.get("/api/logs", headers=auth_headers()).json()["lines"], [])

    def test_avatar_endpoint_batches_user_ids(self):
        from fastapi.testclient import TestClient
        import main

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return json.dumps({
                    "data": [
                        {"targetId": 42, "imageUrl": "https://thumb/42.png"},
                        {"targetId": 99, "imageUrl": "https://thumb/99.png"},
                    ]
                }).encode("utf-8")

        main._AVATAR_CACHE.clear()
        client = TestClient(main.app)
        with patch.object(main.urllib.request, "urlopen", return_value=FakeResponse()) as urlopen:
            response = client.get("/api/accounts/avatars?user_ids=42,bad,99,42")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["avatars"]["42"], "https://thumb/42.png")
        self.assertEqual(payload["avatars"]["99"], "https://thumb/99.png")
        self.assertEqual(payload["missing"], [])
        self.assertIn("userIds=42%2C99", urlopen.call_args.args[0].full_url)

    def test_game_place_lookup_html_fallback_unescapes_title_and_image(self):
        from fastapi.testclient import TestClient
        import api_routes.accounts_routes as accounts_routes
        import main

        class FakeResponse:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return self.body

        page = (
            '<html><head><title>Unit &amp; Place | Roblox</title>'
            '<meta property="og:image" content="https://img.test/icon?a=1&amp;b=2">'
            "</head></html>"
        ).encode("utf-8")
        client = TestClient(main.app)
        with patch.object(
            accounts_routes.urllib.request,
            "urlopen",
            side_effect=[
                accounts_routes.urllib.error.URLError("universe unavailable"),
                accounts_routes.urllib.error.URLError("thumbnail unavailable"),
                FakeResponse(page),
            ],
        ):
            response = client.get("/api/game/place/123456")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["name"], "Unit & Place")
        self.assertEqual(payload["image_url"], "https://img.test/icon?a=1&b=2")

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

    def test_app_shutdown_rejects_wrong_token(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        self.assertEqual(auth_post(client, "/api/app/shutdown", json={"token": "wrong"}).status_code, 403)

    def test_api_token_required_for_mutating_routes(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        self.assertEqual(client.post("/api/config", json={}).status_code, 403)

    def test_api_token_allows_mutating_routes(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        response = auth_post(client, "/api/config", json={})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_api_token_accepts_legacy_header_aliases_during_migration(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        for header_name in ("X-Argus-Token", "X-RoboGuard-Token"):
            with self.subTest(header_name=header_name):
                response = client.post(
                    "/api/config",
                    json={},
                    headers={header_name: main.INSTANCE_TOKEN},
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["ok"])

    def test_config_api_accepts_runtime_guard_settings(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        response = auth_post(client, "/api/config", json={
            "roblox_memory_guard_enabled": True,
            "roblox_memory_guard_mb": 32768,
            "roblox_memory_guard_hold_seconds": 30,
            "relaunch_loop_fatal": False,
            "relaunch_loop_cooldown_seconds": 300,
        })

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        for key in (
            "roblox_memory_guard_enabled",
            "roblox_memory_guard_mb",
            "roblox_memory_guard_hold_seconds",
            "relaunch_loop_fatal",
            "relaunch_loop_cooldown_seconds",
        ):
            self.assertIn(key, payload["updated"])
        config = client.get("/api/config").json()
        self.assertTrue(config["roblox_memory_guard_enabled"])
        self.assertEqual(config["roblox_memory_guard_mb"], 32768.0)
        self.assertEqual(config["roblox_memory_guard_hold_seconds"], 30.0)
        self.assertFalse(config["relaunch_loop_fatal"])
        self.assertEqual(config["relaunch_loop_cooldown_seconds"], 300.0)

    def test_mutating_api_audit_logs_idempotency_key(self):
        from fastapi.testclient import TestClient
        import api_routes.auth as auth_routes
        import main

        client = TestClient(main.app)
        with patch.object(auth_routes, "flog_kv") as log:
            response = auth_post(
                client,
                "/api/config",
                headers={"X-Cronus-Idempotency-Key": "audit-unit-key"},
                json={},
            )

        self.assertEqual(response.status_code, 200)
        audit_calls = [
            call for call in log.call_args_list
            if len(call.args) >= 2 and call.args[0] == "API" and call.args[1] == "mutation_audit"
        ]
        self.assertTrue(audit_calls)
        self.assertEqual(audit_calls[-1].kwargs["method"], "POST")
        self.assertEqual(audit_calls[-1].kwargs["path"], "/api/config")
        self.assertEqual(audit_calls[-1].kwargs["status_code"], 200)
        self.assertEqual(audit_calls[-1].kwargs["idempotency_key"], "audit-unit-key")

    def test_runtime_telemetry_endpoint_is_read_only(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        response = client.get("/api/runtime/telemetry")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn("recovery_rate", payload)
        self.assertIn("memory_usage_mb", payload)

    def test_public_farm_health_endpoint_is_snapshot_only_and_redacted(self):
        from fastapi.testclient import TestClient
        import main
        from services.process_service import ProcessManager

        client = TestClient(main.app)
        with (
            patch.object(ProcessManager, "is_bound_game_alive", side_effect=AssertionError("live process scan")),
            patch.object(ProcessManager, "validate_game_process", side_effect=AssertionError("live process scan")),
            patch.object(ProcessManager, "list_live_game_processes", side_effect=AssertionError("live process scan")),
        ):
            response = client.get("/api/farm/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("account_count", payload)
        self.assertIn("queue", payload)
        self.assertNotIn("accounts", payload)
        serialized = str(payload).lower()
        self.assertNotIn("cookie", serialized)
        self.assertNotIn("session_id", serialized)
        self.assertNotIn("launch_nonce", serialized)

    def test_detailed_farm_health_endpoint_requires_token(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)

        rejected = client.get("/api/farm/health/detail")
        self.assertEqual(rejected.status_code, 403)

        accepted = client.get("/api/farm/health/detail", headers={"X-Cronus-Token": main.INSTANCE_TOKEN})
        self.assertEqual(accepted.status_code, 200)
        payload = accepted.json()
        self.assertIn("workers", payload)
        self.assertIn("dispatcher", payload)
        self.assertIn("maintenance", payload)
        self.assertIn("queue", payload)
        self.assertIn("stuck_states", payload)
        self.assertIn("watchdog_decision", payload)
        serialized = str(payload).lower()
        self.assertNotIn("cookie", serialized)
        self.assertNotIn("launch_nonce", serialized)

    def test_runtime_diagnostics_endpoint_requires_token(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)

        rejected = client.get("/api/runtime/diagnostics")
        self.assertEqual(rejected.status_code, 403)

        accepted = client.get("/api/runtime/diagnostics", headers={"X-Cronus-Token": main.INSTANCE_TOKEN})
        self.assertEqual(accepted.status_code, 200)
        payload = accepted.json()
        self.assertTrue(payload["ok"])
        serialized = str(payload).lower()
        self.assertNotIn(".roblosecurity", serialized)
        self.assertNotIn("_|warning:", serialized)

    def test_runtime_events_endpoint_requires_token(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)

        rejected = client.get("/api/runtime/events")
        self.assertEqual(rejected.status_code, 403)

        accepted = client.get("/api/runtime/events", headers={"X-Cronus-Token": main.INSTANCE_TOKEN})
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(accepted.json()["ok"])

    def test_start_rejects_missing_target_with_actionable_payload(self):
        from fastapi.testclient import TestClient
        import main

        account = Account(username="NoTargetUser")
        account.cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--unit"
        command = {"command_id": "cmd-start-missing-target"}
        client = TestClient(main.app)

        with patch.object(main.farm, "begin_command", return_value=(True, command)), \
             patch.object(main.farm, "finish_command") as finish_command, \
             patch.object(main.farm, "running", False), \
             patch.object(main.farm, "_accounts", [account]), \
             patch.object(main.farm, "start") as start:
            response = auth_post(client, "/api/start")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["accepted"])
        self.assertEqual(payload["error_code"], "missing_launch_target")
        self.assertEqual(payload["missing_target_count"], 1)
        self.assertEqual(payload["missing_targets"], ["NoTargetUser"])
        self.assertIn("Set game_place_id", payload["required_action"])
        start.assert_not_called()
        finish_command.assert_called_once()

    def test_app_shutdown_accepts_legacy_header_token(self):
        from fastapi.testclient import TestClient
        import api_routes.system_routes as system_routes
        import main

        class FakeThread:
            def __init__(self, target, daemon=False, name=""):
                self.target = target
                self.daemon = daemon
                self.name = name

            def start(self):
                return None

        client = TestClient(main.app)
        with patch.object(system_routes.threading, "Thread", FakeThread):
            response = client.post(
                "/api/app/shutdown",
                json={},
                headers={"X-RoboGuard-Token": main.INSTANCE_TOKEN},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_close_all_roblox_endpoint_only_closes_roblox_clients(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        command = {"command_id": "cmd-close-all"}
        with patch.object(main.farm, "begin_command", return_value=(True, command)), \
             patch.object(main.farm, "finish_command") as finish_command, \
             patch.object(main.farm, "running", True), \
             patch.object(main.farm, "stop") as stop_guard, \
             patch.object(ProcessService, "kill_all_roblox_clients", return_value=6) as kill_all:
            response = auth_post(client, "/api/roblox/close-all")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["farm_was_running"])
        self.assertEqual(data["closed"], 6)
        stop_guard.assert_not_called()
        kill_all.assert_called_once_with(
            wait_seconds=4.0,
            exclude_pids=None,
            reason="api_close_all_roblox",
            idempotency_key="",
            command_id="cmd-close-all",
        )
        finish_command.assert_called_once()

    def test_close_all_roblox_replays_idempotent_response(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        headers = auth_headers({"X-Cronus-Idempotency-Key": "close-all-idem-unit"})
        with patch.object(ProcessService, "kill_all_roblox_clients", return_value=2) as kill_all:
            first = client.post("/api/roblox/close-all", headers=headers, json={})
            second = client.post("/api/roblox/close-all", headers=headers, json={})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        first_payload = first.json()
        second_payload = second.json()
        self.assertEqual(first_payload["command_id"], second_payload["command_id"])
        self.assertEqual(first_payload["closed"], 2)
        self.assertEqual(second_payload["closed"], 2)
        self.assertEqual(kill_all.call_count, 1)
        self.assertEqual(kill_all.call_args.kwargs["wait_seconds"], 4.0)
        self.assertEqual(kill_all.call_args.kwargs["idempotency_key"], "close-all-idem-unit")

    def test_account_import_replays_idempotency_without_reimporting(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        headers = auth_headers({"X-Cronus-Idempotency-Key": "slice3-import-idem"})
        with patch("api_routes.accounts_routes.ACCOUNT_STORE.import_cookie_lines", return_value={"ok": True, "imported": 1, "count": 1}) as importer, \
             patch("api_routes.accounts_routes.ACCOUNT_STORE.to_cronus_accounts", return_value=[]), \
             patch.object(main.farm, "set_accounts") as set_accounts, \
             patch.object(main.cfg_mgr, "save_accounts"):
            first = client.post("/api/accounts/import", headers=headers, json={"kind": "cookies", "lines": ["_|WARNING:unit"]})
            second = client.post("/api/accounts/import", headers=headers, json={"lines": ["_|WARNING:unit"], "kind": "cookies"})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())
        importer.assert_called_once()
        set_accounts.assert_called_once()

    def test_accounts_reload_replays_idempotency_without_reloading_twice(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        headers = auth_headers({"X-Cronus-Idempotency-Key": "slice3-reload-idem"})
        with patch("api_routes.accounts_routes.ACCOUNT_STORE.read_records", return_value=[]) as read_records, \
             patch("api_routes.accounts_routes.ACCOUNT_STORE.write_records") as write_records, \
             patch("api_routes.accounts_routes.ACCOUNT_STORE.to_cronus_accounts", return_value=[]), \
             patch.object(main.farm, "set_accounts") as set_accounts, \
             patch.object(main.cfg_mgr, "save_accounts"):
            first = client.post("/api/accounts/reload", headers=headers, json={})
            second = client.post("/api/accounts/reload", headers=headers, json={})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())
        read_records.assert_called_once()
        write_records.assert_called_once()
        set_accounts.assert_called_once()

    def test_account_launch_replays_idempotency_without_launching_twice(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        headers = auth_headers({"X-Cronus-Idempotency-Key": "slice3-launch-idem"})
        record = {"username": "LaunchUnit", "cookie": "_|WARNING:unit", "cookie_username": "LaunchUnit"}
        with patch("api_routes.accounts_routes.ACCOUNT_STORE.read_records", return_value=[record]), \
             patch("api_routes.accounts_routes.AccountLaunchService.launch_record", return_value={"ok": False, "msg": "unit blocked"}) as launch_record, \
             patch("api_routes.accounts_routes.audit_event"):
            first = client.post("/api/account/LaunchUnit/launch", headers=headers, json={"place_id": "123456"})
            second = client.post("/api/account/LaunchUnit/launch", headers=headers, json={"place_id": "123456"})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())
        launch_record.assert_called_once()

    def test_logs_clear_replays_idempotency_without_clearing_twice(self):
        from fastapi.testclient import TestClient
        import api_routes.system_routes as system_routes
        import main

        client = TestClient(main.app)
        headers = auth_headers({"X-Cronus-Idempotency-Key": "slice3-logs-idem"})
        with patch.object(system_routes.os, "makedirs", wraps=system_routes.os.makedirs) as makedirs:
            first = client.post("/api/logs/clear", headers=headers, json={})
            second = client.post("/api/logs/clear", headers=headers, json={})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())
        makedirs.assert_called_once()

    def test_network_fault_replays_idempotency_without_reapplying(self):
        from fastapi.testclient import TestClient
        import main

        class FakeInjector:
            def __init__(self):
                self.block_count = 0
                self.restore_count = 0

            def validate_roblox_pid(self, pid):
                return {"ok": True, "pid": int(pid), "name": "RobloxPlayerBeta.exe", "exe": r"C:\Roblox\RobloxPlayerBeta.exe", "create_time": 1.0}

            def find_live_roblox_processes(self):
                return []

            def block_roblox(self, program_path, *, duration_seconds=90, account_id="", pid=None):
                self.block_count += 1
                return {"ok": True, "program": program_path, "duration_seconds": duration_seconds, "account_id": account_id, "pid": pid}

            def restore(self):
                self.restore_count += 1
                return {"ok": True, "active": False}

        original = main.NETWORK_FAULT_INJECTOR
        fake = FakeInjector()
        main.NETWORK_FAULT_INJECTOR = fake
        try:
            client = TestClient(main.app)
            block_headers = auth_headers({"X-Cronus-Idempotency-Key": "slice3-net-block-idem"})
            body = {"pid": 1234, "account_id": "NetUnit", "duration_seconds": 30}
            first = client.post("/api/test/network-fault/block-roblox", headers=block_headers, json=body)
            second = client.post("/api/test/network-fault/block-roblox", headers=block_headers, json=body)
            restore_headers = auth_headers({"X-Cronus-Idempotency-Key": "slice3-net-restore-idem"})
            restored = client.post("/api/test/network-fault/restore", headers=restore_headers, json={"account_id": "NetUnit"})
            restored_again = client.post("/api/test/network-fault/restore", headers=restore_headers, json={"account_id": "NetUnit"})
        finally:
            main.NETWORK_FAULT_INJECTOR = original

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored_again.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(restored.json(), restored_again.json())
        self.assertEqual(fake.block_count, 1)
        self.assertEqual(fake.restore_count, 1)

    def test_idempotency_helper_fields_are_in_mutation_audit(self):
        from fastapi.testclient import TestClient
        import api_routes.auth as auth_routes
        import main

        client = TestClient(main.app)
        with patch.object(auth_routes, "flog_kv") as log:
            response = client.post(
                "/api/logs/clear",
                headers=auth_headers({"X-Cronus-Idempotency-Key": "slice3-audit-idem"}),
                json={},
            )

        self.assertEqual(response.status_code, 200)
        audit_calls = [
            call for call in log.call_args_list
            if len(call.args) >= 2 and call.args[0] == "API" and call.args[1] == "mutation_audit"
        ]
        self.assertTrue(audit_calls)
        fields = audit_calls[-1].kwargs
        self.assertEqual(fields["idempotency_key"], "slice3-audit-idem")
        self.assertEqual(fields["idempotency_action"], "logs_clear")
        self.assertTrue(fields["idempotency_body_hash"])
