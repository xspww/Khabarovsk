import unittest


class ReleaseGateTests(unittest.TestCase):
    def test_command_result_marks_zero_exit_as_pass(self):
        from ops.release_gate import command_result

        result = command_result("unit_tests", 0, "ok", "")

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["name"], "unit_tests")

    def test_command_result_marks_nonzero_exit_as_fail(self):
        from ops.release_gate import command_result

        result = command_result("unit_tests", 2, "", "failed")

        self.assertEqual(result["status"], "fail")
        self.assertIn("failed", result["stderr"])

    def test_gate_report_is_not_ok_when_any_step_fails(self):
        from ops.release_gate import build_gate_report

        report = build_gate_report([
            {"status": "pass", "name": "compile"},
            {"status": "fail", "name": "tests"},
        ])

        self.assertFalse(report["ok"])
        self.assertEqual(report["fail_count"], 1)

    def test_write_gate_report_cache_writes_last_result(self):
        import json
        import tempfile
        from pathlib import Path

        from ops.release_gate import write_gate_report_cache

        with tempfile.TemporaryDirectory() as tmp:
            path = write_gate_report_cache(
                {"ok": True, "generated_at": 123.4, "fail_count": 0, "warn_count": 1},
                data_dir=Path(tmp),
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["last_result"], "pass")
        self.assertEqual(payload["last_run_at"], 123.4)


if __name__ == "__main__":
    unittest.main()
