import builtins
import json
import os
import tempfile
import unittest
from unittest.mock import patch


class CookieSafetyTests(unittest.TestCase):
    def test_isolation_manager_default_instance_path_stays_outside_repo_root(self):
        from app_paths import APP_ROOT_DIR, USER_DATA_ROOT
        from services.cookie_service import IsolationManager

        repo_root = os.path.normcase(os.path.abspath(APP_ROOT_DIR))
        instance_root = os.path.normcase(os.path.abspath(IsolationManager.BASE_DIR))

        self.assertEqual(
            os.path.commonpath([instance_root, os.path.normcase(os.path.abspath(USER_DATA_ROOT))]),
            os.path.normcase(os.path.abspath(USER_DATA_ROOT)),
        )
        self.assertNotEqual(os.path.commonpath([instance_root, repo_root]), repo_root)

    def test_legacy_plaintext_cookie_store_migrates_to_account_data_and_quarantines_source(self):
        import account_hybrid
        import config_store
        from account_hybrid import AccountDataStore
        from config_store import ConfigManager

        cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--legacy-cookie"
        orphan_cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--orphan-cookie"

        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            accounts_path = os.path.join(tmp, "accounts.txt")
            cookies_path = os.path.join(tmp, "cookies.json")
            account_data_path = os.path.join(tmp, "AccountData.json")

            with open(accounts_path, "w", encoding="utf-8") as handle:
                json.dump([{"username": "LegacyUser", "place_id": "123456"}], handle)
            with open(cookies_path, "w", encoding="utf-8") as handle:
                json.dump({"legacyuser": cookie, "OrphanUser": orphan_cookie}, handle)

            store = AccountDataStore(account_data_path)
            with patch.object(config_store, "CONFIG_FILE", config_path), \
                 patch.object(config_store, "ACCOUNTS_TEXT_FILE", accounts_path), \
                 patch.object(config_store, "COOKIE_STORE_FILE", cookies_path), \
                 patch.object(account_hybrid, "ACCOUNT_STORE", store):
                cfg = ConfigManager()
                accounts = cfg.get_accounts()

            self.assertEqual(accounts[0].username, "LegacyUser")
            self.assertEqual(accounts[0].cookie, cookie)
            self.assertFalse(os.path.exists(cookies_path))
            self.assertTrue(any(name.startswith("cookies.json.migrated") for name in os.listdir(tmp)))

            migrated = {
                record["username"].lower(): record
                for record in store.read_records(include_cookies=True)
            }
            self.assertEqual(migrated["legacyuser"]["cookie"], cookie)
            self.assertEqual(migrated["orphanuser"]["cookie"], orphan_cookie)

    def test_save_cookies_writes_account_data_not_legacy_plaintext_store(self):
        import account_hybrid
        import config_store
        from account_hybrid import AccountDataStore
        from config_store import ConfigManager
        from core import Account

        cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--saved-cookie"

        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            cookies_path = os.path.join(tmp, "cookies.json")
            account_data_path = os.path.join(tmp, "AccountData.json")

            store = AccountDataStore(account_data_path)
            with patch.object(config_store, "CONFIG_FILE", config_path), \
                 patch.object(config_store, "COOKIE_STORE_FILE", cookies_path), \
                 patch.object(account_hybrid, "ACCOUNT_STORE", store):
                cfg = ConfigManager()
                cfg.save_cookies([Account(username="SaveUser", cookie=cookie)])

            self.assertFalse(os.path.exists(cookies_path))
            records = store.read_records(include_cookies=True)
            self.assertEqual(records[0]["username"], "SaveUser")
            self.assertEqual(records[0]["cookie"], cookie)

    def test_cookie_artifact_ledger_stores_hash_and_scrubs_only_matching_json_cookie(self):
        from services.cookie_artifact_ledger import CookieArtifactLedger, cookie_hash

        cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--artifact-cookie"

        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = os.path.join(tmp, "ledger.json")
            artifact_path = os.path.join(tmp, "RobloxLocalStorage.json")
            with open(artifact_path, "w", encoding="utf-8") as handle:
                json.dump({".ROBLOSECURITY": cookie, "keep": "value"}, handle)

            ledger = CookieArtifactLedger(ledger_path)
            self.assertTrue(ledger.record_json_cookie("UserA", artifact_path, cookie))

            with open(ledger_path, "r", encoding="utf-8") as handle:
                ledger_body = handle.read()
            self.assertNotIn(cookie, ledger_body)
            self.assertIn(cookie_hash(cookie), ledger_body)

            result = ledger.scrub_json_cookie_artifacts("UserA")

            self.assertEqual(result["scrubbed"], 1)
            with open(artifact_path, "r", encoding="utf-8") as handle:
                artifact = json.load(handle)
            self.assertNotIn(".ROBLOSECURITY", artifact)
            self.assertEqual(artifact["keep"], "value")

    def test_cookie_artifact_scrub_skips_json_cookie_when_hash_does_not_match(self):
        from services.cookie_artifact_ledger import CookieArtifactLedger

        original_cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--artifact-cookie"
        new_cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--other-cookie"

        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = os.path.join(tmp, "ledger.json")
            artifact_path = os.path.join(tmp, "RobloxLocalStorage.json")
            with open(artifact_path, "w", encoding="utf-8") as handle:
                json.dump({".ROBLOSECURITY": new_cookie, "keep": "value"}, handle)

            ledger = CookieArtifactLedger(ledger_path)
            self.assertTrue(ledger.record_json_cookie("UserA", artifact_path, original_cookie))

            result = ledger.scrub_json_cookie_artifacts("UserA")

            self.assertEqual(result["scrubbed"], 0)
            self.assertEqual(result["skipped"], 1)
            with open(artifact_path, "r", encoding="utf-8") as handle:
                artifact = json.load(handle)
            self.assertEqual(artifact[".ROBLOSECURITY"], new_cookie)
            self.assertEqual(artifact["keep"], "value")

    def test_isolation_manager_records_json_cookie_artifacts_without_secret(self):
        from services import cookie_service
        from services.cookie_artifact_ledger import CookieArtifactLedger

        cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--runtime-cookie"

        with tempfile.TemporaryDirectory() as tmp:
            instance_root = os.path.join(tmp, "instances")
            local_appdata = os.path.join(tmp, "localappdata")
            ledger_path = os.path.join(tmp, "ledger.json")

            original_import = builtins.__import__

            def guarded_import(name, *args, **kwargs):
                if name == "winreg":
                    raise ImportError("winreg disabled for test")
                return original_import(name, *args, **kwargs)

            with patch.object(cookie_service.IsolationManager, "BASE_DIR", instance_root), \
                 patch.object(cookie_service, "CookieArtifactLedger", lambda: CookieArtifactLedger(ledger_path)), \
                 patch.object(
                     cookie_service.IsolationManager,
                     "_encrypt_webview2_cookie",
                     side_effect=RuntimeError("webview2 disabled for test"),
                 ), \
                 patch.dict(os.environ, {"LOCALAPPDATA": local_appdata}), \
                 patch("builtins.__import__", side_effect=guarded_import):
                ok, message = cookie_service.IsolationManager.inject_cookie("UserA", cookie)

            self.assertTrue(ok, message)
            with open(ledger_path, "r", encoding="utf-8") as handle:
                ledger = json.load(handle)
            self.assertEqual(len(ledger["artifacts"]), 3)
            dumped = json.dumps(ledger)
            self.assertNotIn(cookie, dumped)
            self.assertTrue(all(item["target_type"] == "json" for item in ledger["artifacts"]))


if __name__ == "__main__":
    unittest.main()
