from tests.runtime_hardening_shared import *


class RuntimeHardeningRecoveryPolicyCases:
    def test_recovery_evaluator_quarantines_cookie_mismatch_before_queue(self):
        recovery, queue, stop = self._make_recovery()
        acc = Account(username="GateUser", cookie_username="OtherUser", cookie_mismatch=True)
        acc.session_checked = True
        acc.session_valid = True
        try:
            recovery.evaluate(acc, trigger="unit_gate")

            self.assertEqual(acc.state, AccountState.FAILED)
            self.assertEqual(acc.last_crash_reason, "cookie_mismatch")
            self.assertEqual(queue.snapshot()["size"], 0)
        finally:
            stop.set()
            recovery.stop()


    def test_launch_success_detail_can_mention_captcha_without_creating_hold(self):
        from services.captcha_guard import is_account_captcha_required

        recovery, _queue, stop = self._make_recovery()
        acc = Account(username="SolvedCaptchaUser")
        try:
            accepted = recovery.handle_runtime_signal(
                acc,
                "launch_success",
                "manual_verified",
                payload={"detail": "CAPTCHA solved manually", "count_rejoin": False},
            )

            self.assertTrue(accepted)
            self.assertFalse(is_account_captcha_required(acc))
            self.assertNotEqual(acc.last_crash_reason, "captcha_required")
        finally:
            stop.set()
            recovery.stop()


    def test_launch_success_cannot_override_existing_captcha_hold(self):
        from services.captcha_guard import is_account_captcha_required, set_account_captcha_hold

        recovery, _queue, stop = self._make_recovery()
        acc = Account(username="BlockedCaptchaUser")
        set_account_captcha_hold(acc, "Roblox Security verification visible", source="unit")
        try:
            accepted = recovery.handle_runtime_signal(
                acc,
                "launch_success",
                "launch_success",
                payload={"detail": "loaded", "count_rejoin": False},
            )

            self.assertTrue(accepted)
            self.assertTrue(is_account_captcha_required(acc))
            self.assertEqual(acc.state, AccountState.FAILED)
            self.assertEqual(acc.last_crash_reason, "captcha_required")
        finally:
            stop.set()
            recovery.stop()


    def test_recovery_budget_trips_circuit_breaker(self):
        recovery, _queue, stop = self._make_recovery()
        recovery._cfg.update({
            "recovery_budget_enabled": True,
            "recovery_budget_max_attempts": 2,
            "recovery_budget_window_seconds": 60,
        })
        acc = Account(username="BudgetUser")
        try:
            for _ in range(2):
                ctx = recovery._begin_recovery(acc, "connection_error", "recovering", "network", "unit", force=True)
                self.assertIsNotNone(ctx)
                recovery._release_recovery_owner(
                    acc._config_username,
                    acc.runtime_generation,
                    acc.recovery_generation,
                    "unit_release",
                )
                with acc._lock:
                    acc.recovery_inflight = False
                    acc.recovery_status = ""

            ctx = recovery._begin_recovery(acc, "connection_error", "recovering", "network", "unit", force=True)

            self.assertIsNone(ctx)
            self.assertEqual(acc.state, AccountState.FAILED)
            self.assertEqual(acc.last_crash_reason, "recovery_budget_exceeded")
            self.assertEqual(len(acc.recovery_budget_attempts), 2)
        finally:
            stop.set()
            recovery.stop()


    def test_launch_success_clears_recovery_budget(self):
        recovery, _queue, stop = self._make_recovery()
        acc = Account(username="BudgetClearUser")
        acc.recovery_budget_attempts = [time.time(), time.time()]
        try:
            recovery.report_launch_success(acc, count_rejoin=False)

            self.assertEqual(acc.recovery_budget_attempts, [])
        finally:
            stop.set()
            recovery.stop()

    def test_use_lua_defers_process_launch_success_until_lua_confirms(self):
        recovery, _queue, stop = self._make_recovery()
        recovery._cfg["use_lua"] = True
        acc = Account(username="LuaRequiredUser")
        acc.pid = 4321
        acc.process_binding_status = "verified"
        acc.process_binding_confidence = 100.0
        acc.process_proof_level = "strong"
        try:
            accepted = recovery.handle_runtime_signal(
                acc,
                "launch_success",
                "post_launch_detected",
                payload={"trigger": "post_launch_detected", "count_rejoin": False},
            )

            self.assertTrue(accepted)
            self.assertEqual(acc.state, AccountState.VERIFY)
            self.assertEqual(acc.recovery_status, "waiting_for_lua")
            self.assertIsNone(acc.in_game_since)

            accepted = recovery.handle_runtime_signal(
                acc,
                "launch_success",
                "lua_in_game_verified",
                payload={"trigger": "in_game", "evidence_source": "lua_helper", "count_rejoin": False},
            )

            self.assertTrue(accepted)
            self.assertEqual(acc.state, AccountState.IN_GAME)
            self.assertEqual(acc.recovery_status, "in_game")
            self.assertIsNotNone(acc.in_game_since)
        finally:
            stop.set()
            recovery.stop()


    def test_recovery_owner_registry_rejects_duplicate_and_stale_release(self):
        registry = RecoveryOwnerRegistry()
        registry.acquire(
            "owner_user",
            runtime_generation=2,
            recovery_generation=3,
            command_generation=4,
            session_id="session-a",
            transaction_id="txn-a",
            reason="connection_error",
            status="recovering",
            bucket="network",
            priority=80,
            token="token-a",
            now=100.0,
        )
        ctx = RecoveryAttemptContext(
            account_id="owner_user",
            runtime_generation=2,
            category=NETWORK_DISCONNECT,
            priority=10,
        )

        block = registry.block_reason("owner_user", ctx)
        duplicate = registry.check_start(
            "owner_user",
            runtime_generation=2,
            recovery_generation=3,
            reason="connection_error",
            current_state=AccountState.IN_GAME,
        )
        stale = registry.release("owner_user", runtime_generation=1, recovery_generation=3, reason="stale")
        released = registry.release("owner_user", runtime_generation=2, recovery_generation=3, reason="done")

        self.assertTrue(block["blocked"])
        self.assertFalse(duplicate["accepted"])
        self.assertEqual(duplicate["reject"], "active_recovery_owner_duplicate")
        self.assertFalse(stale["released"])
        self.assertEqual(stale["reject"], "stale_runtime_generation")
        self.assertTrue(released["released"])
        self.assertIsNone(registry.get("owner_user"))


    def test_recovery_owner_registry_clear_returns_active_count(self):
        registry = RecoveryOwnerRegistry()
        registry.acquire(
            "clear_user",
            runtime_generation=1,
            recovery_generation=1,
            command_generation=0,
            session_id="",
            transaction_id="",
            reason="unit",
            status="recovering",
            bucket="crash",
        )

        self.assertEqual(registry.clear(), 1)
        self.assertIsNone(registry.get("clear_user"))


    def test_queue_drops_stale_runtime_generation(self):
        acc = Account(username="queue_stale_user")
        queue = SmartQueue()
        queue.push(acc, reason="test_enqueue")
        acc.runtime_generation += 1

        self.assertIsNone(queue.pop(timeout=0.01))
        snapshot = queue.snapshot()
        self.assertEqual(snapshot["size"], 0)
        self.assertEqual(snapshot["stale_rejections"], 1)


    def test_error_267_normalizes_to_rejoinable_network_disconnect(self):
        self.assertEqual(normalize_disconnect_category(popup_code="267"), NETWORK_DISCONNECT)


    def test_error_268_normalizes_to_rejoinable_network_disconnect(self):
        self.assertEqual(normalize_disconnect_category(popup_code="268"), NETWORK_DISCONNECT)


    def test_error_278_normalizes_to_rejoinable_network_disconnect(self):
        self.assertEqual(normalize_disconnect_category(popup_code="278"), NETWORK_DISCONNECT)


    def test_browser_tracker_id_is_parsed_from_launch_command(self):
        self.assertEqual(
            ProcessManager.extract_browser_tracker_id_from_cmdline("roblox-player:1+browsertrackerid:BT_123"),
            "BT_123",
        )
        self.assertEqual(
            ProcessManager.extract_browser_tracker_id_from_cmdline("https://x/?browserTrackerId=ABC-789"),
            "ABC-789",
        )


    def test_session_conflict_kills_only_matching_tracker_duplicate(self):
        acc = Account(username="tracker_target")
        acc.pid = 100
        acc.browser_tracker_id = "TRACKER_A"
        ctx = RecoveryAttemptContext(
            account_id=acc._config_username,
            runtime_generation=acc.runtime_generation,
            pid=acc.pid,
            category=SESSION_CONFLICT,
            popup_code="273",
        )
        killed = []
        events = []
        entries = [
            {"pid": 101, "owner": "", "browser_tracker_id": "TRACKER_A"},
            {"pid": 102, "owner": acc._config_username, "browser_tracker_id": "TRACKER_B"},
            {"pid": 103, "owner": "other", "browser_tracker_id": "TRACKER_C"},
        ]

        result = kill_local_duplicate_for_session_conflict(
            acc,
            ctx,
            lambda: list(entries),
            lambda pid: killed.append(pid) or True,
            lambda event, **fields: events.append((event, fields)),
        )

        self.assertEqual(result, 1)
        self.assertEqual(killed, [101])
        self.assertEqual(events[0][0], "session_conflict_duplicate_killed")
        self.assertTrue(events[0][1]["browser_tracker_match"])


    def test_session_conflict_logs_when_no_matching_local_duplicate(self):
        acc = Account(username="tracker_target_none")
        acc.pid = 200
        acc.browser_tracker_id = "TRACKER_A"
        ctx = RecoveryAttemptContext(
            account_id=acc._config_username,
            runtime_generation=acc.runtime_generation,
            pid=acc.pid,
            category=SESSION_CONFLICT,
            popup_code="273",
        )
        events = []

        result = kill_local_duplicate_for_session_conflict(
            acc,
            ctx,
            lambda: [{"pid": 201, "owner": "other", "browser_tracker_id": "TRACKER_B"}],
            lambda pid: True,
            lambda event, **fields: events.append((event, fields)),
        )

        self.assertEqual(result, 0)
        self.assertEqual(events[0][0], "session_conflict_no_local_duplicate")


    def test_roblox_log_evidence_reads_recent_disconnect_without_triggering_rejoin(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "Player.log")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("info\nDisconnected from game. Error Code: 279\n")

            evidence = collect_recent_log_evidence(log_dir=tmp, since_seconds=60)

        self.assertTrue(evidence["matched"])
        self.assertEqual(evidence["error_code"], "279")
        self.assertEqual(evidence["source"], "roblox_log")


    def test_roblox_log_line_classifier_ignores_plain_runtime_noise(self):
        evidence = classify_log_line("Joining experience with place id 123")
        self.assertFalse(evidence["matched"])
        self.assertEqual(evidence["confidence"], 0.0)


    def test_roblox_log_line_maps_joined_from_other_device_to_273(self):
        evidence = classify_log_line(
            "Client has been disconnected with reason: Disconnected from game, possibly due to game joined from another device"
        )
        self.assertTrue(evidence["matched"])
        self.assertEqual(evidence["error_code"], "273")
        self.assertEqual(evidence["keyword"], "disconnected")


    def test_roblox_log_evidence_searches_past_disconnect_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "Player.log")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(
                    "Client has been disconnected with reason: Disconnected from game, possibly due to game joined from another device\n"
                )
                for index in range(500):
                    fh.write(f"render noise {index}\n")

            evidence = collect_recent_log_evidence(log_dir=tmp, since_seconds=60)

        self.assertTrue(evidence["matched"])
        self.assertEqual(evidence["error_code"], "273")


    def test_cached_log_evidence_reuses_recent_snapshot(self):
        from services.roblox_log_evidence import CachedLogEvidenceCollector

        calls = []

        def collector(**_kwargs):
            calls.append(time.time())
            return {"matched": False, "source": "unit", "reason": f"call_{len(calls)}"}

        cache = CachedLogEvidenceCollector(ttl_seconds=30.0)

        first = cache.collect(collector=collector, since_seconds=60, now=100.0)
        second = cache.collect(collector=collector, since_seconds=60, now=101.0)
        third = cache.collect(collector=collector, since_seconds=60, now=131.0)

        self.assertEqual(len(calls), 2)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(second["reason"], "call_1")
        self.assertFalse(third["cached"])
        self.assertEqual(third["reason"], "call_2")


    def test_popup_log_evidence_hot_path_does_not_sleep_or_retry(self):
        import services.roblox_liveness as roblox_liveness

        calls = []
        original_collect = roblox_liveness.collect_recent_log_evidence
        original_sleep = roblox_liveness.time.sleep
        try:
            roblox_liveness._LOG_EVIDENCE_CACHE.clear()
            roblox_liveness.collect_recent_log_evidence = lambda **kwargs: calls.append(kwargs) or {  # type: ignore[assignment]
                "matched": False,
                "source": "roblox_log",
                "reason": "unit_no_match",
            }
            roblox_liveness.time.sleep = lambda _seconds: (_ for _ in ()).throw(AssertionError("hot liveness path slept"))  # type: ignore[assignment]

            first = roblox_liveness._collect_popup_log_evidence(now=200.0)
            second = roblox_liveness._collect_popup_log_evidence(now=201.0)

            self.assertFalse(first["matched"])
            self.assertTrue(second["cached"])
            self.assertEqual(len(calls), 1)
        finally:
            roblox_liveness.collect_recent_log_evidence = original_collect  # type: ignore[assignment]
            roblox_liveness.time.sleep = original_sleep  # type: ignore[assignment]
            if hasattr(roblox_liveness, "_LOG_EVIDENCE_CACHE"):
                roblox_liveness._LOG_EVIDENCE_CACHE.clear()
