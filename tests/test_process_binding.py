"""Tests for the binding predicates (pure dicts, no psutil/processes)."""
import unittest

from services.process_service import _has_binding_evidence, _validate_process_ownership


def _valid():
    return {
        "ok": True,
        "pid": 4242,
        "identity": "robloxplayerbeta.exe|1700000000.000000|c:\\x\\robloxplayerbeta.exe",
        "name": "RobloxPlayerBeta.exe",
        "created": 1700000100.0,
        "owner": "alice",
        "browser_tracker_id": "t1",
    }


class TestHasBindingEvidence(unittest.TestCase):
    def test_tracker_match(self):
        self.assertTrue(_has_binding_evidence(_valid(), "bob", "", None, "t1"))

    def test_identity_match(self):
        v = _valid()
        self.assertTrue(_has_binding_evidence(v, "bob", v["identity"], None, ""))

    def test_owner_match(self):
        self.assertTrue(_has_binding_evidence(_valid(), "alice", "", None, ""))

    def test_launch_window(self):
        self.assertTrue(_has_binding_evidence(_valid(), "bob", "", 1700000101.0, ""))

    def test_nothing_matches(self):
        v = _valid()
        v["owner"] = "mallory"
        v["browser_tracker_id"] = "other"
        self.assertFalse(_has_binding_evidence(v, "bob", "nope", 1800000000.0, ""))


class TestValidateProcessOwnership(unittest.TestCase):
    def test_ok_path(self):
        out = _validate_process_ownership(
            _valid(), pid=4242, owner_key="alice",
            expected_identity=_valid()["identity"], launched_after=1700000090.0,
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["ownership_reasons"], [])

    def test_missing_pid(self):
        out = _validate_process_ownership(_valid(), pid=None, owner_key="alice")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "missing_pid")

    def test_generation_mismatch(self):
        out = _validate_process_ownership(
            _valid(), pid=1, owner_key="alice",
            expected_runtime_generation=2, current_runtime_generation=3,
        )
        self.assertEqual(out["reason"], "runtime_generation_mismatch")

    def test_identity_mismatch(self):
        out = _validate_process_ownership(
            _valid(), pid=1, owner_key="alice", expected_identity="other",
        )
        self.assertEqual(out["reason"], "identity_mismatch")

    def test_stale_pid_reuse(self):
        out = _validate_process_ownership(
            _valid(), pid=1, owner_key="alice", launched_after=1700000200.0,
        )
        self.assertEqual(out["reason"], "stale_pid_reuse")

    def test_wrong_executable(self):
        v = _valid()
        v["name"] = "notepad.exe"
        out = _validate_process_ownership(v, pid=1, owner_key="alice")
        self.assertEqual(out["reason"], "wrong_executable")

    def test_owner_mismatch(self):
        out = _validate_process_ownership(_valid(), pid=1, owner_key="mallory")
        self.assertEqual(out["reason"], "owner_mismatch")

    def test_missing_create_time(self):
        v = _valid()
        v["created"] = 0.0
        out = _validate_process_ownership(v, pid=1, owner_key="alice")
        self.assertEqual(out["reason"], "missing_create_time")

    def test_first_reason_wins(self):
        out = _validate_process_ownership(_valid(), pid=None, owner_key="mallory")
        self.assertEqual(out["reason"], "missing_pid")
        self.assertIn("owner_mismatch", out["ownership_reasons"])


if __name__ == "__main__":
    unittest.main()
