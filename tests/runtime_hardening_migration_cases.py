from tests.runtime_hardening_shared import *


class RuntimeHardeningMigrationCases:
    def test_runtime_migration_flags_default_to_disabled(self):
        from config_store import DEFAULTS
        from runtime.migration_flags import RUNTIME_MIGRATION_FLAGS, enabled_flags

        flags = enabled_flags(DEFAULTS)

        self.assertEqual(set(flags), set(RUNTIME_MIGRATION_FLAGS))
        self.assertTrue(all(value is False for value in flags.values()))


    def test_capacity_profiles_describe_5_10_20_account_budgets(self):
        from runtime.migration_flags import capacity_profile

        low = capacity_profile({"runtime_capacity_profile": "low"})
        medium = capacity_profile({"runtime_capacity_profile": "medium"})
        high = capacity_profile({"runtime_capacity_profile": "high"})

        self.assertEqual(low.target_accounts, 5)
        self.assertEqual(medium.target_accounts, 10)
        self.assertEqual(high.target_accounts, 20)
        self.assertLessEqual(low.max_launching, medium.max_launching)
        self.assertLessEqual(medium.max_launching, high.max_launching)


    def test_fake_process_adapter_requires_strong_proof_for_kill(self):
        from services.roblox_process_adapter import FakeRobloxProcessAdapter

        acc = Account(username="weak_process_user")
        acc.pid = 1001
        adapter = FakeRobloxProcessAdapter([
            {"pid": 1001, "alive": True, "proof_level": "medium"},
        ])

        result = adapter.safe_kill_bound_process(acc)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "insufficient_process_proof")
        self.assertEqual(adapter.killed_pids, [])


    def test_process_adapter_preserves_service_process_proof_level(self):
        from services.roblox_process_adapter import RobloxProcessAdapter

        class FakeProcessService:
            @staticmethod
            def validate_binding(account, pid, **kwargs):
                return {
                    "ok": True,
                    "pid": pid,
                    "reason": "validated",
                    "process_proof_level": "strong",
                }

        adapter = RobloxProcessAdapter(process_service=FakeProcessService)

        result = adapter.validate_binding(Account(username="proof_user"), 1002)

        self.assertTrue(result.ok)
        self.assertEqual(result.proof_level, "strong")


    def test_fake_process_adapter_kills_strongly_proven_bound_process(self):
        from services.roblox_process_adapter import FakeRobloxProcessAdapter

        acc = Account(username="strong_process_user")
        acc.pid = 1002
        adapter = FakeRobloxProcessAdapter([
            {"pid": 1002, "alive": True, "proof_level": "strong"},
        ])

        result = adapter.safe_kill_bound_process(acc)

        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "killed")
        self.assertEqual(adapter.killed_pids, [1002])
        self.assertFalse(adapter.is_bound_game_alive(acc, 1002))


    def test_process_snapshot_cache_reuses_fresh_snapshot_and_refreshes_stale_one(self):
        from runtime.process_snapshot_cache import ProcessSnapshotCache
        from services.roblox_process_adapter import FakeRobloxProcessAdapter

        now = [100.0]
        adapter = FakeRobloxProcessAdapter([
            {"pid": 2001, "alive": True, "proof_level": "strong"},
        ])
        cache = ProcessSnapshotCache(adapter, ttl_seconds=2.0, clock=lambda: now[0])

        first = cache.snapshot()
        second = cache.snapshot()
        now[0] = 103.0
        third = cache.snapshot()

        self.assertEqual(first.version, second.version)
        self.assertGreater(third.version, second.version)
        self.assertEqual(third.count, 1)


    def test_process_snapshot_cache_uses_live_validation_for_actions(self):
        from runtime.process_snapshot_cache import ProcessSnapshotCache
        from services.roblox_process_adapter import FakeRobloxProcessAdapter

        acc = Account(username="snapshot_action_user")
        acc.pid = 2002
        adapter = FakeRobloxProcessAdapter([
            {"pid": 2002, "alive": True, "proof_level": "strong", "identity": "old"},
        ])
        cache = ProcessSnapshotCache(adapter, ttl_seconds=60.0, clock=lambda: 100.0)
        snapshot = cache.snapshot()
        adapter._processes[2002]["identity"] = "new"

        result = cache.validate_live_for_action(acc, 2002, reason="unit")

        self.assertEqual(snapshot.find_pid(2002)["identity"], "old")
        self.assertEqual(result.payload["identity"], "new")


    def test_farm_supervisor_blocks_launch_stampede_for_20_account_profile(self):
        from runtime.farm_supervisor import FarmSupervisor

        accounts = [Account(username=f"user{i}") for i in range(20)]
        accounts[0].state = AccountState.LAUNCHING
        accounts[1].state = AccountState.VERIFY
        candidate = accounts[2]
        supervisor = FarmSupervisor({"runtime_capacity_profile": "high"}, clock=lambda: 100.0)

        decision = supervisor.admit_launch(candidate, accounts, live_processes=2)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "launch_capacity_busy")
        self.assertEqual(decision.snapshot.profile.target_accounts, 20)


    def test_farm_supervisor_blocks_launch_when_profile_target_is_exceeded(self):
        from runtime.farm_supervisor import FarmSupervisor

        accounts = [Account(username=f"user{i}") for i in range(21)]
        supervisor = FarmSupervisor({"runtime_capacity_profile": "high"}, clock=lambda: 100.0)

        decision = supervisor.admit_launch(accounts[0], accounts, live_processes=0)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "profile_target_exceeded")
        self.assertEqual(decision.snapshot.desired_accounts, 21)


    def test_farm_supervisor_allows_medium_profile_when_launch_slot_available(self):
        from runtime.farm_supervisor import FarmSupervisor

        accounts = [Account(username=f"user{i}") for i in range(10)]
        candidate = accounts[0]
        supervisor = FarmSupervisor({"runtime_capacity_profile": "medium"}, clock=lambda: 100.0)

        decision = supervisor.admit_launch(candidate, accounts, live_processes=4)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed")
        self.assertEqual(decision.snapshot.profile.target_accounts, 10)


    def test_account_runtime_actor_prioritizes_stop_signals_in_shadow_mode(self):
        from runtime.account_runtime_actor import AccountRuntimeActor, AccountRuntimeSignal

        actor = AccountRuntimeActor(Account(username="actor_user"), max_mailbox=4, shadow_only=True)
        actor.submit(AccountRuntimeSignal("evaluate", reason="normal", priority=50))
        actor.submit(AccountRuntimeSignal("stop", reason="shutdown", priority=0))

        decisions = actor.drain(max_items=2)

        self.assertEqual(decisions[0].action, "shadow_stop")
        self.assertEqual(decisions[1].action, "shadow_evaluate")
        self.assertEqual(actor.queue_depth, 0)


    def test_account_runtime_actor_keeps_concurrent_submissions_ordered_and_counted(self):
        from runtime.account_runtime_actor import AccountRuntimeActor, AccountRuntimeSignal

        actor = AccountRuntimeActor(Account(username="actor_concurrent_user"), max_mailbox=200, shadow_only=True)

        def submit_range(offset):
            for index in range(20):
                actor.submit(AccountRuntimeSignal("evaluate", reason=f"{offset + index:03d}", priority=50))

        threads = [threading.Thread(target=submit_range, args=(offset,)) for offset in range(0, 100, 20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1.0)

        decisions = actor.drain(max_items=100)

        self.assertEqual(len(decisions), 100)
        self.assertEqual(actor.queue_depth, 0)
        self.assertEqual(sorted(item.reason for item in decisions), [f"{index:03d}" for index in range(100)])


    def test_account_runtime_actor_rejects_when_mailbox_is_full(self):
        from runtime.account_runtime_actor import AccountRuntimeActor, AccountRuntimeSignal

        actor = AccountRuntimeActor(Account(username="actor_full_user"), max_mailbox=1, shadow_only=True)
        first = actor.submit(AccountRuntimeSignal("evaluate", reason="first"))
        second = actor.submit(AccountRuntimeSignal("evaluate", reason="second"))

        self.assertTrue(first.accepted)
        self.assertFalse(second.accepted)
        self.assertEqual(second.reason, "mailbox_full")


    def test_account_runtime_actor_can_delegate_low_risk_signals_when_enabled(self):
        from runtime.account_runtime_actor import AccountRuntimeActor, AccountRuntimeSignal

        calls = []

        class Controller:
            def request_evaluate(self, account, trigger, force_restart=False):
                calls.append(("evaluate", account._config_username, trigger, force_restart))
                return True

        actor = AccountRuntimeActor(
            Account(username="actor_delegate_user"),
            controller=Controller(),
            shadow_only=False,
        )
        actor.submit(AccountRuntimeSignal("evaluate", reason="unit", priority=50))

        decisions = actor.drain(max_items=1)

        self.assertTrue(decisions[0].accepted)
        self.assertEqual(calls, [("evaluate", "actor_delegate_user", "unit", False)])


    def test_recovery_decision_policy_fails_auth_and_captcha_reasons(self):
        from runtime.recovery_decision_policy import RecoveryDecisionPolicy, RecoveryPolicyInput

        policy = RecoveryDecisionPolicy()

        auth = policy.decide(RecoveryPolicyInput(state="CRASH", reason="cookie_invalid"))
        captcha = policy.decide(RecoveryPolicyInput(state="CRASH", reason="captcha"))

        self.assertEqual(auth.action, "FAIL_ACCOUNT")
        self.assertTrue(auth.fatal)
        self.assertEqual(captcha.action, "FAIL_ACCOUNT")
        self.assertTrue(captcha.fatal)


    def test_recovery_decision_policy_waits_for_network_and_cooldown(self):
        from runtime.recovery_decision_policy import RecoveryDecisionPolicy, RecoveryPolicyInput

        policy = RecoveryDecisionPolicy()

        network = policy.decide(RecoveryPolicyInput(state="CRASH", reason="pid_dead", network_online=False))
        cooldown = policy.decide(RecoveryPolicyInput(state="CRASH", reason="pid_dead", cooldown_until=120.0, now=100.0))

        self.assertEqual(network.action, "WAIT")
        self.assertEqual(network.reason, "network_not_online")
        self.assertEqual(cooldown.action, "WAIT")
        self.assertEqual(cooldown.retry_after_seconds, 20.0)


    def test_recovery_decision_policy_queues_relaunch_for_recoverable_process_loss(self):
        from runtime.recovery_decision_policy import RecoveryDecisionPolicy, RecoveryPolicyInput

        decision = RecoveryDecisionPolicy().decide(RecoveryPolicyInput(state="IN_GAME", reason="pid_dead"))

        self.assertEqual(decision.action, "QUEUE_RELAUNCH")
        self.assertEqual(decision.reason, "process_crash")
