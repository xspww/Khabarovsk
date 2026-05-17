import os
import atexit
import hashlib
import json
import re
import shutil
import stat
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

_TEST_USER_ROOT = tempfile.mkdtemp(prefix="argus-test-user-root-")
if "ARGUS_USER_ROOT" not in os.environ:
    os.environ["ARGUS_USER_ROOT"] = _TEST_USER_ROOT
    atexit.register(shutil.rmtree, _TEST_USER_ROOT, ignore_errors=True)
else:
    shutil.rmtree(_TEST_USER_ROOT, ignore_errors=True)

from account_hybrid import AccountDataStore, decrypt_cookie, encrypt_cookie, parse_cookie_line
from core import Account, AccountState, ServerType, account_launch_block_reason
from domain.session_identity import build_launch_intent
from farm import Dispatcher, FarmController, SystemMaintenance
from performance_settings import (
    apply_graphics_settings_file,
    apply_performance_settings_file,
    apply_fps_limiter_file,
    normalize_graphics_quality,
    normalize_process_priority,
    is_readonly,
    normalize_fps_limit,
    priority_to_psutil_value,
    read_fps_settings,
    set_readonly,
)
from services.roblox_install_manager import RobloxInstallManager, normalize_roblox_version
from services.cpu_limiter import CpuLimiter, normalize_cpu_limiter_settings
from services.process_service import ProcessService
from services.captcha_guard import CAPTCHA_BLOCK_REASON, CAPTCHA_REASON, captcha_detail, clear_account_captcha_hold, is_account_captcha_required, set_account_captcha_hold
from runtime.runtime_state_manager import RuntimeStateManager
from process_net import ProcessManager
from roblox_hybrid import (
    HybridLauncher,
    build_owned_private_server_link,
    build_place_launcher_url,
    build_roblox_player_uri,
    ensure_owned_private_server,
    ensure_multi_roblox_guard,
    multi_roblox_guard_status,
    parse_launch_destination_from_cmdline,
    parse_vip_access_code_html,
    parse_vip_link,
    record_multi_roblox_guard_failure,
    release_multi_roblox_guard,
    validate_record_cookie_identity,
)


def auth_headers(extra=None):
    import main

    headers = {"X-Argus-Token": main.INSTANCE_TOKEN}
    if extra:
        headers.update(extra)
    return headers


def auth_post(client, path, **kwargs):
    import main

    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("X-Argus-Token", main.INSTANCE_TOKEN)
    return client.post(path, headers=headers, **kwargs)


class HybridAccountTests(unittest.TestCase):
    def test_dpapi_cookie_roundtrip(self):
        cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--unit-test-cookie"
        encrypted = encrypt_cookie(cookie)
        self.assertTrue(encrypted.startswith("dpapi:v1:"))
        self.assertEqual(decrypt_cookie(encrypted), cookie)

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
            self.assertEqual(store.to_roboguard_accounts()[0]["cookie"], cookie)

    def test_api_record_redacts_vip_links_and_preserves_on_saveback(self):
        raw_link = "https://www.roblox.com/games/123/?privateServerLinkCode=secret-link"
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountDataStore(os.path.join(tmp, "AccountData.json"))
            store.write_records([{"username": "UserA", "vip_links": [raw_link], "description": "old"}])
            api_record = store.to_api_record(store.read_records()[0])
            self.assertIn("[REDACTED]", api_record["vip_links"][0])
            self.assertNotIn("secret-link", json.dumps(api_record))
            api_record["description"] = "new"
            store.replace_from_roboguard_payload([api_record])
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

    def test_browser_tracker_persists_into_roboguard_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountDataStore(os.path.join(tmp, "AccountData.json"))
            store.write_records([{"username": "UserA", "browser_tracker_id": "112233"}])
            account = store.to_roboguard_accounts()[0]
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

        with patch("runtime.runtime_view_model.ProcessManager.is_bound_game_alive", return_value=True), \
             patch("runtime.runtime_view_model.ProcessManager.get_pid_owner", return_value="UserA"), \
             patch("runtime.runtime_view_model.ProcessManager.is_not_responding", return_value=False):
            status = controller.get_status()

        self.assertIn("runtime_health", status)
        self.assertIn("queue_snapshot", status)
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
            argus_file = os.path.join(tmp, "Argus Launcher", "data", "keep.txt")
            os.makedirs(nested_dir, exist_ok=True)
            os.makedirs(os.path.dirname(argus_file), exist_ok=True)
            for path in (settings_file, nested_file):
                with open(path, "w", encoding="utf-8") as f:
                    f.write("roblox")
                os.chmod(path, stat.S_IREAD)
            with open(argus_file, "w", encoding="utf-8") as f:
                f.write("argus")

            with patch.dict(os.environ, {"LOCALAPPDATA": tmp, "ProgramFiles": "", "ProgramFiles(x86)": ""}, clear=False):
                manager = RobloxInstallManager(guard_running=lambda: False, roblox_running=lambda: False)
                with patch.object(manager, "remove_protocol_registry", return_value={"removed": [], "failed": []}):
                    result = manager.full_wipe()

            self.assertIn(roblox_root, result["removed"])
            self.assertFalse(os.path.exists(roblox_root))
            self.assertTrue(os.path.exists(argus_file))

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
        self.assertEqual(payload["msg"], "Stop Cronus and close Roblox first.")

    def test_roblox_install_downgrade_endpoint_removed(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        response = auth_post(client, "/api/troubleshoot/roblox-install/version", json={"version": ""})

        self.assertEqual(response.status_code, 404)

    def test_ui_separates_graphics_and_hides_toggle_controls(self):
        import inspect
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        page = client.get("/").text
        css_response = client.get("/ui/dashboard.css")
        app_response = client.get("/ui/app.js")
        api_response = client.get("/ui/runtime/api.js")
        status_response = client.get("/ui/runtime/status.js")
        account_status_response = client.get("/ui/runtime/accountStatus.js")
        table_response = client.get("/ui/components/accountsTable.js")
        feedback_response = client.get("/ui/components/feedback.js")
        icons_response = client.get("/ui/components/icons.js")
        bindings_response = client.get("/ui/events/bindings.js")
        settings_response = client.get("/ui/panels/settingsPanels.js")
        js_response = client.get("/ui/dashboard.js")
        self.assertEqual(css_response.status_code, 200)
        self.assertEqual(app_response.status_code, 200)
        self.assertEqual(api_response.status_code, 200)
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(account_status_response.status_code, 200)
        self.assertEqual(table_response.status_code, 200)
        self.assertEqual(feedback_response.status_code, 200)
        self.assertEqual(icons_response.status_code, 200)
        self.assertEqual(bindings_response.status_code, 200)
        self.assertEqual(settings_response.status_code, 200)
        self.assertEqual(js_response.status_code, 200)
        css = css_response.text
        js = "\n".join([
            app_response.text,
            api_response.text,
            status_response.text,
            account_status_response.text,
            table_response.text,
            feedback_response.text,
            icons_response.text,
            bindings_response.text,
            settings_response.text,
            js_response.text,
        ])
        html = page + "\n" + css + "\n" + js
        def normalize_css_fragment(value: str) -> str:
            return re.sub(r";}", "}", re.sub(r"\s+", "", value))

        compact_css = normalize_css_fragment(css)
        assert_css_contains = lambda snippet: self.assertIn(normalize_css_fragment(snippet), compact_css)
        self.assertEqual(main.app.title, "Cronus Launcher")
        self.assertIn("<title>Cronus Launcher</title>", html)
        self.assertIn('<link rel="icon" type="image/png" href="/ui/cronus-favicon.png">', page)
        self.assertIn('<meta name="argus-api-token" content="', page)
        self.assertIn(main.INSTANCE_TOKEN, page)
        self.assertIn('<link rel="stylesheet" href="/ui/dashboard.css">', page)
        self.assertIn('<script type="module" src="/ui/app.js"></script>', page)
        self.assertIn("import './dashboard.js';", js)
        self.assertIn("export async function api", js)
        self.assertIn("export function createStatusRuntime", js)
        self.assertIn("export function rowStatusLabel", js)
        self.assertIn("export function renderAccountRows", js)
        self.assertIn("export function createFeedback", js)
        self.assertIn("export function solarIcon", js)
        self.assertIn("export function applySolarStaticIcons", js)
        self.assertIn("export function bindDashboardEvents", js)
        self.assertIn("export function renderSettingsPanel", js)
        self.assertIn("from './runtime/status.js'", js)
        self.assertIn("from './runtime/accountStatus.js'", js)
        self.assertIn("from './components/accountsTable.js'", js)
        self.assertIn("from './components/feedback.js'", js)
        self.assertIn("from './components/icons.js'", js)
        self.assertIn("from './events/bindings.js'", js)
        self.assertIn("from './panels/settingsPanels.js'", js)
        self.assertNotIn("setInterval(manualSnapshot,2500)", js)
        self.assertNotIn("<style>", page)
        self.assertNotIn("<span>Argus Launcher</span>", html)
        self.assertNotIn('<header class="topbar">', html)
        self.assertNotIn('id="stream-state"', html)
        self.assertNotIn('id="sync-btn"', html)
        self.assertNotIn('class="view-head"', html)
        self.assertNotIn('id="view-kicker"', html)
        self.assertNotIn('id="view-title"', html)
        self.assertNotIn('id="view-sub"', html)
        self.assertIn('class="page-head"', html)
        self.assertIn(".page-head", html)
        self.assertIn("panel-body-only", html)
        self.assertIn('class="side-launch"', html)
        self.assertIn('id="nav-accounts-count"', html)
        sidebar_head = html.split('<aside class="sidebar">', 1)[1].split('<nav id="nav"', 1)[0]
        self.assertIn('id="guard-btn"', sidebar_head)
        side_status = html.split('<section class="side-status">', 1)[1].split('</section>', 1)[0]
        for cls in ("metric-online", "metric-queued", "metric-captcha", "status-running"):
            self.assertIn(cls, html)
        self.assertIn('id="h-online"', side_status)
        self.assertIn('id="h-queued"', side_status)
        self.assertIn('id="h-captcha"', side_status)
        self.assertIn('id="h-running-time"', side_status)
        self.assertIn(">CAPTCHA<", side_status)
        self.assertIn("Running: 0s", side_status)
        self.assertNotIn("System Healthy", side_status)
        self.assertNotIn(">State<", side_status)
        self.assertNotIn(">Done<", side_status)
        self.assertNotIn('id="h-ingame"', side_status)
        self.assertNotIn('id="h-running"', side_status)
        self.assertIn('id="close-all-roblox-btn"', html)
        self.assertIn("Close All Roblox", html)
        self.assertIn('id="reload-cookies-btn"', html)
        self.assertIn("Reload Cookies", html)
        self.assertNotIn('class="account-stats"', html)
        self.assertNotIn('id="accounts-stat-online"', html)
        self.assertNotIn('id="accounts-stat-queued"', html)
        self.assertNotIn('id="accounts-stat-ingame"', html)
        self.assertNotIn('id="accounts-stat-attention"', html)
        assert_css_contains("--panel2:#1A1A1A")
        assert_css_contains("--muted:#71717A")
        spacex_css = compact_css
        assert_css_contains("background:#0A0A0A")
        assert_css_contains("background:#111111")
        assert_css_contains(".nav button.active{background:#202020;color:#FFFFFF;box-shadow:inset 2px 0 0 #FFFFFF}")
        assert_css_contains(".side-launch .guard-action{height:32px;border-radius:6px;background:transparent")
        self.assertIn("SOLAR_ICON_SOURCE='SVGRepo Solar Linear Icons'", html)
        self.assertIn("data-solar-icon=", html)
        self.assertIn("applySolarStaticIcons(document);", html)
        self.assertNotIn("solarIcon('exit','guard-icon stop')", html)
        self.assertIn('class="guard-icon stop"', html)
        self.assertIn('x="6.2" y="4.5" width="4.9" height="15" rx="1.8"', html)
        self.assertIn('x="12.9" y="4.5" width="4.9" height="15" rx="1.8"', html)
        self.assertIn('class="guard-icon start"', html)
        self.assertIn('d="M7.5 5.4 18 12 7.5 18.6Z"', html)
        self.assertNotIn('class="guard-icon start-logo"', html)
        self.assertNotIn('src="/ui/cronus-start-icon.png"', sidebar_head)
        self.assertNotIn('class="guard-icon bolt"', html)
        self.assertNotIn('d="M13 2 4 14h7l-1 8 10-13h-7z"', html)
        self.assertNotIn("solarIcon('playCircle','guard-icon bolt')", html)
        self.assertIn("'home','nav-icon'", html)
        self.assertIn("solarIcon('presentationGraph','nav-icon')", html)
        self.assertIn("solarIcon('tuning','nav-icon')", html)
        self.assertIn("'userPlus','btn-icon'", html)
        self.assertIn("'restart','btn-icon'", html)
        self.assertIn("solarIcon('checkSquare','btn-icon')", html)
        self.assertIn("delete:'trash'", html)
        self.assertIn("install:'downloadSquare'", html)
        self.assertIn("M9 4.5H8", html)
        self.assertIn('<circle cx="12" cy="12" r="10"', html)
        assert_css_contains("rgba(31,157,98,.72)")
        assert_css_contains("rgba(224,91,106,.72)")
        assert_css_contains("drop-shadow(0 0 5px rgba(31,157,98,.72))")
        assert_css_contains("drop-shadow(0 0 5px rgba(224,91,106,.72))")
        self.assertNotIn('class="guard-icon play"', html)
        self.assertNotIn('d="M10 8.2v7.6l6-3.8-6-3.8z"', html)
        self.assertNotIn('x="7" y="7" width="10" height="10" rx="1.5"', html)
        self.assertIn('d="M4 11.2 9.8 5.4a3.1 3.1 0 0 1 4.4 0L20 11.2"', html)
        self.assertIn('d="M6.4 10.4v5.7c0 2.35 1.45 3.9 3.95 3.9h3.3c2.5 0 3.95-1.55 3.95-3.9v-5.7"', html)
        self.assertIn('d="M17.6 6.4v4.05"', html)
        self.assertIn('circle cx="12" cy="12.35" r="1.45"', html)
        self.assertIn('d="M10.1 16.35h3.8"', html)
        for stroke_width in ('stroke-width="2.75"', 'stroke-width="2.45"', 'stroke-width="2.65"'):
            self.assertIn(stroke_width, html)
        assert_css_contains(".nav-root,.nav-group-head{font-weight:800!important}")
        assert_css_contains(".nav-root svg,.nav-group-head svg{width:8px;height:8px;flex-basis:8px}")
        assert_css_contains("left:4px")
        assert_css_contains("pointer-events:none")
        assert_css_contains("transform:translateY(-50%)")
        assert_css_contains("#nav .nav-child.active:before{background:#25334d}")
        self.assertNotIn("--branch-curve:url", html)
        assert_css_contains("background:#13264a")
        self.assertNotIn('rect x="4" y="4" width="16" height="16" rx="4"', html)
        self.assertNotIn('d="M4 9.5h16"', html)
        self.assertNotIn('d="M9.5 9.5V20"', html)
        self.assertNotIn('circle cx="7" cy="7" r="2.5"', html)
        self.assertIn('d="M14.7 6.3a4.6 4.6 0 0 0-5.8 5.8L3.8 17.2a2.1 2.1 0 0 0 3 3l5.1-5.1a4.6 4.6 0 0 0 5.8-5.8l-3.2 3.2-3-3 3.2-3.2z"', html)
        self.assertNotIn('d="M20 7h-9"', html)
        for old_theme in ("rgba(34,197,94", "rgba(126,188,255", "#22c55e", "#3b82f6", "#60a5fa", "#16a34a"):
            self.assertNotIn(old_theme, spacex_css)
        self.assertNotIn(".account-stat-card", html)
        assert_css_contains("max-height:calc(100vh - 360px)")
        self.assertIn("function renderTop(){const c=counts()", html)
        self.assertIn("$('h-captcha').textContent=c.captcha", html)
        self.assertIn("function syncToggleLabels()", html)
        self.assertIn("input.checked?'Enabled':'Disabled'", html)
        self.assertIn('.toggle-row input[type="checkbox"]', html)
        self.assertNotIn("accounts-stat-attention", html)
        self.assertIn("/accounts/reload", html)
        self.assertIn("function reloadCookies()", html)
        self.assertIn('id="toast" class="toast"', html)
        self.assertIn("toast-icon", html)
        self.assertIn("toast-close", html)
        self.assertIn("Close notification", html)
        self.assertIn(".toast:before", html)
        assert_css_contains(".status{border:0;border-radius:0;padding:0;background:transparent;color:#E5E5E5;font-weight:500;gap:7px}")
        assert_css_contains(".status.finished,.status.blocked{background:transparent;border-color:transparent;color:#C88989}")
        self.assertIn("/roblox/close-all", html)
        self.assertIn("function confirmCloseAllRoblox()", html)
        self.assertIn("cardIcon('close')", html)
        self.assertIn("Close Roblox only", html)
        self.assertIn("Closes every Roblox window. Cronus stays running.", html)
        self.assertNotIn("Stop Cronus and close Roblox</strong>", html)
        self.assertNotIn("Stops Cronus, then closes every Roblox window.", html)
        self.assertNotIn("STATUS.running=false", html)
        self.assertNotIn("confirm('Close all Roblox", html)
        for removed in ('id="h-finished"', 'id="h-cpu"', 'id="h-ram"', ">Finished<", ">CPU<", ">RAM<"):
            self.assertNotIn(removed, html)
        assert_css_contains(".nav-separator,.nav-badge{display:none!important}")
        self.assertNotIn(".nav-separator,.nav-badge,.side-status{display:none!important}", html)
        self.assertIn("function syncRunningClock()", html)
        self.assertIn("$('nav-accounts-count').textContent=c.all", html)
        self.assertNotIn("Argus Log", html)
        self.assertNotIn('data-view="logs"', html)
        self.assertNotIn('id="view-logs"', html)
        self.assertNotIn('id="logs-clear"', html)
        self.assertNotIn('id="log-summary"', html)
        self.assertNotIn('id="logs-timeline"', html)
        self.assertNotIn('id="logs-inspector"', html)
        self.assertNotIn('id="logs-raw"', html)
        self.assertNotIn("/runtime/events?limit=350", html)
        self.assertNotIn("function clearLogs()", html)
        self.assertNotIn("function confirmClearLogs()", html)
        self.assertNotIn("Clear Argus log", html)
        self.assertIn("function confirmDeleteAccount(user)", html)
        self.assertIn("Delete Account", html)
        self.assertIn("cardIcon('delete')", html)
        self.assertIn('class="danger-name"', html)
        self.assertIn("Removes this account and cookie.", html)
        self.assertNotIn('<span class="blocked-note">${esc(user)}</span>', html)
        self.assertNotIn("confirm('Delete account", html)
        self.assertIn('data-view="troubleshoot"', html)
        self.assertIn('id="view-troubleshoot"', html)
        self.assertIn("Roblox Install", html)
        self.assertIn("Currently installed:", html)
        self.assertIn("/troubleshoot/roblox-install", html)
        self.assertIn("function robloxInstallConfirm(action)", html)
        self.assertIn("cardIcon('install')", html)
        self.assertIn("Stop Cronus and close Roblox first.", html)
        self.assertIn("TROUBLESHOOT.block_msg", html)
        self.assertNotIn("Downgrade", html)
        self.assertNotIn("roblox-version", html)
        self.assertNotIn("previous Windows version", html)
        self.assertNotIn("/troubleshoot/roblox-install/version", html)
        self.assertNotIn("confirm('Uninstall", html)
        self.assertNotIn("confirm('Download latest", html)
        self.assertNotIn("RoboGuard RT", html)
        self.assertIn('data-view="graphics"', html)
        self.assertIn('id="view-graphics"', html)
        self.assertIn('data-view="window-size"', html)
        self.assertIn('id="view-window-size"', html)
        self.assertIn('data-view="cpu-limiter"', html)
        self.assertIn('id="view-cpu-limiter"', html)
        game_section = html.split('id="view-game"', 1)[1].split('id="view-performance"', 1)[0]
        fps_section = html.split('id="view-performance"', 1)[1].split('id="view-graphics"', 1)[0]
        graphics_section = html.split('id="view-graphics"', 1)[1].split('id="view-window-size"', 1)[0]
        window_section = html.split('id="view-window-size"', 1)[1].split('id="view-cpu-limiter"', 1)[0]
        cpu_section = html.split('id="view-cpu-limiter"', 1)[1].split('id="view-queue"', 1)[0]
        queue_section = html.split('id="view-queue"', 1)[1].split('id="view-troubleshoot"', 1)[0]
        for button_id in (
            "game-save",
            "game-reset",
            "fps-save",
            "fps-reset",
            "graphics-save",
            "graphics-reset",
            "window-size-save",
            "window-size-reset",
            "cpu-save",
            "cpu-reset",
            "queue-save",
            "queue-reset",
        ):
            self.assertIn(f'id="{button_id}" hidden', html)
        self.assertIn("save.hidden=!isDirty", html)
        self.assertIn("reset.hidden=!isDirty", html)
        assert_css_contains(".savebar .save-action,.btn.good.settings-dirty,.savebar .save-action.settings-dirty{background:transparent;border-color:rgba(255,255,255,.42);color:#FFFFFF;box-shadow:none}")
        self.assertIn(".savebar .reset-action", html)
        self.assertIn(".savebar .save-action.settings-dirty", html)
        self.assertEqual(html.count('class="btn ghost reset-action"'), 6)
        self.assertEqual(html.count('class="btn good save-action"'), 6)
        self.assertEqual(html.count('class="btn-icon" aria-hidden="true"'), 15)
        self.assertIn('id="close-all-roblox-btn"><svg class="btn-icon"', html)
        self.assertIn('id="reload-cookies-btn"><svg class="btn-icon"', html)
        self.assertIn('id="add-btn"><svg class="btn-icon"', html)
        self.assertIn("['Idle','Queued','Launching','In Game','Disconnected','Rejoining','Cooldown','Failed'].includes(apiLabel)", html)
        self.assertNotIn("Checking Disconnect", html)
        self.assertNotIn("save.disabled=!isDirty", html)
        self.assertNotIn("reset.disabled=!isDirty", html)
        self.assertIn("function resetGameSettings()", html)
        self.assertIn("function resetQueueSettings()", html)
        self.assertIn("function resetPerformanceSettings()", html)
        self.assertIn("function resetGraphicsSettings()", html)
        self.assertIn("function resetWindowSizeSettings()", html)
        self.assertIn("$('game-reset').onclick=a.resetGameSettings", html)
        self.assertIn("$('queue-reset').onclick=a.resetQueueSettings", html)
        self.assertNotIn("$('game-reset').onclick=async()=>{clearDirty('game');await loadConfig()}", html)
        self.assertNotIn("$('queue-reset').onclick=async()=>{clearDirty('queue');await loadConfig()}", html)
        self.assertNotIn("Auto Minimize", game_section)
        self.assertNotIn("game-autominimize", html)
        self.assertNotIn("Game Settings", game_section)
        self.assertNotIn("Global defaults for accounts without a target.", game_section)
        self.assertIn('placeholder="Search..."', html)
        self.assertIn("Enter Place ID.", game_section)
        self.assertIn("Place to join.", game_section)
        self.assertIn("Default VIP link.", game_section)
        self.assertIn("Free VIP before launch.", game_section)
        self.assertNotIn("Close Roblox on timer.", game_section)
        self.assertNotIn("Auto Close", game_section)
        self.assertIn("Close Roblox on timer.", queue_section)
        self.assertIn('id="queue-autoclose-enabled"', queue_section)
        self.assertIn('id="queue-autoclose-minutes"', queue_section)
        self.assertNotIn("game-autoclose", html)
        self.assertIn("auto_close_enabled:$('queue-autoclose-enabled').checked", html)
        self.assertIn("Keep other accounts open.", game_section)
        for old_copy in (
            "Search accounts...",
            "Enter a Place ID and click Lookup.",
            "Roblox Place ID to join.",
            "Used as the default VIP server for new or blank-target accounts.",
            "Free only / before launch. New VIP servers are named with the Roblox game name.",
            "Close Roblox clients on a timer, then let Argus Launcher start the queue again.",
            "Keep other account instances alive; Argus Launcher still closes the duplicate instance for the same account tracker.",
        ):
            self.assertNotIn(old_copy, html)
        self.assertNotIn("Graphics Auto", fps_section)
        self.assertNotIn("Auto Process Priority", fps_section)
        self.assertNotIn("Priority Applied", fps_section)
        self.assertNotIn("Cap Roblox FPS to reduce CPU and GPU usage.", fps_section)
        self.assertIn("Limit Roblox FPS.", fps_section)
        self.assertIn("15-1000 FPS.", fps_section)
        self.assertNotIn("Limit the framerate of all Roblox instances.", fps_section)
        self.assertNotIn("Frames per second per instance. Allowed range: 15-1000.", fps_section)
        self.assertIn('id="fps-reset"', fps_section)
        self.assertIn("Save Changes", fps_section)
        self.assertIn("Low Graphics Quality", graphics_section)
        self.assertIn("Auto Process Priority", graphics_section)
        self.assertNotIn("Priority Applied", graphics_section)
        self.assertNotIn("Force Roblox into manual low-quality graphics.", graphics_section)
        self.assertIn("Use low Roblox graphics.", graphics_section)
        self.assertIn("1 low, 10 high.", graphics_section)
        self.assertIn("Set Roblox process priority.", graphics_section)
        self.assertNotIn("Set Roblox to manual low graphics and keep the settings file locked while enabled.", graphics_section)
        self.assertNotIn("1 is lowest, 10 is highest. Use 1 for potato mode.", graphics_section)
        self.assertNotIn("Apply Windows process priority to live Roblox clients and keep reapplying during RT maintenance.", graphics_section)
        self.assertIn('id="graphics-reset"', graphics_section)
        self.assertIn("Enable Window Size", window_section)
        self.assertIn('id="window-size-controls" hidden', window_section)
        self.assertIn('id="window-size-custom" class="control-line" hidden', window_section)
        self.assertNotIn("Resize Roblox windows smaller to reduce render load.", window_section)
        self.assertIn("Resize Roblox windows.", window_section)
        self.assertIn("Choose window size.", window_section)
        self.assertIn("Auto Arrange", window_section)
        self.assertIn("Windows per row", window_section)
        self.assertIn('id="window-arrange-controls" hidden', window_section)
        self.assertNotIn("Minimize Roblox on timer.", window_section)
        self.assertNotIn("Resize live Roblox windows and keep new RT launches at the selected size.", window_section)
        self.assertNotIn("Choose a preset from very small up to 1920 x 1080.", window_section)
        self.assertNotIn("Minimize Roblox windows on a loop.", window_section)
        self.assertIn('id="window-size-reset"', window_section)
        self.assertNotIn("Auto Minimize", window_section)
        self.assertNotIn('id="window-autominimize-enabled"', window_section)
        self.assertNotIn('id="window-autominimize-controls" class="control-line" hidden', window_section)
        self.assertIn("320 x 240", window_section)
        self.assertIn("1920 x 1080", window_section)
        self.assertIn("function saveWindowSize()", html)
        self.assertIn("/performance/window-size", html)
        self.assertIn("Enable CPU Limiter", cpu_section)
        self.assertIn("Default CPU Limit", cpu_section)
        self.assertIn("Apply to all accounts", cpu_section)
        self.assertIn("Roblox PID", cpu_section)
        self.assertIn("Limit %", cpu_section)
        self.assertIn('id="cpu-controls" hidden', cpu_section)
        self.assertIn("function saveCpuLimiter()", html)
        self.assertIn("/performance/cpu-limiter", html)
        self.assertIn("'cpu-limiter':'cpu-save'", html)
        assert_css_contains("grid-template-columns:minmax(180px,250px) minmax(220px,320px)")
        assert_css_contains("#window-size-controls,#window-arrange-controls,#graphics-quality-controls,#priority-controls,#cpu-controls{display:grid;gap:15px}")
        for label in ("Settings File", "Current File Cap", "Read-only", "Roblox State"):
            self.assertNotIn(label, fps_section)
            self.assertNotIn(label, graphics_section)
        self.assertNotIn("AccountData Import", html)
        self.assertIn("choice-icon", html)
        self.assertIn("function openAdd(){openCookie()}", html)
        self.assertNotIn("function openManualMode()", html)
        self.assertNotIn('id="manual-single"', html)
        self.assertNotIn('id="manual-multi"', html)
        self.assertNotIn("function openManualSingle()", html)
        self.assertNotIn("function openManualMulti()", html)
        self.assertNotIn("Manual Login", html)
        self.assertNotIn("Login in browser.", html)
        self.assertIn("Import Cookies", html)
        self.assertIn("One cookie per line.", html)
        self.assertNotIn("One login.", html)
        self.assertNotIn("One account per line.", html)
        self.assertNotIn("manual-login", html)
        self.assertNotIn("kind:'userpass'", html)
        self.assertIn("Save changes first.", html)
        for old_copy in (
            "Sign in via a popup browser. 2FA, captcha, and passkey stay with you.",
            "Paste user:pass:cookie or cookie-only lines.",
            "One account. Enter optional credentials, sign in once.",
            "Paste user:pass per line. Popups open one after another.",
            "Save Changes before running or launching",
            "This removes the account record and its stored cookie",
        ):
            self.assertNotIn(old_copy, html)
        self.assertIn('class="avatar-img"', html)
        self.assertNotIn('id="window-autominimize-enabled"', html)
        self.assertIn('id="game-auto-private-enabled"', html)
        self.assertNotIn("Name Template", html)
        self.assertNotIn('id="auto-private-controls"', html)
        self.assertNotIn('id="game-private-name-template"', html)
        self.assertIn("auto_create_private_server_enabled", html)
        self.assertNotIn('id="window-autominimize-controls" class="control-line" hidden', html)
        self.assertIn('class="compact-toggle-row"', html)
        self.assertIn('id="fps-inline-label">Disabled</span>', html)
        self.assertIn('id="fps-limit-field" class="compact-inline-controls" hidden', html)
        self.assertIn('id="priority-controls" hidden', html)
        self.assertIn('id="graphics-quality-label">Disabled</span>', html)
        self.assertIn('id="graphics-quality-controls" class="compact-inline-controls" hidden', html)
        self.assertIn('id="autoclose-inline-label">Disabled</span>', html)
        self.assertIn('id="autoclose-controls" class="compact-inline-controls" hidden', html)
        self.assertIn("setCompactToggle('queue-autoclose-enabled','autoclose-inline-label','autoclose-controls','Every')", html)
        self.assertIn("setCompactToggle('fps-enabled','fps-inline-label','fps-limit-field','Limit')", html)
        self.assertIn("setCompactToggle('graphics-auto-enabled','graphics-quality-label','graphics-quality-controls','Level')", html)
        self.assertNotIn('id="presence-controls"', html)
        self.assertNotIn('id="presence-interval"', html)
        self.assertNotIn('id="presence-ttl"', html)
        self.assertNotIn('id="presence-assist"', html)
        self.assertNotIn('id="presence-last-poll"', html)
        self.assertNotIn('id="presence-cached"', html)
        self.assertNotIn('id="presence-backoff"', html)
        self.assertNotIn('id="presence-status"', html)
        for new_copy in (
            "Max active accounts.",
            "Used only for rotation.",
            "Delay between launches.",
            "Detect Roblox disconnect popup.",
        ):
            self.assertIn(new_copy, queue_section)
        for old_copy in (
            "Maximum accounts allowed in queued, launching, verifying, or in-game slots.",
            "Ignored while Multi Roblox keep-all-open is enabled; rotation must be enabled separately.",
            "Wait time between launches and between cycling an account back into the queue.",
            "Use Roblox presence as extra evidence before rejoin decisions.",
            "Seconds between Roblox presence checks.",
            "Seconds to reuse presence results.",
        ):
            self.assertNotIn(old_copy, queue_section)
        self.assertIn(".main::-webkit-scrollbar", html)
        self.assertIn(".table-wrap::-webkit-scrollbar", html)
        title_source = inspect.getsource(main._run_desktop_window)
        self.assertIn("MacCloseButton", title_source)
        self.assertIn("MacMinButton", title_source)
        self.assertIn("MacMaxButton", title_source)
        self.assertNotIn("TitleButton", title_source)

    def test_lua_rejoin_helper_is_served_with_token_and_local_endpoint(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        response = client.get("/api/lua/rejoin-helper?account=LuaUnit&port=7777&shutdown_delay=2.5")

        self.assertEqual(response.status_code, 200)
        script = response.text
        self.assertIn('Host = "127.0.0.1"', script)
        self.assertIn("Port = 7777", script)
        self.assertIn(f'Token = "{main.INSTANCE_TOKEN}"', script)
        self.assertIn('Account = "LuaUnit"', script)
        self.assertIn('account = safeString(LocalPlayer.Name)', script)
        self.assertIn('configured_account = safeString(self.Account)', script)
        self.assertIn("pid = getProcessId()", script)
        self.assertIn("ShutdownDelay = 2.50", script)
        self.assertIn('Version = "1.7.0"', script)
        self.assertIn('token = safeString(self.Token)', script)
        self.assertIn('argus_token = safeString(self.Token)', script)
        self.assertIn('api_token = safeString(self.Token)', script)
        self.assertIn('_argus_token = safeString(self.Token)', script)
        self.assertIn('function ArgusRejoin:EndpointWithToken', script)
        self.assertIn('function ArgusRejoin:QueryEndpoint', script)
        self.assertIn('function ArgusRejoin:GetFallback', script)
        self.assertIn('["User-Agent"] = "ArgusLuaRejoin/1.7"', script)
        self.assertIn('Headers = requestHeaders', script)
        self.assertIn('headers = requestHeaders', script)
        self.assertIn('body = body', script)
        self.assertIn('Data = body', script)
        self.assertIn('return self:GetFallback(eventName, payload, status)', script)
        self.assertIn('game:HttpGet(url)', script)
        self.assertIn('["X-RoboGuard-Token"] = self.Token', script)
        self.assertIn("/api/lua/rejoin-event", script)
        self.assertIn('"http://" .. host .. ":" .. port .. "/api/lua/rejoin-event"', script)
        self.assertNotIn('("http://%s:%s/api/lua/rejoin-event"):format', script)
        self.assertIn("GuiService.ErrorMessageChanged", script)
        self.assertIn("function ArgusRejoin:PostAsync", script)
        self.assertIn("function ArgusRejoin:ClientRecoveryFallback", script)
        self.assertIn('log("post begin"', script)
        self.assertIn('log("post async"', script)
        self.assertIn('log("post task error"', script)
        self.assertIn('log("json encode failed"', script)
        self.assertIn('log("client fallback start"', script)
        self.assertIn("TeleportService:Teleport(game.PlaceId, LocalPlayer)", script)
        self.assertIn('LocalPlayer:Kick("Argus recovery fallback")', script)
        self.assertIn('reportDisconnect("poll")', script)
        self.assertIn("task.wait(0.5)", script)
        self.assertIn("shutdown fallback after disconnect", script)
        self.assertIn("TeleportService.TeleportInitFailed", script)
        self.assertNotIn("TeleportToPlaceInstance", script)
        self.assertIn('G.ArgusRejoin = ArgusRejoin', script)
        self.assertNotIn("__ARGUS_", script)
        loader = (Path(__file__).resolve().parents[1] / "lua" / "run_in_executor.lua").read_text(encoding="utf-8")
        self.assertIn("/api/lua/rejoin-helper", loader)
        self.assertIn("local Load = loadstring or load", loader)
        self.assertIn("Load(source)", loader)

    def test_lua_account_module_is_served_with_safe_api_contract(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        response = client.get("/api/lua/account-module?account=LuaUnit&port=7777")

        self.assertEqual(response.status_code, 200)
        script = response.text
        self.assertIn('Host = "127.0.0.1"', script)
        self.assertIn("Port = 7777", script)
        self.assertIn(f'Token = "{main.INSTANCE_TOKEN}"', script)
        self.assertIn('Account = "LuaUnit"', script)
        self.assertIn('Version = "account-1.0.0"', script)
        self.assertIn("function Account.new", script)
        self.assertIn("function Account.SetKey", script)
        self.assertIn("function Account:Send", script)
        self.assertIn("function Account:SetDescription", script)
        self.assertIn("function Account:MarkFinished", script)
        self.assertIn("/api/lua/rejoin-event", script)
        self.assertIn('["X-Argus-Token"] = self.Token', script)
        self.assertIn('["X-RoboGuard-Token"] = self.Token', script)
        self.assertIn('return self:Send("finished"', script)
        self.assertIn('client:Loaded("ArgusAccount module loaded")', script)
        self.assertNotIn("__ARGUS_", script)
        self.assertNotIn("GetCookie", script)
        self.assertNotIn("GetCSRFToken", script)
        self.assertNotIn("Password", script)
        loader = (Path(__file__).resolve().parents[1] / "lua" / "internal" / "load_account_status.lua").read_text(encoding="utf-8")
        self.assertIn("/api/lua/account-module", loader)
        self.assertIn("local Load = loadstring or load", loader)
        self.assertIn("Load(source)", loader)

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

    def test_lua_rejoin_event_accepts_argus_token_alias(self):
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
                    "argus_token": main.INSTANCE_TOKEN,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["accepted"], True)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("argus_token", calls[0])

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
                f"/api/lua/rejoin-event?argus_token={main.INSTANCE_TOKEN}",
                json={"event": "heartbeat", "account": "LuaUnit", "username": "LuaUnit"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["accepted"], True)
        self.assertEqual(len(calls), 1)

    def test_lua_rejoin_event_accepts_local_get_fallback_when_executor_token_is_mangled(self):
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
                "argus_token=bad-token&event=disconnect&account=LuaUnit&username=LuaUnit&"
                "helper_version=1.7.0&error_code=273&reason_key=lua_disconnect_error"
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["event"], "disconnect")
        self.assertNotIn("argus_token", calls[0])

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
            path = os.path.join(tmp, "argus.log")
            with open(path, "w", encoding="utf-8") as f:
                f.write("line one\nline two\n")
            with patch.object(main, "LOG_FILE", path):
                client = TestClient(main.app)
                response = auth_post(client, "/api/logs/clear")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["lines"], [])
                with open(path, encoding="utf-8") as f:
                    self.assertEqual(f.read(), "")
                self.assertEqual(client.get("/api/logs").json()["lines"], [])

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
            "to_roboguard_accounts",
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
            "to_roboguard_accounts",
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
            "to_roboguard_accounts",
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
            "to_roboguard_accounts",
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

    def test_mutating_api_audit_logs_idempotency_key(self):
        from fastapi.testclient import TestClient
        import api_routes.auth as auth_routes
        import main

        client = TestClient(main.app)
        with patch.object(auth_routes, "flog_kv") as log:
            response = auth_post(
                client,
                "/api/config",
                headers={"X-Argus-Idempotency-Key": "audit-unit-key"},
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
        headers = auth_headers({"X-Argus-Idempotency-Key": "close-all-idem-unit"})
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
        headers = auth_headers({"X-Argus-Idempotency-Key": "slice3-import-idem"})
        with patch("api_routes.accounts_routes.ACCOUNT_STORE.import_cookie_lines", return_value={"ok": True, "imported": 1, "count": 1}) as importer, \
             patch("api_routes.accounts_routes.ACCOUNT_STORE.to_roboguard_accounts", return_value=[]), \
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
        headers = auth_headers({"X-Argus-Idempotency-Key": "slice3-reload-idem"})
        with patch("api_routes.accounts_routes.ACCOUNT_STORE.read_records", return_value=[]) as read_records, \
             patch("api_routes.accounts_routes.ACCOUNT_STORE.write_records") as write_records, \
             patch("api_routes.accounts_routes.ACCOUNT_STORE.to_roboguard_accounts", return_value=[]), \
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
        headers = auth_headers({"X-Argus-Idempotency-Key": "slice3-launch-idem"})
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
        headers = auth_headers({"X-Argus-Idempotency-Key": "slice3-logs-idem"})
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
            block_headers = auth_headers({"X-Argus-Idempotency-Key": "slice3-net-block-idem"})
            body = {"pid": 1234, "account_id": "NetUnit", "duration_seconds": 30}
            first = client.post("/api/test/network-fault/block-roblox", headers=block_headers, json=body)
            second = client.post("/api/test/network-fault/block-roblox", headers=block_headers, json=body)
            restore_headers = auth_headers({"X-Argus-Idempotency-Key": "slice3-net-restore-idem"})
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
                headers=auth_headers({"X-Argus-Idempotency-Key": "slice3-audit-idem"}),
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

    def test_single_instance_detection_is_scoped_to_current_folder(self):
        import main

        self.assertTrue(main._cmdline_targets_this_app(["python.exe", "main.py"], main.BASE_DIR))
        self.assertFalse(main._cmdline_targets_this_app(["python.exe", "main.py"], tempfile.gettempdir()))

    def test_auto_close_uses_minutes_not_seconds(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {"auto_close_enabled": True, "auto_close_minutes": 2}
        maint._last_auto_close_at = time.time() - 90
        maint._accounts = []
        maint._state_mgr = None
        maint._recovery = None
        maint._workers = {}
        with patch.object(ProcessService, "kill_all_roblox_clients") as kill_all:
            SystemMaintenance._enforce_auto_close(maint)
        kill_all.assert_not_called()

        maint._last_auto_close_at = time.time() - 121
        with patch.object(ProcessService, "kill_all_roblox_clients", return_value=0) as kill_all:
            SystemMaintenance._enforce_auto_close(maint)
        kill_all.assert_called_once()

    def test_auto_minimize_runtime_path_removed(self):
        self.assertFalse(hasattr(SystemMaintenance, "_enforce_auto_minimize"))

    def test_disabled_popup_setting_avoids_popup_inspection(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {
            "watchdog_enabled": True,
            "popup_disconnected_enabled": False,
            "watchdog_hold_time": 60,
            "watchdog_activity_timeout": 180,
            "watchdog_loading_grace": 90,
            "watchdog_cpu_low": 0.9,
        }
        maint._accounts = []
        maint._workers = {}

        class Net:
            def is_online(self):
                return True

        class Recovery:
            _net = Net()

        maint._recovery = Recovery()
        acc = Account(username="popup_scan_user")
        acc.state = AccountState.IN_GAME
        acc.pid = 1234
        acc.in_game_since = time.time() - 120
        acc.last_activity_at = time.time()
        acc.liveness_state = "alive"
        maint._accounts = [acc]

        liveness = {
            "state": "alive",
            "score": 8.0,
            "validation": {"cpu": 3.0, "ram_mb": 300.0, "windows": 1},
            "reason_key": "",
            "dialog": {},
        }
        with patch.object(ProcessManager, "assess_liveness", return_value=liveness) as assess:
            SystemMaintenance._scan_liveness_watchdog(maint)

        self.assertFalse(assess.call_args.kwargs["inspect_ui"])

    def test_alive_process_periodically_scans_popup_dialog(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {
            "watchdog_enabled": True,
            "popup_disconnected_enabled": True,
            "popup_scan_interval_seconds": 1,
            "watchdog_hold_time": 60,
            "watchdog_activity_timeout": 180,
            "watchdog_loading_grace": 90,
            "watchdog_cpu_low": 0.9,
        }
        maint._accounts = []
        maint._workers = {}
        maint._last_popup_scan_at = {}

        class Net:
            def is_online(self):
                return True

        class Recovery:
            _net = Net()

        maint._recovery = Recovery()
        acc = Account(username="periodic_popup_user")
        acc.state = AccountState.IN_GAME
        acc.pid = 1234
        acc.in_game_since = time.time() - 120
        acc.last_activity_at = time.time()
        acc.liveness_state = "alive"
        maint._accounts = [acc]

        liveness = {
            "state": "alive",
            "score": 8.0,
            "validation": {"cpu": 3.0, "ram_mb": 300.0, "windows": 1},
            "reason_key": "",
            "dialog": {},
        }
        with patch.object(ProcessManager, "assess_liveness", return_value=liveness) as assess:
            SystemMaintenance._scan_liveness_watchdog(maint)

        self.assertTrue(assess.call_args.kwargs["inspect_ui"])
        self.assertIn(acc._config_username, maint._last_popup_scan_at)

    def test_recovery_active_ingame_account_still_scans_captcha_popup(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {
            "watchdog_enabled": True,
            "popup_disconnected_enabled": True,
            "popup_scan_interval_seconds": 1,
            "watchdog_hold_time": 60,
            "watchdog_activity_timeout": 180,
            "watchdog_loading_grace": 90,
            "watchdog_cpu_low": 0.9,
        }
        maint._accounts = []
        maint._workers = {}
        maint._last_popup_scan_at = {}

        class Net:
            def is_online(self):
                return True

        class Recovery:
            _net = Net()

        class State:
            def __init__(self):
                self.runtime = RuntimeStateManager(logger=lambda *_args, **_kwargs: None)
                self.bound_status = []

            def set_recovery(self, account, status="", reason="", inflight=None):
                self.runtime.set_recovery(account, status=status, reason=reason, inflight=inflight)

            def set_cooldown(self, account, until_ts, reason=""):
                self.runtime.set_cooldown(account, until_ts, reason=reason)

            def set_binding_status(self, account, status, reason=""):
                self.bound_status.append((account.username, status, reason))

        maint._recovery = Recovery()
        maint._state_mgr = State()
        acc = Account(username="recovery_active_captcha_user")
        acc.state = AccountState.IN_GAME
        acc.pid = 4321
        acc.in_game_since = time.time() - 120
        acc.last_activity_at = time.time()
        acc.liveness_state = "alive"
        acc.recovery_inflight = True
        acc.recovery_status = "due"
        maint._accounts = [acc]

        liveness = {
            "state": "captcha",
            "score": 8.0,
            "validation": {"cpu": 3.0, "ram_mb": 300.0, "windows": 1},
            "reason_key": CAPTCHA_REASON,
            "dialog": {
                "matched": True,
                "action": "hold",
                "reason_key": CAPTCHA_REASON,
                "detail": "Roblox | R | recovery_active_captcha_user: 13+ | Security",
                "popup_confidence": 1.5,
                "evidence_source": "text",
            },
        }
        with patch.object(ProcessManager, "assess_liveness", return_value=liveness) as assess:
            SystemMaintenance._scan_liveness_watchdog(maint)

        self.assertTrue(assess.call_args.kwargs["inspect_ui"])
        self.assertTrue(is_account_captcha_required(acc))
        self.assertEqual(acc.recovery_status, CAPTCHA_REASON)
        self.assertIn(acc._config_username, maint._last_popup_scan_at)

    def test_popup_scan_max_parallel_limits_periodic_window_scans(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {
            "watchdog_enabled": True,
            "popup_disconnected_enabled": True,
            "popup_scan_interval_seconds": 5,
            "popup_scan_max_parallel": 1,
            "watchdog_hold_time": 60,
            "watchdog_activity_timeout": 180,
            "watchdog_loading_grace": 90,
            "watchdog_cpu_low": 0.9,
        }
        maint._accounts = []
        maint._workers = {}
        maint._last_popup_scan_at = {}

        class Net:
            def is_online(self):
                return True

        class Recovery:
            _net = Net()

        maint._recovery = Recovery()
        accounts = []
        for index in range(3):
            acc = Account(username=f"popup_budget_user_{index}")
            acc.state = AccountState.IN_GAME
            acc.pid = 2000 + index
            acc.in_game_since = time.time() - 120
            acc.last_activity_at = time.time()
            acc.liveness_state = "alive"
            accounts.append(acc)
        maint._accounts = accounts

        inspect_flags = []

        def _liveness(*args, **kwargs):
            inspect_flags.append(bool(kwargs.get("inspect_ui")))
            return {
                "state": "alive",
                "score": 8.0,
                "validation": {"cpu": 3.0, "ram_mb": 300.0, "windows": 1},
                "reason_key": "",
                "dialog": {},
            }

        with patch.object(ProcessManager, "assess_liveness", side_effect=_liveness):
            SystemMaintenance._scan_liveness_watchdog(maint)

        self.assertEqual(inspect_flags, [True, False, False])
        self.assertEqual(len(maint._last_popup_scan_at), 1)

    def test_popup_scan_queue_advances_by_account_order_after_interval(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {
            "watchdog_enabled": True,
            "popup_disconnected_enabled": True,
            "popup_scan_interval_seconds": 30,
            "popup_scan_max_parallel": 2,
            "watchdog_hold_time": 60,
            "watchdog_activity_timeout": 180,
            "watchdog_loading_grace": 90,
            "watchdog_cpu_low": 0.9,
        }
        maint._accounts = []
        maint._workers = {}
        maint._last_popup_scan_at = {}
        maint._last_popup_batch_at = 0.0
        maint._popup_scan_cursor = 0

        class Net:
            def is_online(self):
                return True

        class Recovery:
            _net = Net()

        maint._recovery = Recovery()
        accounts = []
        for index in range(4):
            acc = Account(username=f"popup_queue_user_{index}")
            acc.state = AccountState.IN_GAME
            acc.pid = 3000 + index
            acc.in_game_since = time.time() - 120
            acc.last_activity_at = time.time()
            acc.liveness_state = "alive"
            accounts.append(acc)
        maint._accounts = accounts

        def _liveness(*args, **kwargs):
            return {
                "state": "alive",
                "score": 8.0,
                "validation": {"cpu": 3.0, "ram_mb": 300.0, "windows": 1},
                "reason_key": "",
                "dialog": {},
            }

        inspect_flags = []

        def _record_liveness(*args, **kwargs):
            inspect_flags.append(bool(kwargs.get("inspect_ui")))
            return _liveness(*args, **kwargs)

        with patch.object(ProcessManager, "assess_liveness", side_effect=_record_liveness):
            SystemMaintenance._scan_liveness_watchdog(maint)

        self.assertEqual(inspect_flags, [True, True, False, False])
        self.assertEqual(maint._popup_scan_cursor, 2)
        self.assertEqual(set(maint._last_popup_scan_at), {"popup_queue_user_0", "popup_queue_user_1"})

        inspect_flags.clear()
        with patch.object(ProcessManager, "assess_liveness", side_effect=_record_liveness):
            SystemMaintenance._scan_liveness_watchdog(maint)

        self.assertEqual(inspect_flags, [False, False, False, False])
        self.assertEqual(maint._popup_scan_cursor, 2)

        maint._last_popup_batch_at = time.time() - 31
        inspect_flags.clear()
        with patch.object(ProcessManager, "assess_liveness", side_effect=_record_liveness):
            SystemMaintenance._scan_liveness_watchdog(maint)

        self.assertEqual(inspect_flags, [False, False, True, True])
        self.assertEqual(maint._popup_scan_cursor, 0)
        self.assertEqual(
            set(maint._last_popup_scan_at),
            {"popup_queue_user_0", "popup_queue_user_1", "popup_queue_user_2", "popup_queue_user_3"},
        )

    def test_popup_dialog_rejoin_signal_overrides_alive_process(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {
            "watchdog_enabled": True,
            "popup_disconnected_enabled": True,
            "popup_scan_interval_seconds": 1,
            "connection_error_hold_time": 1,
            "watchdog_hold_time": 60,
            "watchdog_activity_timeout": 180,
            "watchdog_loading_grace": 90,
            "watchdog_cpu_low": 0.9,
        }
        maint._accounts = []
        maint._workers = {}
        maint._last_popup_scan_at = {}

        class Net:
            def is_online(self):
                return True

        class Recovery:
            _net = Net()

            def __init__(self):
                self.calls = []

            def handle_runtime_signal(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        class State:
            def set_binding_status(self, *args, **kwargs):
                pass

        recovery = Recovery()
        maint._recovery = recovery
        maint._state_mgr = State()
        acc = Account(username="popup_rejoin_user")
        acc.state = AccountState.IN_GAME
        acc.pid = 1234
        acc.in_game_since = time.time() - 120
        acc.last_activity_at = time.time()
        acc.liveness_state = "alive"
        acc.liveness_suspect_since = time.time() - 2
        acc.runtime_generation = 7
        acc.session_id = "sess"
        acc.launch_nonce = "nonce"
        acc.rejoin_transaction_id = "tx"
        maint._accounts = [acc]

        liveness = {
            "state": "reconnecting",
            "score": 8.0,
            "validation": {"cpu": 3.0, "ram_mb": 300.0, "windows": 1},
            "reason_key": "session_conflict",
            "dialog": {
                "matched": True,
                "recovery_allowed": True,
                "action": "conditional_rejoin",
                "reason_key": "session_conflict",
                "detail": "Error Code 273",
                "error_code": "273",
                "popup_confidence": 1.5,
                "disconnect_category": "SESSION_CONFLICT",
            },
        }
        with patch.object(ProcessManager, "assess_liveness", return_value=liveness):
            SystemMaintenance._scan_liveness_watchdog(maint)

        self.assertEqual(len(recovery.calls), 1)
        args, kwargs = recovery.calls[0]
        self.assertEqual(args[1], "disconnect_detected")
        self.assertEqual(args[2], "session_conflict")
        self.assertEqual(kwargs["expected_runtime_generation"], 7)
        self.assertEqual(kwargs["payload"]["popup_code"], "273")

    def test_visual_popup_is_enriched_with_recent_log_error_code(self):
        from services.roblox_liveness import assess_liveness

        class FakeProcessManager:
            @classmethod
            def validate_game_process(cls, pid, min_ram_mb=0.0):
                return {"ok": True, "cpu": 0.0, "ram_mb": 180.0, "windows": 1}

            @classmethod
            def is_not_responding(cls, pid):
                return False

            @classmethod
            def inspect_disconnect_dialog(cls, *args, **kwargs):
                return {
                    "matched": True,
                    "recovery_allowed": True,
                    "action": "rejoin",
                    "reason_key": "connection_error",
                    "detail": "visual_disconnect source=center_modal strength=strong",
                    "error_code": "",
                    "popup_confidence": 1.1,
                    "disconnect_category": "VISUAL_DISCONNECT",
                    "visual_disconnect": True,
                    "evidence_source": "visual_strong",
                }

            @classmethod
            def classify_disconnect_dialog_texts(cls, texts):
                return ProcessManager.classify_disconnect_dialog_texts(texts)

        with patch(
            "services.roblox_liveness.collect_recent_log_evidence",
            return_value={
                "matched": True,
                "source": "roblox_log",
                "error_code": "273",
                "keyword": "same account launched",
                "confidence": 1.2,
                "line": "Same account launched experience from different device. (Error Code: 273)",
            },
        ) as collect:
            result = assess_liveness(FakeProcessManager, 1234, inspect_ui=True)

        collect.assert_called_once()
        dialog = result["dialog"]
        self.assertEqual(result["state"], "reconnecting")
        self.assertEqual(result["reason_key"], "session_conflict")
        self.assertEqual(dialog["error_code"], "273")
        self.assertEqual(dialog["action"], "conditional_rejoin")
        self.assertEqual(dialog["disconnect_category"], "SESSION_CONFLICT")
        self.assertEqual(dialog["evidence_source"], "error_code")
        self.assertEqual(dialog["visual_evidence_source"], "visual_strong")
        self.assertTrue(dialog["visual_disconnect"])

    def test_visual_confirmed_popup_overrides_alive_process_without_log_evidence(self):
        from services.roblox_liveness import assess_liveness

        class FakeProcessManager:
            @classmethod
            def validate_game_process(cls, pid, min_ram_mb=0.0):
                return {"ok": True, "cpu": 5.0, "ram_mb": 220.0, "windows": 1}

            @classmethod
            def is_not_responding(cls, pid):
                return False

            @classmethod
            def inspect_disconnect_dialog(cls, *args, **kwargs):
                return {
                    "matched": True,
                    "recovery_allowed": True,
                    "action": "rejoin",
                    "reason_key": "connection_error",
                    "detail": "visual_disconnect source=center_modal strength=strong",
                    "error_code": "",
                    "popup_confidence": 1.1,
                    "disconnect_category": "VISUAL_DISCONNECT",
                    "visual_disconnect": True,
                    "evidence_source": "visual_strong",
                }

            @classmethod
            def classify_disconnect_dialog_texts(cls, texts):
                return ProcessManager.classify_disconnect_dialog_texts(texts)

        with patch(
            "services.roblox_liveness.collect_recent_log_evidence",
            return_value={"matched": False, "source": "roblox_log", "reason": "none"},
        ):
            result = assess_liveness(FakeProcessManager, 1234, inspect_ui=True)

        self.assertEqual(result["state"], "reconnecting")
        self.assertEqual(result["reason_key"], "connection_error")
        self.assertTrue(result["dialog"]["matched"])
        self.assertTrue(result["dialog"]["recovery_allowed"])
        self.assertEqual(result["dialog"]["evidence_source"], "visual_strong")

    def test_log_evidence_alone_does_not_create_popup_recovery(self):
        from services.roblox_liveness import assess_liveness

        class FakeProcessManager:
            @classmethod
            def validate_game_process(cls, pid, min_ram_mb=0.0):
                return {"ok": True, "cpu": 0.0, "ram_mb": 80.0, "windows": 0}

            @classmethod
            def is_not_responding(cls, pid):
                return False

            @classmethod
            def inspect_disconnect_dialog(cls, *args, **kwargs):
                return {
                    "matched": False,
                    "recovery_allowed": False,
                    "action": "",
                    "reason_key": "",
                    "detail": "",
                    "error_code": "",
                }

        with patch(
            "services.roblox_liveness.collect_recent_log_evidence",
            return_value={
                "matched": True,
                "source": "roblox_log",
                "error_code": "273",
                "confidence": 1.2,
                "line": "Same account launched experience from different device. (Error Code: 273)",
            },
        ):
            result = assess_liveness(FakeProcessManager, 1234, inspect_ui=True)

        self.assertNotEqual(result["state"], "reconnecting")
        self.assertFalse(result["dialog"].get("recovery_allowed", False))
        self.assertTrue(result["log_evidence"]["matched"])

    def test_window_resize_uses_interval_and_config(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {
            "roblox_window_resize_enabled": True,
            "roblox_window_width": 640,
            "roblox_window_height": 480,
            "roblox_window_resize_interval_seconds": 10,
        }
        maint._last_window_resize_at = time.time() - 5
        with patch.object(ProcessService, "resize_roblox_windows") as resize:
            SystemMaintenance._enforce_window_resize(maint)
        resize.assert_not_called()

        maint._last_window_resize_at = time.time() - 11
        with patch.object(ProcessService, "resize_roblox_windows", return_value={"resized": 2, "count": 2}) as resize:
            SystemMaintenance._enforce_window_resize(maint)
        resize.assert_called_once_with(640, 480, reason="auto_window_resize_cycle")

        maint._cfg["roblox_window_arrange_enabled"] = True
        maint._cfg["roblox_window_arrange_columns"] = 4
        maint._cfg["roblox_window_arrange_gap"] = 2
        maint._cfg["roblox_window_arrange_margin"] = 0
        maint._last_window_resize_at = time.time() - 11
        with patch.object(ProcessService, "arrange_roblox_windows", return_value={"arranged": 2, "count": 2}) as arrange:
            SystemMaintenance._enforce_window_resize(maint)
        arrange.assert_called_once_with(640, 480, 4, 2, 0, reason="auto_window_resize_cycle")

    def test_process_manager_minimizes_only_visible_roblox_windows(self):
        with patch.object(
            ProcessManager,
            "_visible_roblox_windows",
            return_value=[{"pid": 111, "hwnd": 222}, {"pid": 333, "hwnd": 444}],
        ), patch("services.window_control.ctypes.windll") as windll:
            windll.user32.ShowWindow.return_value = 1
            result = ProcessManager.minimize_roblox_windows()
        self.assertTrue(result["ok"])
        self.assertEqual(result["minimized"], 2)
        self.assertEqual(windll.user32.ShowWindow.call_count, 2)

    def test_disconnect_dialog_277_is_rejoinable(self):
        result = ProcessManager.classify_disconnect_dialog_texts([
            "Disconnected",
            "Please check your internet connection and try again.",
            "(Error Code: 277)",
            "Reconnect",
        ])

        self.assertTrue(result["matched"])
        self.assertTrue(result["recovery_allowed"])
        self.assertEqual(result["evidence_source"], "error_code")
        self.assertEqual(result["action"], "rejoin")
        self.assertEqual(result["reason_key"], "network_drop")
        self.assertEqual(result["error_code"], "277")

    def test_disconnect_dialog_278_is_rejoinable_idle_disconnect(self):
        result = ProcessManager.classify_disconnect_dialog_texts([
            "Disconnected",
            "You were disconnected for being idle 20 minutes",
            "(Error Code: 278)",
            "Reconnect",
        ])

        self.assertTrue(result["matched"])
        self.assertTrue(result["recovery_allowed"])
        self.assertEqual(result["evidence_source"], "error_code")
        self.assertEqual(result["action"], "rejoin")
        self.assertEqual(result["reason_key"], "idle_disconnect")
        self.assertEqual(result["disconnect_category"], "NETWORK_DISCONNECT")
        self.assertEqual(result["error_code"], "278")

    def test_disconnect_dialog_273_is_conditional_rejoin_session_conflict(self):
        result = ProcessManager.classify_disconnect_dialog_texts([
            "Disconnected",
            "Same account launched game from different device. Reconnect if you prefer to use this device.",
            "(Error Code: 273)",
            "Reconnect",
        ])

        self.assertTrue(result["matched"])
        self.assertTrue(result["recovery_allowed"])
        self.assertEqual(result["evidence_source"], "error_code")
        self.assertEqual(result["action"], "conditional_rejoin")
        self.assertEqual(result["reason_key"], "session_conflict")
        self.assertEqual(result["disconnect_category"], "SESSION_CONFLICT")
        self.assertEqual(result["error_code"], "273")

    def test_disconnect_dialog_267_is_rejoinable_data_session_end(self):
        result = ProcessManager.classify_disconnect_dialog_texts([
            "Disconnected",
            "Your data session has ended. Please rejoin.",
            "(Error Code: 267)",
            "Leave",
        ])

        self.assertTrue(result["matched"])
        self.assertTrue(result["recovery_allowed"])
        self.assertEqual(result["evidence_source"], "error_code")
        self.assertEqual(result["action"], "rejoin")
        self.assertEqual(result["reason_key"], "security_kick")
        self.assertEqual(result["disconnect_category"], "NETWORK_DISCONNECT")
        self.assertEqual(result["error_code"], "267")

    def test_disconnect_dialog_268_is_rejoinable(self):
        result = ProcessManager.classify_disconnect_dialog_texts([
            "Disconnected",
            "You have been kicked due to unexpected client behavior.",
            "(Error Code: 268)",
            "Leave",
        ])

        self.assertTrue(result["matched"])
        self.assertTrue(result["recovery_allowed"])
        self.assertEqual(result["evidence_source"], "error_code")
        self.assertEqual(result["action"], "rejoin")
        self.assertEqual(result["reason_key"], "unexpected_client_behavior")
        self.assertEqual(result["disconnect_category"], "NETWORK_DISCONNECT")
        self.assertEqual(result["error_code"], "268")

    def test_disconnect_dialog_unknown_error_code_is_rejoinable(self):
        result = ProcessManager.classify_disconnect_dialog_texts([
            "Disconnected",
            "Roblox closed this session.",
            "(Error Code: 999)",
            "Leave",
        ])

        self.assertTrue(result["matched"])
        self.assertTrue(result["recovery_allowed"])
        self.assertEqual(result["evidence_source"], "error_code")
        self.assertEqual(result["action"], "rejoin")
        self.assertEqual(result["reason_key"], "connection_error")
        self.assertEqual(result["error_code"], "999")

    def test_visual_strong_disconnect_popup_matches_without_window_text(self):
        from runtime.popup_detector.popup_classifier import classify_popup_observation

        visual = {
            "matched": True,
            "score": 1.1,
            "strength": "strong",
            "source": "template",
            "visual_stage": "template",
            "button_pattern": "double",
            "overlay_score": 0.3,
            "modal_score": 0.8,
            "button_score": 0.6,
            "template_score": 0.7,
        }
        result = classify_popup_observation([], visual, process_idle=True)

        self.assertTrue(visual["matched"])
        self.assertTrue(result.matched)
        self.assertTrue(result.recovery_allowed)
        self.assertEqual(result.evidence_source, "visual_strong")
        self.assertEqual(result.action, "rejoin")
        self.assertEqual(result.visual_stage, "template")
        self.assertEqual(result.button_pattern, "double")

    def test_visual_pipeline_detects_overlay_modal_and_buttons_before_text(self):
        from PIL import Image, ImageDraw
        from runtime.popup_detector.popup_classifier import classify_popup_observation
        from runtime.popup_detector.popup_visual_detector import detect_visual_features

        image = Image.new("L", (800, 600), 130)
        draw = ImageDraw.Draw(image)
        draw.rectangle((220, 160, 580, 410), fill=58)
        draw.line((240, 215, 560, 215), fill=190, width=2)
        draw.rounded_rectangle((245, 340, 395, 382), radius=8, fill=245)
        draw.rounded_rectangle((405, 340, 555, 382), radius=8, fill=245)

        visual = detect_visual_features(image)
        result = classify_popup_observation([], visual, process_idle=False)

        self.assertTrue(visual["matched"])
        self.assertEqual(visual["strength"], "strong")
        self.assertEqual(visual["visual_stage"], "modal_button")
        self.assertEqual(visual["button_pattern"], "double")
        self.assertGreaterEqual(visual["overlay_score"], 0.28)
        self.assertGreaterEqual(visual["modal_score"], 1.0)
        self.assertGreaterEqual(visual["button_score"], 0.6)
        self.assertTrue(result.recovery_allowed)
        self.assertEqual(result.evidence_source, "visual_strong")

    def test_visual_pipeline_detects_disconnect_popup_at_supported_window_sizes(self):
        from PIL import Image, ImageDraw
        from runtime.popup_detector.popup_classifier import classify_popup_observation
        from runtime.popup_detector.popup_visual_detector import detect_visual_features

        def make_popup(width: int, height: int):
            image = Image.new("L", (width, height), 130)
            draw = ImageDraw.Draw(image)
            line_width = max(1, int(round(min(width, height) * 0.003)))
            radius = max(2, int(round(min(width, height) * 0.016)))
            draw.rectangle((int(width * 0.275), int(height * 0.267), int(width * 0.725), int(height * 0.683)), fill=58)
            draw.line((int(width * 0.300), int(height * 0.358), int(width * 0.700), int(height * 0.358)), fill=190, width=line_width)
            draw.rounded_rectangle((int(width * 0.306), int(height * 0.567), int(width * 0.494), int(height * 0.638)), radius=radius, fill=245)
            draw.rounded_rectangle((int(width * 0.506), int(height * 0.567), int(width * 0.694), int(height * 0.638)), radius=radius, fill=245)
            return image

        sizes = (
            (320, 240),
            (240, 180),
            (320, 180),
            (400, 300),
            (480, 270),
            (512, 384),
            (640, 360),
            (640, 480),
            (800, 600),
        )
        for width, height in sizes:
            with self.subTest(size=f"{width}x{height}"):
                visual = detect_visual_features(make_popup(width, height))
                result = classify_popup_observation([], visual, process_idle=False)

                self.assertTrue(visual["matched"])
                self.assertEqual(visual["strength"], "strong")
                self.assertEqual(visual["visual_stage"], "modal_button")
                self.assertEqual(visual["button_pattern"], "double")
                self.assertTrue(result.recovery_allowed)
                self.assertEqual(result.evidence_source, "visual_strong")

    def test_visual_pipeline_detects_small_window_disconnect_leave_bar(self):
        from PIL import Image, ImageDraw
        from runtime.popup_detector.popup_classifier import classify_popup_observation
        from runtime.popup_detector.popup_visual_detector import detect_visual_features

        image = Image.new("L", (320, 240), 35)
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 320, 42), fill=28)
        draw.rectangle((0, 42, 320, 170), fill=64)
        draw.text((120, 48), "Disconnected", fill=240)
        draw.text((28, 90), "You have been kicked by this experience or its moderators.", fill=180)
        draw.text((113, 125), "(Error Code: 267)", fill=198)
        draw.rectangle((0, 170, 320, 207), fill=238)
        draw.text((150, 182), "Leave", fill=38)
        draw.rectangle((0, 207, 320, 240), fill=18)

        visual = detect_visual_features(image)
        result = classify_popup_observation([], visual, process_idle=False)

        self.assertTrue(visual["matched"])
        self.assertEqual(visual["strength"], "strong")
        self.assertEqual(visual["visual_stage"], "small_panel")
        self.assertEqual(visual["button_pattern"], "bar")
        self.assertTrue(result.recovery_allowed)
        self.assertEqual(result.evidence_source, "visual_strong")

    def test_modal_shape_without_button_is_visual_weak_and_ignored(self):
        from PIL import Image, ImageDraw
        from runtime.popup_detector.popup_classifier import classify_popup_observation
        from runtime.popup_detector.popup_visual_detector import detect_visual_features

        image = Image.new("L", (800, 600), 130)
        draw = ImageDraw.Draw(image)
        draw.rectangle((220, 160, 580, 410), fill=58)
        draw.line((240, 215, 560, 215), fill=190, width=2)

        visual = detect_visual_features(image)
        result = classify_popup_observation([], visual, process_idle=True)

        self.assertTrue(visual["matched"])
        self.assertEqual(visual["strength"], "weak")
        self.assertEqual(visual["visual_stage"], "structural_weak")
        self.assertEqual(visual["button_pattern"], "none")
        self.assertFalse(result.recovery_allowed)
        self.assertEqual(result.evidence_source, "visual_weak")
        self.assertEqual(result.action, "")

    def test_visual_weak_disconnect_popup_does_not_allow_recovery(self):
        from PIL import Image, ImageDraw
        from runtime.popup_detector.popup_classifier import classify_popup_observation
        from runtime.popup_detector.popup_visual_detector import detect_visual_features

        image = Image.new("L", (800, 600), 130)
        draw = ImageDraw.Draw(image)
        draw.rectangle((160, 140, 640, 560), fill=58)
        draw.line((190, 225, 610, 225), fill=190, width=1)
        draw.rounded_rectangle((180, 492, 620, 535), radius=8, fill=245)

        visual = detect_visual_features(image)
        result = classify_popup_observation([], visual, process_idle=True)

        self.assertTrue(visual["matched"])
        self.assertTrue(result.matched)
        self.assertFalse(result.recovery_allowed)
        self.assertEqual(result.evidence_source, "visual_weak")
        self.assertEqual(result.action, "")

    def test_popup_observer_confirms_repeated_visual_only_popup_below_text_threshold(self):
        from runtime.popup_detector.popup_sampler import PopupObserver

        observer = PopupObserver(sample_count=2, sample_interval=0, threshold=1.0, stable_samples=2)
        observer.sampler.windows_for_pid = lambda pid, include_hidden=True: [{"hwnd": 123}]
        observer.sampler.read_texts = lambda hwnd: []
        observer.sampler.capture_window_image = lambda hwnd: object()

        with patch(
            "runtime.popup_detector.popup_sampler.detect_visual_features",
            return_value={"matched": True, "score": 1.1, "strength": "strong", "source": "template"},
        ):
            result = observer.inspect_pid(100, sample_count=2, sample_interval=0)

        self.assertTrue(result["matched"])
        self.assertTrue(result["recovery_allowed"])
        self.assertEqual(result["action"], "rejoin")
        self.assertEqual(result["reason_key"], "connection_error")
        self.assertEqual(result["positive_samples"], 2)
        self.assertEqual(result["samples_confirmed"], 2)
        self.assertEqual(result["visual_positive_samples"], 2)
        self.assertEqual(result["disconnect_category"], "VISUAL_DISCONNECT")
        self.assertTrue(result["visual_disconnect"])

    def test_popup_observer_ignores_repeated_visual_weak_panel(self):
        from runtime.popup_detector.popup_sampler import PopupObserver

        observer = PopupObserver(sample_count=2, sample_interval=0, threshold=1.0, stable_samples=2)
        observer.sampler.windows_for_pid = lambda pid, include_hidden=True: [{"hwnd": 123}]
        observer.sampler.read_texts = lambda hwnd: []
        observer.sampler.capture_window_image = lambda hwnd: object()

        with patch(
            "runtime.popup_detector.popup_sampler.detect_visual_features",
            return_value={"matched": True, "score": 0.95, "strength": "weak", "source": "structural"},
        ):
            result = observer.inspect_pid(100, sample_count=2, sample_interval=0)

        self.assertFalse(result["matched"])
        self.assertFalse(result["recovery_allowed"])
        self.assertEqual(result["evidence_source"], "visual_weak")
        self.assertEqual(result["action"], "")

    def test_popup_inspection_does_not_resize_supported_window_sizes(self):
        from runtime.popup_detector.popup_sampler import PopupWindowSampler

        sampler = PopupWindowSampler()
        for width, height in ((320, 240), (240, 180), (320, 180), (480, 270), (640, 480), (800, 600)):
            with self.subTest(size=f"{width}x{height}"):
                sampler.windows_for_pid = lambda pid, include_hidden=True, width=width, height=height: [{
                    "pid": pid,
                    "hwnd": 123,
                    "left": 10,
                    "top": 20,
                    "width": width,
                    "height": height,
                    "visible": True,
                    "iconic": False,
                }]

                with patch("runtime.popup_detector.popup_sampler.ctypes.windll") as windll:
                    result = sampler.prepare_popup_inspection(100, hold_seconds=1.0)

                self.assertTrue(result["ok"])
                self.assertFalse(result["resized"])
                windll.user32.SetWindowPos.assert_not_called()
                windll.user32.ShowWindow.assert_not_called()

    def test_non_disconnect_panel_does_not_match_from_process_idle_alone(self):
        from PIL import Image, ImageDraw
        from runtime.popup_detector.popup_classifier import classify_popup_observation
        from runtime.popup_detector.popup_visual_detector import detect_visual_features

        image = Image.new("L", (800, 600), 145)
        draw = ImageDraw.Draw(image)
        draw.rectangle((150, 175, 650, 540), fill=58)
        draw.line((260, 250, 620, 250), fill=180, width=1)

        visual = detect_visual_features(image)
        result = classify_popup_observation([], visual, process_idle=True)

        self.assertFalse(visual["matched"])
        self.assertFalse(result.matched)

    def test_process_manager_resizes_visible_roblox_windows_without_arranging(self):
        with patch.object(
            ProcessManager,
            "_visible_roblox_windows",
            return_value=[
                {"pid": 111, "hwnd": 222, "left": 50, "top": 60, "width": 800, "height": 600},
                {"pid": 333, "hwnd": 444, "left": 70, "top": 80, "width": 640, "height": 480},
            ],
        ), patch("services.window_control.ctypes.windll") as windll:
            windll.user32.GetWindowLongPtrW.return_value = 0x16CF0000
            windll.user32.SetWindowLongPtrW.return_value = 0x16CF0000
            windll.user32.SetWindowPos.return_value = 1
            def fake_rect(hwnd, rect_ref):
                rect_ref._obj.left = 50
                rect_ref._obj.top = 60
                rect_ref._obj.right = 690
                rect_ref._obj.bottom = 540
                return 1
            windll.user32.GetWindowRect.side_effect = fake_rect
            result = ProcessManager.resize_roblox_windows(640, 480)
        self.assertTrue(result["ok"])
        self.assertEqual(result["resized"], 1)
        self.assertEqual(result["skipped"], 1)
        windll.user32.SetWindowPos.assert_called_once()
        call_args = windll.user32.SetWindowPos.call_args.args
        self.assertEqual(call_args[2:6], (50, 60, 640, 480))

    def test_process_manager_arranges_windows_in_grid(self):
        windows = [
            {"pid": 100 + i, "hwnd": 200 + i, "left": 0, "top": 0, "width": 800, "height": 600}
            for i in range(5)
        ]
        with patch.object(ProcessManager, "_visible_roblox_windows", return_value=windows), patch(
            "services.window_control.primary_monitor_work_area",
            return_value={"left": 0, "top": 0, "width": 1200, "height": 800},
        ), patch("services.window_control.ctypes.windll") as windll:
            windll.user32.GetWindowLongPtrW.return_value = 0x16CF0000
            windll.user32.SetWindowLongPtrW.return_value = 0x16CF0000
            windll.user32.SetWindowPos.return_value = 1
            result = ProcessManager.arrange_roblox_windows(320, 240, columns=3, gap=2, margin=0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["arranged"], 5)
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["columns"], 3)
        calls = windll.user32.SetWindowPos.call_args_list
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[0].args[2:6], (0, 0, 320, 240))
        self.assertEqual(calls[3].args[2:6], (0, 242, 320, 240))

    def test_process_manager_arrange_shrinks_to_fit_work_area(self):
        windows = [
            {"pid": 100 + i, "hwnd": 200 + i, "left": 0, "top": 0, "width": 800, "height": 600}
            for i in range(8)
        ]
        with patch.object(ProcessManager, "_visible_roblox_windows", return_value=windows), patch(
            "services.window_control.primary_monitor_work_area",
            return_value={"left": 0, "top": 0, "width": 1000, "height": 300},
        ), patch("services.window_control.ctypes.windll") as windll:
            windll.user32.GetWindowLongPtrW.return_value = 0x16CF0000
            windll.user32.SetWindowLongPtrW.return_value = 0x16CF0000
            windll.user32.SetWindowPos.return_value = 1
            result = ProcessManager.arrange_roblox_windows(320, 240, columns=8, gap=2, margin=0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["arranged"], 8)
        self.assertLess(result["width"], 320)
        last = windll.user32.SetWindowPos.call_args_list[-1].args
        self.assertLessEqual(last[2] + last[4], 1000)

    def test_window_size_endpoint_applies_preset(self):
        from fastapi.testclient import TestClient
        import main

        original = main.cfg_mgr.snapshot()
        try:
            with patch.object(
                ProcessService,
                "resize_roblox_windows",
                return_value={"ok": True, "count": 1, "resized": 1, "skipped": 0},
            ) as resize:
                client = TestClient(main.app)
                response = auth_post(client,
                    "/api/performance/window-size",
                    json={"enabled": True, "preset": "320x240", "width": 1920, "height": 1080, "arrange_enabled": False},
                )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["enabled"])
            self.assertEqual(payload["preset"], "320x240")
            self.assertEqual(payload["width"], 320)
            self.assertEqual(payload["height"], 240)
            self.assertEqual(payload["resize_result"]["resized"], 1)
            resize.assert_called_once_with(320, 240, reason="api_window_size_apply")
        finally:
            main.cfg_mgr.update(original)
            main.cfg_mgr.save()

    def test_window_size_endpoint_arranges_when_enabled(self):
        from fastapi.testclient import TestClient
        import main

        original = main.cfg_mgr.snapshot()
        try:
            with patch.object(
                ProcessService,
                "arrange_roblox_windows",
                return_value={"ok": True, "count": 5, "arranged": 5, "failed": 0},
            ) as arrange:
                client = TestClient(main.app)
                response = auth_post(client,
                    "/api/performance/window-size",
                    json={"enabled": True, "preset": "320x240", "arrange_enabled": True, "arrange_columns": 3, "arrange_gap": 2},
                )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["arrange_enabled"])
            self.assertEqual(payload["arrange_columns"], 3)
            self.assertEqual(payload["resize_result"]["arranged"], 5)
            arrange.assert_called_once_with(320, 240, 3, 2, 0, reason="api_window_size_apply")
        finally:
            main.cfg_mgr.update(original)
            main.cfg_mgr.save()

    def test_multi_roblox_guard_failure_requires_recent_pid_overlap(self):
        from farm import AccountWorker

        now = time.time()
        acc = Account(username="UserA")
        acc.last_launch_at = now - 120
        acc.pid_missing_since = now
        worker = object.__new__(AccountWorker)
        worker.acc = acc
        worker.cfg = {
            "multi_roblox_enabled": True,
            "rt_rotation_enabled": False,
            "multi_roblox_guard_failure_window": 180,
            "multi_roblox_guard_failure_overlap_seconds": 20,
        }
        other = Account(username="UserB")
        worker._accounts = [acc, other]

        stale_presence = {"newest_created": now - 80, "pids": [111, 222]}
        fresh_presence = {"newest_created": now - 5, "pids": [111, 222]}

        other.state = AccountState.IN_GAME
        self.assertFalse(
            AccountWorker._looks_like_multi_roblox_guard_failure(worker, 111, fresh_presence, 12.0, 10.0)
        )
        other.state = AccountState.VERIFY
        self.assertFalse(
            AccountWorker._looks_like_multi_roblox_guard_failure(worker, 111, stale_presence, 12.0, 10.0)
        )
        self.assertTrue(
            AccountWorker._looks_like_multi_roblox_guard_failure(worker, 111, fresh_presence, 12.0, 10.0)
        )

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
        maint._cfg = {"multi_roblox_enabled": True, "rt_rotation_enabled": False, "queue_duration_seconds": 15}
        self.assertEqual(SystemMaintenance._queue_duration_seconds(maint), 0.0)
        maint._cfg = {"multi_roblox_enabled": True, "rt_rotation_enabled": True, "queue_duration_seconds": 15}
        self.assertEqual(SystemMaintenance._queue_duration_seconds(maint), 15.0)

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
                return "multi_roblox_guard_ready ROBLOX_singletonMutex:err=0,ROBLOX_singletonEvent:err=0 pid=123\n"

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
        with patch("roblox_hybrid.subprocess.Popen", return_value=fake):
            ok, detail = ensure_multi_roblox_guard()
        self.assertTrue(ok)
        self.assertIn("ROBLOX_singletonMutex", detail)
        self.assertIn("ROBLOX_singletonEvent", detail)
        status = multi_roblox_guard_status()
        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["pid"], 123)
        record_multi_roblox_guard_failure("unit failure")
        self.assertEqual(multi_roblox_guard_status()["last_failure"], "unit failure")
        release_multi_roblox_guard()
        self.assertTrue(fake.terminated)
        self.assertEqual(multi_roblox_guard_status()["state"], "stopped")

    def test_multi_roblox_guard_accepts_partial_external_provider(self):
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

            self.assertTrue(ok)
            self.assertIn("ROBLOX_singletonEvent", detail)
            self.assertIn("external_provider=possible", detail)
            self.assertIn("missing=ROBLOX_singletonMutex", detail)
            status = multi_roblox_guard_status()
            self.assertEqual(status["state"], "ready")
            self.assertEqual(status["pid"], 6380)
            self.assertEqual(status["handle_names"], ["ROBLOX_singletonEvent:err=0"])
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

if __name__ == "__main__":
    unittest.main()
