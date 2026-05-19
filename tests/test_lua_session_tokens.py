import unittest

from services.lua_session_tokens import (
    LuaEventReplayCache,
    issue_lua_session_token,
    validate_lua_session_token,
)


class LuaSessionTokenTests(unittest.TestCase):
    def test_lua_session_token_is_scoped_to_account_session_nonce_and_ttl(self):
        token = issue_lua_session_token(
            "instance-secret",
            account="LuaUnit",
            session_id="session-1",
            launch_nonce="nonce-1",
            ttl_seconds=60,
            now=1000,
        )

        accepted = validate_lua_session_token(
            "instance-secret",
            token,
            account="LuaUnit",
            session_id="session-1",
            launch_nonce="nonce-1",
            now=1030,
        )
        wrong_nonce = validate_lua_session_token(
            "instance-secret",
            token,
            account="LuaUnit",
            session_id="session-1",
            launch_nonce="nonce-2",
            now=1030,
        )
        expired = validate_lua_session_token(
            "instance-secret",
            token,
            account="LuaUnit",
            session_id="session-1",
            launch_nonce="nonce-1",
            now=1061,
        )

        self.assertTrue(accepted.ok)
        self.assertEqual(accepted.account, "LuaUnit")
        self.assertFalse(wrong_nonce.ok)
        self.assertEqual(wrong_nonce.reason, "launch_nonce_mismatch")
        self.assertFalse(expired.ok)
        self.assertEqual(expired.reason, "expired")

    def test_lua_event_replay_cache_rejects_duplicates_and_stale_timestamps(self):
        cache = LuaEventReplayCache(ttl_seconds=30, max_clock_skew_seconds=10, max_events_per_account=8)

        first = cache.check_and_record("LuaUnit", "event-1", 1000, now=1000)
        duplicate = cache.check_and_record("LuaUnit", "event-1", 1001, now=1001)
        stale = cache.check_and_record("LuaUnit", "event-2", 900, now=1001)
        future = cache.check_and_record("LuaUnit", "event-3", 1020, now=1001)

        self.assertTrue(first.ok)
        self.assertFalse(duplicate.ok)
        self.assertEqual(duplicate.reason, "duplicate_event")
        self.assertFalse(stale.ok)
        self.assertEqual(stale.reason, "stale_event")
        self.assertFalse(future.ok)
        self.assertEqual(future.reason, "future_event")


if __name__ == "__main__":
    unittest.main()
