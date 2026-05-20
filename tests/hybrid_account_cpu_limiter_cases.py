from tests.hybrid_account_fixture import *


class HybridAccountCpuLimiterCases:
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
