"""Unit tests for Manual Login (no browser / network)."""
import time
import unittest

from services.manual_login_service import (
    ManualLoginManager,
    ManualLoginUnavailable,
    parse_userpass_entries,
)


class FakeStore:
    def __init__(self, validator):
        self.validator = validator
        self.imported_lines = []

    def import_cookie_lines(self, lines, validator=None):
        import re as _re

        check = validator or self.validator
        imported = 0
        errors = []
        for line in lines:
            self.imported_lines.append(line)
            match = _re.search(r"_\|WARNING:[^\s'\"<>]+", str(line))
            cookie = match.group(0) if match else ""
            ok, _user, detail, _meta = check(cookie)
            if ok:
                imported += 1
            else:
                errors.append(detail)
        return {"ok": True, "imported": imported, "errors": errors, "count": imported}


def _ok_validator(cookie):
    if str(cookie).startswith("_|WARNING:"):
        return True, "someuser", "", {"user_id": "1", "username": "someuser"}
    return False, "", "invalid cookie", {}




def _noop_check(username, password, timeout=12.0):
    return {"outcome": "unknown", "detail": "", "fresh_cookie": ""}

def _wait_state(manager, session_id, timeout_s=5.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = manager.public_status(session_id)
        if status and status["state"] in {"done", "cancelled", "error"}:
            return status
        time.sleep(0.05)
    return manager.public_status(session_id)


class TestParseUserpassEntries(unittest.TestCase):
    def test_basic(self):
        entries = parse_userpass_entries("user1:pass1\nuser2:pass2")
        self.assertEqual(entries, [
            {"username": "user1", "password": "pass1"},
            {"username": "user2", "password": "pass2"},
        ])

    def test_skips_blanks_and_non_pairs(self):
        entries = parse_userpass_entries("\nuser1:pass1\n  \nnotaline\n:useronly\n")
        self.assertEqual(entries, [{"username": "user1", "password": "pass1"}])

    def test_password_may_contain_colon(self):
        entries = parse_userpass_entries("user1:pa:ss:1")
        self.assertEqual(entries, [{"username": "user1", "password": "pa:ss:1"}])

    def test_empty(self):
        self.assertEqual(parse_userpass_entries(""), [])
        self.assertEqual(parse_userpass_entries(None), [])


class TestManualLoginManager(unittest.TestCase):
    def test_single_success_imports(self):
        seen = {}

        def fake_login(username_hint="", password="", timeout_s=0, cancel_event=None, on_note=None):
            seen["password"] = password
            return "_|WARNING:cookie1"

        manager = ManualLoginManager(
            store=FakeStore(_ok_validator),
            validator=_ok_validator,
            on_imported=lambda: seen.setdefault("refreshed", True),
            browser_login=fake_login,
            credential_check=_noop_check,
        )
        session = manager.start_single(username="user1", password="pass1")
        status = _wait_state(manager, session.session_id)
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["done"], 1)
        self.assertTrue(seen.get("refreshed"))
        # Password reached the login page but is wiped afterwards.
        self.assertEqual(seen.get("password"), "pass1")
        internal = manager.get_session(session.session_id)
        self.assertEqual(internal.entries[0].password, "")

    def test_public_status_never_leaks_password(self):
        manager = ManualLoginManager(
            store=FakeStore(_ok_validator),
            validator=_ok_validator,
            browser_login=lambda **kwargs: "_|WARNING:cookie1",
            credential_check=_noop_check,
        )
        session = manager.start_single(username="user1", password="s3cr3t")
        status = _wait_state(manager, session.session_id)
        blob = repr(status)
        self.assertNotIn("s3cr3t", blob)
        self.assertNotIn("password", blob.lower())

    def test_multi_runs_sequentially(self):
        order = []

        def fake_login(username_hint="", password="", timeout_s=0, cancel_event=None, on_note=None):
            order.append(username_hint)
            return "_|WARNING:cookie-" + username_hint

        manager = ManualLoginManager(
            store=FakeStore(_ok_validator),
            validator=_ok_validator,
            browser_login=fake_login,
            credential_check=_noop_check,
        )
        session = manager.start_multi([
            {"username": "user1", "password": "p1"},
            {"username": "user2", "password": "p2"},
        ])
        status = _wait_state(manager, session.session_id)
        self.assertEqual(status["state"], "done")
        self.assertEqual(order, ["user1", "user2"])
        self.assertEqual(status["done"], 2)

    def test_failed_entry_does_not_stop_queue(self):
        def fake_login(username_hint="", password="", timeout_s=0, cancel_event=None, on_note=None):
            if username_hint == "bad":
                raise TimeoutError("Timed out waiting for sign-in.")
            return "_|WARNING:cookie-ok"

        manager = ManualLoginManager(
            store=FakeStore(_ok_validator),
            validator=_ok_validator,
            browser_login=fake_login,
            credential_check=_noop_check,
        )
        session = manager.start_multi([
            {"username": "bad", "password": "p"},
            {"username": "good", "password": "p"},
        ])
        status = _wait_state(manager, session.session_id)
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["done"], 1)
        self.assertEqual(status["failed"], 1)

    def test_unavailable_browser_marks_error(self):
        def fake_login(**kwargs):
            raise ManualLoginUnavailable("no playwright")

        manager = ManualLoginManager(
            store=FakeStore(_ok_validator),
            validator=_ok_validator,
            browser_login=fake_login,
            credential_check=_noop_check,
        )
        session = manager.start_single()
        status = _wait_state(manager, session.session_id)
        self.assertEqual(status["state"], "error")

    def test_cancel_stops_queue(self):
        def fake_login(username_hint="", password="", timeout_s=0, cancel_event=None, on_note=None):
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("Cancelled.")
            if username_hint == "user1":
                cancel_event.wait(5.0)
                raise RuntimeError("Cancelled.")
            return "_|WARNING:cookie-" + username_hint

        manager = ManualLoginManager(
            store=FakeStore(_ok_validator),
            validator=_ok_validator,
            browser_login=fake_login,
            credential_check=_noop_check,
        )
        session = manager.start_multi([
            {"username": "user1", "password": "p1"},
            {"username": "user2", "password": "p2"},
        ])
        time.sleep(0.2)
        self.assertTrue(manager.cancel(session.session_id))
        status = _wait_state(manager, session.session_id)
        self.assertEqual(status["state"], "cancelled")
        self.assertEqual(status["done"], 0)

    def test_start_multi_rejects_empty(self):
        manager = ManualLoginManager(
            store=FakeStore(_ok_validator),
            validator=_ok_validator,
            browser_login=lambda **kwargs: "_|WARNING:x",
            credential_check=_noop_check,
        )
        with self.assertRaises(ValueError):
            manager.start_multi([])

    def test_cancel_unknown_session(self):
        manager = ManualLoginManager(
            store=FakeStore(_ok_validator),
            validator=_ok_validator,
            browser_login=lambda **kwargs: "_|WARNING:x",
            credential_check=_noop_check,
        )
        self.assertFalse(manager.cancel("missing"))

    def test_bad_password_precheck_skips_without_browser(self):
        calls = []

        def fake_login(**kwargs):
            calls.append(kwargs.get("username_hint"))
            return "_|WARNING:cookie-" + str(kwargs.get("username_hint"))

        def bad_check(username, password, timeout=12.0):
            if username == "wrong":
                return {"outcome": "bad_credentials", "detail": "incorrect", "fresh_cookie": ""}
            return {"outcome": "unknown", "detail": "", "fresh_cookie": ""}

        manager = ManualLoginManager(
            store=FakeStore(_ok_validator),
            validator=_ok_validator,
            browser_login=fake_login,
            credential_check=bad_check,
        )
        session = manager.start_multi([
            {"username": "wrong", "password": "badpass"},
            {"username": "good", "password": "p"},
        ])
        status = _wait_state(manager, session.session_id)
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["done"], 1)
        self.assertEqual(status["failed"], 1)
        # Wrong password never opened a popup.
        self.assertEqual(calls, ["good"])
        bad_entry = next(e for e in status["entries"] if e["username"] == "wrong")
        self.assertEqual(bad_entry["status"], "failed")
        self.assertIn("incorrect username or password", bad_entry["detail"])

    def test_valid_precheck_imports_without_browser(self):
        calls = []

        def fake_login(**kwargs):
            calls.append(kwargs.get("username_hint"))
            return "_|WARNING:should-not-open"

        manager = ManualLoginManager(
            store=FakeStore(_ok_validator),
            validator=_ok_validator,
            browser_login=fake_login,
            credential_check=lambda u, p, timeout=12.0: {
                "outcome": "valid", "detail": "login ok", "fresh_cookie": "_|WARNING:fresh1",
            },
        )
        session = manager.start_single(username="user1", password="pass1")
        status = _wait_state(manager, session.session_id)
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["done"], 1)
        self.assertEqual(calls, [])

    def test_skip_current_moves_queue(self):
        def fake_login(username_hint="", password="", timeout_s=0, cancel_event=None,
                       skip_event=None, on_note=None):
            if username_hint == "user1":
                skip_event.wait(5.0)
                if skip_event.is_set():
                    from services.manual_login_service import ManualLoginSkipped
                    raise ManualLoginSkipped("Skipped by user.")
                raise RuntimeError("Cancelled.")
            return "_|WARNING:cookie-" + username_hint

        manager = ManualLoginManager(
            store=FakeStore(_ok_validator),
            validator=_ok_validator,
            browser_login=fake_login,
            credential_check=_noop_check,
        )
        session = manager.start_multi([
            {"username": "user1", "password": "p1"},
            {"username": "user2", "password": "p2"},
        ])
        time.sleep(0.2)
        self.assertTrue(manager.skip_current(session.session_id))
        status = _wait_state(manager, session.session_id)
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["done"], 1)
        self.assertEqual(status["failed"], 1)

    def test_read_login_error_detects_wrong_password(self):
        from services.manual_login_service import ManualLoginManager as _M

        class FakePage:
            def __init__(self, text):
                self._text = text

            def evaluate(self, _js):
                return self._text

        self.assertIn(
            "incorrect",
            _M._read_login_error(FakePage("Incorrect username or password. Please try again.")).lower(),
        )
        self.assertIn(
            "unknown error",
            _M._read_login_error(FakePage("An unknown error occurred. Please try again.")).lower(),
        )
        self.assertEqual(_M._read_login_error(FakePage("Welcome! 2FA code sent.")), "")

    def test_banned_precheck_skips_without_browser(self):
        calls = []

        def fake_login(**kwargs):
            calls.append(kwargs.get("username_hint"))
            return "_|WARNING:cookie-" + str(kwargs.get("username_hint"))

        def banned_check(username, password, timeout=12.0):
            if username == "gone":
                return {"outcome": "banned", "detail": "banned", "fresh_cookie": ""}
            return {"outcome": "unknown", "detail": "", "fresh_cookie": ""}

        manager = ManualLoginManager(
            store=FakeStore(_ok_validator),
            validator=_ok_validator,
            browser_login=fake_login,
            credential_check=banned_check,
        )
        session = manager.start_multi([
            {"username": "gone", "password": "p"},
            {"username": "good", "password": "p"},
        ])
        status = _wait_state(manager, session.session_id)
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["done"], 1)
        self.assertEqual(status["failed"], 1)
        self.assertEqual(calls, ["good"])
        bad_entry = next(e for e in status["entries"] if e["username"] == "gone")
        self.assertEqual(bad_entry["status"], "failed")
        self.assertIn("แบน", bad_entry["detail"])

    def test_locked_recovery_captcha_notes_threaded_to_popup(self):
        seen_notes = {}

        def fake_login(username_hint="", password="", timeout_s=0, cancel_event=None,
                       status_note="", on_note=None):
            seen_notes[username_hint] = status_note
            return "_|WARNING:cookie-" + username_hint

        outcomes = {"l1": "locked", "r1": "recovery", "c1": "captcha"}

        def check(username, password, timeout=12.0):
            return {"outcome": outcomes.get(username, "unknown"), "detail": "", "fresh_cookie": ""}

        manager = ManualLoginManager(
            store=FakeStore(_ok_validator),
            validator=_ok_validator,
            browser_login=fake_login,
            credential_check=check,
        )
        session = manager.start_multi([
            {"username": "l1", "password": "p"},
            {"username": "r1", "password": "p"},
            {"username": "c1", "password": "p"},
        ])
        status = _wait_state(manager, session.session_id)
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["done"], 3)
        self.assertIn("Locked", seen_notes["l1"])
        self.assertIn("Recovery", seen_notes["r1"])
        self.assertIn("Captcha", seen_notes["c1"])

    def test_wait_loop_shows_status_note(self):
        from services.manual_login_service import ManualLoginManager as _M

        class FakePage:
            def is_closed(self):
                return False

            def evaluate(self, _js):
                return "plain login page, no error"

        class FakeCtx:
            def cookies(self):
                return []

        import threading as _th
        import time as _time

        notes = []
        mgr = _M(store=FakeStore(_ok_validator), validator=_ok_validator,
                 browser_login=lambda **k: "", credential_check=_noop_check)
        with self.assertRaises(TimeoutError):
            mgr._wait_for_roblosecurity(
                FakeCtx(), FakePage(), _th.Event(), _time.time() + 0.2, None,
                status_note="ติด Locked (ต้องยืนยันตัวตน) — ทำต่อใน popup...",
                on_note=notes.append,
            )
        self.assertTrue(notes)
        self.assertIn("Locked", notes[0])



if __name__ == "__main__":
    unittest.main()
