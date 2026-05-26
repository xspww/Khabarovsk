from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WatchdogOpsTests(unittest.TestCase):
    def test_watchdog_scripts_are_scoped_and_installable(self):
        watchdog = (ROOT / "ops" / "cronus_watchdog.ps1").read_text(encoding="utf-8")
        installer = (ROOT / "ops" / "install_watchdog_task.ps1").read_text(encoding="utf-8")
        runner = (ROOT / "ops" / "run_backend.py").read_text(encoding="utf-8")

        self.assertIn("Global\\CronusLauncherWatchdog", watchdog)
        self.assertIn("Invoke-RestMethod -Uri $HealthUrl", watchdog)
        self.assertIn("Update-CronusEndpointFromInstanceState", watchdog)
        self.assertIn("cronus_rt_instance.json", watchdog)
        self.assertIn("ops\\run_backend.py", watchdog)
        self.assertIn('"logs"', watchdog)
        self.assertIn("Stop-KnownCronusBackends", watchdog)
        self.assertNotIn("taskkill /F /IM python.exe", watchdog)
        self.assertNotIn("Stop-Process -Name python", watchdog)
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", installer)
        self.assertIn("-MultipleInstances IgnoreNew", installer)
        self.assertIn("-DontStopIfGoingOnBatteries", installer)
        self.assertNotIn("-DisallowStartIfOnBatteries", installer)
        self.assertIn('uvicorn.run(', runner)
        self.assertIn('"main:app"', runner)
        self.assertIn("prepare_backend_single_instance", runner)
        self.assertIn("clear_instance_state", runner)
        self.assertIn("refused to start a hidden duplicate", runner)

    def test_instance_state_records_endpoint_contract_for_watchdog_and_lua(self):
        guard = (ROOT / "desktop" / "instance_guard.py").read_text(encoding="utf-8")

        self.assertIn('"schema_version": 2', guard)
        self.assertIn('"host": HOST', guard)
        self.assertIn('"port": int(port)', guard)
        self.assertIn('"token": INSTANCE_TOKEN', guard)

    def test_watchdog_validates_instance_state_before_endpoint_update(self):
        watchdog = (ROOT / "ops" / "cronus_watchdog.ps1").read_text(encoding="utf-8")

        self.assertIn("Test-CronusInstanceState", watchdog)
        self.assertIn("$stateBaseDir", watchdog)
        self.assertIn("ProjectRoot", watchdog)
        self.assertIn("Get-Process -Id $statePid", watchdog)
        self.assertIn("ignoring stale instance_state", watchdog)

    def test_watchdog_status_reports_task_action_and_stale_project_root(self):
        status = (ROOT / "ops" / "watchdog_status.ps1").read_text(encoding="utf-8")

        self.assertIn("ExpectedProjectRoot", status)
        self.assertIn("TaskWorkingDirectory", status)
        self.assertIn("TaskArguments", status)
        self.assertIn("ProjectRootMatches", status)
        self.assertIn("WatchdogScriptExists", status)

    def test_soak_monitor_accepts_existing_running_farm(self):
        from ops.soak_monitor import start_response_allows_monitoring

        self.assertTrue(start_response_allows_monitoring({"ok": True}))
        self.assertTrue(start_response_allows_monitoring({"ok": False, "duplicate": True}))
        self.assertTrue(start_response_allows_monitoring({"ok": False, "msg": "Already running"}))
        self.assertFalse(start_response_allows_monitoring({"ok": False, "msg": "No launchable accounts"}))

    def test_soak_summary_fails_when_account_never_reaches_in_game(self):
        from ops.soak_monitor import build_soak_summary

        summary = build_soak_summary(
            account="A",
            reached_in_game=False,
            fatal_hits=[],
            orphan_processes=[],
            runtime_warnings=[],
            duration_seconds=60,
        )

        self.assertFalse(summary["ok"])
        self.assertIn("never reached IN_GAME", summary["failures"][0])

    def test_soak_monitor_accepts_summary_json_argument(self):
        from ops.soak_monitor import parse_args

        args = parse_args(["--account", "A", "--summary-json", "summary.json"])

        self.assertEqual(args.summary_json, "summary.json")


if __name__ == "__main__":
    unittest.main()
