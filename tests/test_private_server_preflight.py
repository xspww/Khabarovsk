"""Tests for START-time private-server preflight link persistence.

The Shared "Private Server URL" and per-game link fields are the user's
explicit "join this room together" switches. An auto-created link must
never be written back into them, otherwise accounts could share one
server without the user ever entering a link. The created server record
belongs on its owner account instead, so launch can reuse the join data.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from domain.roblox_private_servers import ensure_owned_private_server
from services.private_server_preflight import (
    _PreflightDeps,
    run_private_server_preflight,
)
from services.roblox_launch_service import _auth_shared_direct

PLACE = "123456789"
LINK = "https://www.roblox.com/games/123456789/?privateServerLinkCode=abc"


class TestSharedDirectSwitch(unittest.TestCase):
    def test_empty_link_does_not_pin_accounts(self):
        class _PM:
            GLOBAL_VIP_LINK = ""

        self.assertEqual(_auth_shared_direct(_PM, None, False, PLACE), "")

    def test_entered_link_pins_accounts(self):
        class _PM:
            GLOBAL_VIP_LINK = LINK

        self.assertEqual(_auth_shared_direct(_PM, None, False, PLACE), LINK)


class _CfgMgr:
    def __init__(self):
        self.updates = []
        self.saved = False

    def update(self, updates):
        self.updates.append(dict(updates))

    def save(self):
        self.saved = True


class _FakeStore:
    def __init__(self, records=None):
        self.records = [dict(record) for record in (records or [])]
        self.writes = []

    def read_records(self, include_cookies=False):
        return [dict(record) for record in self.records]

    def update_record(self, username, updates):
        key = str(username or "").strip().lower()
        for record in self.records:
            if str(record.get("username") or "").strip().lower() != key:
                continue
            record.update(updates)
            self.writes.append((username, dict(updates)))
            return dict(record)
        return None


def _deps(ensure=None, cookie_user_id="1"):
    return _PreflightDeps(
        ensure_owned_private_server=ensure
        or (lambda *args, **kwargs: {"ok": True, "link": LINK}),
        roblox_http=lambda cookie: object(),
        validate_record_cookie_identity=lambda record, cookie, update_store=True: {
            "ok": True,
            "cookie_user_id": cookie_user_id,
        },
    )


def _account(username="u1", **overrides):
    base = {
        "username": username,
        "cookie": "cookie",
        "game_id": "",
        "place_id": "",
        "vip_links": [],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _run_preflight(
    cfg,
    accounts,
    games,
    mode,
    *,
    deps=None,
    records=None,
    store=None,
):
    cfg_mgr = _CfgMgr()
    fake_store = store if store is not None else _FakeStore()
    with patch(
        "services.private_server_preflight._load_preflight_deps",
        return_value=deps or _deps(),
    ), patch(
        "services.private_server_preflight._load_records_by_name",
        return_value=records or {},
    ), patch(
        "services.account_hybrid.ACCOUNT_STORE",
        fake_store,
    ):
        outcome = run_private_server_preflight(
            cfg,
            accounts,
            games,
            mode,
            cfg_mgr,
        )
    return outcome, cfg_mgr


class TestPreflightNeverPersistsLinks(unittest.TestCase):
    def test_shared_auto_created_link_is_not_written_to_config(self):
        cfg = {
            "game_place_id": PLACE,
            "game_private_server_url": "",
            "auto_create_private_server_enabled": True,
        }
        outcome, cfg_mgr = _run_preflight(cfg, [_account()], [], "shared")

        self.assertEqual(len(outcome["created"]), 1)
        self.assertEqual(cfg_mgr.updates, [])
        self.assertFalse(cfg_mgr.saved)

    def test_per_account_auto_created_link_is_not_written_to_games(self):
        cfg = {
            "game_mode": "per_account",
            "games": [{"id": "game-1", "place_id": PLACE, "private_server_url": ""}],
            "auto_create_private_server_enabled": True,
        }
        outcome, cfg_mgr = _run_preflight(
            cfg,
            [_account(game_id="game-1")],
            cfg["games"],
            "per_account",
        )

        self.assertEqual(len(outcome["created"]), 1)
        self.assertEqual(cfg_mgr.updates, [])
        self.assertFalse(cfg_mgr.saved)


class TestPreflightStoresOwnerServer(unittest.TestCase):
    @staticmethod
    def _created_result(**overrides):
        result = {
            "ok": True,
            "source": "created",
            "private_server_id": "ps-1",
            "owner_user_id": "1",
            "place_id": PLACE,
            "universe_id": "u-1",
            "name": "Game",
            "active": True,
            "link": LINK,
            "join_code": "abc",
            "access_code": "",
            "status": "ok",
            "error": "",
            "synced_at": 111.0,
        }
        result.update(overrides)
        return result

    def test_two_accounts_create_once_and_store_on_owner(self):
        calls = []

        def ensure(client, **kwargs):
            calls.append(dict(kwargs))
            return self._created_result(
                owner_user_id=kwargs.get("owner_user_id") or "1",
                place_id=kwargs.get("place_id") or PLACE,
            )

        cfg = {
            "game_place_id": PLACE,
            "game_private_server_url": "",
            "auto_create_private_server_enabled": True,
        }
        store = _FakeStore(
            [
                {"username": "a", "owned_private_servers": []},
                {"username": "b", "owned_private_servers": []},
            ]
        )
        outcome, cfg_mgr = _run_preflight(
            cfg,
            [_account("a"), _account("b")],
            [],
            "shared",
            deps=_deps(ensure=ensure),
            records={
                "a": {"username": "a", "cookie": "cookie"},
                "b": {"username": "b", "cookie": "cookie"},
            },
            store=store,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["owner_user_id"], "1")
        self.assertEqual(calls[0]["place_id"], PLACE)
        self.assertEqual(outcome["created"][0]["usernames"], ["a", "b"])
        owner_servers = store.records[0]["owned_private_servers"]
        self.assertEqual(len(owner_servers), 1)
        self.assertEqual(owner_servers[0]["private_server_id"], "ps-1")
        self.assertEqual(owner_servers[0]["link"], LINK)
        self.assertEqual(store.records[1]["owned_private_servers"], [])
        self.assertEqual(cfg_mgr.updates, [])
        self.assertFalse(cfg_mgr.saved)

    def test_existing_owner_servers_are_kept(self):
        old_server = {
            "private_server_id": "ps-old",
            "owner_user_id": "1",
            "place_id": "999",
            "universe_id": "u-old",
            "link": "https://www.roblox.com/games/999/?privateServerLinkCode=old",
        }
        calls = []

        def ensure(client, **kwargs):
            calls.append(dict(kwargs))
            return self._created_result()

        store = _FakeStore(
            [{"username": "a", "owned_private_servers": [old_server]}]
        )
        outcome, _cfg_mgr = _run_preflight(
            {
                "game_place_id": PLACE,
                "game_private_server_url": "",
                "auto_create_private_server_enabled": True,
            },
            [_account("a")],
            [],
            "shared",
            deps=_deps(ensure=ensure),
            records={"a": {"username": "a", "cookie": "cookie"}},
            store=store,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(outcome["created"]), 1)
        ids = {
            server["private_server_id"]
            for server in store.records[0]["owned_private_servers"]
        }
        self.assertEqual(ids, {"ps-old", "ps-1"})


class TestKnownServerReuse(unittest.TestCase):
    def test_owner_record_supplies_missing_join_data(self):
        calls = {"enabled": 0, "place": 0}

        def universe_lookup(place_id):
            return True, "u-1", "ok"

        def list_my(client):
            return (
                True,
                [
                    {
                        "privateServerId": "ps-1",
                        "ownerId": "1",
                        "placeId": PLACE,
                        "universeId": "u-1",
                    }
                ],
                "ok",
            )

        def fetch_meta(client, private_server_id):
            return (
                True,
                {
                    "privateServerId": "ps-1",
                    "ownerId": "1",
                    "placeId": PLACE,
                    "universeId": "u-1",
                },
                "ok",
            )

        def list_place(client, place_id):
            calls["place"] += 1
            return True, [], "ok"

        def enabled(client, universe_id):
            calls["enabled"] += 1
            return True, True, "ok"

        def game_name(client, universe_id):
            return True, "Game", "ok"

        result = ensure_owned_private_server(
            object(),
            "1",
            PLACE,
            known_servers=[
                {
                    "private_server_id": "ps-1",
                    "owner_user_id": "1",
                    "place_id": PLACE,
                    "universe_id": "u-1",
                    "link": LINK,
                    "join_code": "abc",
                }
            ],
            universe_lookup=universe_lookup,
            list_my_private_servers_fn=list_my,
            list_private_servers_for_place_fn=list_place,
            fetch_private_server_metadata_fn=fetch_meta,
            private_servers_enabled_for_universe_fn=enabled,
            game_name_for_universe_fn=game_name,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "existing")
        self.assertEqual(result["link"], LINK)
        self.assertEqual(calls, {"enabled": 0, "place": 0})


if __name__ == "__main__":
    unittest.main()
