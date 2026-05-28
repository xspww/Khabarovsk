from tests.hybrid_account_fixture import *


class HybridAccountPopupWindowCases:
    def test_process_manager_minimizes_only_visible_roblox_windows(self):
        with patch.object(
            ProcessManager,
            "_visible_roblox_windows",
            return_value=[{"pid": 111, "hwnd": 222}, {"pid": 333, "hwnd": 444}],
        ), patch("services.window_control.ctypes.windll") as windll:
            windll.user32.ShowWindow.return_value = 1
            result = ProcessManager.minimize_roblox_windows()
        self.assertTrue(result["ok"])
        self.assertEqual(result["minimized"], 2)
        self.assertEqual(windll.user32.ShowWindow.call_count, 2)

    def test_disconnect_dialog_277_is_rejoinable(self):
        result = ProcessManager.classify_disconnect_dialog_texts([
            "Disconnected",
            "Please check your internet connection and try again.",
            "(Error Code: 277)",
            "Reconnect",
        ])

        self.assertTrue(result["matched"])
        self.assertTrue(result["recovery_allowed"])
        self.assertEqual(result["evidence_source"], "error_code")
        self.assertEqual(result["action"], "rejoin")
        self.assertEqual(result["reason_key"], "network_drop")
        self.assertEqual(result["error_code"], "277")

    def test_disconnect_dialog_278_is_rejoinable_idle_disconnect(self):
        result = ProcessManager.classify_disconnect_dialog_texts([
            "Disconnected",
            "You were disconnected for being idle 20 minutes",
            "(Error Code: 278)",
            "Reconnect",
        ])

        self.assertTrue(result["matched"])
        self.assertTrue(result["recovery_allowed"])
        self.assertEqual(result["evidence_source"], "error_code")
        self.assertEqual(result["action"], "rejoin")
        self.assertEqual(result["reason_key"], "idle_disconnect")
        self.assertEqual(result["disconnect_category"], "NETWORK_DISCONNECT")
        self.assertEqual(result["error_code"], "278")

    def test_disconnect_dialog_273_is_conditional_rejoin_session_conflict(self):
        result = ProcessManager.classify_disconnect_dialog_texts([
            "Disconnected",
            "Same account launched game from different device. Reconnect if you prefer to use this device.",
            "(Error Code: 273)",
            "Reconnect",
        ])

        self.assertTrue(result["matched"])
        self.assertTrue(result["recovery_allowed"])
        self.assertEqual(result["evidence_source"], "error_code")
        self.assertEqual(result["action"], "conditional_rejoin")
        self.assertEqual(result["reason_key"], "session_conflict")
        self.assertEqual(result["disconnect_category"], "SESSION_CONFLICT")
        self.assertEqual(result["error_code"], "273")

    def test_disconnect_dialog_267_is_rejoinable_data_session_end(self):
        result = ProcessManager.classify_disconnect_dialog_texts([
            "Disconnected",
            "Your data session has ended. Please rejoin.",
            "(Error Code: 267)",
            "Leave",
        ])

        self.assertTrue(result["matched"])
        self.assertTrue(result["recovery_allowed"])
        self.assertEqual(result["evidence_source"], "error_code")
        self.assertEqual(result["action"], "rejoin")
        self.assertEqual(result["reason_key"], "security_kick")
        self.assertEqual(result["disconnect_category"], "NETWORK_DISCONNECT")
        self.assertEqual(result["error_code"], "267")

    def test_disconnect_dialog_268_is_rejoinable(self):
        result = ProcessManager.classify_disconnect_dialog_texts([
            "Disconnected",
            "You have been kicked due to unexpected client behavior.",
            "(Error Code: 268)",
            "Leave",
        ])

        self.assertTrue(result["matched"])
        self.assertTrue(result["recovery_allowed"])
        self.assertEqual(result["evidence_source"], "error_code")
        self.assertEqual(result["action"], "rejoin")
        self.assertEqual(result["reason_key"], "unexpected_client_behavior")
        self.assertEqual(result["disconnect_category"], "NETWORK_DISCONNECT")
        self.assertEqual(result["error_code"], "268")

    def test_disconnect_dialog_unknown_error_code_is_rejoinable(self):
        result = ProcessManager.classify_disconnect_dialog_texts([
            "Disconnected",
            "Roblox closed this session.",
            "(Error Code: 999)",
            "Leave",
        ])

        self.assertTrue(result["matched"])
        self.assertTrue(result["recovery_allowed"])
        self.assertEqual(result["evidence_source"], "error_code")
        self.assertEqual(result["action"], "rejoin")
        self.assertEqual(result["reason_key"], "connection_error")
        self.assertEqual(result["error_code"], "999")

    def test_visual_strong_disconnect_popup_matches_without_window_text(self):
        from runtime.popup_detector.popup_classifier import classify_popup_observation

        visual = {
            "matched": True,
            "score": 1.1,
            "strength": "strong",
            "source": "template",
            "visual_stage": "template",
            "button_pattern": "double",
            "overlay_score": 0.3,
            "modal_score": 0.8,
            "button_score": 0.6,
            "template_score": 0.7,
        }
        result = classify_popup_observation([], visual, process_idle=True)

        self.assertTrue(visual["matched"])
        self.assertTrue(result.matched)
        self.assertTrue(result.recovery_allowed)
        self.assertEqual(result.evidence_source, "visual_strong")
        self.assertEqual(result.action, "rejoin")
        self.assertEqual(result.visual_stage, "template")
        self.assertEqual(result.button_pattern, "double")

    def test_visual_pipeline_detects_overlay_modal_and_buttons_before_text(self):
        from PIL import Image, ImageDraw
        from runtime.popup_detector.popup_classifier import classify_popup_observation
        from runtime.popup_detector.popup_visual_detector import detect_visual_features

        image = Image.new("L", (800, 600), 130)
        draw = ImageDraw.Draw(image)
        draw.rectangle((220, 160, 580, 410), fill=58)
        draw.line((240, 215, 560, 215), fill=190, width=2)
        draw.rounded_rectangle((245, 340, 395, 382), radius=8, fill=245)
        draw.rounded_rectangle((405, 340, 555, 382), radius=8, fill=245)

        visual = detect_visual_features(image)
        result = classify_popup_observation([], visual, process_idle=False)

        self.assertTrue(visual["matched"])
        self.assertEqual(visual["strength"], "strong")
        self.assertEqual(visual["visual_stage"], "modal_button")
        self.assertEqual(visual["button_pattern"], "double")
        self.assertGreaterEqual(visual["overlay_score"], 0.28)
        self.assertGreaterEqual(visual["modal_score"], 1.0)
        self.assertGreaterEqual(visual["button_score"], 0.6)
        self.assertFalse(result.recovery_allowed)
        self.assertEqual(result.action, "")
        self.assertEqual(result.evidence_source, "visual_strong")

    def test_visual_pipeline_detects_disconnect_popup_at_supported_window_sizes(self):
        from PIL import Image, ImageDraw
        from runtime.popup_detector.popup_classifier import classify_popup_observation
        from runtime.popup_detector.popup_visual_detector import detect_visual_features

        def make_popup(width: int, height: int):
            image = Image.new("L", (width, height), 130)
            draw = ImageDraw.Draw(image)
            line_width = max(1, int(round(min(width, height) * 0.003)))
            radius = max(2, int(round(min(width, height) * 0.016)))
            draw.rectangle((int(width * 0.275), int(height * 0.267), int(width * 0.725), int(height * 0.683)), fill=58)
            draw.line((int(width * 0.300), int(height * 0.358), int(width * 0.700), int(height * 0.358)), fill=190, width=line_width)
            draw.rounded_rectangle((int(width * 0.306), int(height * 0.567), int(width * 0.494), int(height * 0.638)), radius=radius, fill=245)
            draw.rounded_rectangle((int(width * 0.506), int(height * 0.567), int(width * 0.694), int(height * 0.638)), radius=radius, fill=245)
            return image

        sizes = (
            (320, 240),
            (240, 180),
            (320, 180),
            (400, 300),
            (480, 270),
            (512, 384),
            (640, 360),
            (640, 480),
            (800, 600),
        )
        for width, height in sizes:
            with self.subTest(size=f"{width}x{height}"):
                visual = detect_visual_features(make_popup(width, height))
                result = classify_popup_observation([], visual, process_idle=False)

                self.assertTrue(visual["matched"])
                self.assertEqual(visual["strength"], "strong")
                self.assertEqual(visual["visual_stage"], "modal_button")
                self.assertEqual(visual["button_pattern"], "double")
                self.assertFalse(result.recovery_allowed)
                self.assertEqual(result.action, "")
                self.assertEqual(result.evidence_source, "visual_strong")

    def test_visual_pipeline_detects_captcha_challenge_page_without_text(self):
        from PIL import Image, ImageDraw
        from runtime.popup_detector.popup_classifier import classify_popup_observation
        from runtime.popup_detector.popup_visual_detector import detect_visual_features

        image = Image.new("RGB", (800, 600), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 800, 38), fill=(238, 239, 241))
        draw.text((376, 12), "Security", fill=(20, 20, 20))
        draw.text((274, 70), "Verification", fill=(10, 10, 10))
        draw.rectangle((260, 185, 540, 430), outline=(235, 235, 235), width=1)
        draw.text((350, 245), "Verification", fill=(0, 0, 0))
        draw.text((315, 292), "Please solve this challenge", fill=(100, 110, 120))
        draw.rectangle((340, 350, 460, 385), fill=(64, 200, 120))
        draw.text((365, 360), "Start Puzzle", fill=(255, 255, 255))

        visual = detect_visual_features(image)
        result = classify_popup_observation([], visual, process_idle=False)

        self.assertTrue(visual["matched"])
        self.assertTrue(visual["captcha_challenge"])
        self.assertEqual(visual["visual_stage"], "captcha_challenge")
        self.assertEqual(result.action, "hold")
        self.assertEqual(result.reason_key, CAPTCHA_REASON)
        self.assertFalse(result.recovery_allowed)

    def test_visual_pipeline_detects_small_window_disconnect_leave_bar(self):
        from PIL import Image, ImageDraw
        from runtime.popup_detector.popup_classifier import classify_popup_observation
        from runtime.popup_detector.popup_visual_detector import detect_visual_features

        image = Image.new("L", (320, 240), 35)
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 320, 42), fill=28)
        draw.rectangle((0, 42, 320, 170), fill=64)
        draw.text((120, 48), "Disconnected", fill=240)
        draw.text((28, 90), "You have been kicked by this experience or its moderators.", fill=180)
        draw.text((113, 125), "(Error Code: 267)", fill=198)
        draw.rectangle((0, 170, 320, 207), fill=238)
        draw.text((150, 182), "Leave", fill=38)
        draw.rectangle((0, 207, 320, 240), fill=18)

        visual = detect_visual_features(image)
        result = classify_popup_observation([], visual, process_idle=False)

        self.assertTrue(visual["matched"])
        self.assertEqual(visual["strength"], "strong")
        self.assertEqual(visual["visual_stage"], "small_panel")
        self.assertEqual(visual["button_pattern"], "bar")
        self.assertFalse(result.recovery_allowed)
        self.assertEqual(result.action, "")
        self.assertEqual(result.evidence_source, "visual_strong")

    def test_modal_shape_without_button_is_visual_weak_and_ignored(self):
        from PIL import Image, ImageDraw
        from runtime.popup_detector.popup_classifier import classify_popup_observation
        from runtime.popup_detector.popup_visual_detector import detect_visual_features

        image = Image.new("L", (800, 600), 130)
        draw = ImageDraw.Draw(image)
        draw.rectangle((220, 160, 580, 410), fill=58)
        draw.line((240, 215, 560, 215), fill=190, width=2)

        visual = detect_visual_features(image)
        result = classify_popup_observation([], visual, process_idle=True)

        self.assertTrue(visual["matched"])
        self.assertEqual(visual["strength"], "weak")
        self.assertEqual(visual["visual_stage"], "structural_weak")
        self.assertEqual(visual["button_pattern"], "none")
        self.assertFalse(result.recovery_allowed)
        self.assertEqual(result.evidence_source, "visual_weak")
        self.assertEqual(result.action, "")

    def test_visual_weak_disconnect_popup_does_not_allow_recovery(self):
        from PIL import Image, ImageDraw
        from runtime.popup_detector.popup_classifier import classify_popup_observation
        from runtime.popup_detector.popup_visual_detector import detect_visual_features

        image = Image.new("L", (800, 600), 130)
        draw = ImageDraw.Draw(image)
        draw.rectangle((160, 140, 640, 560), fill=58)
        draw.line((190, 225, 610, 225), fill=190, width=1)
        draw.rounded_rectangle((180, 492, 620, 535), radius=8, fill=245)

        visual = detect_visual_features(image)
        result = classify_popup_observation([], visual, process_idle=True)

        self.assertTrue(visual["matched"])
        self.assertTrue(result.matched)
        self.assertFalse(result.recovery_allowed)
        self.assertEqual(result.evidence_source, "visual_weak")
        self.assertEqual(result.action, "")

    def test_popup_observer_confirms_repeated_visual_only_popup_below_text_threshold(self):
        from runtime.popup_detector.popup_sampler import PopupObserver

        observer = PopupObserver(sample_count=2, sample_interval=0, threshold=1.0, stable_samples=2)
        observer.sampler.windows_for_pid = lambda pid, include_hidden=True: [{"hwnd": 123}]
        observer.sampler.read_texts = lambda hwnd: []
        observer.sampler.capture_window_image = lambda hwnd: object()

        with patch(
            "runtime.popup_detector.popup_sampler.detect_visual_features",
            return_value={"matched": True, "score": 1.1, "strength": "strong", "source": "template"},
        ):
            result = observer.inspect_pid(100, sample_count=2, sample_interval=0)

        self.assertTrue(result["matched"])
        self.assertTrue(result["recovery_allowed"])
        self.assertEqual(result["action"], "rejoin")
        self.assertEqual(result["reason_key"], "connection_error")
        self.assertEqual(result["positive_samples"], 2)
        self.assertEqual(result["samples_confirmed"], 2)
        self.assertEqual(result["visual_positive_samples"], 2)
        self.assertEqual(result["disconnect_category"], "VISUAL_DISCONNECT")
        self.assertTrue(result["visual_disconnect"])

    def test_popup_observer_ignores_repeated_visual_pipeline_without_text_or_code(self):
        from runtime.popup_detector.popup_sampler import PopupObserver

        observer = PopupObserver(sample_count=2, sample_interval=0, threshold=1.0, stable_samples=2)
        observer.sampler.windows_for_pid = lambda pid, include_hidden=True: [{"hwnd": 123}]
        observer.sampler.read_texts = lambda hwnd: []
        observer.sampler.capture_window_image = lambda hwnd: object()

        with patch(
            "runtime.popup_detector.popup_sampler.detect_visual_features",
            return_value={
                "matched": True,
                "score": 1.1,
                "strength": "strong",
                "source": "visual_pipeline",
                "visual_stage": "modal_button",
                "button_pattern": "bar",
                "title_rms": 68.16,
                "reconnect_rms": 69.0,
            },
        ):
            result = observer.inspect_pid(100, sample_count=2, sample_interval=0)

        self.assertFalse(result["matched"])
        self.assertFalse(result["recovery_allowed"])
        self.assertEqual(result["positive_samples"], 0)
        self.assertEqual(result["visual_positive_samples"], 0)
        self.assertEqual(result["action"], "")

    def test_popup_observer_confirms_repeated_visual_pipeline_single_button_popup(self):
        from runtime.popup_detector.popup_sampler import PopupObserver

        observer = PopupObserver(sample_count=2, sample_interval=0, threshold=1.0, stable_samples=2)
        observer.sampler.windows_for_pid = lambda pid, include_hidden=True: [{"hwnd": 123}]
        observer.sampler.read_texts = lambda hwnd: []
        observer.sampler.capture_window_image = lambda hwnd: object()

        with patch(
            "runtime.popup_detector.popup_sampler.detect_visual_features",
            return_value={
                "matched": True,
                "score": 1.1,
                "strength": "strong",
                "source": "visual_pipeline",
                "visual_stage": "modal_button",
                "button_pattern": "single",
                "overlay_score": 0.6,
                "modal_score": 1.08,
                "button_score": 0.48,
                "template_score": 0.0,
                "title_rms": 91.59,
                "reconnect_rms": 110.9,
            },
        ):
            result = observer.inspect_pid(100, sample_count=2, sample_interval=0)

        self.assertTrue(result["matched"])
        self.assertTrue(result["recovery_allowed"])
        self.assertEqual(result["action"], "rejoin")
        self.assertEqual(result["reason_key"], "connection_error")
        self.assertEqual(result["positive_samples"], 2)
        self.assertEqual(result["visual_positive_samples"], 2)
        self.assertEqual(result["disconnect_category"], "VISUAL_DISCONNECT")

    def test_popup_observer_ignores_repeated_visual_weak_panel(self):
        from runtime.popup_detector.popup_sampler import PopupObserver

        observer = PopupObserver(sample_count=2, sample_interval=0, threshold=1.0, stable_samples=2)
        observer.sampler.windows_for_pid = lambda pid, include_hidden=True: [{"hwnd": 123}]
        observer.sampler.read_texts = lambda hwnd: []
        observer.sampler.capture_window_image = lambda hwnd: object()

        with patch(
            "runtime.popup_detector.popup_sampler.detect_visual_features",
            return_value={"matched": True, "score": 0.95, "strength": "weak", "source": "structural"},
        ):
            result = observer.inspect_pid(100, sample_count=2, sample_interval=0)

        self.assertFalse(result["matched"])
        self.assertFalse(result["recovery_allowed"])
        self.assertEqual(result["evidence_source"], "visual_weak")
        self.assertEqual(result["action"], "")

    def test_popup_inspection_does_not_resize_supported_window_sizes(self):
        from runtime.popup_detector.popup_sampler import PopupWindowSampler

        sampler = PopupWindowSampler()
        for width, height in ((320, 240), (240, 180), (320, 180), (480, 270), (640, 480), (800, 600)):
            with self.subTest(size=f"{width}x{height}"):
                sampler.windows_for_pid = lambda pid, include_hidden=True, width=width, height=height: [{
                    "pid": pid,
                    "hwnd": 123,
                    "left": 10,
                    "top": 20,
                    "width": width,
                    "height": height,
                    "visible": True,
                    "iconic": False,
                }]

                with patch("runtime.popup_detector.popup_sampler.ctypes.windll") as windll:
                    result = sampler.prepare_popup_inspection(100, hold_seconds=1.0)

                self.assertTrue(result["ok"])
                self.assertFalse(result["resized"])
                windll.user32.SetWindowPos.assert_not_called()
                windll.user32.ShowWindow.assert_not_called()

    def test_non_disconnect_panel_does_not_match_from_process_idle_alone(self):
        from PIL import Image, ImageDraw
        from runtime.popup_detector.popup_classifier import classify_popup_observation
        from runtime.popup_detector.popup_visual_detector import detect_visual_features

        image = Image.new("L", (800, 600), 145)
        draw = ImageDraw.Draw(image)
        draw.rectangle((150, 175, 650, 540), fill=58)
        draw.line((260, 250, 620, 250), fill=180, width=1)

        visual = detect_visual_features(image)
        result = classify_popup_observation([], visual, process_idle=True)

        self.assertFalse(visual["matched"])
        self.assertFalse(result.matched)

    def test_process_manager_resizes_visible_roblox_windows_without_arranging(self):
        with patch.object(
            ProcessManager,
            "_visible_roblox_windows",
            return_value=[
                {"pid": 111, "hwnd": 222, "left": 50, "top": 60, "width": 800, "height": 600},
                {"pid": 333, "hwnd": 444, "left": 70, "top": 80, "width": 640, "height": 480},
            ],
        ), patch("services.window_control.ctypes.windll") as windll:
            windll.user32.GetWindowLongPtrW.return_value = 0x16CF0000
            windll.user32.SetWindowLongPtrW.return_value = 0x16CF0000
            windll.user32.SetWindowPos.return_value = 1
            def fake_rect(hwnd, rect_ref):
                rect_ref._obj.left = 50
                rect_ref._obj.top = 60
                rect_ref._obj.right = 690
                rect_ref._obj.bottom = 540
                return 1
            windll.user32.GetWindowRect.side_effect = fake_rect
            result = ProcessManager.resize_roblox_windows(640, 480)
        self.assertTrue(result["ok"])
        self.assertEqual(result["resized"], 1)
        self.assertEqual(result["skipped"], 1)
        windll.user32.SetWindowPos.assert_called_once()
        call_args = windll.user32.SetWindowPos.call_args.args
        self.assertEqual(call_args[2:6], (50, 60, 640, 480))

    def test_process_manager_arranges_windows_in_grid(self):
        windows = [
            {"pid": 100 + i, "hwnd": 200 + i, "left": 0, "top": 0, "width": 800, "height": 600}
            for i in range(5)
        ]
        with patch.object(ProcessManager, "_visible_roblox_windows", return_value=windows), patch(
            "services.window_control.primary_monitor_work_area",
            return_value={"left": 0, "top": 0, "width": 1200, "height": 800},
        ), patch("services.window_control.ctypes.windll") as windll:
            windll.user32.GetWindowLongPtrW.return_value = 0x16CF0000
            windll.user32.SetWindowLongPtrW.return_value = 0x16CF0000
            windll.user32.SetWindowPos.return_value = 1
            result = ProcessManager.arrange_roblox_windows(320, 240, columns=3, gap=2, margin=0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["arranged"], 5)
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["columns"], 3)
        calls = windll.user32.SetWindowPos.call_args_list
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[0].args[2:6], (0, 0, 320, 240))
        self.assertEqual(calls[3].args[2:6], (0, 242, 320, 240))

    def test_process_manager_arrange_shrinks_to_fit_work_area(self):
        windows = [
            {"pid": 100 + i, "hwnd": 200 + i, "left": 0, "top": 0, "width": 800, "height": 600}
            for i in range(8)
        ]
        with patch.object(ProcessManager, "_visible_roblox_windows", return_value=windows), patch(
            "services.window_control.primary_monitor_work_area",
            return_value={"left": 0, "top": 0, "width": 1000, "height": 300},
        ), patch("services.window_control.ctypes.windll") as windll:
            windll.user32.GetWindowLongPtrW.return_value = 0x16CF0000
            windll.user32.SetWindowLongPtrW.return_value = 0x16CF0000
            windll.user32.SetWindowPos.return_value = 1
            result = ProcessManager.arrange_roblox_windows(320, 240, columns=8, gap=2, margin=0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["arranged"], 8)
        self.assertLess(result["width"], 320)
        last = windll.user32.SetWindowPos.call_args_list[-1].args
        self.assertLessEqual(last[2] + last[4], 1000)

    def test_window_size_endpoint_applies_preset(self):
        from fastapi.testclient import TestClient
        import main

        original = main.cfg_mgr.snapshot()
        try:
            with patch.object(
                ProcessService,
                "resize_roblox_windows",
                return_value={"ok": True, "count": 1, "resized": 1, "skipped": 0},
            ) as resize:
                client = TestClient(main.app)
                response = auth_post(client,
                    "/api/performance/window-size",
                    json={"enabled": True, "preset": "320x240", "width": 1920, "height": 1080, "arrange_enabled": False},
                )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["enabled"])
            self.assertEqual(payload["preset"], "320x240")
            self.assertEqual(payload["width"], 320)
            self.assertEqual(payload["height"], 240)
            self.assertEqual(payload["resize_result"]["resized"], 1)
            resize.assert_called_once_with(320, 240, reason="api_window_size_apply")
        finally:
            main.cfg_mgr.update(original)
            main.cfg_mgr.save()

    def test_window_size_endpoint_arranges_when_enabled(self):
        from fastapi.testclient import TestClient
        import main

        original = main.cfg_mgr.snapshot()
        try:
            with patch.object(
                ProcessService,
                "arrange_roblox_windows",
                return_value={"ok": True, "count": 5, "arranged": 5, "failed": 0},
            ) as arrange:
                client = TestClient(main.app)
                response = auth_post(client,
                    "/api/performance/window-size",
                    json={"enabled": True, "preset": "320x240", "arrange_enabled": True, "arrange_columns": 3, "arrange_gap": 2},
                )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["arrange_enabled"])
            self.assertEqual(payload["arrange_columns"], 3)
            self.assertEqual(payload["resize_result"]["arranged"], 5)
            arrange.assert_called_once_with(320, 240, 3, 2, 0, reason="api_window_size_apply")
        finally:
            main.cfg_mgr.update(original)
            main.cfg_mgr.save()

    def test_multi_roblox_guard_failure_requires_recent_pid_overlap(self):
        from farm import AccountWorker

        now = time.time()
        acc = Account(username="UserA")
        acc.last_launch_at = now - 120
        acc.pid_missing_since = now
        worker = object.__new__(AccountWorker)
        worker.acc = acc
        worker.cfg = {
            "multi_roblox_enabled": True,
            "rt_rotation_enabled": False,
            "multi_roblox_guard_failure_window": 180,
            "multi_roblox_guard_failure_overlap_seconds": 20,
        }
        other = Account(username="UserB")
        worker._accounts = [acc, other]

        stale_presence = {"newest_created": now - 80, "pids": [111, 222]}
        fresh_presence = {"newest_created": now - 5, "pids": [111, 222]}

        other.state = AccountState.IN_GAME
        self.assertFalse(
            AccountWorker._looks_like_multi_roblox_guard_failure(worker, 111, fresh_presence, 12.0, 10.0)
        )
        other.state = AccountState.VERIFY
        self.assertFalse(
            AccountWorker._looks_like_multi_roblox_guard_failure(worker, 111, stale_presence, 12.0, 10.0)
        )
        self.assertTrue(
            AccountWorker._looks_like_multi_roblox_guard_failure(worker, 111, fresh_presence, 12.0, 10.0)
        )
