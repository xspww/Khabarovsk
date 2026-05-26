from tests.hybrid_account_fixture import *


class HybridAccountDataCases:
    def test_dpapi_cookie_roundtrip(self):
        cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--unit-test-cookie"
        encrypted = encrypt_cookie(cookie)
        self.assertTrue(encrypted.startswith("dpapi:v1:"))
        self.assertEqual(decrypt_cookie(encrypted), cookie)

    def test_legacy_roboguard_dpapi_account_file_still_loads(self):
        payload = json.dumps({
            "accounts": [
                {"username": "LegacyUser", "cookie": "_|WARNING:-DO-NOT-SHARE-THIS.--legacy-cookie"}
            ]
        }).encode("utf-8")
        legacy_blob = dpapi_protect(payload, b"RoboGuard Hybrid AccountData v1")

        records = AccountDataStore.decode_account_file_bytes(legacy_blob)

        self.assertEqual(records[0]["username"], "LegacyUser")
        self.assertEqual(AccountDataStore.get_cookie_from_record(records[0]), "_|WARNING:-DO-NOT-SHARE-THIS.--legacy-cookie")

    def test_legacy_roboguard_dpapi_cookie_value_still_loads(self):
        cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--legacy-cookie"
        encrypted_cookie = "dpapi:v1:" + base64.b64encode(
            dpapi_protect(cookie.encode("utf-8"), b"RoboGuard Hybrid AccountData v1")
        ).decode("ascii")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "AccountData.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"accounts": [{"username": "LegacyUser", "encrypted_cookie": encrypted_cookie}]}, handle)
            store = AccountDataStore(path)

            account = store.to_cronus_accounts()[0]

        self.assertEqual(account["username"], "LegacyUser")
        self.assertEqual(account["cookie"], cookie)

    def test_legacy_roboguard_config_filename_migrates_to_cronus_name(self):
        import app_paths

        with tempfile.TemporaryDirectory() as tmp:
            target_data = os.path.join(tmp, "Cronus Launcher", "data")
            legacy_data = os.path.join(tmp, "Argus Launcher", "data")
            os.makedirs(legacy_data, exist_ok=True)
            with open(os.path.join(legacy_data, "roboguard_rt1_config.json"), "w", encoding="utf-8") as handle:
                handle.write('{"auto_rejoin": false}')

            with patch.object(app_paths, "APP_DATA_DIR", target_data), \
                 patch.object(app_paths, "LEGACY_DATA_DIR", legacy_data), \
                 patch.object(app_paths, "LEGACY_APP_DATA_DIR", os.path.dirname(legacy_data)):
                app_paths.migrate_legacy_data_files(("cronus_rt1_config.json",))

            migrated = os.path.join(target_data, "cronus_rt1_config.json")
            self.assertTrue(os.path.exists(migrated))
            with open(migrated, "r", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["auto_rejoin"], False)

    def test_runtime_logs_and_cache_have_dedicated_data_subdirs(self):
        import account_hybrid
        import app_paths
        import core_logging

        self.assertEqual(Path(core_logging.LOG_FILE).parent, Path(app_paths.LOG_DIR))
        self.assertEqual(Path(core_logging.STRUCTURED_LOG_FILE).parent, Path(app_paths.LOG_DIR))
        self.assertEqual(Path(account_hybrid.ACCOUNT_AUDIT_FILE).parent, Path(app_paths.LOG_DIR))
        self.assertEqual(Path(app_paths.CACHE_DIR).parent, Path(app_paths.APP_DATA_DIR))

    def test_app_data_file_migration_moves_root_log_into_logs_dir(self):
        import app_paths

        with tempfile.TemporaryDirectory() as tmp:
            target_data = os.path.join(tmp, "Cronus Launcher", "data")
            os.makedirs(target_data, exist_ok=True)
            source = os.path.join(target_data, "cronus_rt1.log")
            with open(source, "w", encoding="utf-8") as handle:
                handle.write("old log")

            with patch.object(app_paths, "APP_DATA_DIR", target_data):
                app_paths.move_app_data_file("cronus_rt1.log", os.path.join("logs", "cronus_rt1.log"))

            migrated = os.path.join(target_data, "logs", "cronus_rt1.log")
            self.assertFalse(os.path.exists(source))
            with open(migrated, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "old log")

    def test_app_data_file_migration_discards_duplicate_root_cache_icon(self):
        import app_paths

        with tempfile.TemporaryDirectory() as tmp:
            target_data = os.path.join(tmp, "Cronus Launcher", "data")
            target_cache = os.path.join(target_data, "cache")
            os.makedirs(target_cache, exist_ok=True)
            source = os.path.join(target_data, "cronus_console_icon.ico")
            target = os.path.join(target_cache, "cronus_console_icon.ico")
            with open(source, "wb") as handle:
                handle.write(b"old")
            with open(target, "wb") as handle:
                handle.write(b"cache")

            with patch.object(app_paths, "APP_DATA_DIR", target_data):
                app_paths.move_app_data_file(
                    "cronus_console_icon.ico",
                    os.path.join("cache", "cronus_console_icon.ico"),
                    discard_if_target_exists=True,
                )

            self.assertFalse(os.path.exists(source))
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), b"cache")

    def test_account_data_never_exposes_cookie_by_default(self):
        cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--unit-test-cookie"
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountDataStore(os.path.join(tmp, "AccountData.json"))
            store.write_records([{"username": "UserA", "cookie": cookie, "place_id": "123"}])
            records = store.read_records()
            api_record = store.to_api_record(records[0])
            self.assertTrue(api_record["cookie_present"])
            self.assertNotIn("cookie", api_record)
            self.assertNotIn("encrypted_cookie", api_record)
            self.assertEqual(store.to_cronus_accounts()[0]["cookie"], cookie)

    def test_api_record_redacts_vip_links_and_preserves_on_saveback(self):
        raw_link = "https://www.roblox.com/games/123/?privateServerLinkCode=secret-link"
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountDataStore(os.path.join(tmp, "AccountData.json"))
            store.write_records([{"username": "UserA", "vip_links": [raw_link], "description": "old"}])
            api_record = store.to_api_record(store.read_records()[0])
            self.assertIn("[REDACTED]", api_record["vip_links"][0])
            self.assertNotIn("secret-link", json.dumps(api_record))
            api_record["description"] = "new"
            store.replace_from_cronus_payload([api_record])
            saved = store.read_records()[0]
        self.assertEqual(saved["description"], "new")
        self.assertEqual(saved["vip_links"], [raw_link])

    def test_owned_private_server_metadata_is_redacted_in_api_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountDataStore(os.path.join(tmp, "AccountData.json"))
            store.write_records([
                {
                    "username": "UserA",
                    "owned_private_servers": [
                        {
                            "private_server_id": "vip-1",
                            "owner_user_id": "42",
                            "place_id": "123",
                            "universe_id": "456",
                            "link": "https://www.roblox.com/games/123/?privateServerLinkCode=secret-link",
                            "join_code": "secret-link",
                            "access_code": "secret-access",
                            "status": "ok",
                        }
                    ],
                }
            ])
            api_record = store.to_api_record(store.read_records()[0])
        server = api_record["owned_private_servers"][0]
        self.assertTrue(server["link_present"])
        self.assertTrue(server["join_code_present"])
        self.assertTrue(server["access_code_present"])
        self.assertNotIn("link", server)
        self.assertNotIn("join_code", server)
        self.assertNotIn("access_code", server)
        self.assertNotIn("secret-link", json.dumps(api_record))
        self.assertNotIn("secret-access", json.dumps(api_record))

    def test_browser_tracker_persists_into_cronus_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountDataStore(os.path.join(tmp, "AccountData.json"))
            store.write_records([{"username": "UserA", "browser_tracker_id": "112233"}])
            account = store.to_cronus_accounts()[0]
            self.assertEqual(account["browser_tracker_id"], "112233")
