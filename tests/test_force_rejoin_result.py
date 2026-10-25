"""Manual rejoin: the coordinator result must reach the caller (R1), and a
manual retry must break only a transient FAILED hold (R2)."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import AccountState
from domain.account_model import Account
from runtime.farm import FarmController
from runtime.recovery_reports import RecoveryReportsMixin
from runtime.recovery_signal_router import RecoverySignalRouter
from runtime.recovery_txn import RecoveryTxnMixin
from runtime.runtime_state_manager import RuntimeStateManager
from services.auth_gate import AuthGateDecision
from services.captcha_guard import CAPTCHA_REASON, set_account_captcha_hold


class _FakeRecovery:
    def __init__(self, result=True):
        self.result = result
        self.rejoin_calls = 0
        self.failures = []

    def force_rejoin(self, acc):
        self.rejoin_calls += 1
        return self.result

    def fail_account(self, acc, reason, msg=""):
        self.failures.append((reason, msg))


class _Txn(RecoveryTxnMixin):
    def __init__(self):
        self._stop = threading.Event()
        self._closed = False
        self.decisions = []

    def _log_recovery_decision(self, *args, **kwargs):
        self.decisions.append((args, kwargs))


class _Reports(RecoveryReportsMixin):
    pass


class _ForceRejoin(RecoveryReportsMixin):
    def __init__(self, begin, *, evaluate_result=True, evaluate_fails=False, kill_result=None):
        self._begin = begin
        self._runtime_state = SimpleNamespace(
            set_cooldown=lambda *a, **k: None,
            set_recovery=lambda *a, **k: None,
        )
        self._state_mgr = SimpleNamespace(transition=self._transition)
        self._bus = SimpleNamespace(emit=lambda *a, **k: None)
        self.transitions = []
        self.evaluated = []
        self.persisted = 0
        self.begin_calls = []
        self.released = []
        self.evaluate_result = evaluate_result
        self.evaluate_fails = evaluate_fails
        self.kill_result = kill_result or {"ok": True, "killed": True}
        self.kill_calls = []

    def _transition(self, acc, state, **kwargs):
        self.transitions.append((acc, state, kwargs))
        acc.state = state
        return True

    def _executor_held(self):
        return False

    def _begin_recovery(self, *args, **kwargs):
        self.begin_calls.append((args, kwargs))
        return self._begin

    def _log_recovery_decision(self, *args, **kwargs):
        pass

    def request_evaluate(self, *args, **kwargs):
        self.evaluated.append((args, kwargs))
        if self.evaluate_fails:
            acc = args[0] if args else kwargs.get("acc")
            acc.state = AccountState.FAILED
            acc.recovery_status = "failed"
        return self.evaluate_result

    def _release_recovery_owner(self, *args, **kwargs):
        self.released.append((args, kwargs))

    def _persist_runtime(self, **kwargs):
        self.persisted += 1


class _FarmStub:
    def __init__(self, acc, recovery):
        self.acc = acc
        self._recovery = recovery
        self.running = True
        self.rate_checks = 0

    def _find_account(self, username):
        return self.acc if username == self.acc.username else None

    def _check_force_rejoin_rate_limit(self, account_key):
        self.rate_checks += 1
        return True, ""

    def _force_rejoin_block_reason(self, acc):
        return FarmController._force_rejoin_block_reason(self, acc)

    def _route_rejoin_command(self, acc, reason):
        return FarmController._route_rejoin_command(self, acc, reason)

    def _wake_rejoin_worker(self, acc):
        return FarmController._wake_rejoin_worker(self, acc)

    def _push_event(self, *args, **kwargs):
        pass


def _router():
    return RecoverySignalRouter(
        RuntimeStateManager(logger=lambda *a, **k: None),
        is_closed=lambda: False,
        log_decision=lambda *a, **k: None,
        recovery_gate=SimpleNamespace(check=lambda **k: {"ignored": False}),
    )


def _rejoin_acc():
    acc = Account(username="u")
    acc.desired_state = AccountState.IN_GAME
    acc.state = AccountState.FAILED
    return acc


def _gate(blocked=False, reason_key="", reason=""):
    return SimpleNamespace(blocked=blocked, reason_key=reason_key, reason=reason)


class RouterResultPropagationTests(unittest.TestCase):
    def _dispatch(self, result):
        acc = _rejoin_acc()
        return _router()._dispatch(
            _FakeRecovery(result),
            acc,
            "rejoin_requested",
            "manual",
            {},
            None,
            None,
            "",
            "",
            "",
        )

    def test_rejoin_failure_is_propagated(self):
        self.assertFalse(self._dispatch(False))

    def test_rejoin_success_is_propagated(self):
        self.assertTrue(self._dispatch(True))


class RouterRouteGateTests(unittest.TestCase):
    def _router(self, verdict):
        return RecoverySignalRouter(
            RuntimeStateManager(logger=lambda *a, **k: None),
            is_closed=lambda: False,
            log_decision=lambda *a, **k: None,
            recovery_gate=SimpleNamespace(check=lambda **k: verdict),
        )

    def test_ignored_gate_does_not_report_rejoin_success(self):
        acc = _rejoin_acc()
        fake = _FakeRecovery(True)
        router = self._router(
            {"ignored": True, "event": "recovery_ignored", "fields": {}}
        )
        result = router.route(
            fake,
            acc,
            "rejoin_requested",
            "manual",
            payload={"trigger": "force_rejoin"},
        )
        self.assertFalse(result)
        self.assertEqual(fake.rejoin_calls, 0)

    def test_auth_block_does_not_report_rejoin_success(self):
        acc = _rejoin_acc()
        fake = _FakeRecovery(True)
        decision = AuthGateDecision(
            blocked=True,
            reason_key="banned",
            reason="Account banned",
            category="banned",
        )
        with patch(
            "runtime.recovery_signal_router.evaluate_account_auth_gate",
            return_value=decision,
        ), patch(
            "runtime.recovery_signal_router.mark_account_auth_quarantined",
        ):
            result = self._router({"ignored": False}).route(
                fake,
                acc,
                "rejoin_requested",
                "manual",
                payload={"trigger": "force_rejoin"},
            )
        self.assertFalse(result)
        self.assertEqual(fake.rejoin_calls, 0)
        self.assertEqual(len(fake.failures), 1)

    def test_captcha_hold_does_not_report_rejoin_success(self):
        acc = _rejoin_acc()
        fake = _FakeRecovery(True)
        with patch("runtime.recovery_signal_router.set_account_captcha_hold"):
            result = self._router({"ignored": False}).route(
                fake,
                acc,
                "rejoin_requested",
                "captcha_required",
                payload={"reason_key": "captcha_required", "detail": "captcha"},
            )
        self.assertFalse(result)
        self.assertEqual(fake.rejoin_calls, 0)
        self.assertEqual(len(fake.failures), 1)


class BeginGateTests(unittest.TestCase):
    def test_failed_is_rejected_by_default(self):
        txn = _Txn()
        self.assertTrue(txn._begin_gate_reason(_rejoin_acc(), "force_rejoin"))

    def test_failed_is_allowed_for_manual_rejoin(self):
        txn = _Txn()
        self.assertFalse(
            txn._begin_gate_reason(_rejoin_acc(), "force_rejoin", allow_failed=True)
        )

    def test_stopped_account_still_rejected(self):
        txn = _Txn()
        acc = _rejoin_acc()
        acc.desired_state = AccountState.IDLE
        self.assertTrue(
            txn._begin_gate_reason(acc, "force_rejoin", allow_failed=True)
        )


class BlockReasonTests(unittest.TestCase):
    def test_stopped_account_is_blocked(self):
        acc = _rejoin_acc()
        acc.desired_state = AccountState.IDLE
        with patch(
            "runtime.recovery_reports.evaluate_account_auth_gate",
            return_value=_gate(),
        ):
            reason = _Reports().force_rejoin_block_reason(acc)
        self.assertIn("stopped", reason)

    def test_finished_account_is_blocked(self):
        acc = _rejoin_acc()
        acc.manual_status = "finished"
        reason = _Reports().force_rejoin_block_reason(acc)
        self.assertIn("Finished", reason)

    def test_captcha_hold_is_blocked(self):
        acc = _rejoin_acc()
        with patch(
            "runtime.recovery_reports.evaluate_account_auth_gate",
            return_value=_gate(True, CAPTCHA_REASON, "captcha"),
        ):
            reason = _Reports().force_rejoin_block_reason(acc)
        self.assertIn("captcha", reason)

    def test_captcha_hold_with_real_gate_is_blocked(self):
        acc = _rejoin_acc()
        set_account_captcha_hold(acc, "challenge")
        self.assertIn("captcha", _Reports().force_rejoin_block_reason(acc))

    def test_auth_hold_is_blocked(self):
        acc = _rejoin_acc()
        with patch(
            "runtime.recovery_reports.evaluate_account_auth_gate",
            return_value=_gate(True, "banned", "Account banned"),
        ):
            reason = _Reports().force_rejoin_block_reason(acc)
        self.assertIn("banned", reason)

    def test_transient_failed_is_allowed(self):
        acc = _rejoin_acc()
        with patch(
            "runtime.recovery_reports.evaluate_account_auth_gate",
            return_value=_gate(),
        ):
            self.assertEqual(_Reports().force_rejoin_block_reason(acc), "")

    def test_transient_failed_with_real_gate_is_allowed(self):
        acc = _rejoin_acc()
        acc.last_crash_reason = "max_fail"
        acc.last_recovery_reason = "recovery_budget_exceeded"
        self.assertEqual(_Reports().force_rejoin_block_reason(acc), "")

    def test_terminal_failed_reason_is_blocked(self):
        acc = _rejoin_acc()
        acc.last_crash_reason = "account_launched_elsewhere"
        with patch(
            "runtime.recovery_reports.evaluate_account_auth_gate",
            return_value=_gate(),
        ):
            reason = _Reports().force_rejoin_block_reason(acc)
        self.assertIn("account_launched_elsewhere", reason)

    def test_terminal_reason_on_non_failed_account_is_ignored(self):
        acc = _rejoin_acc()
        acc.state = AccountState.IN_GAME
        acc.last_crash_reason = "account_launched_elsewhere"
        with patch(
            "runtime.recovery_reports.evaluate_account_auth_gate",
            return_value=_gate(),
        ):
            self.assertEqual(_Reports().force_rejoin_block_reason(acc), "")

    def test_auth_gate_exception_fails_closed(self):
        acc = _rejoin_acc()
        with patch(
            "runtime.recovery_reports.evaluate_account_auth_gate",
            side_effect=RuntimeError("gate unavailable"),
        ):
            reason = _Reports().force_rejoin_block_reason(acc)
        self.assertIn("auth check unavailable", reason)


class CoordinatorForceRejoinTests(unittest.TestCase):
    def test_failed_begin_returns_false(self):
        acc = _rejoin_acc()
        acc.retry_count = 4
        acc.fail_count = 3
        acc.last_crash_reason = "max_fail"
        helper = _ForceRejoin(begin=None)
        with patch(
            "runtime.recovery_reports.evaluate_account_auth_gate",
            return_value=_gate(),
        ):
            self.assertFalse(helper.force_rejoin(acc))
        self.assertEqual(helper.transitions, [])
        self.assertEqual(helper.evaluated, [])
        self.assertEqual(helper.persisted, 0)
        self.assertTrue(helper.begin_calls[0][1]["allow_failed"])
        # A rejected begin must not wipe the previous recovery state.
        self.assertEqual(acc.retry_count, 4)
        self.assertEqual(acc.fail_count, 3)
        self.assertEqual(acc.last_crash_reason, "max_fail")

    def test_successful_reset_returns_true(self):
        helper = _ForceRejoin(begin={"ok": True})
        with patch(
            "runtime.recovery_reports.evaluate_account_auth_gate",
            return_value=_gate(),
        ):
            self.assertTrue(helper.force_rejoin(_rejoin_acc()))
        self.assertEqual(len(helper.transitions), 1)
        self.assertEqual(len(helper.evaluated), 1)
        self.assertEqual(helper.persisted, 1)

    def test_blocked_account_never_enters_recovery(self):
        helper = _ForceRejoin(begin={"ok": True})
        with patch(
            "runtime.recovery_reports.evaluate_account_auth_gate",
            return_value=_gate(True, "banned", "Account banned"),
        ):
            self.assertFalse(helper.force_rejoin(_rejoin_acc()))
        self.assertEqual(helper.begin_calls, [])

    def test_kill_failure_aborts_before_ready(self):
        acc = _rejoin_acc()
        acc.runtime_generation = 5
        acc.recovery_generation = 7
        helper = _ForceRejoin(
            begin={"ok": True, "pid": 123, "runtime_generation": 5, "recovery_generation": 7},
            kill_result={"ok": False, "killed": False, "pid": 123, "reason": "access_denied"},
        )
        with patch(
            "runtime.recovery_reports.evaluate_account_auth_gate",
            return_value=_gate(),
        ), patch(
            "runtime.recovery_reports.ProcessService.safe_kill_bound_process",
            return_value=helper.kill_result,
        ):
            self.assertFalse(helper.force_rejoin(acc))
        self.assertEqual(helper.evaluated, [])
        self.assertEqual(helper.persisted, 1)
        self.assertEqual(
            [state for _acc, state, _kwargs in helper.transitions],
            [AccountState.FAILED],
        )
        self.assertIn(
            ("u", 5, 7, "force_rejoin_kill_failed"),
            [args for args, _kwargs in helper.released],
        )

    def test_kill_success_continues(self):
        acc = _rejoin_acc()
        helper = _ForceRejoin(
            begin={"ok": True, "pid": 123, "runtime_generation": 5, "recovery_generation": 7},
        )
        with patch(
            "runtime.recovery_reports.evaluate_account_auth_gate",
            return_value=_gate(),
        ), patch(
            "runtime.recovery_reports.ProcessService.safe_kill_bound_process",
            return_value=helper.kill_result,
        ):
            self.assertTrue(helper.force_rejoin(acc))
        self.assertEqual(len(helper.evaluated), 1)
        self.assertEqual(helper.persisted, 1)

    def test_evaluate_rejection_returns_false(self):
        acc = _rejoin_acc()
        helper = _ForceRejoin(begin={"ok": True}, evaluate_result=False)
        with patch(
            "runtime.recovery_reports.evaluate_account_auth_gate",
            return_value=_gate(),
        ):
            self.assertFalse(helper.force_rejoin(acc))
        self.assertEqual(helper.persisted, 1)
        self.assertEqual(
            [state for _acc, state, _kwargs in helper.transitions],
            [AccountState.READY, AccountState.FAILED],
        )
        self.assertIn(
            ("u", 0, 0, "force_rejoin_evaluate_rejected"),
            [args for args, _kwargs in helper.released],
        )

    def test_evaluate_internal_failure_returns_false(self):
        acc = _rejoin_acc()
        helper = _ForceRejoin(begin={"ok": True}, evaluate_fails=True)
        with patch(
            "runtime.recovery_reports.evaluate_account_auth_gate",
            return_value=_gate(),
        ):
            self.assertFalse(helper.force_rejoin(acc))
        self.assertEqual(helper.persisted, 0)
        self.assertEqual(
            [state for _acc, state, _kwargs in helper.transitions],
            [AccountState.READY],
        )
        self.assertIn(
            ("u", 0, 0, "force_rejoin_evaluate_failed"),
            [args for args, _kwargs in helper.released],
        )


class FarmCapabilityTests(unittest.TestCase):
    def test_capability_uses_coordinator_block_reason(self):
        acc = _rejoin_acc()
        recovery = SimpleNamespace(
            force_rejoin_block_reason=lambda a: "Account is stopped; start it first"
        )
        farm = _FarmStub(acc, recovery)
        ok, msg, found = FarmController._capability_force_rejoin(farm, acc)
        self.assertFalse(ok)
        self.assertIn("stopped", msg)
        self.assertIs(found, acc)

    def test_capability_allows_transient_failed(self):
        acc = _rejoin_acc()
        recovery = SimpleNamespace(force_rejoin_block_reason=lambda a: "")
        farm = _FarmStub(acc, recovery)
        ok, msg, found = FarmController._capability_force_rejoin(farm, acc)
        self.assertTrue(ok)
        self.assertEqual(msg, "")
        self.assertIs(found, acc)

    def test_block_runs_before_rate_limit(self):
        acc = _rejoin_acc()
        recovery = SimpleNamespace(
            force_rejoin_block_reason=lambda a: "Account is stopped; start it first"
        )
        farm = _FarmStub(acc, recovery)
        ok, msg = FarmController.force_rejoin(farm, "u")
        self.assertFalse(ok)
        self.assertIn("stopped", msg)
        self.assertEqual(farm.rate_checks, 0)

    def test_block_check_exception_fails_closed(self):
        acc = _rejoin_acc()

        def _raise(_acc):
            raise RuntimeError("gate unavailable")

        recovery = SimpleNamespace(force_rejoin_block_reason=_raise)
        farm = _FarmStub(acc, recovery)
        ok, msg, found = FarmController._capability_force_rejoin(farm, acc)
        self.assertFalse(ok)
        self.assertIn("check failed", msg)
        self.assertIs(found, acc)

    def test_executor_hold_reports_deferred(self):
        acc = _rejoin_acc()
        acc.recovery_status = "wait_exploit_patch"
        recovery = SimpleNamespace(
            force_rejoin_block_reason=lambda a: "",
            _executor_held=lambda: True,
            _park_for_executor_hold=lambda a, t: None,
        )
        farm = _FarmStub(acc, recovery)
        farm._runtime_orchestrator = SimpleNamespace(
            request_rejoin=lambda *a, **k: False
        )
        ok, msg = FarmController.force_rejoin(farm, "u")
        self.assertFalse(ok)
        self.assertIn("deferred", msg.lower())


if __name__ == "__main__":
    unittest.main()
