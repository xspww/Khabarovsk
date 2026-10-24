"""Tests for the single-owner recovery policy helpers.

Pure stdlib unittest — no farm, locks, or network needed. Covers the
canonicalization/dedupe seam that every recovery caller now leverages.
"""
import unittest

from runtime.recovery_context import RecoveryAttemptContext
from runtime.recovery_policy import (
    RecoveryDedupeTracker,
    RecoveryGate,
    canonical_launch_failure_reason,
    canonical_reason,
    canonical_recovery_reason,
    display_recovery_reason,
    duplicate_signal_key,
    is_game_unavailable_detail,
    is_transient_recovery_mark,
    policy_for,
)


def _ctx(**overrides):
    base = {
        "account_id": "acc1",
        "runtime_generation": 7,
        "pid": 123,
        "trigger": "fault",
        "category": "PROCESS_CRASH",
        "priority": 100,
    }
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

    def test_generic_teleport_failure_is_not_game_down(self):
        # A network-caused teleport failure used to be classified as
        # game_unavailable, which made the dashboard say "Game Close" while
        # the account was actually offline.
        for detail in (
            "Teleport failed: Enum.TeleportResult.Failure",
            "Teleport failed: Enum.TeleportResult.Error",
            "Teleport failed: Enum.TeleportResult.HttpError",
        ):
            with self.subTest(detail=detail):
                self.assertFalse(is_game_unavailable_detail(detail))
                self.assertNotEqual(
                    canonical_launch_failure_reason(detail),
                    "game_unavailable",
                )

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


class _FakeOwner:
    def __init__(self, blocked=False):
        self._blocked = blocked

    def block_reason(self, account_key, ctx):
        if self._blocked:
            return {
                "blocked": True,
                "ignore": "lower_priority_than_active_recovery",
                "active_reason": "process_crash",
                "active_priority": 100,
                "incoming_priority": ctx.priority,
            }
        return {"blocked": False}


class _FakeDedupe:
    def __init__(self, ignore=False):
        self._ignore = ignore
        self.calls = 0

    def check_and_mark(self, ctx, now=None):
        self.calls += 1
        if self._ignore:
            return {
                "ignore": True,
                "reason": "duplicate_recovery_signature",
                "signature": "|".join(str(part) for part in ctx.signature),
                "priority": ctx.priority,
                "previous_priority": ctx.priority,
                "age": "0.00",
            }
        return {"ignore": False}


def _gate(owner_blocked=False, dedupe_ignore=False):
    return RecoveryGate(
        owner=_FakeOwner(blocked=owner_blocked),
        dedupe=_FakeDedupe(ignore=dedupe_ignore),
        duplicate_window=8.0,
    )


class TestRecoveryGate(unittest.TestCase):
    def test_fresh_signal_passes(self):
        verdict = _gate().check(
            account_key="acc1", ctx=_ctx(), signal_name="fault",
            reason_key="process_crash", recovery_generation=1, now=1000.0,
        )
        self.assertFalse(verdict["ignored"])

    def test_owner_block_wins_first(self):
        gate = _gate(owner_blocked=True, dedupe_ignore=True)
        verdict = gate.check(
            account_key="acc1", ctx=_ctx(), signal_name="fault",
            reason_key="process_crash", recovery_generation=1, now=1000.0,
        )
        self.assertTrue(verdict["ignored"])
        self.assertEqual(verdict["event"], "recovery_ignored")
        self.assertEqual(
            verdict["fields"]["ignore"], "lower_priority_than_active_recovery"
        )
        self.assertEqual(gate._dedupe.calls, 0)

    def test_signature_dedupe_second(self):
        gate = _gate(dedupe_ignore=True)
        verdict = gate.check(
            account_key="acc1", ctx=_ctx(), signal_name="fault",
            reason_key="process_crash", recovery_generation=1, now=1000.0,
        )
        self.assertTrue(verdict["ignored"])
        self.assertEqual(verdict["event"], "recovery_ignored")
        # "reason" stays out of log fields (positional reason_key owns it),
        # and keys overlapping the context ("signature", "priority") stay
        # out too — the surviving fields match the old per-stage merge.
        self.assertNotIn("reason", verdict["fields"])
        self.assertIn("previous_priority", verdict["fields"])
        self.assertIn("age", verdict["fields"])

    def test_signal_suppress_same_key(self):
        gate = _gate()
        first = gate.check(
            account_key="acc1", ctx=_ctx(), signal_name="fault",
            reason_key="pid_dead", recovery_generation=1, now=1000.0,
        )
        self.assertFalse(first["ignored"])
        second = gate.check(
            account_key="acc1", ctx=_ctx(), signal_name="fault",
            reason_key="process_crash", recovery_generation=1, now=1001.0,
        )
        self.assertTrue(second["ignored"])
        self.assertEqual(second["event"], "recovery_duplicate_suppressed")
        self.assertIn("age", second["fields"])

    def test_signal_suppress_canonical_key(self):
        gate = _gate()
        gate.check(
            account_key="acc1", ctx=_ctx(), signal_name="fault",
            reason_key="pid_dead", recovery_generation=1, now=1000.0,
        )
        other_reason = gate.check(
            account_key="acc1", ctx=_ctx(), signal_name="fault",
            reason_key="network_drop", recovery_generation=1, now=1001.0,
        )
        self.assertFalse(other_reason["ignored"])

    def test_signal_suppress_window_expiry(self):
        gate = _gate()
        gate.check(
            account_key="acc1", ctx=_ctx(), signal_name="fault",
            reason_key="pid_dead", recovery_generation=1, now=1000.0,
        )
        later = gate.check(
            account_key="acc1", ctx=_ctx(), signal_name="fault",
            reason_key="pid_dead", recovery_generation=1, now=1010.0,
        )
        self.assertFalse(later["ignored"])

    def test_clear_resets_signal_map(self):
        gate = _gate()
        gate.check(
            account_key="acc1", ctx=_ctx(), signal_name="fault",
            reason_key="pid_dead", recovery_generation=1, now=1000.0,
        )
        gate.clear()
        again = gate.check(
            account_key="acc1", ctx=_ctx(), signal_name="fault",
            reason_key="pid_dead", recovery_generation=1, now=1001.0,
        )
        self.assertFalse(again["ignored"])

    def test_real_owner_and_tracker(self):
        from runtime.recovery_owner import RecoveryOwnerRegistry

        owner = RecoveryOwnerRegistry()
        owner.acquire(
            "acc1", runtime_generation=7, recovery_generation=1,
            command_generation=1, session_id="s", transaction_id="t",
            reason="process_crash", status="recovering", bucket="crash",
            priority=100,
        )
        gate = RecoveryGate(
            owner=owner,
            dedupe=RecoveryDedupeTracker(window_seconds=60),
            duplicate_window=8.0,
        )
        low = gate.check(
            account_key="acc1", ctx=_ctx(priority=20), signal_name="fault",
            reason_key="network_drop", recovery_generation=1, now=1000.0,
        )
        self.assertTrue(low["ignored"])
        self.assertEqual(
            low["fields"]["ignore"], "lower_priority_than_active_recovery"
        )


class TestLaunchFailureClassification(unittest.TestCase):
    def test_captcha_first(self):
        self.assertEqual(
            canonical_launch_failure_reason("captcha required, please solve"),
            "captcha_required",
        )

    def test_game_down_before_auth_substring(self):
        self.assertEqual(
            canonical_launch_failure_reason("Teleport failed: Enum.TeleportResult.Unauthorized"),
            "game_unavailable",
        )
        self.assertEqual(
            canonical_launch_failure_reason("VIP invite did not expose accessCode (200)"),
            "game_unavailable",
        )

    def test_server_full(self):
        self.assertEqual(
            canonical_launch_failure_reason("This experience is full"),
            "server_full",
        )

    def test_auth_failure(self):
        self.assertEqual(
            canonical_launch_failure_reason("cookie invalid .roblosecurity"),
            "auth_failure",
        )

    def test_watchdog_timeout(self):
        self.assertEqual(
            canonical_launch_failure_reason("verify_timeout waiting for pid"),
            "watchdog_timeout",
        )
        self.assertEqual(
            canonical_launch_failure_reason("pid not detected after launch"),
            "watchdog_timeout",
        )

    def test_fallback_launch_fail(self):
        self.assertEqual(
            canonical_launch_failure_reason("some weird launcher error"),
            "launch_fail",
        )


class TestTransientRecoveryMark(unittest.TestCase):
    def test_transient_marks(self):
        self.assertTrue(is_transient_recovery_mark("launch_fail", ""))
        self.assertTrue(is_transient_recovery_mark("", "game_unavailable"))
        self.assertTrue(is_transient_recovery_mark("teleport failed: unauthorized", ""))

    def test_non_transient(self):
        self.assertFalse(is_transient_recovery_mark("process_crash", "session_retry"))
        self.assertFalse(is_transient_recovery_mark("", ""))


if __name__ == "__main__":
    unittest.main()
