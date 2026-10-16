"""Session identity + launch intent: pure, no Windows/network."""
import unittest
from types import SimpleNamespace

from domain.session_identity import (
    build_launch_intent,
    create_rejoin_transaction,
    create_session_identity,
)


class TestSessionIdentity(unittest.TestCase):
    def test_create_identity_unique(self) -> None:
        a = create_session_identity("alice", 3)
        b = create_session_identity("alice", 3)
        self.assertEqual(a.account_id, "alice")
        self.assertEqual(a.runtime_generation, 3)
        self.assertTrue(a.session_id.startswith("sess_"))
        self.assertTrue(a.launch_nonce.startswith("nonce_"))
        self.assertNotEqual(a.session_id, b.session_id)
        self.assertNotEqual(a.launch_nonce, b.launch_nonce)

    def test_rejoin_links_identity(self) -> None:
        ident = create_session_identity("bob", 5, reason="start")
        rtx = create_rejoin_transaction(ident, recovery_generation=2, command_generation=7)
        self.assertEqual(rtx.account_id, "bob")
        self.assertEqual(rtx.runtime_generation, 5)
        self.assertEqual(rtx.session_id, ident.session_id)
        self.assertEqual(rtx.launch_nonce, ident.launch_nonce)
        self.assertEqual(rtx.status, "pending")

    def test_launch_intent_public(self) -> None:
        acc = SimpleNamespace(vip_links=[], active_vip="", browser_tracker_id="", place_id="123", server_type="PUBLIC", launch_strategy="")
        intent = build_launch_intent(acc)
        self.assertEqual(intent["place_id"], "123")
        self.assertEqual(intent["vip_intent"], "none")
        self.assertFalse(intent["private_server_intent"])

    def test_launch_intent_private(self) -> None:
        vip = "https://www.roblox.com/games/123?privateServerLinkCode=ABCDEF123456"
        acc = SimpleNamespace(vip_links=[vip], active_vip=vip, browser_tracker_id="TRACKER123456789", place_id="", server_type="VIP", launch_strategy="vip")
        intent = build_launch_intent(acc)
        self.assertEqual(intent["place_id"], "123")
        self.assertIn(intent["vip_intent"], {"active_private", "configured_private"})
        self.assertTrue(intent["private_server_intent"])
        # tracker_label path: long id truncated to ...last8
        self.assertTrue(intent["browser_tracker_id"].endswith("123456789"[-8:]))

    def test_launch_intent_empty_account(self) -> None:
        intent = build_launch_intent(SimpleNamespace())
        self.assertEqual(intent["vip_intent"], "none")
        self.assertEqual(intent["browser_tracker_id"], "")

    def test_tracker_label_parity_with_services(self) -> None:
        # domain/session_identity._tracker_label is a deliberate local copy of
        # services.browser_tracker.tracker_label (domain must not import
        # services). Any behavior change must land in both places.
        from domain.session_identity import _tracker_label
        from services.browser_tracker import tracker_label

        for value in ["", "ABC", "12345678", "TRACKER123456789", None, 12345]:
            self.assertEqual(_tracker_label(value), tracker_label(value))


if __name__ == "__main__":
    unittest.main()
