from tests.hybrid_account_fixture import *


class HybridAccountSettingsCases:
    def test_fps_limiter_updates_cap_and_sets_readonly(self):
        xml = '<roblox><Item><Properties><int name="FramerateCap">240</int></Properties></Item></roblox>'
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "GlobalBasicSettings_13.xml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(xml)
            try:
                result = apply_fps_limiter_file(True, 144, path)
                self.assertTrue(result["ok"])
                self.assertEqual(read_fps_settings(path)["framerate_cap"], 144)
                self.assertTrue(is_readonly(path))
            finally:
                if os.path.exists(path):
                    set_readonly(path, False)

    def test_fps_limiter_disable_clears_readonly_without_changing_cap(self):
        xml = '<roblox><Item><Properties><int name="FramerateCap">60</int></Properties></Item></roblox>'
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "GlobalBasicSettings_13.xml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(xml)
            set_readonly(path, True)
            try:
                result = apply_fps_limiter_file(False, 240, path)
                self.assertTrue(result["ok"])
                self.assertEqual(read_fps_settings(path)["framerate_cap"], 60)
                self.assertFalse(is_readonly(path))
            finally:
                if os.path.exists(path):
                    set_readonly(path, False)

    def test_fps_limiter_rejects_invalid_and_missing_file(self):
        with self.assertRaises(ValueError):
            normalize_fps_limit(14)
        with self.assertRaises(ValueError):
            normalize_fps_limit(1001)
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "GlobalBasicSettings_13.xml")
            self.assertFalse(read_fps_settings(missing)["exists"])
            with self.assertRaises(FileNotFoundError):
                apply_fps_limiter_file(True, 60, missing)

    def test_graphics_auto_updates_settings(self):
        xml = (
            '<roblox><Item><Properties>'
            '<int name="FramerateCap">240</int>'
            '<token name="GraphicsOptimizationMode">0</token>'
            '<int name="GraphicsQualityLevel">1</int>'
            '<bool name="MaxQualityEnabled">true</bool>'
            '<int name="QualityResetLevel">5</int>'
            '<token name="SavedQualityLevel">10</token>'
            '</Properties></Item></roblox>'
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "GlobalBasicSettings_13.xml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(xml)
            result = apply_performance_settings_file(False, 240, True, path)
            self.assertTrue(result["ok"])
            status = read_fps_settings(path)
            self.assertFalse(status["graphics_auto_active"])
            self.assertTrue(status["graphics_low_active"])
            self.assertEqual(status["graphics_optimization_mode"], "0")
            self.assertEqual(status["graphics_quality_level"], "1")
            self.assertEqual(status["saved_quality_level"], "1")
            self.assertTrue(is_readonly(path))
            set_readonly(path, False)

    def test_graphics_quality_rejects_out_of_range(self):
        self.assertEqual(normalize_graphics_quality("1"), 1)
        with self.assertRaises(ValueError):
            normalize_graphics_quality(0)
        with self.assertRaises(ValueError):
            normalize_graphics_quality(11)

    def test_graphics_route_is_separate_from_fps_limiter(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        payload = {
            "ok": True,
            "path": "settings.xml",
            "read_only": True,
            "graphics_low_active": True,
            "graphics_low_enabled": True,
            "graphics_auto_enabled": True,
            "graphics_quality_level": 1,
            "msg": "ok",
        }
        priority_result = {"ok": True, "priority": "low", "applied": 1, "count": 1, "results": []}
        with patch.object(main, "apply_graphics_settings_file", return_value=dict(payload)) as apply_graphics, patch.object(
            main, "apply_process_priority_to_roblox", return_value=priority_result
        ) as apply_priority, patch.object(
            main.cfg_mgr, "update"
        ) as update, patch.object(main.cfg_mgr, "save") as save:
            response = auth_post(client,
                "/api/performance/graphics",
                json={
                    "graphics_low_enabled": True,
                    "graphics_quality_level": 1,
                    "auto_process_priority_enabled": True,
                    "process_priority": "low",
                },
            )
        self.assertEqual(response.status_code, 200)
        apply_graphics.assert_called_once_with(True, readonly_after=True, quality_level=1)
        apply_priority.assert_called_once_with("low")
        self.assertEqual(update.call_args.args[0], {
            "graphics_low_enabled": True,
            "graphics_auto_enabled": True,
            "graphics_quality_level": 1,
            "auto_process_priority_enabled": True,
            "process_priority": "low",
        })
        save.assert_called_once()
