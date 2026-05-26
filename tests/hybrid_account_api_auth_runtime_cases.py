from tests.hybrid_account_fixture import *


class HybridAccountApiAuthRuntimeCases:
    def test_app_shutdown_rejects_wrong_token(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        self.assertEqual(auth_post(client, "/api/app/shutdown", json={"token": "wrong"}).status_code, 403)

    def test_app_shutdown_requires_api_token_header_before_body_validation(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        response = client.post("/api/app/shutdown", json={"token": main.INSTANCE_TOKEN})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Invalid API token")

    def test_api_token_required_for_mutating_routes(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        self.assertEqual(client.post("/api/config", json={}).status_code, 403)

    def test_api_token_allows_mutating_routes(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        response = auth_post(client, "/api/config", json={})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_api_token_accepts_legacy_header_aliases_during_migration(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        for header_name in ("X-Argus-Token", "X-RoboGuard-Token"):
            with self.subTest(header_name=header_name):
                response = client.post(
                    "/api/config",
                    json={},
                    headers={header_name: main.INSTANCE_TOKEN},
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["ok"])

    def test_config_api_accepts_runtime_guard_settings(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        response = auth_post(client, "/api/config", json={
            "roblox_memory_guard_enabled": True,
            "roblox_memory_guard_mb": 32768,
            "roblox_memory_guard_hold_seconds": 30,
            "relaunch_loop_fatal": False,
            "relaunch_loop_cooldown_seconds": 300,
        })

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        for key in (
            "roblox_memory_guard_enabled",
            "roblox_memory_guard_mb",
            "roblox_memory_guard_hold_seconds",
            "relaunch_loop_fatal",
            "relaunch_loop_cooldown_seconds",
        ):
            self.assertIn(key, payload["updated"])
        config = client.get("/api/config").json()
        self.assertTrue(config["roblox_memory_guard_enabled"])
        self.assertEqual(config["roblox_memory_guard_mb"], 32768.0)
        self.assertEqual(config["roblox_memory_guard_hold_seconds"], 30.0)
        self.assertFalse(config["relaunch_loop_fatal"])
        self.assertEqual(config["relaunch_loop_cooldown_seconds"], 300.0)

    def test_mutating_api_audit_logs_idempotency_key(self):
        from fastapi.testclient import TestClient
        import api_routes.auth as auth_routes
        import main

        client = TestClient(main.app)
        with patch.object(auth_routes, "flog_kv") as log:
            response = auth_post(
                client,
                "/api/config",
                headers={"X-Cronus-Idempotency-Key": "audit-unit-key"},
                json={},
            )

        self.assertEqual(response.status_code, 200)
        audit_calls = [
            call for call in log.call_args_list
            if len(call.args) >= 2 and call.args[0] == "API" and call.args[1] == "mutation_audit"
        ]
        self.assertTrue(audit_calls)
        self.assertEqual(audit_calls[-1].kwargs["method"], "POST")
        self.assertEqual(audit_calls[-1].kwargs["path"], "/api/config")
        self.assertEqual(audit_calls[-1].kwargs["status_code"], 200)
        self.assertEqual(audit_calls[-1].kwargs["idempotency_key"], "audit-unit-key")

    def test_runtime_telemetry_endpoint_is_read_only(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        response = client.get("/api/runtime/telemetry")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn("recovery_rate", payload)
        self.assertIn("memory_usage_mb", payload)

    def test_public_farm_health_endpoint_is_snapshot_only_and_redacted(self):
        from fastapi.testclient import TestClient
        import main
        from services.process_service import ProcessManager

        client = TestClient(main.app)
        with (
            patch.object(ProcessManager, "is_bound_game_alive", side_effect=AssertionError("live process scan")),
            patch.object(ProcessManager, "validate_game_process", side_effect=AssertionError("live process scan")),
            patch.object(ProcessManager, "list_live_game_processes", side_effect=AssertionError("live process scan")),
        ):
            response = client.get("/api/farm/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("account_count", payload)
        self.assertIn("queue", payload)
        self.assertNotIn("accounts", payload)
        serialized = str(payload).lower()
        self.assertNotIn("cookie", serialized)
        self.assertNotIn("session_id", serialized)
        self.assertNotIn("launch_nonce", serialized)

    def test_detailed_farm_health_endpoint_requires_token(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)

        rejected = client.get("/api/farm/health/detail")
        self.assertEqual(rejected.status_code, 403)

        accepted = client.get("/api/farm/health/detail", headers={"X-Cronus-Token": main.INSTANCE_TOKEN})
        self.assertEqual(accepted.status_code, 200)
        payload = accepted.json()
        self.assertIn("workers", payload)
        self.assertIn("dispatcher", payload)
        self.assertIn("maintenance", payload)
        self.assertIn("queue", payload)
        self.assertIn("stuck_states", payload)
        self.assertIn("watchdog_decision", payload)
        serialized = str(payload).lower()
        self.assertNotIn("cookie", serialized)
        self.assertNotIn("launch_nonce", serialized)

    def test_runtime_diagnostics_endpoint_requires_token(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)

        rejected = client.get("/api/runtime/diagnostics")
        self.assertEqual(rejected.status_code, 403)

        accepted = client.get("/api/runtime/diagnostics", headers={"X-Cronus-Token": main.INSTANCE_TOKEN})
        self.assertEqual(accepted.status_code, 200)
        payload = accepted.json()
        self.assertTrue(payload["ok"])
        serialized = str(payload).lower()
        self.assertNotIn(".roblosecurity", serialized)
        self.assertNotIn("_|warning:", serialized)

    def test_runtime_events_endpoint_requires_token(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)

        rejected = client.get("/api/runtime/events")
        self.assertEqual(rejected.status_code, 403)

        accepted = client.get("/api/runtime/events", headers={"X-Cronus-Token": main.INSTANCE_TOKEN})
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(accepted.json()["ok"])

    def test_start_rejects_missing_target_with_actionable_payload(self):
        from fastapi.testclient import TestClient
        import main

        account = Account(username="NoTargetUser")
        account.cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--unit"
        command = {"command_id": "cmd-start-missing-target"}
        client = TestClient(main.app)

        with patch.object(main.farm, "begin_command", return_value=(True, command)), \
             patch.object(main.farm, "finish_command") as finish_command, \
             patch.object(main.farm, "running", False), \
             patch.object(main.farm, "_accounts", [account]), \
             patch.object(main.farm, "start") as start:
            response = auth_post(client, "/api/start")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["accepted"])
        self.assertEqual(payload["error_code"], "missing_launch_target")
        self.assertEqual(payload["missing_target_count"], 1)
        self.assertEqual(payload["missing_targets"], ["NoTargetUser"])
        self.assertIn("Set game_place_id", payload["required_action"])
        start.assert_not_called()
        finish_command.assert_called_once()

    def test_app_shutdown_accepts_legacy_header_token(self):
        from fastapi.testclient import TestClient
        import api_routes.system_routes as system_routes
        import main

        class FakeThread:
            def __init__(self, target, daemon=False, name=""):
                self.target = target
                self.daemon = daemon
                self.name = name

            def start(self):
                return None

        client = TestClient(main.app)
        with patch.object(system_routes.threading, "Thread", FakeThread):
            response = client.post(
                "/api/app/shutdown",
                json={},
                headers={"X-RoboGuard-Token": main.INSTANCE_TOKEN},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
