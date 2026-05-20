from tests.hybrid_account_fixture import *


class HybridAccountCoreCases:
    def test_dpapi_cookie_roundtrip(self):
        cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--unit-test-cookie"
        encrypted = encrypt_cookie(cookie)
        self.assertTrue(encrypted.startswith("dpapi:v1:"))
        self.assertEqual(decrypt_cookie(encrypted), cookie)

    def test_legacy_roboguard_dpapi_account_file_still_loads(self):
        payload = json.dumps({
            "accounts": [
                {"username": "LegacyUser", "cookie": "_|WARNING:-DO-NOT-SHARE-THIS.--legacy-cookie"}
            ]
        }).encode("utf-8")
        legacy_blob = dpapi_protect(payload, b"RoboGuard Hybrid AccountData v1")

        records = AccountDataStore.decode_account_file_bytes(legacy_blob)

        self.assertEqual(records[0]["username"], "LegacyUser")
        self.assertEqual(AccountDataStore.get_cookie_from_record(records[0]), "_|WARNING:-DO-NOT-SHARE-THIS.--legacy-cookie")

    def test_legacy_roboguard_dpapi_cookie_value_still_loads(self):
        cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--legacy-cookie"
        encrypted_cookie = "dpapi:v1:" + base64.b64encode(
            dpapi_protect(cookie.encode("utf-8"), b"RoboGuard Hybrid AccountData v1")
        ).decode("ascii")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "AccountData.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"accounts": [{"username": "LegacyUser", "encrypted_cookie": encrypted_cookie}]}, handle)
            store = AccountDataStore(path)

            account = store.to_cronus_accounts()[0]

        self.assertEqual(account["username"], "LegacyUser")
        self.assertEqual(account["cookie"], cookie)

    def test_legacy_roboguard_config_filename_migrates_to_cronus_name(self):
        import app_paths

        with tempfile.TemporaryDirectory() as tmp:
            target_data = os.path.join(tmp, "Cronus Launcher", "data")
            legacy_data = os.path.join(tmp, "Argus Launcher", "data")
            os.makedirs(legacy_data, exist_ok=True)
            with open(os.path.join(legacy_data, "roboguard_rt1_config.json"), "w", encoding="utf-8") as handle:
                handle.write('{"auto_rejoin": false}')

            with patch.object(app_paths, "APP_DATA_DIR", target_data), \
                 patch.object(app_paths, "LEGACY_DATA_DIR", legacy_data), \
                 patch.object(app_paths, "LEGACY_APP_DATA_DIR", os.path.dirname(legacy_data)):
                app_paths.migrate_legacy_data_files(("cronus_rt1_config.json",))

            migrated = os.path.join(target_data, "cronus_rt1_config.json")
            self.assertTrue(os.path.exists(migrated))
            with open(migrated, "r", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["auto_rejoin"], False)

    def test_account_data_never_exposes_cookie_by_default(self):
        cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--unit-test-cookie"
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountDataStore(os.path.join(tmp, "AccountData.json"))
            store.write_records([{"username": "UserA", "cookie": cookie, "place_id": "123"}])
            records = store.read_records()
            api_record = store.to_api_record(records[0])
            self.assertTrue(api_record["cookie_present"])
            self.assertNotIn("cookie", api_record)
            self.assertNotIn("encrypted_cookie", api_record)
            self.assertEqual(store.to_cronus_accounts()[0]["cookie"], cookie)

    def test_api_record_redacts_vip_links_and_preserves_on_saveback(self):
        raw_link = "https://www.roblox.com/games/123/?privateServerLinkCode=secret-link"
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountDataStore(os.path.join(tmp, "AccountData.json"))
            store.write_records([{"username": "UserA", "vip_links": [raw_link], "description": "old"}])
            api_record = store.to_api_record(store.read_records()[0])
            self.assertIn("[REDACTED]", api_record["vip_links"][0])
            self.assertNotIn("secret-link", json.dumps(api_record))
            api_record["description"] = "new"
            store.replace_from_cronus_payload([api_record])
            saved = store.read_records()[0]
        self.assertEqual(saved["description"], "new")
        self.assertEqual(saved["vip_links"], [raw_link])

    def test_owned_private_server_metadata_is_redacted_in_api_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountDataStore(os.path.join(tmp, "AccountData.json"))
            store.write_records([
                {
                    "username": "UserA",
                    "owned_private_servers": [
                        {
                            "private_server_id": "vip-1",
                            "owner_user_id": "42",
                            "place_id": "123",
                            "universe_id": "456",
                            "link": "https://www.roblox.com/games/123/?privateServerLinkCode=secret-link",
                            "join_code": "secret-link",
                            "access_code": "secret-access",
                            "status": "ok",
                        }
                    ],
                }
            ])
            api_record = store.to_api_record(store.read_records()[0])
        server = api_record["owned_private_servers"][0]
        self.assertTrue(server["link_present"])
        self.assertTrue(server["join_code_present"])
        self.assertTrue(server["access_code_present"])
        self.assertNotIn("link", server)
        self.assertNotIn("join_code", server)
        self.assertNotIn("access_code", server)
        self.assertNotIn("secret-link", json.dumps(api_record))
        self.assertNotIn("secret-access", json.dumps(api_record))

    def test_browser_tracker_persists_into_cronus_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountDataStore(os.path.join(tmp, "AccountData.json"))
            store.write_records([{"username": "UserA", "browser_tracker_id": "112233"}])
            account = store.to_cronus_accounts()[0]
            self.assertEqual(account["browser_tracker_id"], "112233")

    def test_status_step_maps_disconnect_check_to_simple_disconnected_label(self):
        controller = FarmController.__new__(FarmController)
        controller._net_mon = None
        account = Account("UserA")
        account.recovery_status = "checking_disconnect"
        account.liveness_state = "reconnecting"
        account.last_recovery_at = 123.0
        account.last_state_change_at = 100.0

        step, index, started_at = controller._recovery_step_for_account(account, AccountState.IN_GAME)

        self.assertEqual(step, "Disconnected")
        self.assertEqual(index, 4)
        self.assertEqual(started_at, 123.0)

    def test_status_step_treats_stale_disconnect_check_as_in_game_when_alive(self):
        controller = FarmController.__new__(FarmController)
        controller._net_mon = None
        account = Account("UserA")
        account.recovery_status = "checking_disconnect"
        account.last_recovery_reason = "connection_error"
        account.liveness_state = "alive"
        account.in_game_since = 456.0

        step, index, started_at = controller._recovery_step_for_account(account, AccountState.IN_GAME)

        self.assertEqual(step, "Recovery Complete")
        self.assertEqual(index, 8)
        self.assertEqual(started_at, 456.0)

    def test_status_view_model_does_not_show_stale_disconnect_for_alive_in_game(self):
        from config_store import ConfigManager
        from runtime.runtime_view_model import RuntimeViewModelBuilder

        controller = FarmController(ConfigManager())
        account = Account("UserA")
        account.state = AccountState.IN_GAME
        account.desired_state = AccountState.IN_GAME
        account.pid = 4321
        account.bound_process_identity = "RobloxPlayerBeta.exe|1|C:\\Roblox\\RobloxPlayerBeta.exe"
        account.process_binding_status = "verified"
        account.recovery_status = "checking_disconnect"
        account.last_recovery_reason = "connection_error"
        account.liveness_state = "alive"
        account.in_game_since = 456.0
        controller.set_accounts([account])
        controller._runtime_scheduler = type(
            "FakeScheduler",
            (),
            {"snapshot": lambda _self: {"pending_count": 2, "overdue_count": 0, "last_dispatch_latency_seconds": 0.25}},
        )()

        with patch("runtime.runtime_view_model.ProcessManager.is_bound_game_alive", return_value=True), \
             patch("runtime.runtime_view_model.ProcessManager.get_pid_owner", return_value="UserA"), \
             patch("runtime.runtime_view_model.ProcessManager.is_not_responding", return_value=False):
            status = controller.get_status()

        self.assertIn("runtime_health", status)
        self.assertIn("queue_snapshot", status)
        self.assertIn("scheduler_health", status)
        self.assertEqual(status["scheduler_health"]["pending_count"], 2)
        self.assertEqual(status["runtime_health"]["scheduler"]["pending_count"], 2)
        self.assertIn("accounts", status)
        row = status["accounts"][0]
        self.assertEqual(row["state"], "IN_GAME")
        self.assertEqual(row["state_label"], "In Game")
        self.assertEqual(row["recovery_step"], "Recovery Complete")
        self.assertNotEqual(row["state_label"], "Checking Disconnect")
        controller._runtime_store.close()

    def test_status_view_model_counts_blocked_live_captcha_out_of_online_total(self):
        from config_store import ConfigManager

        controller = FarmController(ConfigManager())
        account = Account("CaptchaLiveUser")
        account.state = AccountState.IN_GAME
        account.desired_state = AccountState.IN_GAME
        account.pid = 4321
        account.bound_process_identity = "RobloxPlayerBeta.exe|1|C:\\Roblox\\RobloxPlayerBeta.exe"
        account.process_binding_status = "verified"
        set_account_captcha_hold(account, "Roblox Security verification visible", source="unit")
        controller.set_accounts([account])

        with patch("runtime.runtime_view_model.ProcessManager.is_bound_game_alive", return_value=True), \
             patch("runtime.runtime_view_model.ProcessManager.get_pid_owner", return_value="CaptchaLiveUser"), \
             patch("runtime.runtime_view_model.ProcessManager.is_not_responding", return_value=False):
            status = controller.get_status()

        self.assertEqual(status["blocked_count"], 1)
        self.assertEqual(status["in_game"], 0)
        self.assertEqual(status["failed"], 1)
        row = status["accounts"][0]
        self.assertEqual(row["state_label"], "Captcha")
        self.assertTrue(row["captcha_required"])
        controller._runtime_store.close()

    def test_status_step_marks_in_game_complete_even_with_old_launch_reason(self):
        controller = FarmController.__new__(FarmController)
        controller._net_mon = None
        account = Account("UserA")
        account.recovery_status = "in_game"
        account.last_state_reason = "launch_sent"
        account.in_game_since = 456.0

        step, index, started_at = controller._recovery_step_for_account(account, AccountState.IN_GAME)

        self.assertEqual(step, "Recovery Complete")
        self.assertEqual(index, 8)
        self.assertEqual(started_at, 456.0)

    def test_fps_limiter_updates_cap_and_sets_readonly(self):
        xml = '<roblox><Item><Properties><int name="FramerateCap">240</int></Properties></Item></roblox>'
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "GlobalBasicSettings_13.xml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(xml)
            try:
                result = apply_fps_limiter_file(True, 144, path)
                self.assertTrue(result["ok"])
                self.assertEqual(read_fps_settings(path)["framerate_cap"], 144)
                self.assertTrue(is_readonly(path))
            finally:
                if os.path.exists(path):
                    set_readonly(path, False)

    def test_fps_limiter_disable_clears_readonly_without_changing_cap(self):
        xml = '<roblox><Item><Properties><int name="FramerateCap">60</int></Properties></Item></roblox>'
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "GlobalBasicSettings_13.xml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(xml)
            set_readonly(path, True)
            try:
                result = apply_fps_limiter_file(False, 240, path)
                self.assertTrue(result["ok"])
                self.assertEqual(read_fps_settings(path)["framerate_cap"], 60)
                self.assertFalse(is_readonly(path))
            finally:
                if os.path.exists(path):
                    set_readonly(path, False)

    def test_fps_limiter_rejects_invalid_and_missing_file(self):
        with self.assertRaises(ValueError):
            normalize_fps_limit(14)
        with self.assertRaises(ValueError):
            normalize_fps_limit(1001)
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "GlobalBasicSettings_13.xml")
            self.assertFalse(read_fps_settings(missing)["exists"])
            with self.assertRaises(FileNotFoundError):
                apply_fps_limiter_file(True, 60, missing)

    def test_graphics_auto_updates_settings(self):
        xml = (
            '<roblox><Item><Properties>'
            '<int name="FramerateCap">240</int>'
            '<token name="GraphicsOptimizationMode">0</token>'
            '<int name="GraphicsQualityLevel">1</int>'
            '<bool name="MaxQualityEnabled">true</bool>'
            '<int name="QualityResetLevel">5</int>'
            '<token name="SavedQualityLevel">10</token>'
            '</Properties></Item></roblox>'
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "GlobalBasicSettings_13.xml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(xml)
            result = apply_performance_settings_file(False, 240, True, path)
            self.assertTrue(result["ok"])
            status = read_fps_settings(path)
            self.assertFalse(status["graphics_auto_active"])
            self.assertTrue(status["graphics_low_active"])
            self.assertEqual(status["graphics_optimization_mode"], "0")
            self.assertEqual(status["graphics_quality_level"], "1")
            self.assertEqual(status["saved_quality_level"], "1")
            self.assertTrue(is_readonly(path))
            set_readonly(path, False)

    def test_graphics_quality_rejects_out_of_range(self):
        self.assertEqual(normalize_graphics_quality("1"), 1)
        with self.assertRaises(ValueError):
            normalize_graphics_quality(0)
        with self.assertRaises(ValueError):
            normalize_graphics_quality(11)

    def test_graphics_route_is_separate_from_fps_limiter(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        payload = {
            "ok": True,
            "path": "settings.xml",
            "read_only": True,
            "graphics_low_active": True,
            "graphics_low_enabled": True,
            "graphics_auto_enabled": True,
            "graphics_quality_level": 1,
            "msg": "ok",
        }
        priority_result = {"ok": True, "priority": "low", "applied": 1, "count": 1, "results": []}
        with patch.object(main, "apply_graphics_settings_file", return_value=dict(payload)) as apply_graphics, patch.object(
            main, "apply_process_priority_to_roblox", return_value=priority_result
        ) as apply_priority, patch.object(
            main.cfg_mgr, "update"
        ) as update, patch.object(main.cfg_mgr, "save") as save:
            response = auth_post(client,
                "/api/performance/graphics",
                json={
                    "graphics_low_enabled": True,
                    "graphics_quality_level": 1,
                    "auto_process_priority_enabled": True,
                    "process_priority": "low",
                },
            )
        self.assertEqual(response.status_code, 200)
        apply_graphics.assert_called_once_with(True, readonly_after=True, quality_level=1)
        apply_priority.assert_called_once_with("low")
        self.assertEqual(update.call_args.args[0], {
            "graphics_low_enabled": True,
            "graphics_auto_enabled": True,
            "graphics_quality_level": 1,
            "auto_process_priority_enabled": True,
            "process_priority": "low",
        })
        save.assert_called_once()

    def test_cpu_limiter_route_saves_and_applies(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        result = {
            "ok": True,
            "enabled": True,
            "mode": "hard",
            "default_limit_percent": 20,
            "apply_all": True,
            "accounts": {},
            "rows": [],
            "applied": 0,
            "fallback": 0,
            "failed": 0,
        }
        with patch.object(main.CPU_LIMITER, "apply", return_value=result) as apply, \
             patch.object(main.cfg_mgr, "update") as update, \
             patch.object(main.cfg_mgr, "save") as save:
            response = auth_post(client,
                "/api/performance/cpu-limiter",
                json={
                    "enabled": True,
                    "mode": "hard",
                    "default_limit_percent": 20,
                    "apply_all": True,
                    "accounts": [],
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["enabled"])
        apply.assert_called_once()
        self.assertEqual(update.call_args.args[0], {
            "cpu_limiter_enabled": True,
            "cpu_limiter_mode": "hard",
            "cpu_limiter_default_percent": 20,
            "cpu_limiter_apply_all": True,
            "cpu_limiter_accounts": {},
        })
        save.assert_called_once()

    def test_cpu_limiter_apply_all_clears_stale_account_overrides(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        result = {
            "ok": True,
            "enabled": True,
            "mode": "hard",
            "default_limit_percent": 10,
            "apply_all": True,
            "accounts": {},
            "rows": [],
            "applied": 0,
            "fallback": 0,
            "failed": 0,
        }
        with patch.object(main.CPU_LIMITER, "apply", return_value=result) as apply, \
             patch.object(main.cfg_mgr, "update") as update, \
             patch.object(main.cfg_mgr, "save"):
            response = auth_post(client,
                "/api/performance/cpu-limiter",
                json={
                    "enabled": True,
                    "mode": "hard",
                    "default_limit_percent": 10,
                    "apply_all": True,
                    "accounts": [{"username": "UserA", "enabled": True, "limit_percent": 20}],
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(update.call_args.args[0]["cpu_limiter_default_percent"], 10)
        self.assertEqual(update.call_args.args[0]["cpu_limiter_accounts"], {})
        self.assertEqual(apply.call_args.args[1]["accounts"], {})

    def test_cpu_limiter_route_rejects_invalid_percent(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        response = auth_post(client, "/api/performance/cpu-limiter", json={"enabled": True, "default_limit_percent": 4})
        self.assertEqual(response.status_code, 400)

    def test_cpu_limiter_status_returns_account_rows(self):
        from fastapi.testclient import TestClient
        import main

        account = Account.from_dict({"username": "UserA"})
        account.pid = 4321
        client = TestClient(main.app)
        with patch.object(main.farm, "_accounts", [account]):
            response = client.get("/api/performance/cpu-limiter")
        self.assertEqual(response.status_code, 200)
        rows = response.json()["rows"]
        self.assertEqual(rows[0]["username"], "UserA")
        self.assertEqual(rows[0]["pid"], 4321)

    def test_priority_mapper_rejects_realtime(self):
        self.assertEqual(normalize_process_priority("below normal"), "below_normal")
        with self.assertRaises(ValueError):
            normalize_process_priority("realtime")
        with patch("performance_settings.psutil", create=True):
            pass

    def test_cpu_limiter_settings_validation(self):
        settings = normalize_cpu_limiter_settings({
            "enabled": True,
            "mode": "hard-cap",
            "default_limit_percent": 20,
            "apply_all": False,
            "accounts": [{"username": "UserA", "enabled": True, "limit_percent": 30}],
        })
        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["mode"], "hard")
        self.assertEqual(settings["default_limit_percent"], 20)
        self.assertEqual(settings["accounts"]["UserA"]["limit_percent"], 30)
        apply_all = normalize_cpu_limiter_settings({
            "enabled": True,
            "default_limit_percent": 10,
            "apply_all": True,
            "accounts": [{"username": "UserA", "enabled": True, "limit_percent": 30}],
        })
        self.assertEqual(apply_all["accounts"], {})
        with self.assertRaises(ValueError):
            normalize_cpu_limiter_settings({"enabled": True, "default_limit_percent": 4})
        with self.assertRaises(ValueError):
            normalize_cpu_limiter_settings({"enabled": True, "mode": "realtime"})

    def test_cpu_limiter_apply_all_row_uses_default_limit_over_override(self):
        account = Account.from_dict({"username": "UserA"})
        limiter = CpuLimiter()
        result = limiter.snapshot([account], {
            "enabled": True,
            "mode": "hard",
            "default_limit_percent": 10,
            "apply_all": True,
            "accounts": {"UserA": {"enabled": True, "limit_percent": 20}},
        })
        row = result["rows"][0]
        self.assertTrue(row["enabled"])
        self.assertEqual(row["limit_percent"], 10)

    def test_cpu_limiter_hard_fallbacks_to_soft(self):
        account = Account.from_dict({"username": "UserA"})
        account.pid = 1234
        limiter = CpuLimiter()
        with patch.object(limiter, "_is_roblox_pid", return_value=True), \
             patch.object(limiter, "_apply_hard", side_effect=RuntimeError("job denied")), \
             patch.object(limiter, "_apply_soft", return_value=(True, "soft ok")):
            result = limiter.apply([account], {
                "enabled": True,
                "mode": "hard",
                "default_limit_percent": 20,
                "apply_all": True,
                "accounts": {},
            })
        self.assertEqual(result["rows"][0]["status"], "Fallback")
        self.assertEqual(result["fallback"], 1)

    def test_cpu_limiter_no_pid_status(self):
        account = Account.from_dict({"username": "UserA"})
        limiter = CpuLimiter()
        result = limiter.apply([account], {
            "enabled": True,
            "mode": "soft",
            "default_limit_percent": 20,
            "apply_all": True,
            "accounts": {},
        })
        self.assertEqual(result["rows"][0]["status"], "No PID")

    def test_cpu_limiter_disabled_account_releases_existing_pid(self):
        account = Account.from_dict({"username": "UserA"})
        account.pid = 1234
        limiter = CpuLimiter()
        with patch.object(limiter, "release_pid", return_value=True) as release:
            result = limiter.apply([account], {
                "enabled": True,
                "mode": "hard",
                "default_limit_percent": 20,
                "apply_all": False,
                "accounts": {},
            })
        release.assert_called_once_with(1234)
        self.assertEqual(result["rows"][0]["status"], "Disabled")

    def test_cpu_limiter_release_all_restores_soft_only_pids(self):
        limiter = CpuLimiter()
        limiter._soft_originals[1234] = {"create_time": 1.0}
        with patch.object(limiter, "_restore_soft", return_value=True) as restore:
            result = limiter.release_all()
        restore.assert_called_once_with(1234)
        self.assertEqual(result["released"], 1)

    def test_cpu_limiter_hard_assign_failure_closes_job_handle(self):
        limiter = CpuLimiter()

        class FakeKernel32:
            def CreateJobObjectW(self, *_):
                return 111

            def OpenProcess(self, *_):
                return 222

            def AssignProcessToJobObject(self, *_):
                return False

        with patch("services.cpu_limiter.sys.platform", "win32"), \
             patch.object(limiter, "_kernel32", return_value=FakeKernel32()), \
             patch.object(limiter, "_process_create_time", return_value=1.0), \
             patch.object(limiter, "_set_job_cpu_rate"), \
             patch.object(limiter, "_verify_job_cpu_rate"), \
             patch.object(limiter, "_close_handle") as close_handle, \
             patch("services.cpu_limiter.ctypes.get_last_error", return_value=5):
            with self.assertRaises(OSError):
                limiter._apply_hard(1234, 20)
        close_handle.assert_any_call(111)
        close_handle.assert_any_call(222)
        self.assertNotIn(1234, limiter._job_handles)

    def test_cpu_limiter_rebinds_when_pid_create_time_changes(self):
        limiter = CpuLimiter()
        limiter._job_handles[1234] = {"handle": 111, "create_time": 1.0}

        class FakeKernel32:
            def CreateJobObjectW(self, *_):
                return 333

            def OpenProcess(self, *_):
                return 444

            def AssignProcessToJobObject(self, *_):
                return True

        with patch("services.cpu_limiter.sys.platform", "win32"), \
             patch.object(limiter, "_kernel32", return_value=FakeKernel32()), \
             patch.object(limiter, "_process_create_time", return_value=2.0), \
             patch.object(limiter, "_set_job_cpu_rate"), \
             patch.object(limiter, "_verify_job_cpu_rate"), \
             patch.object(limiter, "_close_handle") as close_handle:
            limiter._apply_hard(1234, 20)
        close_handle.assert_any_call(111)
        close_handle.assert_any_call(444)
        self.assertEqual(limiter._job_handles[1234]["handle"], 333)
        self.assertEqual(limiter._job_handles[1234]["create_time"], 2.0)

    def test_roblox_install_version_normalization(self):
        self.assertEqual(normalize_roblox_version("abcdef1234567890"), "version-abcdef1234567890")
        self.assertEqual(normalize_roblox_version("version-abcdef1234567890"), "version-abcdef1234567890")
        with self.assertRaises(ValueError):
            normalize_roblox_version("not-a-version")

    def test_roblox_install_fetches_weao_windows_versions(self):
        manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b'{"Windows":"version-abcdef1234567890"}'

        with patch("services.roblox_install_manager.urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            version = manager.fetch_weao_windows_version("current")
        self.assertEqual(version, "version-abcdef1234567890")
        request = urlopen.call_args.args[0]
        self.assertIn("/api/versions/current", request.full_url)
        self.assertEqual(request.headers["User-agent"], "WEAO-3PService")

    def test_roblox_install_latest_falls_back_to_official_when_weao_fails(self):
        manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)
        with patch.object(manager, "fetch_weao_windows_version", side_effect=RuntimeError("rate limit")), \
             patch.object(manager, "fetch_official_latest_version", return_value="version-abcdef1234567890") as official:
            self.assertEqual(manager.fetch_latest_version(), "version-abcdef1234567890")
        official.assert_called_once()

    def test_roblox_install_detects_temp_installed_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            version = "version-abcdef1234567890"
            exe = os.path.join(tmp, "Roblox", "Versions", version, "RobloxPlayerBeta.exe")
            os.makedirs(os.path.dirname(exe), exist_ok=True)
            with open(exe, "wb") as f:
                f.write(b"exe")

            with patch.dict(os.environ, {"LOCALAPPDATA": tmp, "ProgramFiles": "", "ProgramFiles(x86)": ""}, clear=False):
                manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)
                installed = manager.detect_installed()

        self.assertTrue(installed["installed"])
        self.assertEqual(installed["version"], version)
        self.assertTrue(installed["path"].endswith("RobloxPlayerBeta.exe"))

    def test_roblox_install_full_wipe_only_allowed_roblox_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            roblox_root = os.path.join(tmp, "Roblox")
            nested_dir = os.path.join(roblox_root, "Versions", "version-abcdef1234567890")
            settings_file = os.path.join(roblox_root, "GlobalBasicSettings_13.xml")
            nested_file = os.path.join(nested_dir, "RobloxPlayerBeta.exe")
            cronus_file = os.path.join(tmp, "Cronus Launcher", "data", "keep.txt")
            os.makedirs(nested_dir, exist_ok=True)
            os.makedirs(os.path.dirname(cronus_file), exist_ok=True)
            for path in (settings_file, nested_file):
                with open(path, "w", encoding="utf-8") as f:
                    f.write("roblox")
                os.chmod(path, stat.S_IREAD)
            with open(cronus_file, "w", encoding="utf-8") as f:
                f.write("cronus")

            with patch.dict(os.environ, {"LOCALAPPDATA": tmp, "ProgramFiles": "", "ProgramFiles(x86)": ""}, clear=False):
                manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)
                with patch.object(manager, "remove_protocol_registry", return_value={"removed": [], "failed": []}):
                    result = manager.full_wipe()

            self.assertIn(roblox_root, result["removed"])
            self.assertFalse(os.path.exists(roblox_root))
            self.assertTrue(os.path.exists(cronus_file))

    def test_roblox_install_detects_related_process_blockers(self):
        manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)

        class FakeProc:
            info = {
                "pid": 4321,
                "name": "Roblox Account Manager.exe",
                "exe": r"C:\Users\Administrator\Documents\acc\Roblox Account Manager.exe",
                "cmdline": [r"C:\Users\Administrator\Documents\acc\Roblox Account Manager.exe"],
            }

        with patch("psutil.process_iter", return_value=[FakeProc()]):
            status = manager.status()
            result = manager.start_uninstall()

        self.assertTrue(status["running_blocked"])
        self.assertIn("Roblox Account Manager.exe", status["block_msg"])
        self.assertFalse(result["ok"])
        self.assertIn("Roblox Account Manager.exe", result["msg"])

    def test_roblox_install_ignores_stale_protocol_launcher_cmd(self):
        manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)

        class FakeProc:
            info = {
                "pid": 9876,
                "name": "cmd.exe",
                "exe": r"C:\Windows\System32\cmd.exe",
                "cmdline": ["cmd.exe", "/c", "start", "roblox-player:1+launchmode:play+gameinfo:[redacted]"],
            }

        with patch("psutil.process_iter", return_value=[FakeProc()]):
            blockers = manager.find_install_blockers()

        self.assertEqual(blockers, [])

    def test_roblox_install_remove_tree_repairs_permissions_before_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            roblox_root = Path(tmp) / "Roblox"
            roblox_root.mkdir()
            (roblox_root / "locked.txt").write_text("roblox", encoding="utf-8")
            with patch.dict(os.environ, {"LOCALAPPDATA": tmp, "ProgramFiles": "", "ProgramFiles(x86)": ""}, clear=False):
                manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)
                with patch("services.roblox_install_manager.shutil.rmtree", side_effect=[PermissionError("denied"), None]) as rmtree, \
                     patch.object(manager, "_repair_tree_permissions") as repair:
                    manager._remove_roblox_tree(roblox_root)

        self.assertEqual(rmtree.call_count, 2)
        repair.assert_called_once_with(roblox_root)

    def test_roblox_install_remove_helper_rejects_unsafe_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            unsafe_root = os.path.join(tmp, "NotRoblox")
            os.makedirs(unsafe_root, exist_ok=True)
            with patch.dict(os.environ, {"LOCALAPPDATA": tmp, "ProgramFiles": "", "ProgramFiles(x86)": ""}, clear=False):
                manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)
                with self.assertRaises(RuntimeError):
                    manager._remove_roblox_tree(Path(unsafe_root))
            self.assertTrue(os.path.exists(unsafe_root))

    def test_roblox_install_manifest_preserves_package_names(self):
        manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)
        packages = manager._parse_pkg_manifest(
            "RobloxApp.zip\nhash\n1\n2\ncontent-avatar.zip\nhash\n1\n2\nshaders.zip\nhash\n1\n2\n"
        )
        self.assertEqual([p["name"] for p in packages], ["RobloxApp.zip", "content-avatar.zip", "shaders.zip"])

    def test_roblox_install_extracts_packages_to_roblox_layout(self):
        manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)
        manifest = (
            "RobloxApp.zip\nhash\n1\n2\n"
            "content-avatar.zip\nhash\n1\n2\n"
            "content-textures3.zip\nhash\n1\n2\n"
            "extracontent-places.zip\nhash\n1\n2\n"
            "shaders.zip\nhash\n1\n2\n"
            "ssl.zip\nhash\n1\n2\n"
        )

        def fake_download_file(url, path):
            package = Path(path).name
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(f"{package}.txt", "ok")

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "version-abcdef1234567890"
            with patch.object(manager, "_download_text", return_value=manifest), \
                 patch.object(manager, "_download_file", side_effect=fake_download_file):
                manager.install_from_manifest("version-abcdef1234567890", target)

            self.assertTrue((target / "RobloxApp.zip.txt").exists())
            self.assertTrue((target / "content" / "avatar" / "content-avatar.zip.txt").exists())
            self.assertTrue((target / "PlatformContent" / "pc" / "textures" / "content-textures3.zip.txt").exists())
            self.assertTrue((target / "ExtraContent" / "places" / "extracontent-places.zip.txt").exists())
            self.assertTrue((target / "shaders" / "shaders.zip.txt").exists())
            self.assertTrue((target / "ssl" / "ssl.zip.txt").exists())

    def test_roblox_install_safe_extract_skips_root_directory_entry(self):
        manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "pkg.zip"
            target = Path(tmp) / "out"
            target.mkdir()
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("/", "")
                archive.writestr("ok.txt", "ok")
            with zipfile.ZipFile(zip_path) as archive:
                manager._safe_extract_zip(archive, target)
            self.assertEqual((target / "ok.txt").read_text(), "ok")

    def test_roblox_install_validation_rejects_exe_only_install(self):
        manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "version-abcdef1234567890" / "RobloxPlayerBeta.exe"
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"exe")
            with self.assertRaisesRegex(RuntimeError, "Roblox install incomplete"):
                manager.validate_install(exe, require_protocol=False)

    def test_roblox_install_writes_required_app_settings(self):
        manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)
        with tempfile.TemporaryDirectory() as tmp:
            path = manager.write_app_settings(Path(tmp))
            text = path.read_text(encoding="utf-8")
        self.assertIn("<ContentFolder>content</ContentFolder>", text)
        self.assertIn("<BaseUrl>http://www.roblox.com</BaseUrl>", text)

    def test_roblox_install_validation_checks_protocol_registration(self):
        manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "version-abcdef1234567890"
            exe = root / "RobloxPlayerBeta.exe"
            root.mkdir(parents=True)
            exe.write_bytes(b"exe")
            manager.write_app_settings(root)
            for name in ("content", "PlatformContent", "ExtraContent", "shaders", "ssl"):
                (root / name).mkdir()
            with patch.object(manager, "protocol_points_to", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "roblox protocol"):
                    manager.validate_install(exe, require_protocol=True)

    def test_roblox_install_endpoint_blocks_while_guard_running(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        with patch.object(main.ROBLOX_INSTALLER, "guard_running", return_value=True), \
             patch.object(main.ROBLOX_INSTALLER, "roblox_running", return_value=False):
            response = auth_post(client, "/api/troubleshoot/roblox-install/uninstall")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["msg"], "Stop Cronus and close Roblox first")

    def test_roblox_install_downgrade_endpoint_removed(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        response = auth_post(client, "/api/troubleshoot/roblox-install/version", json={"version": ""})

        self.assertEqual(response.status_code, 404)

    def test_console_header_sets_cronus_console_icon(self):
        import inspect
        import desktop_host

        header_source = inspect.getsource(desktop_host._console_header)
        icon_source = inspect.getsource(desktop_host._set_console_window_icon)
        ensure_source = inspect.getsource(desktop_host._ensure_console_icon_file)

        self.assertIn("_set_console_window_icon()", header_source)
        self.assertIn("WM_SETICON", icon_source)
        self.assertIn("SetClassLongPtrW", icon_source)
        self.assertIn("APP_ICON_FILE", ensure_source)
        self.assertIn("cronus_console_icon.ico", ensure_source)

    def test_startup_progress_updates_inline_and_clears_after_window_open(self):
        import inspect
        import desktop_host

        with patch.object(desktop_host, "_console_write_inline") as write_inline:
            desktop_host._console_startup_progress(55, "Starting FastAPI server")

        output = "".join(str(call.args[0]) for call in write_inline.call_args_list)
        self.assertIn("\r", output)
        self.assertIn("55%", output)
        self.assertIn("Starting FastAPI server", output)
        self.assertIn("3/6", output)
        self.assertIn("█", output)
        self.assertIn("░", output)
        self.assertTrue(any(frame in output for frame in desktop_host._STARTUP_SPINNER_FRAMES))
        self.assertIn("[", output)
        self.assertIn("]", output)

        with patch.object(desktop_host, "_console_write_inline") as write_inline, \
             patch.object(desktop_host, "_console_clear_startup_screen") as clear_screen:
            desktop_host._console_startup_progress(100, "Opening desktop window")
            desktop_host._console_finish_startup(clear=True)

        clear_screen.assert_called_once()
        clear_output = "".join(str(call.args[0]) for call in write_inline.call_args_list)
        self.assertIn("\r", clear_output)

        window_source = inspect.getsource(desktop_host._run_desktop_window)
        self.assertLess(window_source.index("window.show()"), window_source.index("_console_finish_after_window_show()"))
        run_source = inspect.getsource(desktop_host.run_desktop)
        self.assertIn("_console_clear_after_window_show(ready)", run_source)
        self.assertIn("_console_finish_startup(clear=ready)", run_source)

    def test_startup_progress_uses_navy_blue_terminal_theme(self):
        import desktop_host

        with patch.object(desktop_host, "_startup_colors_enabled", return_value=True):
            line = desktop_host._startup_progress_line(55, "Starting FastAPI server")

        self.assertIn(desktop_host._COLOR_NAVY_BLUE, line)
        self.assertIn(desktop_host._COLOR_NAVY_TEXT, line)
        self.assertNotIn("139;92;246", line)
        self.assertNotIn("236;72;153", line)

    def test_direct_startup_preflight_reports_requirements_before_progress(self):
        import main

        with tempfile.TemporaryDirectory() as tmp:
            req = Path(tmp) / "requirements.txt"
            req.write_text("fastapi>=0.110\nuvicorn[standard]>=0.29\n", encoding="utf-8")

            versions = {"fastapi": "0.111.0", "uvicorn": "0.30.0"}
            with patch.object(main, "_startup_distribution_version", side_effect=lambda name: versions.get(name)), \
                 patch.object(main, "_startup_import_available", return_value=True), \
                 patch.object(main, "_startup_console_write") as write_line:
                ok = main._run_startup_dependency_checks(str(req), exit_on_failure=False)

        self.assertTrue(ok)
        output = "\n".join(str(call.args[0]) for call in write_line.call_args_list)
        self.assertIn("✓ fastapi v0.111.0", output)
        self.assertIn("✓ uvicorn v0.30.0", output)
        self.assertIn("✓ Runtime models: none required", output)
        self.assertNotIn("Installing", output)

    def test_direct_startup_preflight_blocks_missing_requirements(self):
        import main

        with tempfile.TemporaryDirectory() as tmp:
            req = Path(tmp) / "requirements.txt"
            req.write_text("PySide6>=6.6\n", encoding="utf-8")

            with patch.object(main, "_startup_distribution_version", return_value=None), \
                 patch.object(main, "_startup_import_available", return_value=False), \
                 patch.object(main, "_startup_console_write") as write_line:
                ok = main._run_startup_dependency_checks(str(req), exit_on_failure=False)

        self.assertFalse(ok)
        output = "\n".join(str(call.args[0]) for call in write_line.call_args_list)
        self.assertIn("× PySide6 missing", output)
        self.assertIn("python -m pip install -r requirements.txt", output)
