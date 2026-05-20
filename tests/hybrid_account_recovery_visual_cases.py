from tests.hybrid_account_fixture import *


class HybridAccountRecoveryVisualCases:
    def test_visual_popup_is_enriched_with_recent_log_error_code(self):
        from services.roblox_liveness import assess_liveness

        class FakeProcessManager:
            @classmethod
            def validate_game_process(cls, pid, min_ram_mb=0.0):
                return {"ok": True, "cpu": 0.0, "ram_mb": 180.0, "windows": 1}

            @classmethod
            def is_not_responding(cls, pid):
                return False

            @classmethod
            def inspect_disconnect_dialog(cls, *args, **kwargs):
                return {
                    "matched": True,
                    "recovery_allowed": True,
                    "action": "rejoin",
                    "reason_key": "connection_error",
                    "detail": "visual_disconnect source=center_modal strength=strong",
                    "error_code": "",
                    "popup_confidence": 1.1,
                    "disconnect_category": "VISUAL_DISCONNECT",
                    "visual_disconnect": True,
                    "evidence_source": "visual_strong",
                }

            @classmethod
            def classify_disconnect_dialog_texts(cls, texts):
                return ProcessManager.classify_disconnect_dialog_texts(texts)

        with patch(
            "services.roblox_liveness.collect_recent_log_evidence",
            return_value={
                "matched": True,
                "source": "roblox_log",
                "error_code": "273",
                "keyword": "same account launched",
                "confidence": 1.2,
                "line": "Same account launched experience from different device. (Error Code: 273)",
            },
        ) as collect:
            result = assess_liveness(FakeProcessManager, 1234, inspect_ui=True)

        collect.assert_called_once()
        dialog = result["dialog"]
        self.assertEqual(result["state"], "reconnecting")
        self.assertEqual(result["reason_key"], "session_conflict")
        self.assertEqual(dialog["error_code"], "273")
        self.assertEqual(dialog["action"], "conditional_rejoin")
        self.assertEqual(dialog["disconnect_category"], "SESSION_CONFLICT")
        self.assertEqual(dialog["evidence_source"], "error_code")
        self.assertEqual(dialog["visual_evidence_source"], "visual_strong")
        self.assertTrue(dialog["visual_disconnect"])

    def test_visual_confirmed_popup_overrides_alive_process_without_log_evidence(self):
        from services.roblox_liveness import assess_liveness

        class FakeProcessManager:
            @classmethod
            def validate_game_process(cls, pid, min_ram_mb=0.0):
                return {"ok": True, "cpu": 5.0, "ram_mb": 220.0, "windows": 1}

            @classmethod
            def is_not_responding(cls, pid):
                return False

            @classmethod
            def inspect_disconnect_dialog(cls, *args, **kwargs):
                return {
                    "matched": True,
                    "recovery_allowed": True,
                    "action": "rejoin",
                    "reason_key": "connection_error",
                    "detail": "visual_disconnect source=center_modal strength=strong",
                    "error_code": "",
                    "popup_confidence": 1.1,
                    "disconnect_category": "VISUAL_DISCONNECT",
                    "visual_disconnect": True,
                    "evidence_source": "visual_strong",
                }

            @classmethod
            def classify_disconnect_dialog_texts(cls, texts):
                return ProcessManager.classify_disconnect_dialog_texts(texts)

        with patch(
            "services.roblox_liveness.collect_recent_log_evidence",
            return_value={"matched": False, "source": "roblox_log", "reason": "none"},
        ):
            result = assess_liveness(FakeProcessManager, 1234, inspect_ui=True)

        self.assertEqual(result["state"], "reconnecting")
        self.assertEqual(result["reason_key"], "connection_error")
        self.assertTrue(result["dialog"]["matched"])
        self.assertTrue(result["dialog"]["recovery_allowed"])
        self.assertEqual(result["dialog"]["evidence_source"], "visual_strong")

    def test_log_evidence_alone_does_not_create_popup_recovery(self):
        from services.roblox_liveness import assess_liveness

        class FakeProcessManager:
            @classmethod
            def validate_game_process(cls, pid, min_ram_mb=0.0):
                return {"ok": True, "cpu": 0.0, "ram_mb": 80.0, "windows": 0}

            @classmethod
            def is_not_responding(cls, pid):
                return False

            @classmethod
            def inspect_disconnect_dialog(cls, *args, **kwargs):
                return {
                    "matched": False,
                    "recovery_allowed": False,
                    "action": "",
                    "reason_key": "",
                    "detail": "",
                    "error_code": "",
                }

        with patch(
            "services.roblox_liveness.collect_recent_log_evidence",
            return_value={
                "matched": True,
                "source": "roblox_log",
                "error_code": "273",
                "confidence": 1.2,
                "line": "Same account launched experience from different device. (Error Code: 273)",
            },
        ):
            result = assess_liveness(FakeProcessManager, 1234, inspect_ui=True)

        self.assertNotEqual(result["state"], "reconnecting")
        self.assertFalse(result["dialog"].get("recovery_allowed", False))
        self.assertTrue(result["log_evidence"]["matched"])

    def test_window_resize_uses_interval_and_config(self):
        maint = object.__new__(SystemMaintenance)
        maint._cfg = {
            "roblox_window_resize_enabled": True,
            "roblox_window_width": 640,
            "roblox_window_height": 480,
            "roblox_window_resize_interval_seconds": 10,
        }
        maint._last_window_resize_at = time.time() - 5
        with patch.object(ProcessService, "resize_roblox_windows") as resize:
            SystemMaintenance._enforce_window_resize(maint)
        resize.assert_not_called()

        maint._last_window_resize_at = time.time() - 11
        with patch.object(ProcessService, "resize_roblox_windows", return_value={"resized": 2, "count": 2}) as resize:
            SystemMaintenance._enforce_window_resize(maint)
        resize.assert_called_once_with(640, 480, reason="auto_window_resize_cycle")

        maint._cfg["roblox_window_arrange_enabled"] = True
        maint._cfg["roblox_window_arrange_columns"] = 4
        maint._cfg["roblox_window_arrange_gap"] = 2
        maint._cfg["roblox_window_arrange_margin"] = 0
        maint._last_window_resize_at = time.time() - 11
        with patch.object(ProcessService, "arrange_roblox_windows", return_value={"arranged": 2, "count": 2}) as arrange:
            SystemMaintenance._enforce_window_resize(maint)
        arrange.assert_called_once_with(640, 480, 4, 2, 0, reason="auto_window_resize_cycle")
