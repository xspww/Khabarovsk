from tests.hybrid_account_fixture import *


class HybridAccountApiMiscCases:
    def test_queue_popup_disconnected_toggle_persists(self):
        from fastapi.testclient import TestClient
        import main

        original = main.cfg_mgr.snapshot()
        try:
            client = TestClient(main.app)
            html = (
                client.get("/").text
                + "\n" + client.get("/ui/dashboard.js").text
                + "\n" + client.get("/ui/panels/settingsPanels.js").text
            )
            self.assertIn("Popup Detector", html)
            self.assertNotIn("Use Popup Disconnected", html)
            self.assertIn('id="popup-disconnected-enabled"', html)
            self.assertIn('id="popup-scan-interval"', html)
            self.assertIn('id="popup-scan-max-parallel"', html)
            self.assertIn("popup_disconnected_enabled:$('popup-disconnected-enabled').checked", html)
            self.assertIn("popup_scan_interval_seconds:Number($('popup-scan-interval').value)||30", html)
            self.assertIn("popup_scan_max_parallel:Number($('popup-scan-max-parallel').value)||2", html)
            self.assertNotIn('id="presence-enabled"', html)
            self.assertNotIn("Use Presence API", html)
            self.assertNotIn("$('presence-interval')", html)
            self.assertNotIn("$('presence-ttl')", html)
            self.assertNotIn("$('presence-assist')", html)

            response = auth_post(client,
                "/api/config",
                json={
                    "popup_disconnected_enabled": False,
                    "popup_scan_interval_seconds": 45,
                    "popup_scan_max_parallel": 3,
                },
            )
            self.assertEqual(response.status_code, 200)
            config = client.get("/api/config").json()
            self.assertFalse(config["popup_disconnected_enabled"])
            self.assertEqual(config["popup_scan_interval_seconds"], 45)
            self.assertEqual(config["popup_scan_max_parallel"], 3)
        finally:
            main.cfg_mgr.update(original)
            main.cfg_mgr.save()

    def test_popup_disconnected_config_gates_popup_scans(self):
        import inspect
        from farm import AccountWorker, SystemMaintenance

        worker_source = inspect.getsource(AccountWorker.run)
        maintenance_source = inspect.getsource(SystemMaintenance._scan_liveness_watchdog)
        self.assertIn('self.cfg.get("popup_disconnected_enabled", True)', worker_source)
        self.assertIn("popup_scan_interval_seconds", worker_source)
        self.assertIn("effective_hold_sec", worker_source)
        self.assertIn("disconnect_detected", worker_source)
        self.assertIn('"279"', worker_source)
        self.assertIn("popup_enabled", maintenance_source)
        self.assertIn("popup_scan_max_parallel", maintenance_source)
        self.assertIn("_popup_periodic_scan_batch", maintenance_source)
        self.assertIn("inspect_ui = popup_enabled", maintenance_source)
        self.assertIn("state == \"reconnecting\" and popup_enabled", maintenance_source)

    def test_clear_logs_endpoint_truncates_runtime_log(self):
        from fastapi.testclient import TestClient
        import main

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cronus.log")
            with open(path, "w", encoding="utf-8") as f:
                f.write("line one\nline two\n")
            with patch.object(main, "LOG_FILE", path):
                client = TestClient(main.app)
                response = auth_post(client, "/api/logs/clear")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["lines"], [])
                with open(path, encoding="utf-8") as f:
                    self.assertEqual(f.read(), "")
                self.assertEqual(client.get("/api/logs").status_code, 403)
                self.assertEqual(client.get("/api/logs", headers=auth_headers()).json()["lines"], [])

    def test_avatar_endpoint_batches_user_ids(self):
        from fastapi.testclient import TestClient
        import main

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return json.dumps({
                    "data": [
                        {"targetId": 42, "imageUrl": "https://thumb/42.png"},
                        {"targetId": 99, "imageUrl": "https://thumb/99.png"},
                    ]
                }).encode("utf-8")

        main._AVATAR_CACHE.clear()
        client = TestClient(main.app)
        with patch.object(main.urllib.request, "urlopen", return_value=FakeResponse()) as urlopen:
            response = client.get("/api/accounts/avatars?user_ids=42,bad,99,42")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["avatars"]["42"], "https://thumb/42.png")
        self.assertEqual(payload["avatars"]["99"], "https://thumb/99.png")
        self.assertEqual(payload["missing"], [])
        self.assertIn("userIds=42%2C99", urlopen.call_args.args[0].full_url)

    def test_game_place_lookup_html_fallback_unescapes_title_and_image(self):
        from fastapi.testclient import TestClient
        import api_routes.accounts_routes as accounts_routes
        import main

        class FakeResponse:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return self.body

        page = (
            '<html><head><title>Unit &amp; Place | Roblox</title>'
            '<meta property="og:image" content="https://img.test/icon?a=1&amp;b=2">'
            "</head></html>"
        ).encode("utf-8")
        client = TestClient(main.app)
        with patch.object(
            accounts_routes.urllib.request,
            "urlopen",
            side_effect=[
                accounts_routes.urllib.error.URLError("universe unavailable"),
                accounts_routes.urllib.error.URLError("thumbnail unavailable"),
                FakeResponse(page),
            ],
        ):
            response = client.get("/api/game/place/123456")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["name"], "Unit & Place")
        self.assertEqual(payload["image_url"], "https://img.test/icon?a=1&b=2")
