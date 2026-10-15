"""Tests for the single-owner cookie-verdict mapping (pure, no store)."""
import unittest

from services.roblox_validation import cookie_verdict_updates, persist_cookie_verdict


class TestCookieVerdictUpdates(unittest.TestCase):
    def test_cleared_ban(self):
        self.assertEqual(
            cookie_verdict_updates("cleared_ban"),
            {"manual_status": "", "import_status": ""},
        )

    def test_banned_carries_identity(self):
        updates = cookie_verdict_updates("banned", cookie_username="u", cookie_user_id="1")
        self.assertEqual(updates["import_status"], "banned")
        self.assertEqual(updates["cookie_username"], "u")
        self.assertEqual(updates["cookie_user_id"], "1")
        self.assertFalse(updates["cookie_mismatch"])

    def test_locked_recovery_captcha_invalid(self):
        self.assertEqual(cookie_verdict_updates("locked")["import_status"], "identity_locked")
        self.assertEqual(cookie_verdict_updates("recovery")["import_status"], "account_recovery")
        captcha = cookie_verdict_updates("captcha")
        self.assertTrue(captcha["manual_status"])
        self.assertEqual(
            cookie_verdict_updates("invalid"),
            {"cookie_mismatch": True, "import_status": "cookie_invalid"},
        )

    def test_identity_mismatch_flag(self):
        ok = cookie_verdict_updates("identity", cookie_username="a", cookie_user_id="1", mismatch=False)
        self.assertFalse(ok["cookie_mismatch"])
        self.assertEqual(ok["import_status"], "")
        bad = cookie_verdict_updates("identity", cookie_username="a", cookie_user_id="2", mismatch=True)
        self.assertTrue(bad["cookie_mismatch"])
        self.assertEqual(bad["import_status"], "cookie_mismatch")

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            cookie_verdict_updates("nope")

    def test_persist_noop_on_empty_username(self):
        self.assertFalse(persist_cookie_verdict("", "locked"))
        self.assertFalse(persist_cookie_verdict("   ", "locked"))


if __name__ == "__main__":
    unittest.main()
