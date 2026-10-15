"""Tests for the launch-target builder branches.

Pure stdlib unittest — no launcher, no network. Covers the assigned-game,
unassigned-account and legacy branches sharing one finished tail.
"""
import unittest

from services.roblox_launch_service import (
    AccountLaunchService,
    _checked_global_vip_link,
)

LINK_999 = "https://www.roblox.com/games/999?privateServerLinkCode=AAA"
LINK_222 = "https://www.roblox.com/games/222?privateServerLinkCode=BBB"


class _FakeCfg(dict):
    def snapshot(self):
        return dict(self)


def _per_account_cfg():
    return _FakeCfg({
        "game_mode": "per_account",
        "games": [{"id": "game-1", "place_id": "999", "private_server_url": LINK_999}],
        "game_place_id": "",
        "game_private_server_url": "",
        "auto_create_private_server_enabled": False,
        "auto_create_private_server_free_only": True,
    })


class TestCheckedGlobalVipLink(unittest.TestCase):
    def test_matching_passes(self):
        self.assertEqual(_checked_global_vip_link(LINK_999, "999"), LINK_999)

    def test_mismatched_dropped(self):
        self.assertEqual(_checked_global_vip_link(LINK_999, "222"), "")

    def test_empty_place_passes(self):
        self.assertEqual(_checked_global_vip_link(LINK_999, ""), LINK_999)

    def test_empty_link(self):
        self.assertEqual(_checked_global_vip_link("", "999"), "")


class TestBuildTarget(unittest.TestCase):
    def test_legacy_branch(self):
        cfg = _FakeCfg({
            "game_place_id": "999",
            "game_private_server_url": LINK_999,
            "auto_create_private_server_enabled": False,
            "auto_create_private_server_free_only": True,
        })
        target = AccountLaunchService.build_target(
            {}, {"vip_links": [LINK_999, LINK_222]}, cfg,
        )
        self.assertEqual(target["place_id"], "999")
        self.assertEqual(target["vip_links"], [LINK_999])
        self.assertEqual(target["global_vip_link"], LINK_999)

    def test_legacy_drops_mismatched_global(self):
        cfg = _FakeCfg({
            "game_place_id": "999",
            "game_private_server_url": LINK_222,
            "auto_create_private_server_enabled": False,
            "auto_create_private_server_free_only": True,
        })
        target = AccountLaunchService.build_target({}, {"vip_links": []}, cfg)
        self.assertEqual(target["global_vip_link"], "")

    def test_unassigned_branch_prefers_record_place(self):
        cfg = _per_account_cfg()
        target = AccountLaunchService.build_target(
            {}, {"vip_links": [LINK_999, LINK_222], "place_id": "555"}, cfg,
        )
        self.assertNotIn("game_id", target)
        self.assertEqual(target["place_id"], "555")
        self.assertEqual(target["vip_links"], [])

    def test_assigned_branch_game_authoritative(self):
        cfg = _per_account_cfg()
        target = AccountLaunchService.build_target(
            {}, {"vip_links": [LINK_999, LINK_222], "place_id": "999"}, cfg,
        )
        self.assertEqual(target["game_id"], "game-1")
        self.assertEqual(target["place_id"], "999")
        self.assertEqual(target["global_vip_link"], LINK_999)
        self.assertEqual(target["vip_links"], [LINK_999])


if __name__ == "__main__":
    unittest.main()
