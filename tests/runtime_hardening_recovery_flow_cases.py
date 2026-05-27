from tests.runtime_hardening_shared import *


class RuntimeHardeningRecoveryFlowCases:
    def test_memory_pressure_hold_log_is_rate_limited(self):
        import runtime.maintenance_watchdog_actions as watchdog_actions

        class AccountStub:
            display_name = "RateLimitUser"

        calls = []
        original_log = watchdog_actions.flog_kv
        try:
            watchdog_actions.WATCHDOG_LOG_RATE_LIMITER.clear()
            watchdog_actions.flog_kv = lambda *args, **kwargs: calls.append((args, kwargs))  # type: ignore[assignment]
            pressure = {"ram_mb": 7000, "limit_mb": 6144, "high_for": 1.0}

            watchdog_actions.log_memory_pressure_hold(AccountStub(), 1234, pressure, 30.0)
            watchdog_actions.log_memory_pressure_hold(AccountStub(), 1234, pressure, 30.0)

            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0][1], "memory_pressure_hold")
        finally:
            watchdog_actions.flog_kv = original_log  # type: ignore[assignment]
            if hasattr(watchdog_actions, "WATCHDOG_LOG_RATE_LIMITER"):
                watchdog_actions.WATCHDOG_LOG_RATE_LIMITER.clear()


    def test_recovery_evaluate_rejects_stale_runtime_generation(self):
        recovery, queue, stop = self._make_recovery()
        acc = Account(username="stale_eval_user")
        acc.state = AccountState.READY
        acc.desired_state = AccountState.IN_GAME
        acc.session_checked = True
        acc.session_valid = True
        acc.runtime_generation = 2
        try:
            recovery.evaluate(acc, trigger="unit_stale", expected_runtime_generation=1)
            self.assertEqual(queue.snapshot()["size"], 0)
            self.assertEqual(acc.state, AccountState.READY)
        finally:
            stop.set()
            recovery.stop()


    def test_rejoin_requested_routes_through_runtime_signal_boundary(self):
        recovery, _queue, stop = self._make_recovery()
        acc = Account(username="manual_rejoin_user")
        calls = []
        recovery.force_rejoin = lambda target: calls.append(target)  # type: ignore[method-assign]
        try:
            routed = recovery.handle_runtime_signal(
                acc,
                "rejoin_requested",
                "unit_manual",
                expected_runtime_generation=acc.runtime_generation,
            )
            self.assertTrue(routed)
            self.assertEqual(calls, [acc])
        finally:
            stop.set()
            recovery.stop()


    def test_visual_disconnect_signal_is_enriched_from_late_roblox_log_evidence(self):
        recovery, _queue, stop = self._make_recovery()
        acc = Account(username="visual_log_enrich_user")
        acc.state = AccountState.IN_GAME
        acc.desired_state = AccountState.IN_GAME
        captured = {}
        original_collect = farm_module.collect_recent_log_evidence
        farm_module.collect_recent_log_evidence = lambda **kwargs: {  # type: ignore[assignment]
            "matched": True,
            "source": "roblox_log",
            "error_code": "273",
            "keyword": "disconnected",
            "confidence": 1.2,
            "line": "Lost connection with reason : Disconnected from game, possibly due to game joined from another device",
        }
        recovery.report_crash = lambda target, reason_key, reason_msg, cooldown=None, context=None: captured.update({  # type: ignore[method-assign]
            "target": target,
            "reason_key": reason_key,
            "reason_msg": reason_msg,
            "context": context,
        })
        try:
            routed = recovery.handle_runtime_signal(
                acc,
                "disconnect_detected",
                "connection_error",
                payload={
                    "trigger": "watchdog_popup",
                    "detail": "PID=123 UI=visual_disconnect source=center_modal",
                    "visual_disconnect": True,
                    "evidence_source": "center_modal",
                    "disconnect_category": "VISUAL_DISCONNECT",
                },
                expected_runtime_generation=acc.runtime_generation,
            )
            self.assertTrue(routed)
            self.assertEqual(captured["reason_key"], "session_conflict")
            self.assertIn("roblox_log=", captured["reason_msg"])
            self.assertEqual(captured["context"].popup_code, "273")
            self.assertEqual(captured["context"].category, SESSION_CONFLICT)
        finally:
            farm_module.collect_recent_log_evidence = original_collect  # type: ignore[assignment]
            stop.set()
            recovery.stop()


    def test_duplicate_recovery_signal_suppresses_second_side_effect(self):
        recovery, _queue, stop = self._make_recovery()
        acc = Account(username="duplicate_signal_user")
        acc.state = AccountState.IN_GAME
        acc.desired_state = AccountState.IN_GAME
        calls = []
        recovery.report_crash = lambda target, reason_key, reason_msg, cooldown=None, context=None: calls.append(reason_key)  # type: ignore[method-assign]
        try:
            first = recovery.handle_runtime_signal(
                acc,
                "fault",
                "connection_error",
                payload={"detail": "Disconnected 277"},
                expected_runtime_generation=acc.runtime_generation,
            )
            second = recovery.handle_runtime_signal(
                acc,
                "fault",
                "connection_error",
                payload={"detail": "Disconnected 277"},
                expected_runtime_generation=acc.runtime_generation,
            )
            self.assertTrue(first)
            self.assertTrue(second)
            self.assertEqual(calls, ["connection_error"])
        finally:
            stop.set()
            recovery.stop()


    def test_recovery_owner_releases_on_success_fail_and_queue(self):
        recovery, _queue, stop = self._make_recovery()
        try:
            success_acc = Account(username="owner_success_user")
            success_acc.state = AccountState.IN_GAME
            success_acc.desired_state = AccountState.IN_GAME
            self.assertIsNotNone(recovery._begin_recovery(success_acc, "connection_error", "recovering", "network"))
            self.assertIsNotNone(recovery._owner_registry.get(success_acc._config_username))
            recovery.report_launch_success(success_acc)
            self.assertIsNone(recovery._owner_registry.get(success_acc._config_username))

            failed_acc = Account(username="owner_failed_user")
            failed_acc.state = AccountState.IN_GAME
            failed_acc.desired_state = AccountState.IN_GAME
            self.assertIsNotNone(recovery._begin_recovery(failed_acc, "process_crash", "recovering", "crash"))
            self.assertIsNotNone(recovery._owner_registry.get(failed_acc._config_username))
            recovery.fail_account(failed_acc, "unit_fail", "unit fail")
            self.assertIsNone(recovery._owner_registry.get(failed_acc._config_username))

            queued_acc = Account(username="owner_queued_user")
            queued_acc.state = AccountState.READY
            queued_acc.desired_state = AccountState.IN_GAME
            self.assertIsNotNone(recovery._begin_recovery(queued_acc, "force_rejoin", "manual", "manual", force=True))
            self.assertIsNotNone(recovery._owner_registry.get(queued_acc._config_username))
            recovery._queue_account(queued_acc, "unit_queue")
            self.assertIsNone(recovery._owner_registry.get(queued_acc._config_username))
        finally:
            stop.set()
            recovery.stop()


    def test_connection_error_is_not_treated_as_rapid_crash_loop(self):
        recovery, _queue, stop = self._make_recovery()
        acc = Account(username="disconnect_277_user")
        acc.state = AccountState.IN_GAME
        acc.desired_state = AccountState.IN_GAME
        acc.in_game_since = time.time()
        acc.rapid_relaunch_count = 2
        try:
            for _ in range(5):
                self.assertIsNone(recovery._detect_relaunch_loop(acc, "connection_error"))
            self.assertEqual(acc.rapid_relaunch_count, 0)
        finally:
            stop.set()
            recovery.stop()


    def test_relaunch_loop_enters_cooldown_instead_of_failed_by_default(self):
        recovery, _queue, stop = self._make_recovery()
        recovery._cfg["relaunch_loop_limit"] = 3
        recovery._cfg["relaunch_loop_window"] = 45
        recovery._cfg["relaunch_loop_cooldown_seconds"] = 30
        acc = Account(username="rapid_crash_user")
        acc.state = AccountState.IN_GAME
        acc.desired_state = AccountState.IN_GAME
        acc.session_checked = True
        acc.session_valid = True
        acc.in_game_since = time.time()
        acc.rapid_relaunch_count = 2
        acc.pid = 4321
        acc.bound_process_identity = "robloxplayerbeta.exe|1|c:\\roblox\\robloxplayerbeta.exe"
        original_kill = ProcessService.safe_kill_bound_process
        ProcessService.safe_kill_bound_process = staticmethod(
            lambda *args, **kwargs: {"ok": True, "killed": True, "pid": 4321, "reason": "killed"}
        )
        try:
            recovery.report_crash(acc, "process_crash", "rapid crash")

            self.assertEqual(acc.state, AccountState.COOLDOWN)
            self.assertEqual(acc.recovery_status, "scheduled")
            self.assertEqual(acc.last_recovery_reason, "relaunch_loop")
            self.assertEqual(acc.rapid_relaunch_count, 0)
            self.assertGreater(acc.recovery_scheduled_at, time.time())
            self.assertNotEqual(acc.state, AccountState.FAILED)
        finally:
            ProcessService.safe_kill_bound_process = original_kill
            stop.set()
            recovery.stop()


    def test_connection_error_recovery_does_not_increment_crash_or_fail_counts(self):
        recovery, _queue, stop = self._make_recovery()
        acc = Account(username="disconnect_recovery_user")
        acc.state = AccountState.IN_GAME
        acc.desired_state = AccountState.IN_GAME
        acc.session_checked = True
        acc.session_valid = True
        acc.in_game_since = time.time()
        try:
            recovery.report_crash(acc, "connection_error", "Disconnected 277")
            self.assertEqual(acc.crash_count, 0)
            self.assertEqual(acc.fail_count, 0)
            self.assertEqual(acc.network_retry_count, 1)
            self.assertNotEqual(acc.state, AccountState.FAILED)
        finally:
            stop.set()
            recovery.stop()


    def test_launch_success_clears_stale_disconnect_watchdog_status(self):
        recovery, _queue, stop = self._make_recovery()
        acc = Account(username="launch_success_status_user")
        acc.state = AccountState.VERIFY
        acc.desired_state = AccountState.IN_GAME
        acc.session_checked = True
        acc.session_valid = True
        acc.pid = 1234
        acc.process_binding_status = "verified"
        acc.process_proof_level = "strong"
        acc.last_watchdog_classification = "disconnect_dialog_rejoin"
        acc.liveness_state = "reconnecting"
        acc.liveness_suspect_since = time.time()
        try:
            recovery.report_launch_success(acc)
            self.assertEqual(acc.state, AccountState.IN_GAME)
            self.assertEqual(acc.recovery_status, "in_game")
            self.assertEqual(acc.last_recovery_reason, "launch_success")
            self.assertEqual(acc.last_watchdog_classification, "alive")
            self.assertEqual(acc.liveness_state, "alive")
            self.assertEqual(acc.liveness_suspect_since, 0.0)
        finally:
            stop.set()
            recovery.stop()


    def test_recovery_with_context_pid_schedules_after_kill(self):
        recovery, queue, stop = self._make_recovery()
        acc = Account(username="context_pid_recovery_user")
        acc.state = AccountState.IN_GAME
        acc.desired_state = AccountState.IN_GAME
        acc.session_checked = True
        acc.session_valid = True
        acc.pid = 4321
        acc.bound_process_identity = "robloxplayerbeta.exe|1|c:\\roblox\\robloxplayerbeta.exe"
        acc.sync_runtime("unit")
        context = RecoveryAttemptContext(
            account_id=acc._config_username,
            runtime_generation=acc.runtime_generation,
            pid=acc.pid,
            trigger="fault",
            category=NETWORK_DISCONNECT,
        )
        original_kill = ProcessService.safe_kill_bound_process
        ProcessService.safe_kill_bound_process = staticmethod(
            lambda *args, **kwargs: {"ok": True, "killed": True, "pid": 4321, "reason": "killed"}
        )
        try:
            recovery.report_crash(acc, "connection_error", "Disconnected 277", cooldown=60, context=context)
            self.assertEqual(acc.state, AccountState.COOLDOWN)
            self.assertGreater(acc.recovery_scheduled_at, time.time())
            self.assertEqual(acc.recovery_status, "scheduled")
        finally:
            ProcessService.safe_kill_bound_process = original_kill
            stop.set()
            recovery.stop()


    def test_recovery_schedule_survives_process_cleanup_runtime_generation_drift(self):
        stop = threading.Event()
        queue = SmartQueue()
        bus = EventBus()
        runtime_state = RuntimeStateManager(logger=lambda *args, **kwargs: None)
        scheduler = RuntimeScheduler(
            stop=stop,
            state_manager=runtime_state,
            logger=lambda *args, **kwargs: None,
            autostart=False,
        )
        recovery = RecoveryCoordinator(
            queue,
            StateManager(bus),
            bus,
            self._AlwaysOnlineNet(),
            stop,
            {"auto_rejoin": True, "max_fail_count": 5, "max_retry": 10, "queue_delay_seconds": 1},
            accounts=[],
            runtime_state=runtime_state,
            scheduler=scheduler,
        )
        acc = Account(username="popup_runtime_drift_user")
        acc.state = AccountState.IN_GAME
        acc.desired_state = AccountState.IN_GAME
        acc.session_checked = True
        acc.session_valid = True
        acc.pid = 4321
        acc.sync_runtime("unit")
        context = RecoveryAttemptContext(
            account_id=acc._config_username,
            runtime_generation=acc.runtime_generation,
            pid=acc.pid,
            trigger="fault",
            category=NETWORK_DISCONNECT,
            popup_code="277",
        )
        original_kill = ProcessService.safe_kill_bound_process
        ProcessService.safe_kill_bound_process = staticmethod(
            lambda *args, **kwargs: {"ok": True, "killed": True, "pid": 4321, "reason": "killed"}
        )
        try:
            recovery.report_crash(acc, "connection_error", "Disconnected 277", cooldown=0.0, context=context)
            self.assertIsNotNone(scheduler.get(f"recovery:{acc._config_username}"))
            acc.runtime_generation += 1
            acc.sync_runtime("late_process_cleanup")

            self.assertEqual(scheduler.run_due(now=time.time() + 0.1), 1)

            self.assertEqual(acc.state, AccountState.QUEUED)
            self.assertEqual(acc.recovery_status, "queued")
            self.assertEqual(queue.snapshot()["size"], 1)
        finally:
            ProcessService.safe_kill_bound_process = original_kill
            stop.set()
            recovery.stop()
            scheduler.stop()


    def test_queued_state_without_queue_entry_is_repaired(self):
        recovery, queue, stop = self._make_recovery()
        acc = Account(username="orphan_queued_user")
        acc.state = AccountState.QUEUED
        acc.desired_state = AccountState.IN_GAME
        acc.session_checked = True
        acc.session_valid = True
        try:
            recovery.evaluate(acc, trigger="queue_timeout")

            self.assertEqual(acc.state, AccountState.QUEUED)
            self.assertEqual(acc.recovery_status, "queued")
            self.assertEqual(queue.snapshot()["size"], 1)
            self.assertIs(queue.pop(timeout=0.01), acc)
        finally:
            stop.set()
            recovery.stop()


    def test_recovery_cooldown_schedule_keeps_runtime_fields(self):
        recovery, _queue, stop = self._make_recovery()
        acc = Account(username="cooldown_schedule_user")
        acc.state = AccountState.READY
        acc.desired_state = AccountState.IN_GAME
        try:
            recovery._schedule_cooldown(acc, 30.0, "unit_cooldown", "unit_transition")
            job = recovery._scheduler.get(f"recovery:{acc._config_username}")

            self.assertEqual(acc.recovery_status, "scheduled")
            self.assertGreater(acc.cooldown_until, time.time())
            self.assertGreater(acc.recovery_scheduled_at, time.time())
            self.assertEqual(acc.scheduler_slot, f"recovery:{acc._config_username}")
            self.assertIsNotNone(job)
            self.assertEqual(job.reason, "unit_transition")
            self.assertEqual(job.recovery_generation, acc.recovery_generation)
        finally:
            stop.set()
            recovery.stop()


    def test_network_restore_clears_cooldown_and_queues_immediately(self):
        recovery, queue, stop = self._make_recovery()
        acc = Account(username="network_restore_fast_user")
        acc.state = AccountState.COOLDOWN
        acc.desired_state = AccountState.IN_GAME
        acc.session_checked = True
        acc.session_valid = True
        acc.recovery_status = "scheduled"
        acc.recovery_inflight = True
        acc.cooldown_until = time.time() + 45
        acc.recovery_scheduled_at = acc.cooldown_until
        acc.scheduler_slot = f"recovery:{acc._config_username}"
        key = acc.scheduler_slot
        recovery._scheduler.schedule_once(
            key,
            lambda job: None,
            delay=45,
            account=acc,
            runtime_generation=acc.runtime_generation,
            recovery_generation=acc.recovery_generation,
        )
        try:
            recovery.on_network_restored([acc])

            self.assertEqual(acc.cooldown_until, 0.0)
            self.assertEqual(acc.recovery_scheduled_at, 0.0)
            self.assertEqual(acc.scheduler_slot, "")
            self.assertIsNone(recovery._scheduler.get(key))
            self.assertEqual(acc.state, AccountState.QUEUED)
            self.assertEqual(acc.recovery_status, "queued")
            self.assertEqual(queue.snapshot()["size"], 1)
        finally:
            stop.set()
            recovery.stop()
