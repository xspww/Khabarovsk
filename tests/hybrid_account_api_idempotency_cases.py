from tests.hybrid_account_fixture import *


class HybridAccountApiIdempotencyCases:
    def test_stop_endpoint_clears_terminal_after_farm_stops(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        command = {"command_id": "cmd-stop"}
        with patch.object(main.farm, "begin_command", return_value=(True, command)), \
             patch.object(main.farm, "finish_command") as finish_command, \
             patch.object(main.farm, "running", True), \
             patch.object(main.farm, "stop") as stop_guard, \
             patch("api_routes.runtime_routes.console_output.clear_screen") as clear_screen:
            response = auth_post(client, "/api/stop")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["msg"], "Farm stopped")
        stop_guard.assert_called_once()
        clear_screen.assert_called_once()
        finish_command.assert_called_once()

    def test_close_all_roblox_endpoint_only_closes_roblox_clients(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        command = {"command_id": "cmd-close-all"}
        with patch.object(main.farm, "begin_command", return_value=(True, command)), \
             patch.object(main.farm, "finish_command") as finish_command, \
             patch.object(main.farm, "running", True), \
             patch.object(main.farm, "stop") as stop_guard, \
             patch.object(ProcessService, "kill_all_roblox_clients", return_value=6) as kill_all:
            response = auth_post(client, "/api/roblox/close-all")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["farm_was_running"])
        self.assertEqual(data["closed"], 6)
        stop_guard.assert_not_called()
        kill_all.assert_called_once_with(
            wait_seconds=4.0,
            exclude_pids=None,
            reason="api_close_all_roblox",
            idempotency_key="",
            command_id="cmd-close-all",
        )
        finish_command.assert_called_once()

    def test_close_all_roblox_replays_idempotent_response(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        headers = auth_headers({"X-Cronus-Idempotency-Key": "close-all-idem-unit"})
        with patch.object(ProcessService, "kill_all_roblox_clients", return_value=2) as kill_all:
            first = client.post("/api/roblox/close-all", headers=headers, json={})
            second = client.post("/api/roblox/close-all", headers=headers, json={})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        first_payload = first.json()
        second_payload = second.json()
        self.assertEqual(first_payload["command_id"], second_payload["command_id"])
        self.assertEqual(first_payload["closed"], 2)
        self.assertEqual(second_payload["closed"], 2)
        self.assertEqual(kill_all.call_count, 1)
        self.assertEqual(kill_all.call_args.kwargs["wait_seconds"], 4.0)
        self.assertEqual(kill_all.call_args.kwargs["idempotency_key"], "close-all-idem-unit")

    def test_account_import_replays_idempotency_without_reimporting(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        headers = auth_headers({"X-Cronus-Idempotency-Key": "slice3-import-idem"})
        with patch("api_routes.accounts_routes.ACCOUNT_STORE.import_cookie_lines", return_value={"ok": True, "imported": 1, "count": 1}) as importer, \
             patch("api_routes.accounts_routes.ACCOUNT_STORE.to_cronus_accounts", return_value=[]), \
             patch.object(main.farm, "set_accounts") as set_accounts, \
             patch.object(main.cfg_mgr, "save_accounts"):
            first = client.post("/api/accounts/import", headers=headers, json={"kind": "cookies", "lines": ["_|WARNING:unit"]})
            second = client.post("/api/accounts/import", headers=headers, json={"lines": ["_|WARNING:unit"], "kind": "cookies"})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())
        importer.assert_called_once()
        set_accounts.assert_called_once()

    def test_accounts_reload_replays_idempotency_without_reloading_twice(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        headers = auth_headers({"X-Cronus-Idempotency-Key": "slice3-reload-idem"})
        with patch("api_routes.accounts_routes.ACCOUNT_STORE.read_records", return_value=[]) as read_records, \
             patch("api_routes.accounts_routes.ACCOUNT_STORE.write_records") as write_records, \
             patch("api_routes.accounts_routes.ACCOUNT_STORE.to_cronus_accounts", return_value=[]), \
             patch.object(main.farm, "set_accounts") as set_accounts, \
             patch.object(main.cfg_mgr, "save_accounts"):
            first = client.post("/api/accounts/reload", headers=headers, json={})
            second = client.post("/api/accounts/reload", headers=headers, json={})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())
        read_records.assert_called_once()
        write_records.assert_called_once()
        set_accounts.assert_called_once()

    def test_account_launch_replays_idempotency_without_launching_twice(self):
        from fastapi.testclient import TestClient
        import main

        client = TestClient(main.app)
        headers = auth_headers({"X-Cronus-Idempotency-Key": "slice3-launch-idem"})
        record = {"username": "LaunchUnit", "cookie": "_|WARNING:unit", "cookie_username": "LaunchUnit"}
        with patch("api_routes.accounts_routes.ACCOUNT_STORE.read_records", return_value=[record]), \
             patch("api_routes.accounts_routes.AccountLaunchService.launch_record", return_value={"ok": False, "msg": "unit blocked"}) as launch_record, \
             patch("api_routes.accounts_routes.audit_event"):
            first = client.post("/api/account/LaunchUnit/launch", headers=headers, json={"place_id": "123456"})
            second = client.post("/api/account/LaunchUnit/launch", headers=headers, json={"place_id": "123456"})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())
        launch_record.assert_called_once()

    def test_logs_clear_replays_idempotency_without_clearing_twice(self):
        from fastapi.testclient import TestClient
        import api_routes.system_routes as system_routes
        import main

        client = TestClient(main.app)
        headers = auth_headers({"X-Cronus-Idempotency-Key": "slice3-logs-idem"})
        with patch.object(system_routes.os, "makedirs", wraps=system_routes.os.makedirs) as makedirs:
            first = client.post("/api/logs/clear", headers=headers, json={})
            second = client.post("/api/logs/clear", headers=headers, json={})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())
        makedirs.assert_called_once()

    def test_network_fault_replays_idempotency_without_reapplying(self):
        from fastapi.testclient import TestClient
        import main

        class FakeInjector:
            def __init__(self):
                self.block_count = 0
                self.restore_count = 0

            def validate_roblox_pid(self, pid):
                return {"ok": True, "pid": int(pid), "name": "RobloxPlayerBeta.exe", "exe": r"C:\Roblox\RobloxPlayerBeta.exe", "create_time": 1.0}

            def find_live_roblox_processes(self):
                return []

            def block_roblox(self, program_path, *, duration_seconds=90, account_id="", pid=None):
                self.block_count += 1
                return {"ok": True, "program": program_path, "duration_seconds": duration_seconds, "account_id": account_id, "pid": pid}

            def restore(self):
                self.restore_count += 1
                return {"ok": True, "active": False}

        original = main.NETWORK_FAULT_INJECTOR
        fake = FakeInjector()
        main.NETWORK_FAULT_INJECTOR = fake
        try:
            client = TestClient(main.app)
            block_headers = auth_headers({"X-Cronus-Idempotency-Key": "slice3-net-block-idem"})
            body = {"pid": 1234, "account_id": "NetUnit", "duration_seconds": 30}
            first = client.post("/api/test/network-fault/block-roblox", headers=block_headers, json=body)
            second = client.post("/api/test/network-fault/block-roblox", headers=block_headers, json=body)
            restore_headers = auth_headers({"X-Cronus-Idempotency-Key": "slice3-net-restore-idem"})
            restored = client.post("/api/test/network-fault/restore", headers=restore_headers, json={"account_id": "NetUnit"})
            restored_again = client.post("/api/test/network-fault/restore", headers=restore_headers, json={"account_id": "NetUnit"})
        finally:
            main.NETWORK_FAULT_INJECTOR = original

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored_again.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(restored.json(), restored_again.json())
        self.assertEqual(fake.block_count, 1)
        self.assertEqual(fake.restore_count, 1)

    def test_idempotency_helper_fields_are_in_mutation_audit(self):
        from fastapi.testclient import TestClient
        import api_routes.auth as auth_routes
        import main

        client = TestClient(main.app)
        with patch.object(auth_routes, "flog_kv") as log:
            response = client.post(
                "/api/logs/clear",
                headers=auth_headers({"X-Cronus-Idempotency-Key": "slice3-audit-idem"}),
                json={},
            )

        self.assertEqual(response.status_code, 200)
        audit_calls = [
            call for call in log.call_args_list
            if len(call.args) >= 2 and call.args[0] == "API" and call.args[1] == "mutation_audit"
        ]
        self.assertTrue(audit_calls)
        fields = audit_calls[-1].kwargs
        self.assertEqual(fields["idempotency_key"], "slice3-audit-idem")
        self.assertEqual(fields["idempotency_action"], "logs_clear")
        self.assertTrue(fields["idempotency_body_hash"])
