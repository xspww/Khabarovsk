from tests.hybrid_account_fixture import *


class HybridAccountPrivateServerLaunchCases:
    def test_process_launch_ignores_stale_private_target_when_auto_private_disabled(self):
        vip_link = "https://www.roblox.com/games/123456/Game?privateServerLinkCode=secret"
        acc = Account(username="UserA", cookie="_|WARNING:-cookie", place_id="123456")
        acc.active_vip = vip_link
        acc.vip_links = [vip_link]
        original_auto = ProcessManager.AUTO_CREATE_PRIVATE_SERVER_ENABLED
        original_global = ProcessManager.GLOBAL_VIP_LINK
        ProcessManager.AUTO_CREATE_PRIVATE_SERVER_ENABLED = False
        ProcessManager.GLOBAL_VIP_LINK = vip_link
        try:
            with patch.object(
                HybridLauncher,
                "launch_record",
                return_value={"ok": True, "mode": "public", "browser_tracker_id": "111222", "msg": "ok", "attempted_vip": ""},
            ) as launch_record:
                ok, msg, attempted_vip = ProcessManager.launch(acc)
        finally:
            ProcessManager.AUTO_CREATE_PRIVATE_SERVER_ENABLED = original_auto
            ProcessManager.GLOBAL_VIP_LINK = original_global

        self.assertTrue(ok)
        self.assertEqual(msg, "ok")
        self.assertEqual(attempted_vip, "")
        self.assertEqual(acc.server_type, ServerType.PUBLIC)
        self.assertEqual(acc.active_vip, "")
        _record, kwargs = launch_record.call_args
        target = kwargs["target"]
        self.assertEqual(target["vip_link"], "")
        self.assertEqual(target["vip_links"], [])
        self.assertEqual(target["global_vip_link"], "")

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
        ), patch("roblox_hybrid.ensure_multi_roblox_guard", return_value=(True, "ready")), patch.object(
            HybridLauncher,
            "kill_duplicate_instances",
            return_value={"ok": True, "killed": [], "count": 0},
        ), patch(
            "roblox_hybrid.ensure_owned_private_server",
            return_value={"ok": False, "msg": "Private servers are disabled for this universe", "universe_id": "456"},
        ), patch("roblox_hybrid.RobloxHTTP", FakeRobloxHTTP), patch(
            "roblox_hybrid.os.startfile",
            side_effect=lambda uri: started.append(uri),
            create=True,
        ), patch(
            "roblox_hybrid.ACCOUNT_STORE.update_record",
            side_effect=lambda username, payload: updates.append((username, payload)),
        ):
            result = HybridLauncher.launch_record(
                {"username": "UserA", "cookie": "_|WARNING:-cookie", "place_id": "123456"},
                target={"place_id": "123456", "auto_create_private_server_enabled": True, "auto_create_private_server_free_only": True},
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

    def test_auto_private_disabled_ignores_saved_private_server_links(self):
        class FakeRobloxHTTP:
            def __init__(self, cookie):
                self.cookie = cookie

            def get_auth_ticket(self):
                return True, "ticket-secret"

        vip_link = "https://www.roblox.com/games/123456/Game?privateServerLinkCode=secret"
        started = []
        with patch(
            "roblox_hybrid.validate_record_cookie_identity",
            return_value={"ok": True, "cookie_username": "UserA", "cookie_user_id": "42"},
        ), patch("roblox_hybrid.ensure_multi_roblox_guard", return_value=(True, "ready")), patch.object(
            HybridLauncher,
            "kill_duplicate_instances",
            return_value={"ok": True, "killed": [], "count": 0},
        ), patch(
            "roblox_hybrid.resolve_vip_access_code",
            return_value={"ok": True, "place_id": "123456", "access_code": "access", "link_code": "secret"},
        ) as resolve_vip, patch("roblox_hybrid.RobloxHTTP", FakeRobloxHTTP), patch(
            "roblox_hybrid.os.startfile",
            side_effect=lambda uri: started.append(uri),
            create=True,
        ), patch("roblox_hybrid.ACCOUNT_STORE.update_record"):
            result = HybridLauncher.launch_record(
                {"username": "UserA", "cookie": "_|WARNING:-cookie", "place_id": "123456", "vip_links": [vip_link], "global_vip_link": vip_link},
                target={"place_id": "123456", "global_vip_link": vip_link, "auto_create_private_server_enabled": False},
                multi_roblox=True,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "public")
        self.assertEqual(result["attempted_vip"], "")
        self.assertFalse(result["vip_resolved"])
        self.assertFalse(result["auto_private_server"])
        resolve_vip.assert_not_called()
        self.assertEqual(len(started), 1)
        self.assertIn("RequestGame", started[0])
        self.assertNotIn("RequestPrivateGame", started[0])
