"""Tests for the single-owner recovery policy helpers.

Pure stdlib unittest — no farm, locks, or network needed. Covers the
canonicalization/dedupe seam that every recovery caller now leverages.
"""
import time
import unittest

from runtime.recovery_context import RecoveryAttemptContext
from runtime.recovery_policy import (
    RecoveryDedupeTracker,
    canonical_reason,
    canonical_recovery_reason,
    display_recovery_reason,
    duplicate_signal_key,
    is_game_unavailable_detail,
    policy_for,
)


def _ctx(**overrides):
    base = dict(
        account_id="acc1",
        runtime_generation=7,
        pid=123,
        trigger="fault",
        category="PROCESS_CRASH",
        priority=100,
    )
    base.update(overrides)
    return RecoveryAttemptContext(**base)


class TestCanonicalOwnership(unittest.TestCase):
    def test_alias_map_single_place(self):
        self.assertEqual(canonical_reason("pid_dead"), "process_crash")
        self.assertEqual(canonical_reason("cookie_missing"), "auth_failure")

    def test_specific_crash_survives_process_crash_context(self):
        ctx = _ctx(category="PROCESS_CRASH")
        self.assertEqual(canonical_recovery_reason("pid_dead", ctx), "process_crash")

    def test_category_reconciles_unknown_reason(self):
        ctx = _ctx(category="NETWORK_DISCONNECT")
        self.assertEqual(canonical_recovery_reason("some_new_client_reason", ctx), "network_drop")

    def test_no_context_returns_alias_only(self):
        self.assertEqual(canonical_recovery_reason("pid_dead"), "process_crash")

    def test_display_lua_override(self):
        ctx = _ctx(trigger="lua_wait_timeout")
        self.assertEqual(display_recovery_reason("watchdog_timeout", "watchdog_timeout", "", ctx), "lua_wait_timeout")
        self.assertEqual(
            display_recovery_reason("x", "process_crash", "waiting for lua heartbeat", _ctx(trigger="other")),
            "lua_wait_timeout",
        )

    def test_display_passthrough(self):
        self.assertEqual(display_recovery_reason("x", "process_crash"), "process_crash")

    def test_game_down_never_matches_auth(self):
        self.assertTrue(is_game_unavailable_detail("VIP invite did not expose accessCode (200)"))
        self.assertTrue(is_game_unavailable_detail("Teleport failed: Enum.TeleportResult.Unauthorized"))
        self.assertFalse(is_game_unavailable_detail("cookie invalid .roblosecurity"))
        self.assertFalse(is_game_unavailable_detail("captcha required"))

    def test_policy_buckets(self):
        self.assertEqual(policy_for("pid_dead")["bucket"], "crash")
        self.assertTrue(policy_for("cookie_invalid")["fatal"])

    def test_dedupe_key_canonicalized(self):
        self.assertEqual(
            duplicate_signal_key("a", "fault", "pid_dead", 3),
            duplicate_signal_key("a", "fault", "process_crash", 3),
        )
        self.assertNotEqual(
            duplicate_signal_key("a", "fault", "pid_dead", 3),
            duplicate_signal_key("a", "fault", "pid_dead", 4),
        )


class TestDedupeTracker(unittest.TestCase):
    def test_second_same_signature_ignored(self):
        tracker = RecoveryDedupeTracker(window_seconds=60)
        ctx = _ctx()
        self.assertFalse(tracker.check_and_mark(ctx, now=1000.0)["ignore"])
        ignored = tracker.check_and_mark(ctx, now=1001.0)
        self.assertTrue(ignored["ignore"])
        self.assertEqual(ignored["reason"], "duplicate_recovery_signature")

    def test_higher_priority_breaks_through(self):
        tracker = RecoveryDedupeTracker(window_seconds=60)
        low = _ctx(priority=20)
        high = _ctx(priority=100)
        self.assertFalse(tracker.check_and_mark(low, now=1000.0)["ignore"])
        self.assertFalse(tracker.check_and_mark(high, now=1001.0)["ignore"])

    def test_window_expiry(self):
        tracker = RecoveryDedupeTracker(window_seconds=3)
        ctx = _ctx()
        self.assertFalse(tracker.check_and_mark(ctx, now=1000.0)["ignore"])
        self.assertFalse(tracker.check_and_mark(ctx, now=1020.0)["ignore"])


if __name__ == "__main__":
    unittest.main()
