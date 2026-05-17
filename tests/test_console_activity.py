import os
import unittest
from unittest.mock import patch

import console_activity as console


class ConsoleActivityFormatTests(unittest.TestCase):
    def setUp(self):
        self._old_activity = os.environ.get("ARGUS_CONSOLE_ACTIVITY")
        self._old_color = os.environ.get("ARGUS_CONSOLE_COLOR")
        os.environ["ARGUS_CONSOLE_ACTIVITY"] = "1"
        os.environ["ARGUS_CONSOLE_COLOR"] = "0"
        console._COLOR_SUPPORT = None
        console._LAST_DISCONNECT_AT.clear()
        console._LAST_CAPTCHA_AT.clear()
        console._ACTIVE_ACCOUNTS.clear()
        console._CAPTCHA_ACCOUNTS.clear()
        console._QUEUE_SIZE = 0

    def tearDown(self):
        if self._old_activity is None:
            os.environ.pop("ARGUS_CONSOLE_ACTIVITY", None)
        else:
            os.environ["ARGUS_CONSOLE_ACTIVITY"] = self._old_activity
        if self._old_color is None:
            os.environ.pop("ARGUS_CONSOLE_COLOR", None)
        else:
            os.environ["ARGUS_CONSOLE_COLOR"] = self._old_color
        console._COLOR_SUPPORT = None
        console._LAST_DISCONNECT_AT.clear()
        console._LAST_CAPTCHA_AT.clear()
        console._ACTIVE_ACCOUNTS.clear()
        console._CAPTCHA_ACCOUNTS.clear()
        console._QUEUE_SIZE = 0

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

        self.assertConsoleLine(found, "Found Roblox process 6504 for user IwasTheGuyOni7899")
        self.assertConsoleLine(ready, "✔ IwasTheGuyOni7899 (PID: 6504)")

    def test_found_process_timestamp_is_white_when_color_is_enabled(self):
        with patch.object(console, "_colors_enabled", return_value=True):
            line = console._format_state(
                "process_bind_verified",
                {"account": "IwasTheGuyOni7899", "pid": 6504},
            )

        self.assertTrue(line.startswith("\x1b[97m["), line)
        self.assertIn("\x1b[0m Found Roblox process \x1b[90m6504\x1b[0m for user IwasTheGuyOni7899", line)

    def test_ready_icon_is_green_when_color_is_enabled(self):
        with patch.object(console, "_colors_enabled", return_value=True):
            line = console._format_state(
                "transition",
                {"account": "IwasTheGuyOni7899", "old": "VERIFY", "new": "IN_GAME", "pid": 6504},
            )

        self.assertIn("\x1b[92m✔\x1b[0m IwasTheGuyOni7899", line)

    def test_smart_server_line_uses_vip_icon(self):
        line = console._format_structured(
            "SERVER",
            "smart_selected",
            "info",
            {"server_id": "3659f6a2", "players": 3, "max_players": 6, "ping_ms": 99},
        )

        self.assertConsoleLine(line, "🔐 Smart server selected: 3659f6a2 (players: 3/6, ping: 99ms)")

    def test_vip_detector_line_uses_lock_icon(self):
        line = console._format_structured(
            "VIP",
            "server_detected",
            "info",
            {"account": "Mincepaetz7297", "is_vip": True, "server_type": "VIP", "private_server_id": "3659f6a2"},
        )

        self.assertConsoleLine(line, "🔐 Mincepaetz7297 VIP server detected (id: 3659f6a2)")

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

        self.assertConsoleLine(line, "⚠️ IwasTheGuyOni7899 disconnected (process_crash, restart in 5s)")

    def test_captcha_warning_matches_dashboard_state(self):
        line = console._format_state(
            "transition",
            {"account": "Zuckmu", "old": "IN_GAME", "new": "FAILED", "reason": "captcha_required", "pid": 9108},
        )

        self.assertConsoleLine(line, "⚠️ Zuckmu CAPTCHA required (PID: 9108) - paused, solve manually then Resume")

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

        self.assertConsoleLine(line, "⚠️ Zuckmu CAPTCHA required (PID: 9108) - paused, solve manually then Resume")


if __name__ == "__main__":
    unittest.main()
