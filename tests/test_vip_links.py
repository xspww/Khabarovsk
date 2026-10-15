"""Tests for the single-owner VIP link helpers.

Pure stdlib unittest — no Roblox, no network. Covers the per-place filter
and the shared-direct rule that the launch targets now leverage.
"""
import unittest

from domain.roblox_private_servers import shared_direct_vip_link, vip_links_for_place

PLACE_A = "111"
PLACE_B = "222"
LINK_A = f"https://www.roblox.com/games/{PLACE_A}?privateServerLinkCode=AAA"
LINK_B = f"https://www.roblox.com/games/{PLACE_B}?privateServerLinkCode=BBB"
PLACELESS = "https://www.roblox.com/games?privateServerLinkCode=ZZZ"


class TestVipLinksForPlace(unittest.TestCase):
    def test_keeps_matching_and_placeless(self):
        self.assertEqual(
            vip_links_for_place([LINK_A, LINK_B, PLACELESS], PLACE_A),
            [LINK_A, PLACELESS],
        )

    def test_empty_place_passes_through(self):
        links = [LINK_A, LINK_B]
        self.assertEqual(vip_links_for_place(links, ""), links)

    def test_unparseable_kept(self):
        self.assertEqual(vip_links_for_place(["not-a-link"], PLACE_A), ["not-a-link"])


class TestSharedDirectVipLink(unittest.TestCase):
    def test_qualifies_matching_complete_link(self):
        self.assertEqual(shared_direct_vip_link(LINK_A, PLACE_A), LINK_A)

    def test_empty_place_accepts_complete_link(self):
        self.assertEqual(shared_direct_vip_link(LINK_A, ""), LINK_A)

    def test_mismatched_place_rejected(self):
        self.assertEqual(shared_direct_vip_link(LINK_A, PLACE_B), "")

    def test_incomplete_link_rejected(self):
        self.assertEqual(shared_direct_vip_link(PLACELESS, PLACE_A), "")
        self.assertEqual(shared_direct_vip_link("", PLACE_A), "")
        self.assertEqual(shared_direct_vip_link("not-a-link", PLACE_A), "")


if __name__ == "__main__":
    unittest.main()
