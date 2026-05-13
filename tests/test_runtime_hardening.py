import os
import atexit
import shutil
import tempfile
import threading
import time
import unittest

_TEST_USER_ROOT = tempfile.mkdtemp(prefix="argus-test-user-root-")
if "ARGUS_USER_ROOT" not in os.environ:
    os.environ["ARGUS_USER_ROOT"] = _TEST_USER_ROOT
    atexit.register(shutil.rmtree, _TEST_USER_ROOT, ignore_errors=True)
else:
    shutil.rmtree(_TEST_USER_ROOT, ignore_errors=True)

from core import Account, AccountState, EventBus, SmartQueue, StateManager
import farm as farm_module
from farm import RecoveryCoordinator, SystemMaintenance
from runtime.recovery_context import NETWORK_DISCONNECT, SESSION_CONFLICT, RecoveryAttemptContext, normalize_disconnect_category
from runtime.recovery_owner import RecoveryOwnerRegistry
from runtime.recovery_policy import kill_local_duplicate_for_session_conflict
from runtime.runtime_invariants import check_runtime_invariants
from runtime.runtime_health import build_runtime_health
from runtime.runtime_store import RuntimeStore
from runtime.runtime_timeline import RuntimeTimeline
from runtime.telemetry_view import build_runtime_telemetry
from runtime.command_tracker import RuntimeCommandTracker
from runtime.runtime_scheduler import RuntimeScheduler
from runtime.runtime_state_manager import RuntimeStateManager
from services.network_fault_injector import CommandResult, NetworkFaultInjector, RULE_PREFIX
from services.process_service import ProcessService
from services.roblox_log_evidence import classify_log_line, collect_recent_log_evidence
from process_net import ProcessManager


def auth_post(client, path, **kwargs):
    import main

    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("X-Argus-Token", main.INSTANCE_TOKEN)
    return client.post(path, headers=headers, **kwargs)


class RuntimeHardeningTests(unittest.TestCase):
    class _AlwaysOnlineNet:
        def is_online(self):
            return True

    def _make_recovery(self):
        stop = threading.Event()
        queue = SmartQueue()
        bus = EventBus()
        state_mgr = StateManager(bus)
        recovery = RecoveryCoordinator(
            queue,
            state_mgr,
            bus,
            self._AlwaysOnlineNet(),
            stop,
            {
                "auto_rejoin": True,
                "max_fail_count": 5,
                "max_retry": 10,
                "queue_delay_seconds": 1,
                "network_check_interval": 1,
            },
            accounts=[],
        )
        return recovery, queue, stop

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

    def test_presence_mismatch_does_not_rejoin_when_local_process_is_healthy(self):
        farm = object.__new__(SystemMaintenance)
        farm._cfg = {
            "presence_api_enabled": True,
            "presence_assist_rejoin_enabled": True,
            "connection_error_rejoin": True,
            "connection_error_hold_time": 1,
        }
        farm._supervisor = None
        farm._presence_disconnect_reason = lambda acc, now, in_game_for, loading_grace: (  # type: ignore[attr-defined]
            "presence_not_ingame:Online",
            {"presence_type_name": "Online", "presence_last_location": "Website"},
        )

        acc = Account(username="healthy_presence_user")
        acc.state = AccountState.IN_GAME
        acc.desired_state = AccountState.IN_GAME
        acc.liveness_state = "alive"
        now = time.time()

        handled = farm._handle_presence_disconnect_assist(  # type: ignore[attr-defined]
            acc,
            worker=None,
            now=now,
            pid=1234,
            in_game_for=120,
            loading_grace=30,
            allow_rejoin=False,
        )

        self.assertFalse(handled)
        self.assertEqual(acc.presence_mismatch_reason, "presence_not_ingame:Online")
        self.assertEqual(acc.last_watchdog_classification, "presence_mismatch_observed")
        self.assertNotEqual(acc.liveness_state, "presence_disconnected")

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

    def test_network_fault_scripts_are_scoped_to_argus_rules(self):
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
