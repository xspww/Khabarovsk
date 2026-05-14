import os
import unittest

import console_activity as console


class ConsoleActivityFormatTests(unittest.TestCase):
    def setUp(self):
        self._old_activity = os.environ.get("ARGUS_CONSOLE_ACTIVITY")
        self._old_color = os.environ.get("ARGUS_CONSOLE_COLOR")
        os.environ["ARGUS_CONSOLE_ACTIVITY"] = "1"
        os.environ["ARGUS_CONSOLE_COLOR"] = "0"
        console._COLOR_SUPPORT = None
        console._LAST_DISCONNECT_AT.clear()

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

        self.assertConsoleLine(found, "OK Found Roblox process 6504 for user IwasTheGuyOni7899")
        self.assertConsoleLine(ready, "  OK IwasTheGuyOni7899 (PID: 6504)")

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

        self.assertConsoleLine(line, "!! IwasTheGuyOni7899 disconnected (process_crash, restart in 5s)")


if __name__ == "__main__":
    unittest.main()
