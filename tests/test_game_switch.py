"""Live game switching: assignment/config changes must rejoin running clients."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import Account
from services.game_switch import (
    capture_game_targets,
    effective_game_target,
    rejoin_changed_game_targets,
)

GAMES = [
    {
        "id": "game-1",
        "name": "Game 1",
        "place_id": "111",
        "private_server_url": "",
        "auto_create_private_server_enabled": False,
        "auto_create_private_server_free_only": True,
    },
    {
        "id": "game-2",
        "name": "Game 2",
        "place_id": "222",
        "private_server_url": "https://www.roblox.com/games/222/x?privateServerLinkCode=abc",
        "auto_create_private_server_enabled": False,
        "auto_create_private_server_free_only": True,
    },
]

PER_ACCOUNT = {
    "game_mode": "per_account",
    "game_place_id": "999",
    "game_private_server_url": "",
    "games": GAMES,
}


class _FakeCfg:
    def __init__(self, values):
        self.values = dict(values)

    def snapshot(self):
        return dict(self.values)

    def get(self, key, default=None):
        return self.values.get(key, default)

    def update(self, updates):
        self.values.update(dict(updates))

    def save(self):
        pass

    def save_accounts(self, accounts):
        pass


class _FakeFarm:
    def __init__(self, accounts, running=True):
        self._accounts = list(accounts)
        self.running = running
        self.calls = []
        self.stops = []
        self.snapshots = 0
        self.events = []

    def apply_config_snapshot(self):
        self.snapshots += 1
        self.events.append("snapshot")

    def force_rejoin(self, username, reason="force_rejoin", *, ignore_rate_limit=False):
        self.events.append(("rejoin", username))
        self.calls.append((username, reason, ignore_rate_limit))
        return True, "ok"

    def stop_account(self, username, reason="game_unassigned"):
        self.events.append(("stop", username))
        self.stops.append((username, reason))
        return True, "ok"


class TargetResolutionTests(unittest.TestCase):
    def test_shared_mode_uses_shared_place(self):
        acc = Account(username="u1", game_id="game-2")
        cfg = {"game_mode": "shared", "game_place_id": "999", "games": GAMES}
        self.assertEqual(effective_game_target(acc, cfg), ("999", ""))

    def test_per_account_uses_assigned_game(self):
        acc = Account(username="u1", game_id="game-2")
        place, vip = effective_game_target(acc, PER_ACCOUNT)
        self.assertEqual(place, "222")
        self.assertIn("privateServerLinkCode", vip)

    def test_unassigned_per_account_has_no_target(self):
        # "No game" must not inherit the Shared Place ID while the pool exists.
        acc = Account(username="u1")
        self.assertEqual(effective_game_target(acc, PER_ACCOUNT), ("", ""))

    def test_explicit_account_place_wins(self):
        acc = Account(username="u1", game_id="game-2", place_id="333")
        self.assertEqual(effective_game_target(acc, PER_ACCOUNT)[0], "333")

    def test_shared_vip_place_is_used_when_place_key_empty(self):
        acc = Account(username="u1")
        cfg = {
            "game_mode": "shared",
            "game_place_id": "",
            "game_private_server_url": "https://www.roblox.com/games/444/x",
        }
        self.assertEqual(effective_game_target(acc, cfg), ("444", cfg["game_private_server_url"]))

    def test_capture_keys_are_case_insensitive(self):
        accounts = [Account(username="MiXeD", game_id="game-1")]
        targets = capture_game_targets(accounts, PER_ACCOUNT)
        self.assertIn("mixed", targets)

    def test_personal_vip_link_counts_as_a_target(self):
        link = "https://www.roblox.com/games/333/x?privateServerLinkCode=abc"
        acc = Account(username="u1", vip_links=[link])
        self.assertEqual(effective_game_target(acc, PER_ACCOUNT), ("", link))


class RejoinChangedTargetsTests(unittest.TestCase):
    def test_only_changed_accounts_rejoin(self):
        changed = Account(username="u1", game_id="game-1")
        unchanged = Account(username="u2", game_id="game-1")
        farm = _FakeFarm([changed, unchanged])
        before = capture_game_targets(farm._accounts, PER_ACCOUNT)

        changed.game_id = "game-2"
        result = rejoin_changed_game_targets(farm, before, _FakeCfg(PER_ACCOUNT))

        self.assertEqual(result, {"rejoined": ["u1"], "stopped": []})
        self.assertEqual(farm.calls, [("u1", "game_changed", True)])

    def test_not_running_is_a_noop(self):
        acc = Account(username="u1", game_id="game-1")
        farm = _FakeFarm([acc], running=False)
        before = capture_game_targets(farm._accounts, PER_ACCOUNT)
        acc.game_id = "game-2"

        self.assertEqual(
            rejoin_changed_game_targets(farm, before, _FakeCfg(PER_ACCOUNT)),
            {"rejoined": [], "stopped": []},
        )
        self.assertEqual(farm.calls, [])
        self.assertEqual(farm.stops, [])

    def test_targetless_new_game_stops_the_running_client(self):
        acc = Account(username="u1", game_id="game-1")
        farm = _FakeFarm([acc])
        before = capture_game_targets(farm._accounts, PER_ACCOUNT)

        acc.game_id = ""
        self.assertEqual(
            rejoin_changed_game_targets(farm, before, _FakeCfg(PER_ACCOUNT)),
            {"rejoined": [], "stopped": ["u1"]},
        )
        self.assertEqual(farm.calls, [])
        self.assertEqual(farm.stops, [("u1", "game_unassigned")])

    def test_clearing_personal_vip_target_stops_the_client(self):
        # A personal VIP link is a launch target even with no game/place.
        # Clearing it must stop the client, not look like a no-op.
        link = "https://www.roblox.com/games/333/x?privateServerLinkCode=abc"
        acc = Account(username="u1", vip_links=[link])
        farm = _FakeFarm([acc])
        before = capture_game_targets(farm._accounts, PER_ACCOUNT)
        self.assertEqual(before["u1"], ("", link))

        acc.vip_links = []
        self.assertEqual(
            rejoin_changed_game_targets(farm, before, _FakeCfg(PER_ACCOUNT)),
            {"rejoined": [], "stopped": ["u1"]},
        )
        self.assertEqual(farm.stops, [("u1", "game_unassigned")])


class _FakeApp:
    def __init__(self):
        self.routes = {}

    def get(self, path):
        def deco(fn):
            self.routes["GET " + path] = fn
            return fn

        return deco

    def post(self, path):
        def deco(fn):
            self.routes["POST " + path] = fn
            return fn

        return deco


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return dict(self._body)


def _run(handler, body, **path_params):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            handler(*path_params.values(), _FakeRequest(body))
        )
    finally:
        loop.close()


class _FakeStore:
    def __init__(self, records):
        self.records = [dict(record) for record in records]

    def read_records(self, include_cookies=False):
        return [dict(record) for record in self.records]

    def write_records(self, records):
        self.records = [dict(record) for record in records]


def _sync_farm(ctx, cfg_mgr, farm, store):
    records = {str(r.get("username") or "").lower(): r for r in store.read_records()}
    for acc in farm._accounts:
        record = records.get(acc.username.lower())
        if not record:
            continue
        acc.game_id = str(record.get("game_id") or "")
        acc.place_id = str(record.get("place_id") or "")
        acc.vip_links = list(record.get("vip_links") or [])
    return len(farm._accounts)


def _production_sync(ctx, cfg_mgr, farm, store):
    """Stand-in for replace_farm_accounts_from_store, including its defaults.

    The real resync applies game defaults, which runs the one-time
    ``games_migrated`` re-link. Tests use this to prove the route closes
    that guard before the resync, not after it.
    """
    from api_routes.settings_state import _apply_game_defaults

    count = _sync_farm(ctx, cfg_mgr, farm, store)
    _apply_game_defaults(ctx, farm._accounts, persist=False)
    return count


class AssignRouteTests(unittest.TestCase):
    def _register(self, records, accounts):
        import api_routes.games_routes as routes

        app = _FakeApp()
        cfg = _FakeCfg(PER_ACCOUNT)
        farm = _FakeFarm(accounts)
        store = _FakeStore(records)
        routes.register(app, SimpleNamespace(cfg_mgr=cfg, farm=farm))
        return routes, app, farm, store

    def test_assignment_while_running_forces_rejoin(self):
        acc = Account(username="u1", game_id="game-1")
        routes, app, farm, store = self._register(
            [{"username": "u1", "game_id": "game-1", "place_id": "", "vip_links": []}],
            [acc],
        )

        with patch.object(routes, "ACCOUNT_STORE", store), patch(
            "api_routes.account_records.replace_farm_accounts_from_store",
            side_effect=_sync_farm,
        ):
            result = _run(
                app.routes["POST /api/account/{username}/game"],
                {"game_id": "game-2"},
                username="u1",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(store.records[0]["game_id"], "game-2")
        self.assertEqual(result["game_rejoined"], ["u1"])
        self.assertEqual(farm.calls, [("u1", "game_changed", True)])

    def test_same_assignment_does_not_rejoin(self):
        acc = Account(username="u1", game_id="game-1")
        routes, app, farm, store = self._register(
            [{"username": "u1", "game_id": "game-1", "place_id": "", "vip_links": []}],
            [acc],
        )

        with patch.object(routes, "ACCOUNT_STORE", store), patch(
            "api_routes.account_records.replace_farm_accounts_from_store",
            side_effect=_sync_farm,
        ):
            result = _run(
                app.routes["POST /api/account/{username}/game"],
                {"game_id": "game-1"},
                username="u1",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["game_rejoined"], [])
        self.assertEqual(farm.calls, [])

    def test_no_game_assignment_stops_running_client(self):
        acc = Account(username="u1", game_id="game-1")
        routes, app, farm, store = self._register(
            [{"username": "u1", "game_id": "game-1", "place_id": "", "vip_links": []}],
            [acc],
        )

        with patch.object(routes, "ACCOUNT_STORE", store), patch(
            "api_routes.account_records.replace_farm_accounts_from_store",
            side_effect=_sync_farm,
        ):
            result = _run(
                app.routes["POST /api/account/{username}/game"],
                {"game_id": ""},
                username="u1",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(store.records[0]["game_id"], "")
        self.assertEqual(result["game_rejoined"], [])
        self.assertEqual(result["game_stopped"], ["u1"])
        self.assertEqual(farm.calls, [])
        self.assertEqual(farm.stops, [("u1", "game_unassigned")])

    def test_no_game_closes_migration_guard_before_resync(self):
        # Regression: the resync runs _apply_game_defaults, whose one-time
        # migration re-links target-less accounts to the default game. If the
        # guard is persisted after the resync, "No game" silently becomes
        # "game-1" and the route rejoins instead of stopping.
        acc = Account(username="u1", game_id="game-2")
        routes, app, farm, store = self._register(
            [{"username": "u1", "game_id": "game-2", "place_id": "", "vip_links": []}],
            [acc],
        )

        with patch.object(routes, "ACCOUNT_STORE", store), patch(
            "api_routes.account_records.replace_farm_accounts_from_store",
            side_effect=_production_sync,
        ):
            result = _run(
                app.routes["POST /api/account/{username}/game"],
                {"game_id": ""},
                username="u1",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(acc.game_id, "")
        self.assertEqual(store.records[0]["game_id"], "")
        self.assertEqual(result["game_rejoined"], [])
        self.assertEqual(result["game_stopped"], ["u1"])
        self.assertEqual(farm.stops, [("u1", "game_unassigned")])


class GamesRouteTests(unittest.TestCase):
    def test_game_edit_while_running_forces_rejoin(self):
        import api_routes.games_routes as routes

        app = _FakeApp()
        cfg = _FakeCfg(PER_ACCOUNT)
        acc = Account(username="u1", game_id="game-2")
        farm = _FakeFarm([acc])
        routes.register(app, SimpleNamespace(cfg_mgr=cfg, farm=farm))

        result = _run(
            app.routes["POST /api/games"],
            {"id": "game-2", "place_id": "333", "private_server_url": ""},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["game_rejoined"], ["u1"])
        self.assertEqual(farm.calls, [("u1", "game_changed", True)])
        self.assertEqual(farm.snapshots, 1)
        # Workers must see the edited pool before the relaunch, otherwise
        # they resolve the old place from the stale snapshot.
        self.assertLess(
            farm.events.index("snapshot"),
            farm.events.index(("rejoin", "u1")),
        )

    def test_force_delete_stops_unassigned_running_accounts(self):
        import api_routes.games_routes as routes

        app = _FakeApp()
        cfg = _FakeCfg(PER_ACCOUNT)
        acc = Account(username="u1", game_id="game-2")
        farm = _FakeFarm([acc])
        store = _FakeStore(
            [{"username": "u1", "game_id": "game-2", "place_id": "", "vip_links": []}]
        )
        routes.register(app, SimpleNamespace(cfg_mgr=cfg, farm=farm))

        with patch.object(routes, "ACCOUNT_STORE", store), patch(
            "api_routes.account_records.replace_farm_accounts_from_store",
            side_effect=_production_sync,
        ):
            result = _run(app.routes["POST /api/games/delete"], {"id": "game-2", "force": True})

        self.assertTrue(result["ok"])
        self.assertEqual(store.records[0]["game_id"], "")
        self.assertEqual(result["game_rejoined"], [])
        self.assertEqual(result["game_stopped"], ["u1"])
        self.assertEqual(farm.calls, [])
        self.assertEqual(farm.stops, [("u1", "game_unassigned")])


class ConfigRouteTests(unittest.TestCase):
    def test_game_mode_switch_rejoins_running_accounts(self):
        import api_routes.config_routes as routes

        app = _FakeApp()
        cfg = _FakeCfg(PER_ACCOUNT)
        acc = Account(username="u1", game_id="game-2")
        farm = _FakeFarm([acc])
        routes.register(app, SimpleNamespace(cfg_mgr=cfg, farm=farm))

        result = _run(app.routes["POST /api/config"], {"game_mode": "shared"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["game_rejoined"], ["u1"])
        self.assertEqual(farm.calls, [("u1", "game_changed", True)])

    def test_shared_place_change_rejoins_running_accounts(self):
        import api_routes.config_routes as routes

        app = _FakeApp()
        cfg = _FakeCfg(
            {
                "game_mode": "shared",
                "game_place_id": "111",
                "game_private_server_url": "",
            }
        )
        acc = Account(username="u1")
        farm = _FakeFarm([acc])
        ctx = SimpleNamespace(cfg_mgr=cfg, farm=farm)
        routes.register(app, ctx)

        result = _run(app.routes["POST /api/config"], {"game_place_id": "222"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["game_rejoined"], ["u1"])
        self.assertEqual(farm.calls, [("u1", "game_changed", True)])


if __name__ == "__main__":
    unittest.main()
