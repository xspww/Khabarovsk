import json
import os
import tempfile
import unittest
from unittest.mock import patch

from config_sections import build_config_sections
from config_store import ConfigManager
from config_validation import CONFIG_SCHEMA_VERSION, validate_config_payload
import config_store


class ConfigSectionsTests(unittest.TestCase):
    def test_build_config_sections_normalizes_boundary_values(self):
        sections = build_config_sections({
            "game_place_id": 12345,
            "multi_roblox_enabled": "false",
            "queue_delay_seconds": "30",
            "popup_confidence_threshold": "1.25",
            "cpu_limiter_default_percent": "10",
            "roblox_window_arrange_columns": "3",
        })

        self.assertEqual(sections.game.place_id, "12345")
        self.assertFalse(sections.game.multi_roblox_enabled)
        self.assertEqual(sections.queue.delay_seconds, 30)
        self.assertEqual(sections.popup_detector.confidence_threshold, 1.25)
        self.assertEqual(sections.performance.cpu_limiter_default_percent, 10.0)
        self.assertEqual(sections.window.arrange_columns, 3)

    def test_config_manager_sections_returns_typed_snapshot_without_changing_raw_shape(self):
        cfg = ConfigManager()
        cfg.update({
            "game_place_id": "77747658251236",
            "roblox_window_width": 800,
            "roblox_window_height": 600,
        })

        sections = cfg.sections()
        raw = cfg.snapshot()

        self.assertEqual(sections.game.place_id, "77747658251236")
        self.assertEqual(sections.window.width, 800)
        self.assertEqual(sections.window.height, 600)
        self.assertIn("game_place_id", raw)
        self.assertNotIn("game", raw)

    def test_config_validation_clamps_runtime_values_and_versions_payload(self):
        from config_store import DEFAULTS

        clean = validate_config_payload({
            "max_retry": "-4",
            "fps_limit": "9000",
            "graphics_quality_level": "99",
            "popup_disconnected_enabled": "disabled",
            "cpu_limiter_accounts": "bad",
        }, DEFAULTS)

        self.assertEqual(clean["schema_version"], CONFIG_SCHEMA_VERSION)
        self.assertEqual(clean["max_retry"], 1)
        self.assertEqual(clean["fps_limit"], 1000)
        self.assertEqual(clean["graphics_quality_level"], 10)
        self.assertFalse(clean["popup_disconnected_enabled"])
        self.assertEqual(clean["cpu_limiter_accounts"], {})

    def test_config_manager_recovers_from_corrupt_config_with_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{bad json")

            with patch.object(config_store, "CONFIG_FILE", path):
                cfg = ConfigManager()

        snap = cfg.snapshot()
        self.assertEqual(snap["schema_version"], CONFIG_SCHEMA_VERSION)
        self.assertEqual(snap["max_retry"], 10)
        self.assertEqual(snap["fps_limit"], 240)

    def test_config_manager_recovers_corrupt_config_from_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{bad json")
            with open(path + ".bak", "w", encoding="utf-8") as fh:
                json.dump({"fps_limit": 120, "max_retry": "4", "schema_version": 1}, fh)

            with patch.object(config_store, "CONFIG_FILE", path):
                cfg = ConfigManager()

        snap = cfg.snapshot()
        self.assertEqual(snap["schema_version"], CONFIG_SCHEMA_VERSION)
        self.assertEqual(snap["fps_limit"], 120)
        self.assertEqual(snap["max_retry"], 4)


if __name__ == "__main__":
    unittest.main()
