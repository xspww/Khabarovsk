"""Process proof policy + cookie verdict mapping: pure, no processes."""
import unittest

from services.process_proof_policy import (
    allows_destructive_process_action,
    classify_process_proof,
    is_at_least_process_proof,
    normalize_process_proof_level,
    required_process_proof_for_state,
    score_resource_signals,
)
from services.roblox_validation import cookie_verdict_updates


class TestProcessProof(unittest.TestCase):
    def test_normalize_rank(self) -> None:
        self.assertEqual(normalize_process_proof_level("STRONG"), "strong")
        self.assertEqual(normalize_process_proof_level("bogus"), "untrusted")
        self.assertTrue(is_at_least_process_proof("strong", "medium"))
        self.assertFalse(is_at_least_process_proof("weak", "strong"))

    def test_required_for_state(self) -> None:
        self.assertEqual(required_process_proof_for_state("LAUNCHING"), "medium")
        self.assertEqual(required_process_proof_for_state("IN_GAME"), "strong")
        self.assertEqual(required_process_proof_for_state("IN_GAME", destructive=True), "strong")
        self.assertTrue(allows_destructive_process_action("strong"))
        self.assertFalse(allows_destructive_process_action("medium"))

    def test_score_weights(self) -> None:
        self.assertEqual(score_resource_signals(0, 0, 0), 0.0)
        self.assertGreater(score_resource_signals(2, 240, 5), 10.0)
        # capped
        self.assertLessEqual(score_resource_signals(99, 99999, 99), 37.0)
        self.assertEqual(score_resource_signals("bad", None, []), 0.0)

    def test_classify(self) -> None:
        bad = classify_process_proof({})
        self.assertEqual(bad["process_proof_level"], "untrusted")
        strong = classify_process_proof(
            {"ok": True, "owner": "o1", "browser_tracker_id": "T"},
            owner_key="o1",
            expected_browser_tracker_id="T",
        )
        self.assertEqual(strong["process_proof_level"], "strong")
        medium = classify_process_proof({"ok": True, "windows": 2}, owner_key="x")
        self.assertEqual(medium["process_proof_level"], "medium")


class TestCookieVerdict(unittest.TestCase):
    def test_kinds(self) -> None:
        self.assertEqual(cookie_verdict_updates("cleared_ban"), {"manual_status": "", "import_status": ""})
        banned = cookie_verdict_updates("banned", cookie_username="u", cookie_user_id="1")
        self.assertEqual(banned["import_status"], "banned")
        self.assertEqual(cookie_verdict_updates("invalid")["import_status"], "cookie_invalid")
        ident = cookie_verdict_updates("identity", cookie_username="u", mismatch=True)
        self.assertEqual(ident["import_status"], "cookie_mismatch")
        ident2 = cookie_verdict_updates("identity", cookie_username="u", mismatch=False)
        self.assertEqual(ident2["import_status"], "")
        with self.assertRaises(ValueError):
            cookie_verdict_updates("nope")


if __name__ == "__main__":
    unittest.main()
