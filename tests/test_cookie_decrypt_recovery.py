"""Cookie decrypt recovery — one unreadable DPAPI blob must not sink the file.

Hermetic stdlib unittest: DPAPI is monkeypatched, no Windows API and no real
cookie is touched. Reproduces "AccountData.json copied to another PC": every
dpapi:v1 blob fails with WinError 13 and the whole store read used to raise.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from services import account_hybrid as hybrid

UNREADABLE = "dpapi:v1:bm90LWEtcmVhbC1kcGFwaS1ibG9i"
MALFORMED = "dpapi:v1:%%%not-base64%%%"
DECRYPT_ERROR = OSError("[WinError 13] The data is invalid.")


def _record(username="alpha", **over):
    base = {"username": username, "encrypted_cookie": UNREADABLE}
    base.update(over)
    return base


class TestCookieDecryptRecovery(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "AccountData.json")
        self.store = hybrid.AccountDataStore(self.path)

    def _seed(self, records):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"accounts": records}, handle)

    def test_unreadable_blob_does_not_break_the_whole_read(self):
        self._seed([_record("alpha"), _record("beta")])
        with mock.patch.object(hybrid, "dpapi_unprotect", side_effect=DECRYPT_ERROR):
            records = self.store.read_records()
            with_cookies = self.store.read_records(include_cookies=True)
        self.assertEqual([item.get("username") for item in records], ["alpha", "beta"])
        self.assertEqual([item.get("cookie") for item in with_cookies], ["", ""])

    def test_unreadable_blob_is_reported_not_dropped(self):
        self._seed([_record("alpha")])
        with mock.patch.object(hybrid, "dpapi_unprotect", side_effect=DECRYPT_ERROR):
            api = self.store.to_api_record(self.store.read_records()[0])
        self.assertFalse(api["cookie_present"])
        self.assertEqual(api["cookie_state"], "unreadable")
        self.assertNotIn("encrypted_cookie", api)

    def test_write_keeps_the_unreadable_blob(self):
        with mock.patch.object(hybrid, "dpapi_unprotect", side_effect=DECRYPT_ERROR):
            self.store.write_records([_record("alpha")])
        with open(self.path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["accounts"][0]["encrypted_cookie"], UNREADABLE)

    def test_healthy_blob_still_reports_ok(self):
        with mock.patch.object(hybrid, "dpapi_unprotect", return_value=b"_|WARNING:ok"):
            api = self.store.to_api_record(_record("alpha"))
        self.assertTrue(api["cookie_present"])
        self.assertEqual(api["cookie_state"], "ok")

    def test_missing_cookie_reports_missing(self):
        api = self.store.to_api_record({"username": "alpha"})
        self.assertEqual(api["cookie_state"], "missing")

    def test_strict_decrypt_still_raises(self):
        with mock.patch.object(hybrid, "dpapi_unprotect", side_effect=DECRYPT_ERROR):
            with self.assertRaises(OSError):
                hybrid.decrypt_cookie(UNREADABLE)
            self.assertEqual(hybrid.decrypt_cookie(UNREADABLE, strict=False), "")

    def test_malformed_base64_is_tolerated(self):
        self.assertEqual(hybrid.decrypt_cookie(MALFORMED, strict=False), "")
        with self.assertRaises(ValueError):
            hybrid.decrypt_cookie(MALFORMED)


if __name__ == "__main__":
    unittest.main()
