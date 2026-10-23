"""Console activity tests: identity keys, counters, visible failures, redaction.

Hermetic: no farm, HTTP, network, or Win32 console window.
"""
import importlib
import os
import unittest
from unittest import mock

from desktop import console_output


class ConsoleActivityTest(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {"CRONUS_CONSOLE_ACTIVITY": "1"})
        self._env.start()
        self.addCleanup(self._env.stop)

        import infra.console_activity as module

        self.ca = importlib.reload(module)
        self.ca._set_title_locked = lambda: None
        self.lines: list[str] = []
        self._write = mock.patch.object(console_output, "write_line", self.lines.append)
        self._write.start()
        self.addCleanup(self._write.stop)

    def _output(self):
        return "\n".join(self.lines)

    def test_alias_and_username_share_block_state(self):
        ca = self.ca
        ca.emit_structured(
            "STATE",
            "transition",
            account="AliasName",
            account_id="RealUser",
            old="IDLE",
            new="IN_GAME",
        )
        ca.emit_structured("API", "accounts_finished", account="RealUser")

        self.assertTrue(ca._is_auth_blocked("AliasName"))
        self.assertTrue(ca._is_auth_blocked("realuser"))

        ca.emit_structured(
            "STATE",
            "transition",
            account="AliasName",
            account_id="RealUser",
            old="IDLE",
            new="LAUNCHING",
        )
        self.assertFalse(ca._is_auth_blocked("AliasName"))

    def test_alias_state_recorded_before_mapping_is_remapped(self):
        ca = self.ca
        # A hold can arrive before any STATE event taught us the alias.
        ca.emit_structured("CAPTCHA", "account_hold", account="AliasOnly", detail="captcha_required")
        self.assertTrue(ca._is_auth_blocked("AliasOnly"))

        ca.emit_structured("STATE", "transition", account="AliasOnly", account_id="StableUser", old="IN_GAME", new="FAILED")
        self.assertTrue(ca._is_auth_blocked("StableUser"))

    def test_finished_account_suppresses_checking_line(self):
        ca = self.ca
        ca.emit_structured("STATE", "transition", account="Alias", account_id="Real", old="IDLE", new="IN_GAME")
        ca.emit_structured("API", "accounts_finished", account="Real")
        self.lines.clear()

        ca.emit_structured("RUNTIME", "suspect_process_check", account="Alias")
        self.assertEqual(self.lines, [])

        ca.emit_structured("API", "accounts_unfinished", account="Real")
        ca.emit_structured("RUNTIME", "suspect_process_check", account="Alias")
        self.assertIn("Checking Roblox process", self._output())

    def test_captcha_hold_clears_on_resume(self):
        ca = self.ca
        ca.emit_structured("CAPTCHA", "account_hold", account="Alias", account_id="Real", detail="captcha_required")
        self.assertIn("Captcha: 1", ca._title_text_locked())

        ca.emit_structured(
            "STATE",
            "transition",
            account="Alias",
            account_id="Real",
            old="IDLE",
            new="QUEUED",
            reason="captcha_resume",
        )
        self.assertIn("Captcha: 0", ca._title_text_locked())

    def test_start_failures_reach_console(self):
        ca = self.ca
        ca.emit_structured("API", "start_failed", error="boom")
        ca.emit_structured("API", "start_preflight_error", error="no launchable accounts")
        ca.emit_structured("BOOT", "farm_start_failed", error="port busy")

        out = self._output()
        self.assertIn("Start failed — boom", out)
        self.assertIn("Start blocked — no launchable accounts", out)
        self.assertIn("Farm failed to start — port busy", out)


class LoggingRedactionTest(unittest.TestCase):
    def test_console_receives_redacted_fields(self):
        logging_module = importlib.import_module("infra.core_logging")
        with (
            mock.patch.object(logging_module, "flog_struct"),
            mock.patch.object(logging_module, "flog"),
            mock.patch.object(logging_module, "emit_console_activity") as emit,
        ):
            logging_module.flog_kv(
                "API",
                "test_event",
                cookie="secret-cookie",
                error="privateServerLinkCode=abc123",
            )

        fields = emit.call_args.kwargs
        self.assertEqual(fields["cookie"], "[REDACTED]")
        self.assertNotIn("abc123", fields["error"])


if __name__ == "__main__":
    unittest.main()
