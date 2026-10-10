"""Table tests for the START readiness decision (no farm/HTTP/network)."""
import unittest
from types import SimpleNamespace

from runtime.start_readiness import blocked_summary, decide_start


def _acc(username, **overrides):
    base = dict(
        username=username,
        _config_username=username,
        cookie_mismatch=False,
        cookie_username=username,
        manual_status="",
        import_status="",
        last_error="",
        last_crash_reason="",
        last_recovery_reason="",
        game_id="",
        place_id="",
        vip_links=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _empty_preflight(cfg, accounts, games, mode, cfg_mgr=None):
    return {"created": [], "failures": [], "skipped_usernames": [], "public_fallback": []}


class _Starter:
    def __init__(self, error=None):
        self.calls = 0
        self.error = error

    def __call__(self):
        self.calls += 1
        if self.error is not None:
            raise self.error


class TestBlockedSummary(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(blocked_summary([]), "")

    def test_captcha(self):
        out = blocked_summary([{"reason": "CAPTCHA hold"}, {"reason": "captcha x"}])
        self.assertIn("CAPTCHA", out)

    def test_cookie(self):
        out = blocked_summary([{"reason": "cookie bad"}])
        self.assertIn("cookie", out)

    def test_mixed(self):
        self.assertEqual(blocked_summary([{"reason": "a"}, {"reason": "b"}]), "2 blocked")


class TestDecideStart(unittest.TestCase):
    def test_no_launchable(self):
        starter = _Starter()
        result = decide_start(
            [_acc("u1", cookie_mismatch=True, cookie_username="someone")],
            {}, "cmd1", starter, run_preflight=_empty_preflight,
        )
        self.assertEqual(result["error_code"], "no_launchable_accounts")
        self.assertEqual(result["blocked_count"], 1)
        self.assertEqual(starter.calls, 0)

    def test_missing_shared_place(self):
        starter = _Starter()
        result = decide_start([_acc("u1")], {}, "cmd1", starter, run_preflight=_empty_preflight)
        self.assertEqual(result["error_code"], "missing_shared_place")
        self.assertEqual(starter.calls, 0)

    def test_missing_launch_target(self):
        starter = _Starter()
        result = decide_start(
            [_acc("u1")], {"game_mode": "per_account"}, "cmd1", starter,
            run_preflight=_empty_preflight,
        )
        self.assertEqual(result["error_code"], "missing_launch_target")
        self.assertEqual(starter.calls, 0)

    def test_preflight_blocked(self):
        starter = _Starter()

        def blocked_pf(cfg, accounts, games, mode, cfg_mgr=None):
            return {"created": [], "failures": [], "skipped_usernames": ["u1"], "public_fallback": []}

        result = decide_start(
            [_acc("u1", place_id="123")],
            {"game_place_id": "123"}, "cmd1", starter, run_preflight=blocked_pf,
        )
        self.assertEqual(result["error_code"], "private_server_preflight_blocked")
        self.assertEqual(starter.calls, 0)

    def test_success_starts_once(self):
        starter = _Starter()
        result = decide_start(
            [_acc("u1", place_id="123"), _acc("u2", place_id="123")],
            {"game_place_id": "123"}, "cmd1", starter, run_preflight=_empty_preflight,
        )
        self.assertTrue(result["ok"] and result["accepted"])
        self.assertEqual(result["launchable_count"], 2)
        self.assertEqual(result["command_id"], "cmd1")
        self.assertEqual(starter.calls, 1)
        self.assertIn("2/2", result["msg"])

    def test_success_with_blocked_and_fallback_msg(self):
        starter = _Starter()

        def fallback_pf(cfg, accounts, games, mode, cfg_mgr=None):
            return {"created": [], "failures": [], "skipped_usernames": [], "public_fallback": ["u1"]}

        result = decide_start(
            [_acc("u1", place_id="123"), _acc("bad", cookie_mismatch=True, cookie_username="x")],
            {"game_place_id": "123"}, "cmd1", starter, run_preflight=fallback_pf,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["blocked_count"], 1)
        self.assertIn("public server", result["msg"])
        self.assertEqual(starter.calls, 1)

    def test_starter_error_propagates(self):
        starter = _Starter(error=RuntimeError("Multi Roblox guard failed: x"))
        with self.assertRaises(RuntimeError):
            decide_start(
                [_acc("u1", place_id="123")],
                {"game_place_id": "123"}, "cmd1", starter, run_preflight=_empty_preflight,
            )
        self.assertEqual(starter.calls, 1)


if __name__ == "__main__":
    unittest.main()
