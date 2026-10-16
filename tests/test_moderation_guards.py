"""Moderation guards: pure text predicates, no network."""
import unittest
from types import SimpleNamespace

from services.ban_guard import (
    is_account_banned,
    is_account_suspended,
    is_banned_text,
    is_suspended_text,
)
from services.captcha_guard import is_captcha_text
from services.verify_guard import is_locked_text, is_recovery_text


class TestModerationGuards(unittest.TestCase):
    def test_ban_text(self) -> None:
        self.assertTrue(is_banned_text("Account banned (Roblox)"))
        self.assertTrue(is_banned_text("user is moderated"))
        self.assertTrue(is_banned_text("banned"))
        self.assertFalse(is_banned_text("session terminated for user"))
        self.assertFalse(is_banned_text(""))
        self.assertFalse(is_banned_text(None))

    def test_suspended_not_ban_confusion(self) -> None:
        self.assertTrue(is_suspended_text("Account suspended by Roblox"))
        self.assertFalse(is_suspended_text("Account banned (Roblox)"))
        acc = SimpleNamespace(
            manual_status="Account suspended by Roblox",
            last_error="",
            last_crash_reason="",
            last_recovery_reason="",
            last_state_reason="",
            recovery_status="",
            import_status="suspended",
        )
        self.assertTrue(is_account_suspended(acc))
        suspended_acc = SimpleNamespace(
            manual_status="",
            last_error="",
            last_crash_reason="suspended",
            last_recovery_reason="",
            recovery_status="suspended",
            last_state_reason="",
            import_status="suspended",
        )
        # suspension alone must not read as a permanent ban
        self.assertFalse(is_account_banned(suspended_acc))

    def test_account_banned_markers(self) -> None:
        acc = SimpleNamespace(
            manual_status="Account banned (Roblox)",
            last_error="",
            last_crash_reason="",
            last_recovery_reason="",
            recovery_status="",
            last_state_reason="",
            import_status="banned",
        )
        self.assertTrue(is_account_banned(acc))

    def test_captcha_vs_lock_vs_recovery(self) -> None:
        self.assertTrue(is_captcha_text("please solve captcha puzzle"))
        self.assertTrue(is_captcha_text("prove you're not a bot"))
        self.assertFalse(is_captcha_text("Account banned (Roblox)"))
        self.assertTrue(is_locked_text("verify your identity"))
        self.assertTrue(is_locked_text("สแกนหน้า"))
        self.assertFalse(is_locked_text("please solve captcha puzzle"))
        self.assertTrue(is_recovery_text("passkey required"))
        self.assertFalse(is_recovery_text("Account banned (Roblox)"))


if __name__ == "__main__":
    unittest.main()
