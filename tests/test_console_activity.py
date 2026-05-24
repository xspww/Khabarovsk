import io
import os
import unittest
from unittest.mock import patch

import console_activity as console


class ConsoleActivityFormatTests(unittest.TestCase):
    def setUp(self):
        self._old_activity = os.environ.get("CRONUS_CONSOLE_ACTIVITY")
        self._old_color = os.environ.get("CRONUS_CONSOLE_COLOR")
        os.environ["CRONUS_CONSOLE_ACTIVITY"] = "1"
        os.environ["CRONUS_CONSOLE_COLOR"] = "0"
        console._COLOR_SUPPORT = None
        console._LAST_DISCONNECT_AT.clear()
        console._LAST_CAPTCHA_AT.clear()
        console._SUSPECT_LOGGED_ACCOUNTS.clear()
        console._SUSPECT_FINALIZED_AT_BY_ACCOUNT.clear()
        console._ACTIVE_ACCOUNTS.clear()
        console._CAPTCHA_ACCOUNTS.clear()
        console._QUEUE_SIZE = 0
        console.set_lua_liveness_required(False)

    def tearDown(self):
        if self._old_activity is None:
            os.environ.pop("CRONUS_CONSOLE_ACTIVITY", None)
        else:
            os.environ["CRONUS_CONSOLE_ACTIVITY"] = self._old_activity
        if self._old_color is None:
            os.environ.pop("CRONUS_CONSOLE_COLOR", None)
        else:
            os.environ["CRONUS_CONSOLE_COLOR"] = self._old_color
        console._COLOR_SUPPORT = None
        console._LAST_DISCONNECT_AT.clear()
        console._LAST_CAPTCHA_AT.clear()
        console._SUSPECT_LOGGED_ACCOUNTS.clear()
        console._SUSPECT_FINALIZED_AT_BY_ACCOUNT.clear()
        console._ACTIVE_ACCOUNTS.clear()
        console._CAPTCHA_ACCOUNTS.clear()
        console._QUEUE_SIZE = 0
        console.set_lua_liveness_required(False)

    def assertConsoleLine(self, line, suffix):
        self.assertRegex(line, r"^\[\d{2}:\d{2}:\d{2}\] ")
        self.assertTrue(line.endswith(suffix), line)

    def test_process_ready_uses_two_line_shape(self):
        found = console._format_state(
            "process_bind_verified",
            {"account": "IwasTheGuyOni7899", "pid": 6504},
        )
        ready = console._format_state(
            "transition",
            {"account": "IwasTheGuyOni7899", "old": "VERIFY", "new": "IN_GAME", "pid": 6504},
        )

        self.assertConsoleLine(found, "Found Roblox process 6504 for user (IwasTheGuyOni7899)")
        self.assertConsoleLine(ready, "✔ (IwasTheGuyOni7899) (PID: 6504)")

    def test_found_process_line_is_hidden_when_lua_liveness_is_required(self):
        console.set_lua_liveness_required(True)

        found = console._format_state(
            "process_bind_verified",
            {"account": "IwasTheGuyOni7899", "pid": 6504},
        )
        adopted = console._format_misc(
            "WORKER",
            "visible_process_adopted",
            {"account": "IwasTheGuyOni7899", "pid": 6504},
        )

        self.assertIsNone(found)
        self.assertIsNone(adopted)

    def test_found_process_timestamp_is_white_when_color_is_enabled(self):
        with patch.object(console, "_colors_enabled", return_value=True):
            line = console._format_state(
                "process_bind_verified",
                {"account": "IwasTheGuyOni7899", "pid": 6504},
            )

        self.assertTrue(line.startswith("\x1b[97m["), line)
        self.assertIn("Found Roblox process \x1b[38;2;128;128;128m6504\x1b[0m for user \x1b[38;2;34;139;34m(IwasTheGuyOni7899)\x1b[0m", line)

    def test_ready_line_uses_requested_colors_when_color_is_enabled(self):
        with patch.object(console, "_colors_enabled", return_value=True):
            line = console._format_state(
                "transition",
                {"account": "IwasTheGuyOni7899", "old": "VERIFY", "new": "IN_GAME", "pid": 6504},
            )

        self.assertTrue(line.startswith("\x1b[38;2;128;128;128m["), line)
        self.assertIn("\x1b[92m✔\x1b[0m \x1b[38;2;34;139;34m(IwasTheGuyOni7899)\x1b[0m", line)
        self.assertIn("\x1b[38;2;128;128;128m(PID: 6504)\x1b[0m", line)

    def test_smart_server_line_is_hidden(self):
        line = console._format_structured(
            "SERVER",
            "smart_selected",
            "info",
            {"server_id": "3659f6a2", "players": 3, "max_players": 6, "ping_ms": 99},
        )

        self.assertIsNone(line)
        self.assertIsNone(
            console._format_text("Smart server selected: 3659f6a2 (players: 3/6, ping: 99ms)", "info")
        )

    def test_vip_detector_line_uses_crown_icon(self):
        line = console._format_structured(
            "VIP",
            "server_detected",
            "info",
            {"account": "Mincepaetz7297", "is_vip": True, "server_type": "VIP", "private_server_id": "3659f6a2"},
        )

        self.assertConsoleLine(line, "👑 Mincepaetz7297 VIP server detected (id: 3659f6a2)")

    def test_public_detector_line_uses_vest_icon(self):
        line = console._format_structured(
            "VIP",
            "server_detected",
            "info",
            {"account": "Mincepaetz7297", "is_vip": False, "server_type": "PUBLIC"},
        )

        self.assertConsoleLine(line, "🦺 Mincepaetz7297 public server detected")

    def test_launch_wait_and_queue_noise_are_hidden(self):
        self.assertIsNone(
            console._format_state(
                "transition",
                {"account": "IwasTheGuyOni7899", "old": "QUEUED", "new": "LAUNCHING"},
            )
        )
        self.assertIsNone(
            console._format_state(
                "transition",
                {"account": "IwasTheGuyOni7899", "old": "LAUNCHING", "new": "VERIFY"},
            )
        )
        self.assertIsNone(console._format_structured("QUEUE", "push", "info", {"account": "IwasTheGuyOni7899"}))
        self.assertIsNone(console._format_text("[LAUNCH] Sent for IwasTheGuyOni7899 (roblox-player:...)", "info"))

    def test_disconnect_warning_is_visible(self):
        line = console._format_recovery(
            "cooldown",
            {"account": "IwasTheGuyOni7899", "reason": "process_crash", "delay": "5.0"},
        )

        self.assertConsoleLine(line, "⚠️ (IwasTheGuyOni7899) disconnected (process_crash, restart in 5s)")

    def test_captcha_warning_matches_dashboard_state(self):
        line = console._format_state(
            "transition",
            {"account": "Zuckmu", "old": "IN_GAME", "new": "FAILED", "reason": "captcha_required", "pid": 9108},
        )

        self.assertConsoleLine(line, "🔐 (Zuckmu) CAPTCHA required (PID: 9108) - paused, solve manually then Resume")

    def test_suspect_process_check_uses_plain_line(self):
        line = console._format_structured(
            "RUNTIME",
            "suspect_process_check",
            "warning",
            {"account": "UserA", "pid": 4321},
        )

        self.assertConsoleLine(line, "🚧 Checking Roblox process (UserA)")
        self.assertNotIn("█", line)
        self.assertNotIn("░", line)
        self.assertNotIn("40%", line)

    def test_suspect_process_check_colors_only_timestamp(self):
        with patch.object(console, "_colors_enabled", return_value=True):
            line = console._format_structured(
                "RUNTIME",
                "suspect_process_check",
                "warning",
                {"account": "UserA", "pid": 4321},
            )

        self.assertTrue(line.startswith("\x1b[38;2;255;215;0m["), line)
        self.assertIn("\x1b[0m 🚧 Checking Roblox process (UserA)", line)
        self.assertEqual(line.count("\x1b["), 2, line)

    def test_suspect_process_check_prints_once_as_regular_log(self):
        with patch.object(console, "_print_line") as write_line:
            console.emit_structured(
                "RUNTIME",
                "suspect_process_check",
                "warning",
                account="UserA",
                pid=4321,
                duration_seconds=0,
            )

        write_line.assert_called_once()
        self.assertIn("🚧 Checking Roblox process (UserA)", write_line.call_args.args[0])
        self.assertNotIn("4321", write_line.call_args.args[0])
        self.assertIn("usera", console._SUSPECT_LOGGED_ACCOUNTS)

    def test_suspect_process_check_does_not_spam_while_already_logged(self):
        with patch.object(console, "_print_line") as write_line:
            console.emit_structured(
                "RUNTIME",
                "suspect_process_check",
                "warning",
                account="UserA",
                duration_seconds=0,
            )
            console.emit_structured(
                "RUNTIME",
                "suspect_process_check",
                "warning",
                account="UserA",
                pid=4321,
                duration_seconds=0,
            )

        write_line.assert_called_once()

    def test_suspect_process_check_final_resets_dedupe_without_clearing(self):
        console._SUSPECT_LOGGED_ACCOUNTS.add("usera")
        with (
            patch.object(console, "_print_line") as write_line,
            patch.object(console.time, "monotonic", return_value=100.0),
        ):
            console.emit_structured(
                "RUNTIME",
                "suspect_process_check",
                "warning",
                account="UserA",
                pid=4321,
                final=True,
                duration_seconds=0,
            )

        write_line.assert_not_called()
        self.assertNotIn("usera", console._SUSPECT_LOGGED_ACCOUNTS)

    def test_process_bind_verified_finishes_active_suspect_line_without_duplicate(self):
        console._SUSPECT_LOGGED_ACCOUNTS.add("usera")
        with (
            patch.object(console, "_print_line") as write_line,
            patch.object(console.time, "monotonic", return_value=100.0),
        ):
            console.emit_structured(
                "STATE",
                "process_bind_verified",
                "info",
                account="UserA",
                pid=4321,
            )

        write_line.assert_called_once()
        self.assertIn("Found Roblox process 4321 for user (UserA)", write_line.call_args.args[0])
        self.assertIn("usera", console._SUSPECT_LOGGED_ACCOUNTS)

        with (
            patch.object(console, "_print_line") as write_line,
            patch.object(console.time, "monotonic", return_value=101.0),
        ):
            console.emit_structured(
                "RUNTIME",
                "suspect_process_check",
                "warning",
                account="UserA",
                pid=4321,
                final=True,
                duration_seconds=0,
            )

        write_line.assert_not_called()
        self.assertNotIn("usera", console._SUSPECT_LOGGED_ACCOUNTS)

    def test_post_bind_suspect_update_is_suppressed_during_settle_window(self):
        console._SUSPECT_FINALIZED_AT_BY_ACCOUNT["usera"] = 100.0
        with (
            patch.object(console, "_print_line") as write_line,
            patch.object(console.time, "monotonic", return_value=101.0),
        ):
            console.emit_structured(
                "RUNTIME",
                "suspect_process_check",
                "warning",
                account="UserA",
                pid=4321,
                final=False,
                duration_seconds=0,
            )

        write_line.assert_not_called()
        self.assertNotIn("usera", console._SUSPECT_LOGGED_ACCOUNTS)

    def test_found_and_ready_logs_print_after_plain_suspect_check(self):
        account = "IwasTheGuyOni7899"
        console._SUSPECT_LOGGED_ACCOUNTS.add(account.lower())
        output = io.StringIO()
        with (
            patch.object(console.sys, "stdout", output),
            patch.object(console.time, "monotonic", return_value=100.0),
            patch.object(console.time, "strftime", side_effect=["10:13:16", "10:13:17"]),
        ):
            console.emit_structured(
                "STATE",
                "process_bind_verified",
                "info",
                account=account,
                pid=11880,
            )
            console.emit_structured(
                "STATE",
                "transition",
                "info",
                account=account,
                old="VERIFY",
                new="IN_GAME",
                pid=11880,
            )

        text = output.getvalue()
        self.assertIn(f"Found Roblox process 11880 for user ({account})\n", text)
        self.assertIn(f"[10:13:17] ✔ ({account}) (PID: 11880)\n", text)
        self.assertNotIn("█", text)

    def test_captcha_hold_removes_account_from_active_counter(self):
        console._ACTIVE_ACCOUNTS.add("Zuckmu")

        console._update_counters("CAPTCHA", "account_hold", {"account": "Zuckmu"})

        self.assertNotIn("Zuckmu", console._ACTIVE_ACCOUNTS)
        self.assertIn("Zuckmu", console._CAPTCHA_ACCOUNTS)

    def test_console_title_includes_captcha_count(self):
        console._ACTIVE_ACCOUNTS.update({"A", "B"})
        console._CAPTCHA_ACCOUNTS.add("Zuckmu")
        console._QUEUE_SIZE = 3

        self.assertEqual(console._title_text_locked(), "Cronus | Active: 2 | Queue: 3 | Captcha: 1")

    def test_manual_resume_removes_captcha_from_console_title_count(self):
        console._CAPTCHA_ACCOUNTS.add("Zuckmu")

        console._update_counters("STATE", "transition", {"account": "Zuckmu", "old": "FAILED", "new": "IDLE", "reason": "manual_resume"})

        self.assertNotIn("Zuckmu", console._CAPTCHA_ACCOUNTS)

    def test_watchdog_captcha_hold_matches_dashboard_state(self):
        line = console._format_structured(
            "WATCHDOG",
            "captcha_dialog_hold",
            "warning",
            {"account": "Zuckmu", "pid": 9108, "detail": "Roblox | Security | Chrome Legacy Window"},
        )

        self.assertConsoleLine(line, "🔐 (Zuckmu) CAPTCHA required (PID: 9108) - paused, solve manually then Resume")

    def test_disconnect_line_uses_requested_colors_when_color_is_enabled(self):
        with patch.object(console, "_colors_enabled", return_value=True):
            line = console._format_recovery(
                "cooldown",
                {"account": "IwasTheGuyOni7899", "reason": "process_crash", "delay": "5.0"},
            )

        self.assertTrue(line.startswith("\x1b[38;2;255;127;80m["), line)
        self.assertIn("\x1b[38;2;255;0;0m(IwasTheGuyOni7899) disconnected\x1b[0m", line)

    def test_vip_detector_line_uses_requested_colors_when_color_is_enabled(self):
        with patch.object(console, "_colors_enabled", return_value=True):
            line = console._format_structured(
                "VIP",
                "server_detected",
                "info",
                {"account": "Mincepaetz7297", "is_vip": True, "server_type": "VIP", "private_server_id": "3659f6a2"},
            )

        self.assertTrue(line.startswith("\x1b[38;2;128;128;128m["), line)
        self.assertIn("\x1b[38;2;128;128;128m👑\x1b[0m", line)
        self.assertIn("\x1b[38;2;128;128;128mMincepaetz7297 VIP server detected\x1b[0m", line)
        self.assertIn("\x1b[38;2;128;128;128m(id: 3659f6a2)\x1b[0m", line)


if __name__ == "__main__":
    unittest.main()
