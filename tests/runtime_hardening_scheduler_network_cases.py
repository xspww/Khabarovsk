from tests.runtime_hardening_shared import *


class RuntimeHardeningSchedulerNetworkCases:
    def test_runtime_scheduler_runs_due_jobs_in_due_order(self):
        scheduler = RuntimeScheduler(
            stop=threading.Event(),
            state_manager=RuntimeStateManager(),
            logger=lambda *args, **kwargs: None,
            autostart=False,
        )
        events = []
        scheduler.schedule_once("second", lambda job: events.append(job.job_key), delay=2.0, now=100.0)
        scheduler.schedule_once("first", lambda job: events.append(job.job_key), delay=1.0, now=100.0)

        self.assertEqual(scheduler.run_due(now=101.5), 1)
        self.assertEqual(events, ["first"])
        self.assertEqual(scheduler.run_due(now=102.5), 1)
        self.assertEqual(events, ["first", "second"])


    def test_runtime_scheduler_duplicate_key_replaces_previous_job(self):
        scheduler = RuntimeScheduler(
            stop=threading.Event(),
            state_manager=RuntimeStateManager(),
            logger=lambda *args, **kwargs: None,
            autostart=False,
        )
        events = []
        scheduler.schedule_once("same", lambda job: events.append("old"), delay=10.0, now=100.0)
        scheduler.schedule_once("same", lambda job: events.append("new"), delay=1.0, now=100.0)

        scheduler.run_due(now=101.1)

        self.assertEqual(events, ["new"])


    def test_runtime_scheduler_cancel_account_prevents_callback(self):
        scheduler = RuntimeScheduler(
            stop=threading.Event(),
            state_manager=RuntimeStateManager(),
            logger=lambda *args, **kwargs: None,
            autostart=False,
        )
        events = []
        scheduler.schedule_once("job-a", lambda job: events.append("a"), delay=1.0, account_id="acc-a", now=100.0)
        scheduler.schedule_once("job-b", lambda job: events.append("b"), delay=1.0, account_id="acc-b", now=100.0)

        self.assertEqual(scheduler.cancel_account("acc-a"), 1)
        scheduler.run_due(now=101.5)

        self.assertEqual(events, ["b"])


    def test_runtime_scheduler_rejects_stale_generation(self):
        scheduler = RuntimeScheduler(
            stop=threading.Event(),
            state_manager=RuntimeStateManager(),
            logger=lambda *args, **kwargs: None,
            autostart=False,
        )
        account = Account(username="stale_scheduler_user")
        account.runtime_generation = 1
        account.recovery_generation = 1
        events = []
        scheduler.schedule_once(
            "stale",
            lambda job: events.append("ran"),
            delay=1.0,
            account=account,
            runtime_generation=1,
            recovery_generation=1,
            now=100.0,
        )
        account.runtime_generation = 2

        scheduler.run_due(now=101.5)

        self.assertEqual(events, [])


    def test_runtime_scheduler_runtime_drift_requires_same_command_generation(self):
        scheduler = RuntimeScheduler(
            stop=threading.Event(),
            state_manager=RuntimeStateManager(),
            logger=lambda *args, **kwargs: None,
            autostart=False,
        )
        account = Account(username="stale_command_scheduler_user")
        account.runtime_generation = 1
        account.recovery_generation = 1
        account.command_generation = 1
        events = []
        scheduler.schedule_once(
            "stale-command",
            lambda job: events.append("ran"),
            delay=1.0,
            account=account,
            runtime_generation=1,
            recovery_generation=1,
            command_generation=1,
            payload={"allow_runtime_generation_drift": True},
            now=100.0,
        )
        account.runtime_generation = 2
        account.command_generation = 2

        scheduler.run_due(now=101.5)

        self.assertEqual(events, [])


    def test_runtime_scheduler_periodic_jobs_reschedule_until_cancelled(self):
        scheduler = RuntimeScheduler(
            stop=threading.Event(),
            state_manager=RuntimeStateManager(),
            logger=lambda *args, **kwargs: None,
            autostart=False,
        )
        events = []
        scheduler.schedule_periodic("periodic", 2.0, lambda job: events.append(job.job_key), initial_delay=1.0, now=100.0)

        scheduler.run_due(now=101.0)
        scheduler.run_due(now=103.0)
        scheduler.cancel("periodic")
        scheduler.run_due(now=105.0)

        self.assertEqual(events, ["periodic", "periodic"])


    def test_runtime_scheduler_stop_clears_pending_jobs(self):
        scheduler = RuntimeScheduler(
            stop=threading.Event(),
            state_manager=RuntimeStateManager(),
            logger=lambda *args, **kwargs: None,
            autostart=False,
        )
        scheduler.schedule_once("pending", lambda job: None, delay=30.0, now=100.0)

        self.assertIsNotNone(scheduler.get("pending"))
        scheduler.stop()

        self.assertIsNone(scheduler.get("pending"))


    def test_runtime_scheduler_snapshot_exposes_operational_lag(self):
        scheduler = RuntimeScheduler(
            stop=threading.Event(),
            state_manager=RuntimeStateManager(),
            logger=lambda *args, **kwargs: None,
            autostart=False,
        )
        events = []
        scheduler.schedule_once("late", lambda job: events.append(job.job_key), delay=1.0, now=100.0)

        before = scheduler.snapshot(now=112.0)
        scheduler.run_due(now=112.0)
        after = scheduler.snapshot(now=112.5)

        self.assertEqual(before["pending_count"], 1)
        self.assertEqual(before["overdue_count"], 1)
        self.assertEqual(before["max_overdue_seconds"], 11.0)
        self.assertEqual(events, ["late"])
        self.assertEqual(after["dispatch_count"], 1)
        self.assertEqual(after["last_dispatch_latency_seconds"], 11.0)

        def fail(_job):
            raise RuntimeError("unit scheduler failure")

        scheduler.schedule_once("bad", fail, delay=0.0, now=120.0)
        scheduler.run_due(now=120.0)

        self.assertEqual(scheduler.snapshot(now=121.0)["callback_failure_count"], 1)


    def test_runtime_scheduler_can_dispatch_callbacks_without_blocking_due_loop(self):
        ran = []
        release = threading.Event()
        scheduler = RuntimeScheduler(
            stop=threading.Event(),
            state_manager=RuntimeStateManager(),
            logger=lambda *args, **kwargs: None,
            autostart=False,
            dispatch_worker_count=2,
        )
        try:
            scheduler.schedule_once(
                "slow",
                lambda job: (release.wait(timeout=1.0), ran.append("slow")),
                due_at=1.0,
                now=0.0,
            )
            scheduler.schedule_once(
                "fast",
                lambda job: ran.append("fast"),
                due_at=1.0,
                now=0.0,
            )

            dispatched = scheduler.run_due(now=1.0)
            deadline = time.time() + 1.0
            while "fast" not in ran and time.time() < deadline:
                time.sleep(0.01)

            self.assertEqual(dispatched, 2)
            self.assertIn("fast", ran)
            self.assertNotIn("slow", ran)
        finally:
            release.set()
            scheduler.stop()


    def test_runtime_scheduler_dispatch_pool_reports_and_joins_workers(self):
        release = threading.Event()
        scheduler = RuntimeScheduler(
            stop=threading.Event(),
            state_manager=RuntimeStateManager(),
            logger=lambda *args, **kwargs: None,
            autostart=False,
            dispatch_worker_count=1,
        )
        try:
            scheduler.schedule_once(
                "slow",
                lambda job: release.wait(timeout=1.0),
                due_at=1.0,
                now=0.0,
            )
            scheduler.schedule_once(
                "queued",
                lambda job: None,
                due_at=1.0,
                now=0.0,
            )

            scheduler.run_due(now=1.0)
            deadline = time.time() + 1.0
            while scheduler.snapshot(now=1.0)["dispatch_pool"]["active_count"] < 1 and time.time() < deadline:
                time.sleep(0.01)
            snapshot = scheduler.snapshot(now=1.0)

            self.assertGreaterEqual(snapshot["dispatch_pool"]["active_count"], 1)
            self.assertGreaterEqual(snapshot["dispatch_pool"]["queued_count"], 1)
        finally:
            release.set()
            scheduler.stop(timeout=1.0)

        self.assertEqual(scheduler.snapshot(now=2.0)["dispatch_pool"]["worker_alive_count"], 0)


    def test_network_fault_scripts_are_scoped_to_cronus_rules(self):
        block = NetworkFaultInjector.build_block_script(r"C:\Roblox\RobloxPlayerBeta.exe", f"{RULE_PREFIX}_unit")
        restore = NetworkFaultInjector.build_restore_script()

        self.assertIn(RULE_PREFIX, block)
        self.assertIn("-Direction Outbound", block)
        self.assertIn("-Action Block", block)
        self.assertIn("-Program $program", block)
        self.assertIn("Remove-NetFirewallRule", restore)
        self.assertIn(f"{RULE_PREFIX}*", restore)
        self.assertNotIn("Disable-NetAdapter", block + restore)


    def test_network_fault_duplicate_block_keeps_single_rule(self):
        state = {"rules": []}

        def fake_runner(script: str) -> CommandResult:
            if "Remove-NetFirewallRule" in script:
                state["rules"] = []
            if "New-NetFirewallRule" in script:
                state["rules"] = [RULE_PREFIX + "_unit"]
            if "Get-NetFirewallRule" in script and "New-NetFirewallRule" not in script:
                stdout = '{"ok":true,"active":%s,"count":%d,"rules":[]}' % (
                    "true" if state["rules"] else "false",
                    len(state["rules"]),
                )
                return CommandResult(ok=True, returncode=0, stdout=stdout, script=script)
            return CommandResult(ok=True, returncode=0, stdout='{"ok":true}', script=script)

        injector = NetworkFaultInjector(runner=fake_runner)
        first = injector.block_roblox(r"C:\Roblox\RobloxPlayerBeta.exe", duration_seconds=0, account_id="unit")
        second = injector.block_roblox(r"C:\Roblox\RobloxPlayerBeta.exe", duration_seconds=0, account_id="unit")
        status = injector.status()

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertTrue(status["active"])
        self.assertEqual(len(state["rules"]), 1)


    def test_network_fault_invalid_non_roblox_pid_is_rejected(self):
        result = NetworkFaultInjector.validate_roblox_pid(os.getpid())
        self.assertFalse(result["ok"])
        self.assertIn(result["reason"], {"not_roblox_process", "missing_executable", "pid_validation_failed"})


    def test_network_fault_api_uses_injector_without_secret_output(self):
        from fastapi.testclient import TestClient
        import main

        class FakeInjector:
            def status(self):
                return {"ok": True, "active": False, "rules": []}

            def validate_roblox_pid(self, pid):
                return {
                    "ok": True,
                    "pid": int(pid),
                    "name": "RobloxPlayerBeta.exe",
                    "exe": r"C:\Roblox\RobloxPlayerBeta.exe",
                    "create_time": 123.0,
                }

            def find_live_roblox_processes(self):
                return []

            def block_roblox(self, program_path, *, duration_seconds=90, account_id="", pid=None):
                return {
                    "ok": True,
                    "active": True,
                    "program": program_path,
                    "duration_seconds": duration_seconds,
                    "pid": pid,
                    "stdout": "",
                    "stderr": "",
                }

            def restore(self):
                return {"ok": True, "active": False, "stdout": "", "stderr": ""}

        original = main.NETWORK_FAULT_INJECTOR
        main.NETWORK_FAULT_INJECTOR = FakeInjector()
        try:
            client = TestClient(main.app)
            self.assertEqual(client.get("/api/test/network-fault/status").status_code, 200)
            block = auth_post(client,
                "/api/test/network-fault/block-roblox",
                json={"account_id": "IwasTheGuyOni7899", "pid": 1234, "duration_seconds": 30},
            )
            self.assertEqual(block.status_code, 200)
            payload = block.json()
            self.assertTrue(payload["ok"])
            self.assertNotIn("ROBLOSECURITY", str(payload).upper())
            restore = auth_post(client, "/api/test/network-fault/restore", json={"account_id": "IwasTheGuyOni7899"})
            self.assertEqual(restore.status_code, 200)
        finally:
            main.NETWORK_FAULT_INJECTOR = original


if __name__ == "__main__":
    unittest.main()
