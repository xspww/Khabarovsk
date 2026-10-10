"""Hardening tests for the Lua helper path (real-use fragility fixes).

Covers the four brittle seams that blinded long sessions / PID-less
executors / VIP servers:
1. Session token TTL + expired-but-session-bound grace.
2. PID-missing acceptance when per-launch session secrets match.
3. VIP/private-server detection parsing.
4. Rendered helper size ceiling + template markers.
"""
import os
import threading
import time
import unittest

from runtime.lua_identity import resolve_lua_account
from runtime.lua_rejoin_events import _lua_session_binding_matches, handle_lua_rejoin_event
from runtime.lua_server_detection import detect_lua_server
from services.lua_session_tokens import (
    DEFAULT_LUA_SESSION_TOKEN_TTL_SECONDS,
    LuaEventReplayCache,
    issue_lua_session_token,
    token_session_binding_matches,
    validate_lua_session_token,
)

SECRET = "test-instance-secret"


class _FakeLockAcc:
    def __init__(self):
        self._lock = threading.RLock()
        self._config_username = "Hero123"
        self.display_name = "Hero123"
        self.username = "Hero123"
        self.cookie_username = ""
        self.alias = ""
        self.user_id = "111"
        self.cookie_user_id = ""
        self.pid = 1234
        self.session_id = "sess-abc"
        self.launch_nonce = "nonce-xyz"
        self.state = None
        self.last_activity_at = 0.0
        self.last_activity_reason = ""
        self.lua_last_event = ""
        self.lua_last_event_at = 0.0
        self.lua_session_id = ""
        self.lua_launch_nonce = ""
        self.teleport_suppress_until = 0.0
        self.process_binding_confidence = 0.0

    def sync_runtime(self, _reason=""):
        return None


class _FakeOrchestrator:
    def __init__(self):
        self.calls = []

    def handle_runtime_signal(self, acc, signal, reason, payload=None):
        self.calls.append((signal, reason))
        return True


class _FakeFarm:
    def __init__(self, acc):
        self._accounts = [acc]
        self._runtime_orchestrator = _FakeOrchestrator()
        self._workers = {}
        self._maintenance = None
        self.pushed = []
        self.bumped = 0

    def _apply_lua_server_detection(self, acc, payload):
        return {}

    def _push_event(self, *args, **kwargs):
        self.pushed.append((args, kwargs))

    def _bump_status_revision(self):
        self.bumped += 1

    def _lua_event_handler_error(self, acc, event_name, error):
        return {"ok": False, "status_code": 500, "accepted": False,
                "event": event_name, "account": acc._config_username,
                "msg": "Event processing failed, retry later"}

    def _set_lua_account_description(self, acc, description):
        return True, description


def _disconnect_payload(**overrides):
    base = {
        "event": "disconnect",
        "username": "Hero123",
        "account": "Hero123",
        "user_id": "111",
        "session_id": "sess-abc",
        "launch_nonce": "nonce-xyz",
        "event_id": "sess-abc:nonce-xyz:disconnect:%d:1:123456" % int(time.time()),
        "ts": str(int(time.time())),
        "place_id": "123",
        "job_id": "job-1",
    }
    base.update(overrides)
    return base


class TestLuaSessionTokenLongSessions(unittest.TestCase):
    def test_default_ttl_covers_real_sessions(self):
        self.assertGreaterEqual(DEFAULT_LUA_SESSION_TOKEN_TTL_SECONDS, 12 * 60 * 60)

    def test_roundtrip_ok(self):
        token = issue_lua_session_token(SECRET, account="Hero123",
                                         session_id="sess-abc", launch_nonce="nonce-xyz")
        result = validate_lua_session_token(SECRET, token, account="Hero123",
                                            session_id="sess-abc", launch_nonce="nonce-xyz")
        self.assertTrue(result.ok)

    def test_expired_but_session_bound_grace(self):
        token = issue_lua_session_token(SECRET, account="Hero123",
                                         session_id="sess-abc", launch_nonce="nonce-xyz",
                                         ttl_seconds=30, now=1000.0)
        expired = validate_lua_session_token(SECRET, token, account="Hero123",
                                             session_id="sess-abc", launch_nonce="nonce-xyz",
                                             now=1000.0 + 3600.0)
        self.assertFalse(expired.ok)
        self.assertEqual(expired.reason, "expired")
        # Same (current) session: grace applies.
        self.assertTrue(token_session_binding_matches(
            SECRET, token, account="Hero123",
            session_id="sess-abc", launch_nonce="nonce-xyz"))
        # Rotated session (relaunch): grace must NOT apply.
        self.assertFalse(token_session_binding_matches(
            SECRET, token, account="Hero123",
            session_id="sess-NEW", launch_nonce="nonce-NEW"))
        # Forged signature: grace must NOT apply.
        self.assertFalse(token_session_binding_matches(
            "wrong-secret", token, account="Hero123",
            session_id="sess-abc", launch_nonce="nonce-xyz"))

    def test_replay_cache_still_rejects_duplicates(self):
        cache = LuaEventReplayCache()
        now = time.time()
        first = cache.check_and_record("Hero123", "evt-1", now, now=now)
        self.assertTrue(first.ok)
        second = cache.check_and_record("Hero123", "evt-1", now, now=now)
        self.assertFalse(second.ok)
        self.assertEqual(second.reason, "duplicate_event")


class TestLuaPidLessExecutors(unittest.TestCase):
    def test_session_binding_matches(self):
        acc = _FakeLockAcc()
        self.assertTrue(_lua_session_binding_matches(
            acc, {"session_id": "sess-abc", "launch_nonce": "nonce-xyz"}))
        self.assertFalse(_lua_session_binding_matches(
            acc, {"session_id": "sess-OTHER", "launch_nonce": "nonce-xyz"}))
        self.assertFalse(_lua_session_binding_matches(
            acc, {"session_id": "sess-abc", "launch_nonce": ""}))

    def test_pid_missing_with_session_binding_is_accepted(self):
        acc = _FakeLockAcc()
        farm = _FakeFarm(acc)
        result = handle_lua_rejoin_event(farm, _disconnect_payload())
        self.assertTrue(result["ok"])
        self.assertTrue(result["accepted"], result)
        self.assertTrue(farm._runtime_orchestrator.calls)

    def test_pid_missing_without_session_binding_is_ignored(self):
        acc = _FakeLockAcc()
        farm = _FakeFarm(acc)
        result = handle_lua_rejoin_event(
            farm, _disconnect_payload(session_id="sess-OTHER", launch_nonce="nonce-OTHER"))
        self.assertTrue(result["ok"])
        self.assertFalse(result["accepted"])
        self.assertEqual(result.get("signal"), "")
        self.assertFalse(farm._runtime_orchestrator.calls)

    def test_pid_mismatch_still_ignored(self):
        acc = _FakeLockAcc()
        farm = _FakeFarm(acc)
        result = handle_lua_rejoin_event(farm, _disconnect_payload(pid="9999"))
        self.assertTrue(result["ok"])
        self.assertFalse(result["accepted"])
        self.assertFalse(farm._runtime_orchestrator.calls)

    def test_identity_resolution_prefers_user_id(self):
        acc = _FakeLockAcc()
        resolution = resolve_lua_account(
            [acc], {"username": "Hero123", "user_id": "111", "pid": "1234"})
        self.assertTrue(resolution.matched)
        self.assertIn("user_id", resolution.match_reason)
        self.assertTrue(resolution.pid_match)


class TestLuaServerDetection(unittest.TestCase):
    def test_private_server_id_is_vip(self):
        detection = detect_lua_server({"private_server_id": "ps-123"})
        self.assertTrue(detection.observed)
        self.assertTrue(detection.is_vip)
        self.assertEqual(detection.server_type, "VIP")

    def test_owner_id_is_vip(self):
        detection = detect_lua_server({"private_server_owner_id": "456"})
        self.assertTrue(detection.is_vip)

    def test_explicit_public(self):
        detection = detect_lua_server({"server_type": "PUBLIC", "is_vip_server": "false"})
        self.assertTrue(detection.observed)
        self.assertFalse(detection.is_vip)

    def test_empty_is_unobserved(self):
        detection = detect_lua_server({})
        self.assertFalse(detection.observed)


class TestLuaTemplateCeiling(unittest.TestCase):
    def _repo_root(self):
        here = os.path.dirname(os.path.abspath(__file__))
        return os.path.dirname(here)

    def test_templates_under_executor_ceiling(self):
        root = self._repo_root()
        markers = {
            os.path.join("lua", "internal", "rejoin_monitor.lua"): "CronusRejoin",
            os.path.join("lua", "internal", "account_status_client.lua"): "CronusAccount",
        }
        for rel, marker in markers.items():
            with open(os.path.join(root, rel), "r", encoding="utf-8") as handle:
                src = handle.read()
            # Executor loadstring ceiling is ~14 KiB on the RENDERED helper.
            # Rendering substitutes token + session + requeue payload (~1.2KB
            # worst case), so raw templates must stay under 13 KiB to keep
            # real rendered output loadable.
            self.assertLess(len(src.encode("utf-8")), 13 * 1024, rel)
            self.assertIn(marker, src)

    def test_vip_probing_present_in_both_templates(self):
        root = self._repo_root()
        for rel in (os.path.join("lua", "internal", "rejoin_monitor.lua"),
                    os.path.join("lua", "internal", "account_status_client.lua")):
            with open(os.path.join(root, rel), "r", encoding="utf-8") as handle:
                src = handle.read()
            self.assertIn("PrivateServerId", src, rel)
            self.assertIn("VIPServerId", src, rel)


class TestLuaSessionAuthGrace(unittest.TestCase):
    """Integration: expired-but-session-bound tokens pass auth (real path)."""

    def setUp(self):
        from types import SimpleNamespace

        from api_routes.lua_routes import _validate_lua_session_auth

        self._validate = _validate_lua_session_auth
        self._counter = 0
        acc = _FakeLockAcc()
        farm = _FakeFarm(acc)
        self.acc = acc
        self.farm = farm
        self.ctx = SimpleNamespace(instance_token=SECRET, farm=farm)

    def _body(self, **overrides):
        self._counter += 1
        base = {
            "event": "disconnect",
            "username": "Hero123",
            "account": "Hero123",
            "user_id": "111",
            "session_id": self.acc.session_id,
            "launch_nonce": self.acc.launch_nonce,
            "event_id": "grace-test:%d:%d" % (int(time.time()), self._counter),
            "ts": str(int(time.time())),
        }
        base.update(overrides)
        return base

    def test_expired_token_with_current_session_passes(self):
        token = issue_lua_session_token(
            SECRET, account="Hero123", session_id="sess-abc",
            launch_nonce="nonce-xyz", ttl_seconds=30, now=1000.0)
        reason = self._validate(self.ctx, self.farm, self._body(), token)
        self.assertEqual(reason, "", reason)

    def test_old_session_token_after_relaunch_rejected(self):
        token = issue_lua_session_token(
            SECRET, account="Hero123", session_id="sess-abc",
            launch_nonce="nonce-xyz", ttl_seconds=30, now=1000.0)
        with self.acc._lock:
            self.acc.session_id = "sess-NEW"
            self.acc.launch_nonce = "nonce-NEW"
        try:
            reason = self._validate(self.ctx, self.farm, self._body(
                session_id="sess-abc", launch_nonce="nonce-xyz"), token)
        finally:
            with self.acc._lock:
                self.acc.session_id = "sess-abc"
                self.acc.launch_nonce = "nonce-xyz"
        self.assertNotEqual(reason, "")

    def test_forged_token_rejected(self):
        token = issue_lua_session_token(
            "attacker-secret", account="Hero123", session_id="sess-abc",
            launch_nonce="nonce-xyz")
        reason = self._validate(self.ctx, self.farm, self._body(), token)
        self.assertNotEqual(reason, "")


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()