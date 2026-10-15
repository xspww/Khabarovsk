"""Status priority regression tests.

Locks the label order the dashboard, auth gate and launch blocker all use:

    Finished > Suspended > Banned > Locked > Recovery > Captcha > Invalid

The bug these guard against: an account-level lock (face/ID or
"verify you are human") used to match the captcha keyword list, so the
worker/reload path reported Captcha while /api/status reported Locked.
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from typing import ClassVar

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import account_launch_block_reason
from services.auth_gate import evaluate_account_auth_gate
from services.ban_guard import (
    BANNED_BLOCK_REASON,
    BANNED_IMPORT_STATUS,
    SUSPENDED_BLOCK_REASON,
    SUSPENDED_IMPORT_STATUS,
    is_account_banned,
    is_account_suspended,
)
from services.captcha_guard import (
    CAPTCHA_BLOCK_REASON,
    CAPTCHA_REASON,
    is_account_captcha_required,
    is_captcha_text,
    is_captcha_window_texts,
)
from services.verify_guard import (
    LOCKED_BLOCK_REASON,
    LOCKED_REASON,
    RECOVERY_BLOCK_REASON,
    RECOVERY_REASON,
    is_account_locked,
    is_account_recovery,
)


def make_account(**over):
    acc = SimpleNamespace(
        username="alice",
        cookie_username="",
        cookie_mismatch=False,
        manual_status="",
        import_status="",
        last_error="",
        last_crash_reason="",
        last_recovery_reason="",
        last_state_reason="",
        recovery_status="",
        finished_at=0.0,
    )
    for key, value in over.items():
        setattr(acc, key, value)
    return acc


def hold(**over):
    base = {
        "manual_status": "",
        "import_status": "",
        "last_error": "",
        "last_crash_reason": "",
        "last_recovery_reason": "",
        "last_state_reason": "",
        "recovery_status": "",
    }
    base.update(over)
    return make_account(**base)


class CaptchaKeywordTests(unittest.TestCase):
    def test_real_captcha_is_captcha(self):
        self.assertTrue(is_captcha_text("HTTP 403 arkose captcha challenge"))
        self.assertTrue(is_captcha_text("security challenge required"))
        self.assertTrue(is_captcha_text("robot check"))
        # The Roblox player popup uses its own wording; window-level matching
        # (is_captcha_window_texts) covers it without widening the API list.
        self.assertTrue(is_captcha_window_texts(["please solve this challenge"]))

    def test_locked_screen_is_not_captcha(self):
        # "verify you are human" is the suspicious-activity lock screen, and
        # LOCKED_BLOCK_REASON itself contains "verification required".
        self.assertFalse(is_captcha_text("prove you are human - account locked"))
        self.assertFalse(is_captcha_text(LOCKED_BLOCK_REASON))

    def test_recovery_screen_is_not_captcha(self):
        self.assertFalse(is_captcha_text("sign in with passkey"))

    def test_captcha_challenge_header_still_wins(self):
        self.assertTrue(is_captcha_text("", headers={"rblx-challenge-type": "captcha"}))


class AccountFlagTests(unittest.TestCase):
    def test_locked_hold_flags_only_locked(self):
        acc = hold(manual_status=LOCKED_BLOCK_REASON, import_status=LOCKED_REASON,
                   last_crash_reason=LOCKED_REASON, last_recovery_reason=LOCKED_REASON)
        self.assertTrue(is_account_locked(acc))
        self.assertFalse(is_account_captcha_required(acc))
        self.assertFalse(is_account_recovery(acc))
        self.assertFalse(is_account_banned(acc))

    def test_recovery_hold_flags_only_recovery(self):
        acc = hold(manual_status=RECOVERY_BLOCK_REASON, import_status=RECOVERY_REASON,
                   last_crash_reason=RECOVERY_REASON)
        self.assertTrue(is_account_recovery(acc))
        self.assertFalse(is_account_locked(acc))
        self.assertFalse(is_account_captcha_required(acc))

    def test_captcha_hold_flags_only_captcha(self):
        acc = hold(manual_status=CAPTCHA_BLOCK_REASON, import_status=CAPTCHA_REASON,
                   last_crash_reason=CAPTCHA_REASON)
        self.assertTrue(is_account_captcha_required(acc))
        self.assertFalse(is_account_locked(acc))
        self.assertFalse(is_account_recovery(acc))

    def test_suspended_is_not_banned(self):
        acc = hold(manual_status=SUSPENDED_BLOCK_REASON, import_status=SUSPENDED_IMPORT_STATUS)
        self.assertTrue(is_account_suspended(acc))
        self.assertTrue(is_account_banned(acc))  # blocks the launch like a ban


class AuthGatePriorityTests(unittest.TestCase):
    def test_locked_outranks_captcha(self):
        acc = hold(manual_status=LOCKED_BLOCK_REASON, import_status=LOCKED_REASON,
                   last_crash_reason=LOCKED_REASON,
                   last_error="prove you are human - captcha challenge detected")
        decision = evaluate_account_auth_gate(acc)
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.category, "locked")
        self.assertEqual(decision.reason_key, LOCKED_REASON)

    def test_recovery_outranks_captcha(self):
        acc = hold(manual_status=RECOVERY_BLOCK_REASON, import_status=RECOVERY_REASON,
                   last_crash_reason=RECOVERY_REASON, last_error="captcha challenge")
        self.assertEqual(evaluate_account_auth_gate(acc).category, "account_recovery")

    def test_banned_outranks_locked(self):
        acc = hold(manual_status=BANNED_BLOCK_REASON, import_status=BANNED_IMPORT_STATUS,
                   last_crash_reason="banned", last_error="account locked - face scan")
        self.assertEqual(evaluate_account_auth_gate(acc).category, "banned")

    def test_suspended_outranks_banned(self):
        acc = hold(manual_status=SUSPENDED_BLOCK_REASON, import_status=SUSPENDED_IMPORT_STATUS,
                   last_crash_reason="suspended")
        self.assertEqual(evaluate_account_auth_gate(acc).category, "suspended")

    def test_pure_captcha_stays_captcha(self):
        acc = hold(manual_status=CAPTCHA_BLOCK_REASON, import_status=CAPTCHA_REASON,
                   last_crash_reason=CAPTCHA_REASON)
        self.assertEqual(evaluate_account_auth_gate(acc).category, "captcha")

    def test_invalid_cookie_reports_auth(self):
        acc = hold(import_status="cookie_invalid", last_error="cookie expired")
        decision = evaluate_account_auth_gate(acc)
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason_key, "cookie_invalid")

    def test_healthy_account_launchable(self):
        self.assertFalse(evaluate_account_auth_gate(hold()).blocked)


class BlockReasonTests(unittest.TestCase):
    def test_locked_block_reason(self):
        acc = hold(manual_status=LOCKED_BLOCK_REASON, import_status=LOCKED_REASON)
        self.assertEqual(account_launch_block_reason(acc), LOCKED_BLOCK_REASON)

    def test_recovery_block_reason(self):
        acc = hold(manual_status=RECOVERY_BLOCK_REASON, import_status=RECOVERY_REASON)
        self.assertEqual(account_launch_block_reason(acc), RECOVERY_BLOCK_REASON)

    def test_suspended_block_reason(self):
        acc = hold(manual_status=SUSPENDED_BLOCK_REASON, import_status=SUSPENDED_IMPORT_STATUS)
        self.assertEqual(account_launch_block_reason(acc), SUSPENDED_BLOCK_REASON)

    def test_invalid_block_reason(self):
        acc = hold(import_status="cookie_invalid")
        self.assertIn("Invalid Cookie", account_launch_block_reason(acc))


class RecordsApiTests(unittest.TestCase):
    """The records endpoint feeds the dashboard pills."""

    def _records(self):
        from api_routes.account_records import account_data_api_records

        class FakeStore:
            def read_records(self, include_cookies=False):
                return [
                    {"username": "susp1", "manual_status": SUSPENDED_BLOCK_REASON,
                     "import_status": SUSPENDED_IMPORT_STATUS},
                    {"username": "lock1", "manual_status": LOCKED_BLOCK_REASON,
                     "import_status": LOCKED_REASON},
                    {"username": "rec1", "manual_status": RECOVERY_BLOCK_REASON,
                     "import_status": RECOVERY_REASON},
                    {"username": "cap1", "manual_status": CAPTCHA_BLOCK_REASON,
                     "import_status": CAPTCHA_REASON},
                    {"username": "inv1", "import_status": "cookie_invalid"},
                ]

            def to_api_record(self, record):
                return dict(record)

        class FakeFarm:
            _accounts: ClassVar[list] = []

        rows = account_data_api_records(FakeStore(), FakeFarm())
        return {row["username"]: row for row in rows}

    def test_locked_row_is_blocked_as_locked(self):
        row = self._records()["lock1"]
        self.assertEqual(row["blocked_reason"], LOCKED_BLOCK_REASON)
        self.assertFalse(row["launchable"])

    def test_recovery_row_is_blocked_as_recovery(self):
        row = self._records()["rec1"]
        self.assertEqual(row["blocked_reason"], RECOVERY_BLOCK_REASON)
        self.assertFalse(row["launchable"])

    def test_suspended_row_is_blocked_as_suspended(self):
        row = self._records()["susp1"]
        self.assertEqual(row["blocked_reason"], SUSPENDED_BLOCK_REASON)
        self.assertFalse(row["launchable"])

    def test_captcha_row_is_blocked_as_captcha(self):
        row = self._records()["cap1"]
        self.assertEqual(row["blocked_reason"], CAPTCHA_BLOCK_REASON)

    def test_invalid_row_is_blocked_as_invalid(self):
        row = self._records()["inv1"]
        self.assertIn("Invalid Cookie", row["blocked_reason"])
        self.assertFalse(row["launchable"])


class HoldIsolationTests(unittest.TestCase):
    """Clearing one hold must never wipe another that coexists on the account."""

    def test_clear_captcha_keeps_locked_hold(self):
        import services.captcha_guard as captcha_guard

        acc = hold(manual_status=LOCKED_BLOCK_REASON, import_status=LOCKED_REASON,
                   last_crash_reason=LOCKED_REASON, last_recovery_reason=LOCKED_REASON)
        acc._config_username = "alice"
        calls = []
        original = captcha_guard.persist_account_captcha_status
        captcha_guard.persist_account_captcha_status = lambda a, active=True: calls.append(active)
        try:
            self.assertFalse(captcha_guard.clear_account_captcha_hold(acc))
        finally:
            captcha_guard.persist_account_captcha_status = original
        self.assertEqual(calls, [])
        self.assertTrue(is_account_locked(acc))

    def test_set_locked_hold_syncs_import_status(self):
        import services.verify_guard as verify_guard

        verify_guard.persist_account_locked_status = lambda a, active=True: None
        try:
            acc = hold(import_status="cookie_invalid")
            verify_guard.set_account_locked_hold(acc, "face scan needed", source="test")
            self.assertEqual(acc.import_status, LOCKED_REASON)
            self.assertTrue(is_account_locked(acc))
            self.assertFalse(is_account_captcha_required(acc))
        finally:
            del verify_guard.persist_account_locked_status

    def test_clear_locked_hold_reports_it_was_locked(self):
        import services.verify_guard as verify_guard

        verify_guard.persist_account_locked_status = lambda a, active=True: None
        try:
            acc = hold(manual_status=LOCKED_BLOCK_REASON, import_status=LOCKED_REASON,
                       last_crash_reason=LOCKED_REASON, last_recovery_reason=LOCKED_REASON)
            self.assertTrue(verify_guard.clear_account_locked_hold(acc))
            self.assertFalse(is_account_locked(acc))
        finally:
            del verify_guard.persist_account_locked_status


def _preview_handler():
    """Build the real POST /api/test-cookie handler with a stub app."""
    import api_routes.accounts_routes as routes

    class FakeApp:
        def __init__(self):
            self.routes = {}

        def post(self, path):
            def deco(fn):
                self.routes["POST " + path] = fn
                return fn

            return deco

        def get(self, path):
            def deco(fn):
                self.routes["GET " + path] = fn
                return fn

            return deco

    app = FakeApp()
    farm = SimpleNamespace(_accounts=[])
    cfg_mgr = SimpleNamespace(snapshot=dict, get=lambda *a, **k: None)
    routes.register(app, SimpleNamespace(cfg_mgr=cfg_mgr, farm=farm))
    return app.routes["POST /api/test-cookie"]


def _run_test_cookie_preview(body):
    import asyncio

    class FakeRequest:
        async def json(self):
            return dict(body)

    return asyncio.new_event_loop().run_until_complete(
        _preview_handler()(FakeRequest())
    )


class PreviewApiTests(unittest.TestCase):
    """The import-preview box (DETECTED row) must agree with the import path.

    Regression: a 403 "User is moderated" cookie previewed as Banned even
    with a username hint, while importing the same line marked it Locked.
    """

    def _run_preview(self, body):
        return _run_test_cookie_preview(body)

    def test_moderated_with_username_previews_locked(self):
        import api_routes.accounts_routes as routes
        import services.roblox_identity as roblox_identity

        routes.validate_cookie_details = lambda cookie: (
            False, "", 'HTTP 403 {"errors":[{"code":0,"message":"User is moderated"}]}',
            {"username": "", "user_id": "", "is_banned": True, "banned": True},
        )
        roblox_identity.resolve_moderated_dead_cookie = lambda *a, **k: (
            "locked", LOCKED_BLOCK_REASON, "x1MiuytAi0", "4373927375",
        )
        try:
            result = self._run_preview({
                "cookie": "dead-cookie",
                "username": "x1MiuytAi0",
                "password": "",
            })
        finally:
            import importlib
            importlib.reload(routes)
        self.assertTrue(result.get("locked"), result)
        self.assertFalse(result.get("banned"), result)
        self.assertEqual(result.get("username"), "x1MiuytAi0")

    def test_confirmed_ban_still_previews_banned(self):
        import api_routes.accounts_routes as routes
        import services.roblox_identity as roblox_identity

        routes.validate_cookie_details = lambda cookie: (
            False, "", 'HTTP 403 {"errors":[{"code":0,"message":"User is moderated"}]}',
            {"username": "", "user_id": "", "is_banned": True, "banned": True},
        )
        roblox_identity.resolve_moderated_dead_cookie = lambda *a, **k: (
            "banned", "Account banned (Roblox isBanned=true)", "someuser", "123",
        )
        try:
            result = self._run_preview({
                "cookie": "dead-cookie",
                "username": "someuser",
                "password": "",
            })
        finally:
            import importlib
            importlib.reload(routes)
        self.assertTrue(result.get("banned"), result)
        self.assertFalse(result.get("locked"), result)


def _synthetic_cookie(name="x1MiuytAi0", uid="4373927375"):
    """Build a cookie token with Roblox-style embedded uname/uid fields.

    Avoids pasting real tokens in tests; the payload layout mirrors a real
    `.ROBLOSECURITY` blob (protobuf fields duid/uname/uid).
    """
    import base64

    raw = (
        b"\x1a\x06\x08\x02\x10\x04"
        b"\x22\x14\x0a\x04duid\x12\x0e14153082429918207610"
        b"\x22\x0f\x0a\x05uname\x12\x0a" + name.encode("ascii")
        + b"\x22\x0f\x0a\x03uid\x12\x0a" + uid.encode("ascii")
    )
    return "_|WARNING:x.|_" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


class EmbeddedIdentityTests(unittest.TestCase):
    """Cookie-only pastes (no username: prefix) must still resolve the owner.

    The token embeds uname/uid, so detection works with zero typing.
    """

    def test_reads_name_and_uid(self):
        from services.roblox_identity import cookie_embedded_identity

        self.assertEqual(
            cookie_embedded_identity(_synthetic_cookie()),
            ("x1MiuytAi0", "4373927375"),
        )

    def test_ignores_device_id(self):
        # `duid` also contains "uid" — the device id must never win.
        from services.roblox_identity import cookie_embedded_identity

        _name, uid = cookie_embedded_identity(_synthetic_cookie())
        self.assertEqual(uid, "4373927375")
        self.assertNotEqual(uid, "14153082429918207610")

    def test_rejects_garbage(self):
        from services.roblox_identity import cookie_embedded_identity

        self.assertEqual(cookie_embedded_identity(""), ("", ""))
        self.assertEqual(cookie_embedded_identity("not-a-cookie"), ("", ""))
        self.assertEqual(cookie_embedded_identity("_|WARNING:x.|_QUJD"), ("", ""))

    def test_resolve_uses_embedded_name_without_hint(self):
        import services.roblox_identity as roblox_identity
        import services.roblox_moderation as roblox_moderation

        original_store = roblox_identity.username_for_cookie_in_store
        original_ban = roblox_moderation.fetch_ban_status_by_username
        roblox_identity.username_for_cookie_in_store = lambda cookie: ""
        roblox_moderation.fetch_ban_status_by_username = lambda name, timeout=8.0: (
            (False, "4373927375", "") if name == "x1MiuytAi0" else (None, "", "")
        )
        try:
            verdict, _detail, name, uid = roblox_identity.resolve_moderated_dead_cookie(
                _synthetic_cookie(), ""
            )
        finally:
            roblox_identity.username_for_cookie_in_store = original_store
            roblox_moderation.fetch_ban_status_by_username = original_ban
        self.assertEqual(verdict, "locked")
        self.assertEqual(name, "x1MiuytAi0")
        self.assertEqual(uid, "4373927375")

    def test_preview_cookie_only_moderated_shows_locked(self):
        import api_routes.accounts_routes as routes
        import services.roblox_identity as roblox_identity

        seen = {}
        routes.validate_cookie_details = lambda cookie: (
            False,
            "",
            'HTTP 403 {"errors":[{"code":0,"message":"User is moderated"}]}',
            {"username": "", "user_id": "", "is_banned": True, "banned": True},
        )

        def fake_resolve(cookie, hint="", timeout=8.0):
            seen["hint"] = hint
            return ("locked", LOCKED_BLOCK_REASON, "x1MiuytAi0", "4373927375")

        roblox_identity.resolve_moderated_dead_cookie = fake_resolve
        try:
            result = _run_test_cookie_preview({"cookie": _synthetic_cookie(), "username": "", "password": ""})
        finally:
            import importlib
            importlib.reload(routes)
        self.assertTrue(result.get("locked"), result)
        self.assertFalse(result.get("banned"), result)
        self.assertEqual(result.get("username"), "x1MiuytAi0")

    def test_import_cookie_only_moderated_marks_locked(self):
        import services.roblox_identity as roblox_identity
        from services.account_hybrid import AccountDataStore

        original_resolve = roblox_identity.resolve_moderated_dead_cookie
        roblox_identity.resolve_moderated_dead_cookie = lambda *a, **k: (
            "locked", LOCKED_BLOCK_REASON, "x1MiuytAi0", "4373927375",
        )
        store = AccountDataStore.__new__(AccountDataStore)
        captured = {}

        def fake_upsert(records):
            captured["records"] = list(records)
            return len(records), []

        store.upsert_records = fake_upsert
        validator = lambda cookie: (
            False, "", 'HTTP 403 {"errors":[{"code":0,"message":"User is moderated"}]}', {},
        )
        try:
            result = store.import_cookie_lines([_synthetic_cookie()], validator=validator)
        finally:
            roblox_identity.resolve_moderated_dead_cookie = original_resolve
        self.assertEqual(result.get("locked"), 1)
        record = captured["records"][0]
        self.assertEqual(record["username"], "x1MiuytAi0")
        self.assertEqual(record["import_status"], LOCKED_REASON)


if __name__ == "__main__":
    unittest.main()
