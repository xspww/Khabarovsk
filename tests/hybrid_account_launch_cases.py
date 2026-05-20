from tests.hybrid_account_fixture import *


class HybridAccountLaunchCases:
    def test_cookie_and_vip_parsing(self):
        username, cookie = parse_cookie_line("UserA:pass:_|WARNING:-DO-NOT-SHARE-THIS.--abc")
        self.assertEqual(username, "UserA")
        self.assertTrue(cookie.startswith("_|WARNING:"))
        place, code = parse_vip_link(
            "https://www.roblox.com/games/123456/Game?privateServerLinkCode=abcdef"
        )
        self.assertEqual(place, "123456")
        self.assertEqual(code, "abcdef")

    def test_launch_uri_contains_tracker_and_redacts_ticket_separately(self):
        launcher_url, mode, _vip = build_place_launcher_url("123456", job_id="job-1", browser_tracker_id="111222")
        self.assertEqual(mode, "job")
        uri = build_roblox_player_uri("ticket-secret", launcher_url, "111222")
        self.assertIn("roblox-player:1+launchmode:play", uri)
        self.assertIn("browsertrackerid:111222", uri)

    def test_vip_access_code_html_and_launcher_url(self):
        html = "Roblox.GameLauncher.joinPrivateGame(123456, '5f1769bd-e647-40b0-9150-e1ea9f73987a', '46520250616173349819760266740618')"
        parsed = parse_vip_access_code_html(html)
        self.assertTrue(parsed["ok"])
        launcher_url, mode, _vip = build_place_launcher_url(
            "123456",
            vip_link="https://www.roblox.com/games/123456/Game?privateServerLinkCode=46520250616173349819760266740618",
            vip_access_code=parsed["access_code"],
            vip_link_code=parsed["link_code"],
        )
        self.assertEqual(mode, "vip")
        self.assertIn("accessCode=5f1769bd-e647-40b0-9150-e1ea9f73987a", launcher_url)
        self.assertIn("linkCode=46520250616173349819760266740618", launcher_url)

    def test_private_server_link_builder_uses_join_or_access_code(self):
        link = build_owned_private_server_link("123", {"joinCode": "join-secret"})
        self.assertIn("privateServerLinkCode=join-secret", link)
        access_link = build_owned_private_server_link("123", {"accessCode": "5f1769bd-e647-40b0-9150-e1ea9f73987a"})
        self.assertIn("accessCode=5f1769bd-e647-40b0-9150-e1ea9f73987a", access_link)

    def test_private_server_link_builder_normalizes_share_links(self):
        link = build_owned_private_server_link(
            "123",
            {
                "link": "https://www.roblox.com/share?code=share-secret&type=Server",
                "joinCode": "join-secret",
            },
        )
        self.assertIn("/games/123/", link)
        self.assertIn("privateServerLinkCode=join-secret", link)
        self.assertNotIn("/share", link)

    def test_existing_owned_private_server_is_reused_without_create(self):
        class FakeClient:
            def __init__(self):
                self.created = False

            def request(self, url, method="GET", data=None, headers=None, timeout=12.0, retry_csrf=True):
                if "my-private-servers" in url:
                    return 200, json.dumps({"data": [{"privateServerId": "vip-1", "ownerId": 42, "placeId": 123, "universeId": 456, "active": True}]}), {}
                if "vip-servers/vip-1" in url:
                    return 200, json.dumps({"id": "vip-1", "owner": {"id": 42}, "game": {"placeId": 123, "universeId": 456}, "joinCode": "join-secret", "active": True}), {}
                raise AssertionError(url)

            def csrf_post(self, url, data=None, method="POST"):
                self.created = True
                return True, {}, "ok", {}

        fake = FakeClient()
        with patch("roblox_hybrid.universe_id_for_place", return_value=(True, "456", "ok")):
            result = ensure_owned_private_server(fake, "UserA", "42", "123")
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "existing")
        self.assertEqual(result["private_server_id"], "vip-1")
        self.assertFalse(fake.created)
        self.assertIn("privateServerLinkCode=join-secret", result["link"])

    def test_existing_owned_private_server_reuses_stored_access_code_when_metadata_omits_secret(self):
        access_code = "5f1769bd-e647-40b0-9150-e1ea9f73987a"

        class FakeClient:
            def __init__(self):
                self.created = False

            def request(self, url, method="GET", data=None, headers=None, timeout=12.0, retry_csrf=True):
                if "my-private-servers" in url:
                    return 200, json.dumps({"data": [{"privateServerId": "vip-1", "ownerId": 42, "placeId": 123, "universeId": 456, "active": True}]}), {}
                if "vip-servers/vip-1" in url:
                    return 200, json.dumps({"id": "vip-1", "name": "Unit Game", "active": True}), {}
                raise AssertionError(url)

            def csrf_post(self, url, data=None, method="POST"):
                self.created = True
                return True, {}, "ok", {}

        fake = FakeClient()
        with patch("roblox_hybrid.universe_id_for_place", return_value=(True, "456", "ok")):
            result = ensure_owned_private_server(
                fake,
                "UserA",
                "42",
                "123",
                known_servers=[
                    {
                        "owner_user_id": "42",
                        "place_id": "123",
                        "universe_id": "456",
                        "status": "error",
                    },
                    {
                        "private_server_id": "vip-1",
                        "owner_user_id": "42",
                        "place_id": "123",
                        "universe_id": "456",
                        "access_code": access_code,
                    }
                ],
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "existing")
        self.assertFalse(fake.created)
        self.assertEqual(result["access_code"], access_code)
        self.assertIn(f"accessCode={access_code}", result["link"])

    def test_existing_owned_private_server_uses_place_server_access_code(self):
        access_code = "5f1769bd-e647-40b0-9150-e1ea9f73987a"

        class FakeClient:
            def __init__(self):
                self.created = False
                self.place_list_called = False

            def request(self, url, method="GET", data=None, headers=None, timeout=12.0, retry_csrf=True):
                if "my-private-servers" in url:
                    return 200, json.dumps({
                        "data": [{
                            "privateServerId": "vip-1",
                            "ownerId": 42,
                            "placeId": 123,
                            "universeId": 456,
                            "active": True,
                        }]
                    }), {}
                if "vip-servers/vip-1" in url:
                    return 200, json.dumps({"id": "vip-1", "name": "Unit Game", "active": True}), {}
                if "games/123/private-servers" in url:
                    self.place_list_called = True
                    return 200, json.dumps({
                        "data": [{
                            "id": "vip-1",
                            "vipServerId": "vip-1",
                            "owner": {"id": 42},
                            "accessCode": access_code,
                            "name": "Unit Game",
                        }]
                    }), {}
                raise AssertionError(url)

            def csrf_post(self, url, data=None, method="POST"):
                self.created = True
                return True, {}, "ok", {}

        fake = FakeClient()
        with patch("roblox_hybrid.universe_id_for_place", return_value=(True, "456", "ok")):
            result = ensure_owned_private_server(fake, "UserA", "42", "123")

        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "existing")
        self.assertTrue(fake.place_list_called)
        self.assertFalse(fake.created)
        self.assertEqual(result["access_code"], access_code)
        self.assertIn(f"accessCode={access_code}", result["link"])

    def test_auto_create_private_server_sends_free_payload(self):
        class FakeClient:
            def __init__(self):
                self.create_payload = None

            def request(self, url, method="GET", data=None, headers=None, timeout=12.0, retry_csrf=True):
                if "my-private-servers" in url:
                    return 200, json.dumps({"data": []}), {}
                if "v1/games?universeIds=456" in url:
                    return 200, json.dumps({"data": [{"id": 456, "name": "Attack Game"}]}), {}
                if "enabled-in-universe" in url:
                    return 200, json.dumps({"privateServersEnabled": True}), {}
                if "vip-servers/vip-created" in url:
                    return 200, json.dumps({"id": "vip-created", "owner": {"id": 42}, "game": {"placeId": 123, "universeId": 456}, "active": True}), {}
                raise AssertionError(url)

            def csrf_post(self, url, data=None, method="POST"):
                self.create_payload = dict(data or {})
                return True, {"vipServerId": "vip-created", "accessCode": "5f1769bd-e647-40b0-9150-e1ea9f73987a", "ownerId": 42}, "ok", {}

        fake = FakeClient()
        with patch("roblox_hybrid.universe_id_for_place", return_value=(True, "456", "ok")):
            result = ensure_owned_private_server(fake, "UserA", "42", "123", name_template="ignored template")
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "created")
        self.assertEqual(fake.create_payload["expectedPrice"], 0)
        self.assertTrue(fake.create_payload["isPurchaseConfirmed"])
        self.assertEqual(fake.create_payload["name"], "Attack Game")
        self.assertEqual(result["access_code"], "5f1769bd-e647-40b0-9150-e1ea9f73987a")

    def test_private_servers_disabled_returns_clear_error(self):
        class FakeClient:
            def request(self, url, method="GET", data=None, headers=None, timeout=12.0, retry_csrf=True):
                if "my-private-servers" in url:
                    return 200, json.dumps({"data": []}), {}
                if "enabled-in-universe" in url:
                    return 200, json.dumps({"privateServersEnabled": False}), {}
                raise AssertionError(url)

            def csrf_post(self, url, data=None, method="POST"):
                raise AssertionError("create should not be called")

        with patch("roblox_hybrid.universe_id_for_place", return_value=(True, "456", "ok")):
            result = ensure_owned_private_server(FakeClient(), "UserA", "42", "123")
        self.assertFalse(result["ok"])
        self.assertIn("disabled", result["msg"].lower())

    def test_browser_tracker_detection(self):
        cmd = "RobloxPlayerBeta.exe --app -t token -j url browsertrackerid:123456"
        self.assertEqual(HybridLauncher._tracker_from_cmdline(cmd), "123456")
        self.assertEqual(HybridLauncher._tracker_from_cmdline("RobloxPlayerBeta.exe -b 654321"), "654321")
        self.assertEqual(
            HybridLauncher._tracker_from_cmdline("placelauncherurl:browserTrackerId%3D777888%26placeId"),
            "777888",
        )

    def test_launch_destination_parser_detects_private_server(self):
        cmd = (
            "RobloxPlayerBeta.exe roblox-player:1+launchmode:play+gameinfo:[x]"
            "+placelauncherurl:https%3A%2F%2Fassetgame.roblox.com%2Fgame%2FPlaceLauncher.ashx%3F"
            "request%3DRequestPrivateGame%26placeId%3D123456%26accessCode%3D5f1769bd-e647-40b0-9150-e1ea9f73987a"
            "%26linkCode%3D46520250616173349819760266740618+browsertrackerid:777888"
        )
        evidence = parse_launch_destination_from_cmdline(cmd)
        self.assertEqual(evidence["observed_place_id"], "123456")
        self.assertEqual(evidence["observed_server_type"], "VIP")
        self.assertTrue(evidence["observed_private_link_code_hash"])

    def test_dispatcher_verifies_private_server_evidence(self):
        link_code = "46520250616173349819760266740618"
        link_hash = hashlib.sha256(link_code.encode("utf-8")).hexdigest()[:16]
        acc = Account(username="UserA", place_id="123456")
        acc.launch_intent = {
            "place_id": "123456",
            "server_type": "VIP",
            "private_server_intent": True,
            "active_private_link_code_hash": link_hash,
            "configured_private_link_code_hashes": [link_hash],
        }
        dispatcher = object.__new__(Dispatcher)
        ok, validation, msg = Dispatcher._validate_launch_intent(
            dispatcher,
            acc,
            {
                "observed_place_id": "123456",
                "observed_server_type": "VIP",
                "observed_private_link_code_hash": link_hash,
            },
        )
        self.assertTrue(ok)
        self.assertEqual(validation, "private_server_verified")
        self.assertEqual(msg, "")

    def test_access_code_vip_url_builds_private_intent_hash(self):
        access_code = "5f1769bd-e647-40b0-9150-e1ea9f73987a"
        acc = Account(username="UserA", place_id="123456")
        acc.server_type = ServerType.VIP
        acc.active_vip = f"https://www.roblox.com/games/123456/?accessCode={access_code}"
        intent = build_launch_intent(acc, reason="unit")
        self.assertTrue(intent["private_server_intent"])
        self.assertEqual(intent["active_private_link_code_hash"], hashlib.sha256(access_code.encode("utf-8")).hexdigest()[:16])

    def test_launch_intent_includes_browser_tracker_label(self):
        acc = Account(username="UserA", place_id="123456", browser_tracker_id="TRACKER-ABCDEFGH")
        intent = build_launch_intent(acc, reason="unit")
        self.assertEqual(intent["browser_tracker_id"], "...ABCDEFGH")
        self.assertEqual(intent["launch_intent_summary"]["browser_tracker_id"], "...ABCDEFGH")

    def test_cookie_identity_mismatch_blocks_launch_guard(self):
        with patch("roblox_hybrid.validate_cookie_details", return_value=(True, "RealUser", "ok", {"user_id": "42"})):
            result = validate_record_cookie_identity({"username": "OtherUser"}, "_|WARNING:-cookie", update_store=False)
        self.assertFalse(result["ok"])
        self.assertTrue(result["cookie_mismatch"])
        self.assertIn("RealUser", result["msg"])

    def test_captcha_identity_validation_preserves_cookie_mismatch_gate(self):
        import roblox_hybrid

        with patch("roblox_hybrid.validate_cookie_details", return_value=(False, "", "CAPTCHA required", {})), patch.object(
            roblox_hybrid.ACCOUNT_STORE,
            "update_record",
        ) as update_record:
            result = validate_record_cookie_identity(
                {"username": "CaptchaUser", "cookie_mismatch": True},
                "_|WARNING:-cookie",
                update_store=True,
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["captcha_required"])
        update_record.assert_called_once()
        self.assertNotIn("cookie_mismatch", update_record.call_args.args[1])

    def test_account_launch_block_reason_detects_cookie_owner_mismatch(self):
        acc = Account(username="OtherUser", cookie_username="RealUser", cookie_mismatch=True)
        reason = account_launch_block_reason(acc)
        self.assertIn("RealUser", reason)
        self.assertIn("OtherUser", reason)

    def test_auth_gate_does_not_treat_captcha_username_cookie_mismatch_as_captcha(self):
        from services.auth_gate import evaluate_account_auth_gate

        acc = Account(username="CaptchaUser", cookie_mismatch=True)
        acc.manual_status = "Cookie identity mismatch for CaptchaUser. Reimport the correct .ROBLOSECURITY for this account."

        decision = evaluate_account_auth_gate(acc)

        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason_key, "cookie_mismatch")

    def test_captcha_hold_blocks_launch_until_resume(self):
        acc = Account(username="CaptchaUser")
        detail = captcha_detail(403, "", {"Rblx-Challenge-Type": "captcha"})
        self.assertIn("CAPTCHA", detail)
        set_account_captcha_hold(acc, detail, source="unit_test")
        self.assertEqual(acc.last_crash_reason, CAPTCHA_REASON)
        self.assertEqual(account_launch_block_reason(acc), CAPTCHA_BLOCK_REASON)
        self.assertTrue(clear_account_captcha_hold(acc))
        self.assertEqual(account_launch_block_reason(acc), "")

    def test_captcha_hold_persists_only_configured_account_key(self):
        import account_hybrid

        class Store:
            def __init__(self):
                self.calls = []

            def update_record(self, username, updates):
                self.calls.append((username, dict(updates)))

        store = Store()
        acc = Account(username="ConfigUser", cookie_username="CookieOwner", alias="AliasUser")
        with patch.object(account_hybrid, "ACCOUNT_STORE", store):
            set_account_captcha_hold(acc, "CAPTCHA challenge detected", source="unit_test")
            clear_account_captcha_hold(acc)

        self.assertEqual([call[0] for call in store.calls], ["ConfigUser", "ConfigUser"])
        self.assertNotIn("cookie_mismatch", store.calls[0][1])
        self.assertNotIn("cookie_mismatch", store.calls[1][1])

    def test_captcha_resume_preserves_cookie_mismatch_gate(self):
        acc = Account(username="CaptchaUser", cookie_mismatch=True, manual_status=CAPTCHA_BLOCK_REASON)
        acc.last_crash_reason = CAPTCHA_REASON

        self.assertTrue(clear_account_captcha_hold(acc))

        self.assertTrue(acc.cookie_mismatch)
        self.assertIn("Cookie identity mismatch", account_launch_block_reason(acc))

    def test_farm_captcha_resume_keeps_auth_quarantine_when_cookie_mismatch_remains(self):
        from farm import FarmController

        class Cfg:
            def save_accounts(self, _accounts):
                return None

            def save_runtime(self, _accounts):
                return None

        class Recovery:
            def __init__(self):
                self.failed = []

            def fail_account(self, account, reason, msg):
                account.last_crash_reason = reason
                account.state = AccountState.FAILED
                self.failed.append((reason, msg))

        acc = Account(username="CaptchaUser", cookie_mismatch=True, manual_status=CAPTCHA_BLOCK_REASON)
        farm = object.__new__(FarmController)
        farm._accounts = [acc]
        farm._runtime_state = RuntimeStateManager(logger=lambda *_args, **_kwargs: None)
        farm._recovery = Recovery()
        farm.cfg_mgr = Cfg()
        farm.running = True
        farm._push_event = lambda *_args, **_kwargs: None
        farm._bump_status_revision = lambda: None

        ok, msg = FarmController.resume_captcha_account(farm, "CaptchaUser")

        self.assertFalse(ok)
        self.assertIn("still blocked", msg)
        self.assertEqual(acc.state, AccountState.FAILED)
        self.assertEqual(farm._recovery.failed[-1][0], "cookie_mismatch")
        self.assertIn("Cookie identity mismatch", account_launch_block_reason(acc))

    def test_captcha_hold_runtime_fields_go_through_runtime_writer(self):
        acc = Account(username="CaptchaUser")
        runtime = RuntimeStateManager()
        acc.recovery_status = "scheduled"
        acc.recovery_inflight = True
        acc.cooldown_until = time.time() + 60
        acc.sync_runtime("test_seed")

        set_account_captcha_hold(acc, "CAPTCHA challenge detected", source="unit_test", runtime_writer=runtime)

        self.assertEqual(acc.recovery_status, CAPTCHA_REASON)
        self.assertFalse(acc.recovery_inflight)
        self.assertEqual(acc.cooldown_until, 0.0)
        self.assertEqual(acc.runtime.recovery_status, CAPTCHA_REASON)
        self.assertFalse(acc.runtime.recovery_inflight)

        self.assertTrue(clear_account_captcha_hold(acc, runtime_writer=runtime))

        self.assertEqual(acc.recovery_status, "")
        self.assertFalse(acc.recovery_inflight)
        self.assertEqual(acc.cooldown_until, 0.0)
        self.assertEqual(acc.runtime.recovery_status, "")
        self.assertFalse(acc.runtime.recovery_inflight)

    def test_security_webview_texts_classify_as_captcha_hold(self):
        from runtime.popup_detector.popup_classifier import classify_popup_observation

        result = classify_popup_observation(
            ["Roblox", "R", "Zuckmu: 13+", "Security", "Chrome Legacy Window"],
            threshold=0.75,
        )

        self.assertTrue(result.matched)
        self.assertEqual(result.action, "hold")
        self.assertEqual(result.reason_key, CAPTCHA_REASON)
        self.assertFalse(result.recovery_allowed)
        self.assertEqual(result.evidence_source, "text")

    def test_popup_observer_confirms_captcha_security_webview(self):
        from runtime.popup_detector.popup_sampler import PopupObserver

        observer = PopupObserver(sample_count=2, sample_interval=0, threshold=0.75, stable_samples=2)
        with patch("runtime.popup_detector.popup_sampler.PopupWindowSampler.windows_for_pid", return_value=[{"hwnd": 123}]), patch(
            "runtime.popup_detector.popup_sampler.PopupWindowSampler.read_texts",
            return_value=["Roblox", "R", "Zuckmu: 13+", "Security", "Chrome Legacy Window"],
        ), patch("runtime.popup_detector.popup_sampler.PopupWindowSampler.capture_window_image", return_value=None):
            result = observer.inspect_pid(2532, sample_count=2, sample_interval=0)

        self.assertTrue(result["matched"])
        self.assertEqual(result["reason_key"], CAPTCHA_REASON)
        self.assertEqual(result["action"], "hold")
        self.assertFalse(result["recovery_allowed"])
        self.assertEqual(result["captcha_samples"], 2)

    def test_captcha_open_login_route_uses_manual_browser_flow(self):
        from fastapi.testclient import TestClient
        import main

        username = "CaptchaLoginUser"
        client = TestClient(main.app)
        with patch(
            "api_routes.accounts_routes.ACCOUNT_STORE.read_records",
            return_value=[{"username": username, "manual_status": CAPTCHA_BLOCK_REASON, "import_status": CAPTCHA_REASON}],
        ), patch("api_routes.accounts_routes.webbrowser.open", return_value=True) as open_browser:
            response = auth_post(client, f"/api/account/{username}/captcha/open-login", json={})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["opened"])
        self.assertIn("Solve CAPTCHA manually", payload["msg"])
        open_browser.assert_called_once_with("https://www.roblox.com/login", new=2)

    def test_captcha_focus_route_targets_bound_roblox_window(self):
        from fastapi.testclient import TestClient
        import main

        account = Account(username="CaptchaFocusUser")
        account.pid = 9876
        command = {"command_id": "captcha-focus-command"}
        client = TestClient(main.app)
        with patch.object(main.farm, "begin_command", return_value=(True, command)), patch.object(
            main.farm,
            "finish_command",
        ) as finish_command, patch.object(main.farm, "_find_account", return_value=account), patch(
            "api_routes.runtime_routes.PopupWindowSampler.focus_pid_window",
            return_value={"ok": True, "focused": True, "pid": 9876, "hwnd": 123},
        ) as focus_window:
            response = auth_post(client, "/api/account/CaptchaFocusUser/captcha/focus", json={})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["pid"], 9876)
        self.assertTrue(payload["focused"])
        focus_window.assert_called_once_with(9876)
        finish_command.assert_called_once()

    def test_process_launch_blocks_cookie_mismatch_without_public_fallback(self):
        acc = Account(username="OtherUser", cookie="_|WARNING:-cookie", cookie_username="RealUser", cookie_mismatch=True)
        with patch.object(ProcessManager, "build_launch_url") as build_url:
            ok, msg, attempted_vip = ProcessManager.launch(acc)
        self.assertFalse(ok)
        self.assertEqual(attempted_vip, "")
        self.assertIn("RealUser", msg)
        build_url.assert_not_called()

    def test_multi_roblox_auth_ticket_failure_does_not_use_shared_cookie_fallback(self):
        acc = Account(username="UserA", cookie="_|WARNING:-cookie", place_id="123456")
        original = ProcessManager.MULTI_ROBLOX_ENABLED
        ProcessManager.MULTI_ROBLOX_ENABLED = True
        try:
            with patch.object(HybridLauncher, "launch_record", return_value={"ok": False, "msg": "ticket failed"}), patch.object(
                ProcessManager, "build_launch_url"
            ) as build_url:
                ok, msg, attempted_vip = ProcessManager.launch(acc)
        finally:
            ProcessManager.MULTI_ROBLOX_ENABLED = original
        self.assertFalse(ok)
        self.assertEqual(msg, "ticket failed")
        self.assertEqual(attempted_vip, "")
        build_url.assert_not_called()

    def test_multi_roblox_keep_all_open_disables_queue_duration(self):
        maint = object.__new__(SystemMaintenance)
        maint._accounts = [Account("UserA"), Account("UserB")]
        maint._cfg = {"multi_roblox_enabled": True, "rt_rotation_enabled": False, "queue_duration_seconds": 15}
        self.assertEqual(SystemMaintenance._queue_duration_seconds(maint), 0.0)
        maint._cfg = {"multi_roblox_enabled": True, "rt_rotation_enabled": True, "queue_duration_seconds": 15}
        self.assertEqual(SystemMaintenance._queue_duration_seconds(maint), 15.0)

        maint._accounts = [Account("UserA")]
        maint._cfg = {"multi_roblox_enabled": False, "rt_rotation_enabled": True, "queue_duration_seconds": 15}
        self.assertEqual(SystemMaintenance._queue_duration_seconds(maint), 0.0)

        maint._accounts = [Account("UserA"), Account("UserB")]
        maint._cfg = {
            "multi_roblox_enabled": False,
            "rt_rotation_enabled": True,
            "runtime_account_allowlist": ["UserA"],
            "queue_duration_seconds": 15,
        }
        self.assertEqual(SystemMaintenance._queue_duration_seconds(maint), 0.0)

    def test_kill_duplicate_uses_process_manager_signature(self):
        killed = []

        def fake_kill(pid):
            killed.append(pid)
            return True

        with patch.object(HybridLauncher, "duplicate_pids_for_tracker", return_value=[12345]), patch(
            "services.process_service.ProcessManager.kill_pid",
            side_effect=fake_kill,
        ):
            result = HybridLauncher.kill_duplicate_instances("112233")

        self.assertEqual(killed, [12345])
        self.assertEqual(result["killed"], [12345])
        self.assertEqual(result["count"], 1)

    def test_multi_roblox_guard_lifecycle(self):
        class FakeStdout:
            def readline(self):
                return "multi_roblox_guard_ready ROBLOX_singletonMutex:err=0 pid=123\n"

        class FakeProcess:
            def __init__(self):
                self.pid = 123
                self.stdout = FakeStdout()
                self.terminated = False
                self.killed = False
                self._poll = None

            def poll(self):
                return self._poll

            def terminate(self):
                self.terminated = True
                self._poll = 0

            def wait(self, timeout=None):
                self._poll = 0
                return 0

            def kill(self):
                self.killed = True
                self._poll = -9

        fake = FakeProcess()
        release_multi_roblox_guard()
        launched = {}

        def fake_popen(cmd, **kwargs):
            launched["cmd"] = list(cmd)
            return fake

        with patch("roblox_hybrid.subprocess.Popen", side_effect=fake_popen):
            ok, detail = ensure_multi_roblox_guard()
        self.assertTrue(ok)
        self.assertIn("ROBLOX_singletonMutex", detail)
        self.assertNotIn("ROBLOX_singletonEvent", detail)
        self.assertIn("mutex", launched.get("cmd", []))
        status = multi_roblox_guard_status()
        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["pid"], 123)
        record_multi_roblox_guard_failure("unit failure")
        self.assertEqual(multi_roblox_guard_status()["last_failure"], "unit failure")
        release_multi_roblox_guard()
        self.assertTrue(fake.terminated)
        self.assertEqual(multi_roblox_guard_status()["state"], "stopped")

    def test_multi_roblox_guard_rejects_event_only_ready(self):
        class FakeStdout:
            def readline(self):
                return "multi_roblox_guard_ready ROBLOX_singletonEvent:err=0 pid=6380\n"

        class FakeProcess:
            def __init__(self):
                self.pid = 6380
                self.stdout = FakeStdout()
                self.terminated = False
                self._poll = None

            def poll(self):
                return self._poll

            def terminate(self):
                self.terminated = True
                self._poll = 0

            def wait(self, timeout=None):
                self._poll = 0
                return 0

            def kill(self):
                self._poll = -9

        fake = FakeProcess()
        release_multi_roblox_guard()
        try:
            with patch("roblox_hybrid.subprocess.Popen", return_value=fake):
                ok, detail = ensure_multi_roblox_guard()

            self.assertFalse(ok)
            self.assertIn("missing Roblox singleton mutex", detail)
            status = multi_roblox_guard_status()
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["pid"], 0)
            self.assertTrue(fake.terminated)
        finally:
            release_multi_roblox_guard()

    def test_auto_private_server_failure_blocks_public_launch(self):
        class FakeRobloxHTTP:
            def __init__(self, cookie):
                self.cookie = cookie

            def get_auth_ticket(self):
                return True, "ticket-secret"

        started = []
        updates = []
        with patch(
            "roblox_hybrid.validate_record_cookie_identity",
            return_value={"ok": True, "cookie_username": "UserA", "cookie_user_id": "42"},
        ), patch(
            "roblox_hybrid.ensure_multi_roblox_guard",
            return_value=(True, "ready"),
        ), patch.object(
            HybridLauncher,
            "kill_duplicate_instances",
            return_value={"ok": True, "killed": [], "count": 0},
        ), patch(
            "roblox_hybrid.ensure_owned_private_server",
            return_value={
                "ok": False,
                "msg": "Private servers are disabled for this universe",
                "universe_id": "456",
            },
        ), patch(
            "roblox_hybrid.RobloxHTTP",
            FakeRobloxHTTP,
        ), patch(
            "roblox_hybrid.os.startfile",
            side_effect=lambda uri: started.append(uri),
            create=True,
        ), patch(
            "roblox_hybrid.ACCOUNT_STORE.update_record",
            side_effect=lambda username, payload: updates.append((username, payload)),
        ):
            result = HybridLauncher.launch_record(
                {"username": "UserA", "cookie": "_|WARNING:-cookie", "place_id": "123456"},
                target={
                    "place_id": "123456",
                    "auto_create_private_server_enabled": True,
                    "auto_create_private_server_free_only": True,
                },
                multi_roblox=True,
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["fatal"])
        self.assertEqual(result["mode"], "vip")
        self.assertFalse(result["vip_resolved"])
        self.assertTrue(result["auto_private_server"])
        self.assertIn("Private servers are disabled", result["msg"])
        self.assertEqual(started, [])
        self.assertTrue(any(payload.get("owned_private_servers") for _username, payload in updates))

    def test_multi_roblox_guard_not_ready_blocks_launch(self):
        with patch(
            "roblox_hybrid.validate_record_cookie_identity",
            return_value={"ok": True, "cookie_username": "UserA", "cookie_user_id": "1"},
        ), patch("roblox_hybrid.ensure_multi_roblox_guard", return_value=(False, "not ready")):
            result = HybridLauncher.launch_record(
                {"username": "UserA", "cookie": "_|WARNING:-cookie", "place_id": "123456"},
                target={"place_id": "123456"},
                multi_roblox=True,
            )
        self.assertFalse(result["ok"])
        self.assertTrue(result["fatal"])
        self.assertIn("Multi Roblox guard failed", result["msg"])
