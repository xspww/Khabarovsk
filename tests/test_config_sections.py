import unittest

from config_sections import build_config_sections
from config_store import ConfigManager


class ConfigSectionsTests(unittest.TestCase):
    def test_build_config_sections_normalizes_boundary_values(self):
        sections = build_config_sections({
            "game_place_id": 12345,
            "multi_roblox_enabled": "false",
            "queue_delay_seconds": "30",
            "popup_confidence_threshold": "1.25",
            "presence_api_enabled": "true",
            "cpu_limiter_default_percent": "10",
            "roblox_window_arrange_columns": "3",
        })

        self.assertEqual(sections.game.place_id, "12345")
        self.assertFalse(sections.game.multi_roblox_enabled)
        self.assertEqual(sections.queue.delay_seconds, 30)
        self.assertEqual(sections.popup_detector.confidence_threshold, 1.25)
        self.assertTrue(sections.presence.enabled)
        self.assertEqual(sections.performance.cpu_limiter_default_percent, 10.0)
        self.assertEqual(sections.window.arrange_columns, 3)

    def test_config_manager_sections_returns_typed_snapshot_without_changing_raw_shape(self):
        cfg = ConfigManager()
        cfg.update({
            "game_place_id": "77747658251236",
            "presence_api_enabled": True,
            "presence_poll_interval_seconds": 30,
            "roblox_window_width": 800,
            "roblox_window_height": 600,
        })

        sections = cfg.sections()
        raw = cfg.snapshot()

        self.assertEqual(sections.game.place_id, "77747658251236")
        self.assertTrue(sections.presence.enabled)
        self.assertEqual(sections.presence.poll_interval_seconds, 30)
        self.assertEqual(sections.window.width, 800)
        self.assertEqual(sections.window.height, 600)
        self.assertIn("game_place_id", raw)
        self.assertNotIn("game", raw)


if __name__ == "__main__":
    unittest.main()
