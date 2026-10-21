"""Apply game changes to already-running clients.

Game assignments and Game-card edits update the Account fields immediately,
but a live Roblox process keeps the place it launched with until it rejoins.
Without a nudge, changing the game while the farm is running looks like a
no-op. This module captures each account's effective launch target before a
game change and force-rejoins only the accounts whose target actually moved.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# (place_id, private_server_url) that determines where the client will land.
GameTarget = tuple[str, str]

_VIP_PLACE_QUERY = re.compile(r"[?&]placeId=(\d+)", re.IGNORECASE)
_VIP_PLACE_PATH = re.compile(r"/games/(\d+)", re.IGNORECASE)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _field(account: Any, name: str) -> Any:
    if isinstance(account, Mapping):
        return account.get(name, "")
    return getattr(account, name, "")


def _username(account: Any) -> str:
    return _text(_field(account, "_config_username") or _field(account, "username"))


def _snapshot(cfg_mgr: Any) -> dict[str, Any]:
    try:
        snap = cfg_mgr.snapshot() if hasattr(cfg_mgr, "snapshot") else cfg_mgr
    except Exception:
        return {}
    return snap if isinstance(snap, dict) else {}


def _place_from_vip(url: Any) -> str:
    text = _text(url)
    if not text:
        return ""
    match = _VIP_PLACE_QUERY.search(text) or _VIP_PLACE_PATH.search(text)
    return match.group(1) if match else ""


def effective_game_target(account: Any, cfg: Any) -> GameTarget:
    """Return ``(place_id, vip_url)`` for the next launch of this account.

    Mirrors the resolution used by START/launch: Shared mode uses the Shared
    Place ID, Per-account mode uses the assigned game only, and an explicit
    account place or personal VIP link always wins. An unassigned account in
    Per-account mode is "No game": it has no target and must not inherit the
    Shared Place ID.
    """
    snapshot = cfg if isinstance(cfg, Mapping) else {}
    try:
        from domain.games import (
            effective_place,
            ensure_games_migrated,
            game_for_account,
            normalize_game_mode,
        )
    except Exception:
        return ("", "")

    try:
        games = ensure_games_migrated(dict(snapshot))
    except Exception:
        games = []
    try:
        mode = normalize_game_mode(snapshot.get("game_mode", "shared"))
    except Exception:
        mode = "shared"

    account_place = _text(_field(account, "place_id"))
    game_id = _text(_field(account, "game_id"))
    shared_vip = _text(snapshot.get("game_private_server_url"))
    shared_place = _text(snapshot.get("game_place_id")) or _place_from_vip(shared_vip)

    game = None
    if mode == "per_account" and games:
        try:
            game = game_for_account(game_id, account_place, games)
        except Exception:
            game = None

    # Shared mode (or a legacy empty pool) uses the Shared fallback. In
    # per-account mode with a pool, an unassigned account stays target-less.
    pool_active = mode == "per_account" and bool(games)
    fallback_place = "" if pool_active or game is not None else shared_place
    try:
        place_id = _text(effective_place(account_place, game, fallback_place))
    except Exception:
        place_id = account_place or _text((game or {}).get("place_id")) or fallback_place

    if game is not None:
        vip_url = _text(game.get("private_server_url"))
    elif pool_active:
        vip_url = ""
    else:
        vip_url = shared_vip
    if not place_id and not vip_url:
        # A personal VIP link is a launch target too: build_target/launch
        # fall back to acc.vip_links when place and game/shared targets are
        # empty. Without this, clearing that last target ("No game") looks
        # like a no-op and leaves the old client farming the old game.
        vip_url = _text(_field(account, "active_vip"))
        if not vip_url:
            links = _field(account, "vip_links") or []
            if isinstance(links, str):
                links = [links]
            if isinstance(links, (list, tuple)):
                vip_url = next((_text(link) for link in links if _text(link)), "")
    return (place_id, vip_url)


def capture_game_targets(accounts: Any, cfg: Any) -> dict[str, GameTarget]:
    """Map lower-case username -> effective target for the given accounts."""
    targets: dict[str, GameTarget] = {}
    for account in accounts or []:
        name = _username(account)
        if name:
            targets[name.lower()] = effective_game_target(account, cfg)
    return targets


def rejoin_changed_game_targets(
    farm: Any,
    before: Mapping[str, GameTarget],
    cfg_mgr: Any,
) -> dict[str, list[str]]:
    """Apply game-target changes to running accounts.

    Changed accounts with a target are force-rejoined. Accounts that became
    target-less ("No game") are stopped instead, so the old client does not
    keep farming the game the user just removed. Returns
    ``{"rejoined": [...], "stopped": [...]}``. A no-op when the farm is not
    running or when the target did not move.
    """
    if not before or not bool(getattr(farm, "running", False)):
        return {"rejoined": [], "stopped": []}
    cfg = _snapshot(cfg_mgr)
    result: dict[str, list[str]] = {"rejoined": [], "stopped": []}
    for account in list(getattr(farm, "_accounts", []) or []):
        name = _username(account)
        key = name.lower()
        if not key or key not in before:
            continue
        target = effective_game_target(account, cfg)
        if target == before[key]:
            continue
        if not target[0] and not target[1]:
            ok = False
            stop_account = getattr(farm, "stop_account", None)
            if callable(stop_account):
                try:
                    ok, _msg = stop_account(name, reason="game_unassigned")
                except TypeError:
                    # Older/fake farm implementations may not accept keywords.
                    try:
                        ok, _msg = stop_account(name)
                    except Exception:
                        ok = False
                except Exception:
                    ok = False
            if ok:
                result["stopped"].append(name)
            continue
        try:
            ok, _msg = farm.force_rejoin(
                name,
                reason="game_changed",
                ignore_rate_limit=True,
            )
        except TypeError:
            # Older/fake farm implementations may not accept the keywords.
            try:
                ok, _msg = farm.force_rejoin(name)
            except Exception:
                ok = False
        except Exception:
            ok = False
        if ok:
            result["rejoined"].append(name)
    return result
