from tests.runtime_hardening_shared import *


class RuntimeHardeningCoreCases:
    def test_process_safe_rotating_handler_writes_when_rollover_is_locked(self):
        temp_dir = tempfile.mkdtemp(prefix="cronus-log-handler-")
        path = os.path.join(temp_dir, "events.jsonl")

        class LockedRolloverHandler(ProcessSafeRotatingFileHandler):
            def doRollover(self):
                raise PermissionError(32, "file is locked")

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("x" * 32)
            handler = LockedRolloverHandler(path, maxBytes=1, backupCount=1, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(message)s"))
            record = logging.LogRecord("unit.locked", logging.INFO, __file__, 1, "survived", (), None)

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                handler.emit(record)

            handler.close()
            self.assertIsNone(handler.stream)
            with open(path, "r", encoding="utf-8") as f:
                self.assertIn("survived", f.read())
            self.assertNotIn("--- Logging error ---", stderr.getvalue())
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


    def test_event_bus_slow_handler_log_keeps_worker_alive(self):
        bus = EventBus(workers=1, max_pending=8)
        bus._slow_handler_sec = 0.0
        handled = threading.Event()

        def handler():
            handled.set()

        bus.on("unit_slow_event", handler)
        bus.emit("unit_slow_event")

        self.assertTrue(handled.wait(1.0))
        deadline = time.time() + 1.0
        while getattr(bus._tasks, "unfinished_tasks", 0) and time.time() < deadline:
            time.sleep(0.01)

        self.assertEqual(getattr(bus._tasks, "unfinished_tasks", 0), 0)
        self.assertTrue(bus._workers[0].is_alive())


    def test_manual_start_clears_max_fail_gate_counters(self):
        acc = Account(username="manual_retry_user")
        acc.state = AccountState.FAILED
        acc.fail_count = 8
        acc.retry_count = 5
        acc.launch_fail_count = 5
        acc.crash_retry_count = 3
        acc.network_retry_count = 2
        acc.session_retry_count = 4
        acc.session_wait_started_at = 10.0
        acc.pid_missing_since = 11.0
        acc.last_network_lost_at = 12.0
        acc.last_crash_reason = "max_fail"
        acc.last_recovery_reason = "max_fail"
        acc.recovery_status = "failed"
        acc.recovery_inflight = True
        acc.recovery_scheduled_at = 13.0
        acc.last_rejoin_trigger = "unit"
        acc.cooldown_until = 9999.0
        acc.sync_runtime("seed_failed_gate")

        state = RuntimeStateManager(logger=lambda *args, **kwargs: None)

        self.assertTrue(_clear_manual_start_failure_gate(acc, state, max_fail_count=5))
        self.assertEqual(acc.fail_count, 0)
        self.assertEqual(acc.retry_count, 0)
        self.assertEqual(acc.launch_fail_count, 0)
        self.assertEqual(acc.crash_retry_count, 0)
        self.assertEqual(acc.network_retry_count, 0)
        self.assertEqual(acc.session_retry_count, 0)
        self.assertEqual(acc.session_wait_started_at, 0.0)
        self.assertEqual(acc.pid_missing_since, 0.0)
        self.assertIsNone(acc.last_network_lost_at)
        self.assertEqual(acc.last_crash_reason, "")
        self.assertEqual(acc.last_recovery_reason, "")
        self.assertEqual(acc.recovery_status, "")
        self.assertFalse(acc.recovery_inflight)
        self.assertEqual(acc.recovery_scheduled_at, 0.0)
        self.assertEqual(acc.last_rejoin_trigger, "")
        self.assertEqual(acc.cooldown_until, 0.0)


    def test_farm_stop_skips_unstarted_blocked_workers(self):
        class Cfg:
            def __init__(self):
                self.saved = False

            def save_runtime(self, _accounts):
                self.saved = True

        class UnstartedWorker(threading.Thread):
            def __init__(self):
                super().__init__(daemon=True, name="BlockedWorker")
                self.woken = False

            def wake(self):
                self.woken = True

            def run(self):
                pass

        acc = Account(username="BlockedCaptchaUser")
        farm = type("Farm", (), {})()
        farm.running = True
        farm._shutting_down = False
        farm._stop = threading.Event()
        farm._accounts = [acc]
        farm._workers = {"BlockedCaptchaUser": UnstartedWorker()}
        farm._recovery = None
        farm._queue = None
        farm._dispatcher = None
        farm._maintenance = None
        farm._net_mon = None
        farm._runtime_scheduler = None
        farm._state_mgr = None
        farm._runtime_state = RuntimeStateManager(logger=lambda *_args, **_kwargs: None)
        farm.cfg_mgr = Cfg()
        farm._bump_status_revision = lambda: None
        farm._cancel_commands_for_shutdown = lambda: None
        farm._push_event = lambda *_args, **_kwargs: None

        with patch("runtime.farm_lifecycle.get_rt_monitor") as monitor, patch(
            "roblox_hybrid.release_multi_roblox_guard"
        ):
            FarmLifecycleService(farm).stop()

        self.assertFalse(farm.running)
        self.assertTrue(farm._workers["BlockedCaptchaUser"].woken)
        self.assertFalse(farm._workers["BlockedCaptchaUser"].is_alive())
        self.assertTrue(farm.cfg_mgr.saved)
        monitor.return_value.stop.assert_called_once()

    def test_initial_state_sync_rejects_stale_existing_pid_snapshot(self):
        acc = Account(username="RaceUser")
        acc.pid = 4242
        acc.bound_process_identity = "robloxplayerbeta.exe|100.000000|c:\\roblox\\robloxplayerbeta.exe"
        acc.bound_process_name = "RobloxPlayerBeta.exe"
        acc.browser_tracker_id = "tracker-1"
        acc.runtime_generation = 7
        acc.sync_runtime("seed")
        farm = farm_module.FarmController.__new__(farm_module.FarmController)
        farm._accounts = [acc]

        class State:
            def transition(self, *_args, **_kwargs):
                raise AssertionError("stale binding must not transition")

        def mark_stale(*_args, **_kwargs):
            acc.runtime_generation = 8
            acc.sync_runtime("stale")
            return True

        with patch("runtime.farm_initial_sync.ProcessManager.list_live_game_processes", return_value=[{"pid": 4242}]), \
             patch("runtime.farm_initial_sync.ProcessManager.is_bound_game_alive", side_effect=mark_stale), \
             patch("runtime.farm_initial_sync.ProcessService.bind_account_process") as bind_process, \
             patch("farm.flog"), \
             patch("runtime.farm_initial_sync.flog_kv") as flog_kv:
            farm._initial_state_sync(State())

        bind_process.assert_not_called()
        self.assertTrue(
            any(call.args[:2] == ("FARM", "initial_sync_existing_rejected") for call in flog_kv.call_args_list)
        )

    def test_push_event_does_not_hold_account_lock_while_recording_timeline(self):
        class TrackingRLock:
            def __init__(self):
                self._lock = threading.RLock()
                self.depth = 0

            def __enter__(self):
                self._lock.acquire()
                self.depth += 1
                return self

            def __exit__(self, exc_type, exc, tb):
                self.depth -= 1
                self._lock.release()

            def held(self):
                return self.depth > 0

        acc = Account(username="EventLockUser")
        lock = TrackingRLock()
        acc._lock = lock
        farm = farm_module.FarmController.__new__(farm_module.FarmController)
        farm._event_lock = threading.RLock()
        farm._bump_status_revision = lambda: 1
        case = self

        class Timeline:
            def record(self, *_args, **_kwargs):
                case.assertFalse(lock.held())

        farm._timeline = Timeline()

        with patch("farm.flog"):
            farm._push_event("unit", "event lock check", account=acc)

    def test_preflight_cookie_blocks_logs_and_continues_when_account_gate_raises(self):
        first = Account(username="BrokenGate")
        second = Account(username="HealthyGate")
        farm = farm_module.FarmController.__new__(farm_module.FarmController)
        farm._accounts = [first, second]
        farm._recovery = object()
        farm._state_mgr = object()
        farm._runtime_state = RuntimeStateManager(logger=lambda *_args, **_kwargs: None)

        class Decision:
            blocked = False

        with patch("runtime.farm_preflight.evaluate_account_auth_gate", side_effect=[RuntimeError("gate failed"), Decision()]), \
             patch("runtime.farm_preflight.flog_kv") as flog_kv:
            blocked = farm._preflight_cookie_blocks()

        self.assertEqual(blocked, {})
        self.assertTrue(
            any(call.args[:2] == ("FARM", "preflight_auth_gate_error") for call in flog_kv.call_args_list)
        )

    def test_resume_captcha_returns_error_when_auth_gate_raises(self):
        acc = Account(username="CaptchaGate")
        farm = farm_module.FarmController.__new__(farm_module.FarmController)
        farm._accounts = [acc]
        farm._runtime_state = RuntimeStateManager(logger=lambda *_args, **_kwargs: None)
        farm._state_mgr = None
        farm._recovery = None
        farm.running = False

        with patch("farm.evaluate_account_auth_gate", side_effect=RuntimeError("gate failed")), \
             patch("farm.flog_kv") as flog_kv:
            ok, msg = farm.resume_captcha_account("CaptchaGate")

        self.assertFalse(ok)
        self.assertIn("auth gate unavailable", msg.lower())
        self.assertTrue(
            any(call.args[:2] == ("FARM", "captcha_resume_auth_gate_error") for call in flog_kv.call_args_list)
        )

    def test_force_rejoin_is_rate_limited_per_account(self):
        acc = Account(username="RateLimitUser")
        farm = farm_module.FarmController.__new__(farm_module.FarmController)
        farm.running = True
        farm._accounts = [acc]
        farm._recovery = object()
        farm._workers = {}
        farm._push_event = lambda *_args, **_kwargs: None
        calls = []

        class Orchestrator:
            def request_rejoin(self, account, reason):
                calls.append((account, reason))
                return True

        farm._runtime_orchestrator = Orchestrator()

        with patch("runtime.command_rate_limit.time.time", return_value=100.0):
            first_ok, first_msg = farm.force_rejoin("RateLimitUser")
            second_ok, second_msg = farm.force_rejoin("RateLimitUser")

        self.assertTrue(first_ok)
        self.assertIn("Rejoin", first_msg)
        self.assertFalse(second_ok)
        self.assertIn("rate", second_msg.lower())
        self.assertEqual(len(calls), 1)


    def test_runtime_invariant_monitor_records_suppressed_timeline_events(self):
        acc = Account(username="InvariantUser")
        acc.state = AccountState.IN_GAME
        acc.desired_state = AccountState.IN_GAME
        acc.pid = None
        events = []

        monitor = RuntimeInvariantMonitor(
            [acc],
            record_event=lambda *args, **kwargs: events.append((args, kwargs)),
            suppress_seconds=60.0,
        )

        result = monitor.scan(now=100.0)
        self.assertEqual(result["violations"], 1)
        self.assertEqual(result["emitted"], 1)
        self.assertEqual(events[0][0][0], "runtime_invariant_violation")
        self.assertEqual(events[0][0][1], "InvariantUser")
        self.assertEqual(events[0][1]["reason"], "running_without_pid")
        self.assertEqual(events[0][1]["snapshot"]["public_state"], "IN_GAME")

        self.assertEqual(monitor.scan(now=120.0)["emitted"], 0)
        self.assertEqual(len(events), 1)
        self.assertEqual(monitor.scan(now=161.0)["emitted"], 1)
        self.assertEqual(len(events), 2)


    def test_orphan_sweeper_kills_elapsed_idle_account_orphan(self):
        class FakeProcessService:
            def __init__(self):
                self.calls = []

            def safe_kill_owned_orphan(self, account, pid, runtime_state=None, **kwargs):
                self.calls.append((account, pid, kwargs))
                if runtime_state:
                    runtime_state.clear_orphan_diagnostics(account, reason="unit_swept")
                return {"ok": True, "killed": True, "pid": pid, "reason": "killed"}

        class FakeProcessManager:
            def list_live_game_processes(self, launched_after=None):
                return []

            def get_pid_owner(self, pid):
                return ""

        acc = Account(username="OrphanUser")
        acc.state = AccountState.IDLE
        acc.orphan_pid = 4242
        acc.orphan_identity = "robloxplayerbeta.exe|1|c:\\roblox\\robloxplayerbeta.exe"
        acc.orphan_confidence = 80.0
        acc.orphan_verify_after = 90.0
        events = []
        service = FakeProcessService()
        state = RuntimeStateManager(logger=lambda *args, **kwargs: None)

        sweeper = RuntimeOrphanSweeper(
            [acc],
            runtime_state=state,
            process_service=service,
            process_manager=FakeProcessManager(),
            record_event=lambda *args, **kwargs: events.append((args, kwargs)),
        )

        result = sweeper.sweep(
            {
                "orphan_sweeper_enabled": True,
                "orphan_sweeper_kill_enabled": True,
                "orphan_sweeper_min_confidence": 45.0,
            },
            now=100.0,
        )

        self.assertEqual(result["candidates"], 1)
        self.assertEqual(result["killed"], 1)
        self.assertEqual(service.calls[0][1], 4242)
        self.assertIsNone(acc.orphan_pid)
        self.assertEqual(acc.orphan_confidence, 0.0)
        self.assertEqual(events[0][0][0], "orphan_process_swept")


    def test_orphan_sweeper_skips_active_desired_account(self):
        class FakeProcessService:
            def __init__(self):
                self.calls = []

            def safe_kill_owned_orphan(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return {"ok": True, "killed": True}

        class FakeProcessManager:
            def list_live_game_processes(self, launched_after=None):
                return []

            def get_pid_owner(self, pid):
                return ""

        acc = Account(username="ActiveUser")
        acc.state = AccountState.IN_GAME
        acc.desired_state = AccountState.IN_GAME
        acc.orphan_pid = 5252
        acc.orphan_confidence = 80.0
        acc.orphan_verify_after = 90.0
        service = FakeProcessService()

        sweeper = RuntimeOrphanSweeper(
            [acc],
            process_service=service,
            process_manager=FakeProcessManager(),
        )
        result = sweeper.sweep(
            {"orphan_sweeper_enabled": True, "orphan_sweeper_kill_enabled": True},
            now=100.0,
        )

        self.assertEqual(result["candidates"], 0)
        self.assertEqual(service.calls, [])
