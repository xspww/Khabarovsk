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

    def test_watchdog_readiness_fails_stale_project_root(self):
        from ops.product_preflight import evaluate_watchdog_readiness

        result = evaluate_watchdog_readiness(
            {
                "TaskInstalled": True,
                "ProjectRootMatches": False,
                "WatchdogScriptExists": True,
                "TaskWorkingDirectory": r"C:\old",
                "ExpectedProjectRoot": r"C:\new",
            }
        )

        self.assertEqual(result["status"], "fail")
        self.assertIn("stale", result["msg"].lower())

    def test_watchdog_readiness_passes_current_project_root(self):
        from ops.product_preflight import evaluate_watchdog_readiness

        result = evaluate_watchdog_readiness(
            {
                "TaskInstalled": True,
                "ProjectRootMatches": True,
                "WatchdogScriptExists": True,
                "TaskWorkingDirectory": r"C:\repo",
                "ExpectedProjectRoot": r"C:\repo",
            }
        )

        self.assertEqual(result["status"], "pass")

    def test_watchdog_status_cache_writes_last_inspection(self):
        import json
        import tempfile
        from pathlib import Path

        from ops.product_preflight import write_watchdog_status_cache

        with tempfile.TemporaryDirectory() as tmp:
            path = write_watchdog_status_cache(
                {
                    "TaskInstalled": True,
                    "ProjectRootMatches": True,
                    "WatchdogScriptExists": True,
                },
                data_dir=Path(tmp),
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(payload["TaskInstalled"])
        self.assertTrue(payload["ProjectRootMatches"])

    def test_release_package_script_excludes_runtime_user_data(self):
        from pathlib import Path

        script = Path(__file__).resolve().parents[1] / "ops" / "package_release.ps1"
        self.assertTrue(script.exists())
        text = script.read_text(encoding="utf-8")
        self.assertIn("AccountData.json", text)
        self.assertIn("cronus_rt1_cookies.json", text)
        self.assertIn(".git", text)
        self.assertIn("__pycache__", text)

    def test_release_package_writes_manifest_and_excludes_runtime_state(self):
        from pathlib import Path

        script = Path(__file__).resolve().parents[1] / "ops" / "package_release.ps1"
        text = script.read_text(encoding="utf-8")

        self.assertIn("release-manifest", text.lower())
        self.assertIn("excluded_runtime_data", text)
        self.assertIn("source_commit", text)
        self.assertIn("AccountData.json", text)
        self.assertIn("cronus_rt_instance.json", text)
        self.assertIn("cronus_watchdog.log", text)

    def test_release_package_compresses_stage_contents_with_expandable_path(self):
        from pathlib import Path

        script = Path(__file__).resolve().parents[1] / "ops" / "package_release.ps1"
        text = script.read_text(encoding="utf-8")

        self.assertIn("Compress-Archive -Path", text)
        self.assertNotIn('Compress-Archive -LiteralPath (Join-Path $stage "*")', text)


if __name__ == "__main__":
    unittest.main()
