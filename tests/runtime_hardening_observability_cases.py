from tests.runtime_hardening_shared import *


class RuntimeHardeningObservabilityCases:
    def test_running_invariant_requires_pid(self):
        acc = Account(username="invariant_user")
        acc.state = AccountState.IN_GAME
        acc.pid = None
        acc.sync_runtime("test")

        violations = check_runtime_invariants(acc)
        codes = {item["code"] for item in violations}
        self.assertIn("running_without_pid", codes)


    def test_timeline_records_event_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStore(os.path.join(tmp, "runtime.db"))
            try:
                timeline = RuntimeTimeline(store=store, memory_log=[], memory_limit=10)
                timeline.record({"event_type": "command_accepted", "account": "u1", "reason": "unit"})

                events = store.list_recent_events(account_id="u1", limit=10)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["event_type"], "command_accepted")
            finally:
                store.close()


    def test_runtime_store_filters_events_by_type_and_severity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStore(os.path.join(tmp, "runtime.db"))
            try:
                store.record_event({
                    "event_type": "runtime_invariant_violation",
                    "severity": "warning",
                    "account": "u1",
                    "reason": "running_without_pid",
                })
                store.record_event({
                    "event_type": "command_accepted",
                    "severity": "info",
                    "account": "u1",
                    "reason": "unit",
                })
                store.record_event({
                    "event_type": "runtime_invariant_violation",
                    "severity": "error",
                    "account": "u2",
                    "reason": "running_pid_not_live",
                })

                events = store.list_recent_events(
                    account_id="u1",
                    event_type="runtime_invariant_violation",
                    severity="warning",
                    limit=10,
                )

                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["account"], "u1")
                self.assertEqual(events[0]["event_type"], "runtime_invariant_violation")
                self.assertEqual(events[0]["severity"], "warning")
            finally:
                store.close()


    def test_runtime_diagnostic_bundle_filters_and_redacts_secrets(self):
        status = {
            "running": True,
            "total_accounts": 2,
            "launchable_count": 1,
            "blocked_count": 1,
            "in_game": 1,
            "crash": 0,
            "queued": 0,
            "failed": 1,
            "runtime_health": {"warnings": ["runtime_invariant_violations", "scheduler_overdue"]},
            "scheduler_health": {"pending_count": 3, "overdue_count": 1, "callback_failure_count": 1},
            "queue_snapshot": {"size": 0},
            "supervisor": {"ok": True},
            "accounts": [
                {
                    "username": "DiagUser",
                    "account_id": "DiagUser",
                    "display": "DiagUser",
                    "state": "FAILED",
                    "blocked_reason": "Cookie identity mismatch",
                    "cookie_username": "CookieOwner",
                    "cookie": "_|WARNING:secret-cookie",
                    "active_vip": "https://roblox.com/games/1?privateServerLinkCode=secret-link",
                    "health_flags": ["blocked", "process_binding_warning"],
                    "runtime": {
                        "orphan_pid": 4321,
                        "orphan_identity": "robloxplayerbeta.exe|1|c:\\roblox\\robloxplayerbeta.exe",
                    },
                    "launch_intent_summary": {"place_id": "1"},
                },
                {"username": "OtherUser", "account_id": "OtherUser", "state": "IN_GAME"},
            ],
        }
        events = [
            {
                "event_type": "runtime_invariant_violation",
                "severity": "warning",
                "account": "DiagUser",
                "payload": {"cookie": "_|WARNING:event-cookie", "url": "privateServerLinkCode=event-link"},
            }
        ]
        cfg = {
            "max_retry": 10,
            "runtime_invariant_monitor_enabled": True,
            "password": "secret",
            "game_private_server_url": "privateServerLinkCode=config-link",
        }

        bundle = build_runtime_diagnostic_bundle(
            status,
            events,
            cfg,
            account_id="DiagUser",
            event_type="runtime_invariant_violation",
            severity="warning",
            limit=50,
            now=1000.0,
        )

        self.assertTrue(bundle["ok"])
        self.assertEqual(bundle["summary"]["selected_accounts"], 1)
        self.assertEqual(bundle["accounts"][0]["account_id"], "DiagUser")
        self.assertEqual(bundle["accounts"][0]["cookie_username"], "CookieOwner")
        self.assertNotIn("cookie", bundle["accounts"][0])
        self.assertNotIn("active_vip", bundle["accounts"][0])
        self.assertEqual(bundle["accounts"][0]["orphan_pid"], 4321)
        self.assertEqual(bundle["scheduler_health"]["overdue_count"], 1)
        self.assertEqual(bundle["config"]["max_retry"], 10)
        self.assertNotIn("password", bundle["config"])
        self.assertNotIn("game_private_server_url", bundle["config"])
        serialized = str(bundle)
        self.assertNotIn("secret-cookie", serialized)
        self.assertNotIn("event-cookie", serialized)
        self.assertNotIn("event-link", serialized)
        self.assertIn("Resolve blocked account gates", " ".join(bundle["recommendations"]))
        self.assertIn("Check runtime scheduler health", " ".join(bundle["recommendations"]))


    def test_runtime_health_does_not_count_normal_start_as_relaunch_pressure(self):
        accounts = [{"state": "IN_GAME", "process_alive": True, "last_heartbeat": 100.0} for _ in range(6)]
        events = []
        for _ in range(6):
            events.extend([
                {"event_type": "BEGIN_REJOIN_TRANSACTION", "reason": "dispatcher_launch", "severity": "info"},
                {"event_type": "TRANSACTION_LAUNCH_SENT", "reason": "launch_sent", "severity": "info"},
                {"event_type": "END_REJOIN_TRANSACTION", "reason": "cookie_validated", "severity": "success"},
                {"event_type": "state", "reason": "launch_success", "severity": "info"},
            ])

        health = build_runtime_health(accounts, {"size": 0}, events, now=105.0)

        self.assertNotIn("relaunch_pressure", health["warnings"])
        self.assertEqual(health["relaunch_event_count"], 0)


    def test_runtime_health_counts_recovery_rejoin_pressure(self):
        accounts = [{"state": "IN_GAME", "process_alive": True, "last_heartbeat": 100.0} for _ in range(3)]
        events = [
            {"event_type": "BEGIN_REJOIN_TRANSACTION", "reason": "network_drop", "severity": "warning"},
            {"event_type": "force_rejoin", "reason": "manual_rejoin", "severity": "warning"},
            {"event_type": "launch_failed", "reason": "launch_fail_retry", "severity": "warning"},
        ]

        health = build_runtime_health(accounts, {"size": 0}, events, now=105.0)

        self.assertIn("relaunch_pressure", health["warnings"])
        self.assertEqual(health["relaunch_event_count"], 3)


    def test_runtime_health_ignores_old_relaunch_pressure_events(self):
        accounts = [{"state": "IDLE", "process_alive": False, "last_heartbeat": 0.0}]
        events = [
            {"event_type": "process_lost", "reason": "pid_dead", "severity": "info", "ts": 100.0},
            {"event_type": "error", "reason": "relaunch_loop", "severity": "critical", "ts": 101.0},
            {"event_type": "launch_failed", "reason": "launch_fail_retry", "severity": "warning", "ts": 102.0},
        ]

        health = build_runtime_health(accounts, {"size": 0}, events, now=1000.0)

        self.assertTrue(health["ok"])
        self.assertNotIn("relaunch_pressure", health["warnings"])
        self.assertEqual(health["relaunch_event_count"], 0)


    def test_runtime_health_warns_when_scheduler_is_lagging(self):
        health = build_runtime_health(
            [],
            {"size": 0},
            [],
            scheduler_snapshot={
                "overdue_count": 1,
                "max_overdue_seconds": 15.25,
                "last_dispatch_latency_seconds": 4.0,
                "callback_failure_count": 1,
            },
            now=200.0,
        )

        self.assertFalse(health["ok"])
        self.assertIn("scheduler_overdue", health["warnings"])
        self.assertIn("scheduler_callback_failures", health["warnings"])
        self.assertEqual(health["watchdog_latency_seconds"], 15.2)
        self.assertEqual(health["scheduler"]["overdue_count"], 1)


    def test_public_farm_health_is_aggregate_and_redacted(self):
        health = build_public_farm_health(
            {
                "running": True,
                "status_revision": 7,
                "status_updated_at": 80.0,
                "accounts": [
                    {
                        "account_id": "SecretUser",
                        "username": "SecretUser",
                        "state": "IN_GAME",
                        "pid_bound": True,
                        "pid": 4242,
                        "health_flags": [],
                    },
                    {
                        "account_id": "BlockedUser",
                        "username": "BlockedUser",
                        "state": "FAILED",
                        "blocked_reason": "cookie mismatch",
                        "pid": 5252,
                        "health_flags": ["blocked"],
                    },
                ],
                "queue_snapshot": {"size": 2, "busy": True, "oldest_age_seconds": 44.2},
                "runtime_health": {"ok": False, "warnings": ["relaunch_pressure"]},
                "last_runtime_event_age_seconds": 12.5,
            },
            now=100.0,
        )

        self.assertFalse(health["ok"])
        self.assertEqual(health["state"], "degraded")
        self.assertEqual(health["account_count"], 2)
        self.assertEqual(health["in_game"], 1)
        self.assertEqual(health["blocked"], 1)
        self.assertEqual(health["queue"]["oldest_age_seconds"], 44.2)
        serialized = str(health)
        self.assertNotIn("SecretUser", serialized)
        self.assertNotIn("4242", serialized)
        self.assertNotIn("accounts", health)


    def test_watchdog_action_logs_degraded_without_restarting_control_plane(self):
        action = decide_farm_watchdog_action(
            {
                "running": True,
                "runtime_health": {"warnings": ["relaunch_pressure"]},
                "stuck_states": [],
                "control_plane": {"stuck_reasons": [], "max_stuck_age_seconds": 0.0},
            },
            now=1000.0,
        )

        self.assertEqual(action["action"], "log_degraded")
        self.assertEqual(action["scope"], "farm")
        self.assertNotIn("restart", action["action"])


    def test_watchdog_action_targets_accounts_before_control_plane_restart(self):
        action = decide_farm_watchdog_action(
            {
                "running": True,
                "runtime_health": {"warnings": []},
                "stuck_states": [
                    {"account_id": "UserA", "state": "VERIFY", "age_seconds": 301.0, "reason": "verify_timeout"}
                ],
                "control_plane": {"stuck_reasons": [], "max_stuck_age_seconds": 0.0},
            },
            now=1000.0,
        )

        self.assertEqual(action["action"], "targeted_recovery")
        self.assertEqual(action["scope"], "account")
        self.assertEqual(action["account_count"], 1)


    def test_watchdog_action_restarts_stuck_control_plane_after_threshold_and_backoff(self):
        action = decide_farm_watchdog_action(
            {
                "running": True,
                "runtime_health": {"warnings": []},
                "stuck_states": [],
                "control_plane": {
                    "stuck_reasons": ["dispatcher_dead"],
                    "max_stuck_age_seconds": 301.0,
                    "last_restart_at": 500.0,
                },
            },
            now=1000.0,
            control_plane_restart_threshold_seconds=180.0,
            control_plane_restart_backoff_seconds=300.0,
        )

        self.assertEqual(action["action"], "restart_control_plane")
        self.assertEqual(action["scope"], "control_plane")
        self.assertIn("dispatcher_dead", action["reasons"])


    def test_watchdog_action_backoff_suppresses_control_plane_restart(self):
        action = decide_farm_watchdog_action(
            {
                "running": True,
                "runtime_health": {"warnings": []},
                "stuck_states": [],
                "control_plane": {
                    "stuck_reasons": ["maintenance_dead"],
                    "max_stuck_age_seconds": 301.0,
                    "last_restart_at": 900.0,
                },
            },
            now=1000.0,
            control_plane_restart_threshold_seconds=180.0,
            control_plane_restart_backoff_seconds=300.0,
        )

        self.assertEqual(action["action"], "log_degraded")
        self.assertEqual(action["scope"], "control_plane")
        self.assertTrue(action["backoff_active"])


    def test_runtime_telemetry_summarizes_recovery_and_stale_workers(self):
        status = {
            "total_rejoin": 3,
            "total_crash": 1,
            "runtime_health": {"stale_work_count": 2},
            "accounts": [
                {"state": "IN_GAME", "mem_mb": 125.5, "crash_count": 1, "health_flags": ["recovery_active"]},
                {"state": "QUEUED", "mem_mb": 64.5, "crash_count": 2, "health_flags": ["heartbeat_stale"]},
            ],
            "recent_runtime_events": [
                {"event_type": "runtime_rejoin_requested", "duration_seconds": 4.0},
                {"event_type": "runtime_rejoin_requested", "duration_seconds": 6.0},
            ],
        }

        telemetry = build_runtime_telemetry(status, now=1000.0)

        self.assertEqual(telemetry["account_count"], 2)
        self.assertEqual(telemetry["recovery_active_count"], 1)
        self.assertEqual(telemetry["stale_worker_count"], 2)
        self.assertEqual(telemetry["crash_count"], 3)
        self.assertEqual(telemetry["memory_usage_mb"], 190.0)
        self.assertEqual(telemetry["recovery_rate"], 0.75)
        self.assertEqual(telemetry["reconnect_duration_seconds"]["avg"], 5.0)


    def test_runtime_command_tracker_replays_finished_idempotency_response(self):
        account = Account(username="idem_user")
        account.state = AccountState.IN_GAME
        account.pid = 123
        state = RuntimeStateManager()
        events = []
        revisions = []
        tracker = RuntimeCommandTracker(
            runtime_state=state,
            find_account=lambda username: account if username == "idem_user" else None,
            capability=lambda action, username="": (True, "", account if username else None),
            record_timeline=lambda *args, **kwargs: events.append((args, kwargs)),
            bump_status_revision=lambda: revisions.append(1) or len(revisions),
            idempotency_ttl=60,
        )

        accepted, command = tracker.begin(
            "account:idem_user",
            "kill_pid",
            account="idem_user",
            idempotency_key="idem-1",
            request_fingerprint="POST:/api/account/idem_user/kill",
        )
        self.assertTrue(accepted)
        response = {"ok": True, "accepted": True, "command_id": command["command_id"], "msg": "killed"}
        tracker.finish("account:idem_user", command["command_id"], ok=True, response=response)

        accepted_again, replay = tracker.begin(
            "account:idem_user",
            "kill_pid",
            account="idem_user",
            idempotency_key="idem-1",
            request_fingerprint="POST:/api/account/idem_user/kill",
        )

        self.assertFalse(accepted_again)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["response"], response)


    def test_runtime_command_tracker_rejects_duplicate_and_cleans_expired_command(self):
        state = RuntimeStateManager()
        revisions = []
        tracker = RuntimeCommandTracker(
            runtime_state=state,
            find_account=lambda username: None,
            capability=lambda action, username="": (True, "", None),
            record_timeline=lambda *args, **kwargs: None,
            bump_status_revision=lambda: revisions.append(1) or len(revisions),
        )

        accepted, command = tracker.begin("global", "close_all_roblox", ttl=5)
        self.assertTrue(accepted)

        accepted_again, duplicate = tracker.begin("global", "close_all_roblox", ttl=5)
        self.assertFalse(accepted_again)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["command_id"], command["command_id"])

        tracker._commands["global"]["expires_at"] = time.time() - 1
        self.assertIsNone(tracker.command_inflight("global"))

        accepted_after_cleanup, next_command = tracker.begin("global", "close_all_roblox", ttl=5)
        self.assertTrue(accepted_after_cleanup)
        self.assertNotEqual(next_command["command_id"], command["command_id"])
