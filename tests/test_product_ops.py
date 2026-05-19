import unittest


class ProductOpsTests(unittest.TestCase):
    def test_product_preflight_flags_missing_launch_targets(self):
        from ops.product_preflight import evaluate_launch_target_readiness

        checks = evaluate_launch_target_readiness(
            {"game_place_id": "", "game_private_server_url": ""},
            [
                {"username": "A", "place_id": "", "vip_links": []},
                {"username": "B", "place_id": "", "vip_links": []},
            ],
        )

        self.assertEqual(checks["status"], "fail")
        self.assertEqual(checks["missing_target_count"], 2)
        self.assertIn("Set game_place_id", checks["required_action"])

    def test_product_preflight_allows_global_target(self):
        from ops.product_preflight import evaluate_launch_target_readiness

        checks = evaluate_launch_target_readiness(
            {"game_place_id": "123456", "game_private_server_url": ""},
            [{"username": "A", "place_id": "", "vip_links": []}],
        )

        self.assertEqual(checks["status"], "pass")
        self.assertEqual(checks["missing_target_count"], 0)

    def test_release_package_script_excludes_runtime_user_data(self):
        from pathlib import Path

        script = Path(__file__).resolve().parents[1] / "ops" / "package_release.ps1"
        self.assertTrue(script.exists())
        text = script.read_text(encoding="utf-8")
        self.assertIn("AccountData.json", text)
        self.assertIn("cronus_rt1_cookies.json", text)
        self.assertIn(".git", text)
        self.assertIn("__pycache__", text)


if __name__ == "__main__":
    unittest.main()
