"""Farm runtime reset: Finished and "No game" stop paths stay single-owner."""
import unittest
from types import SimpleNamespace

from runtime.farm import FarmController


class _Lock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Worker:
    def __init__(self):
        self.wakes = 0

    def wake(self):
        self.wakes += 1


class _StubFarm:
    def __init__(self, acc):
        self.acc = acc
        self.killed = []
        self.resets = []
        self.events = []
        self.revisions = 0
        self._workers = {acc._config_username: _Worker()}

    def _find_account(self, username):
        return self.acc if username == self.acc._config_username else None

    def kill_account_pid(self, username, reason="api_kill_pid"):
        self.killed.append((username, reason))
        self.acc.pid = 0
        return True, "Killed"

    def _reset_account_runtime(self, acc, reason="runtime_reset"):
        self.resets.append((acc, reason))

    def _push_event(self, kind, text, **kwargs):
        self.events.append((kind, text, kwargs.get("reason", "")))

    def _bump_status_revision(self):
        self.revisions += 1


def _account(pid=1234):
    return SimpleNamespace(
        username="u1",
        _config_username="u1",
        display_name="u1",
        pid=pid,
        _lock=_Lock(),
    )


class StopAccountTests(unittest.TestCase):
    def test_stop_kills_pid_then_resets_runtime(self):
        acc = _account()
        farm = _StubFarm(acc)

        ok, _msg = FarmController.stop_account(farm, "u1")

        self.assertTrue(ok)
        self.assertEqual(farm.killed, [("u1", "game_unassigned")])
        self.assertEqual(farm.resets, [(acc, "game_unassigned")])
        self.assertEqual(len(farm.events), 1)
        self.assertEqual(farm.revisions, 1)
        # Stopping must not wake the worker into a target-less relaunch.
        self.assertEqual(farm._workers["u1"].wakes, 0)

    def test_stop_without_pid_skips_kill(self):
        acc = _account(pid=0)
        farm = _StubFarm(acc)

        ok, _msg = FarmController.stop_account(farm, "u1")

        self.assertTrue(ok)
        self.assertEqual(farm.killed, [])
        self.assertEqual(farm.resets, [(acc, "game_unassigned")])

    def test_unknown_account(self):
        farm = _StubFarm(_account())

        ok, _msg = FarmController.stop_account(farm, "nope")

        self.assertFalse(ok)
        self.assertEqual(farm.killed, [])
        self.assertEqual(farm.resets, [])


class MarkFinishedTests(unittest.TestCase):
    def test_finished_resets_wakes_worker_and_bumps_revision(self):
        acc = _account()
        farm = _StubFarm(acc)

        FarmController.mark_account_finished(farm, acc)

        self.assertEqual(farm.resets, [(acc, "account_finished")])
        self.assertEqual(farm.killed, [])
        self.assertEqual(farm._workers["u1"].wakes, 1)
        self.assertEqual(farm.revisions, 1)


if __name__ == "__main__":
    unittest.main()
