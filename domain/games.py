"""Multi-game library: a named list of Roblox experiences accounts can join.

Each game entry is a plain dict::

    {
        "id": "game-1",
        "name": "Game 1",
        "place_id": "123456789",
        "private_server_url": "",
        "auto_create_private_server_enabled": False,
        "auto_create_private_server_free_only": True,
    }

Legacy single-game keys (``game_place_id`` / ``game_private_server_url`` /
``auto_create_private_server_*``) are kept as a fallback and are migrated
into the first game exactly once (see :func:`ensure_games_migrated`).

Resolution order for an account (never silently join the wrong map):

1. explicit per-account ``place_id`` / ``vip_links`` always win,
2. otherwise the account's ``game_id`` entry supplies place + VIP + flags,
3. otherwise the legacy global keys apply (only when no games exist),
4. otherwise the account has no target and must be blocked with a warning.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping, Optional

DEFAULT_GAME_ID = "game-1"
LEGACY_PLACE_KEY = "game_place_id"
LEGACY_VIP_KEY = "game_private_server_url"
LEGACY_AUTO_KEY = "auto_create_private_server_enabled"
LEGACY_AUTO_FREE_KEY = "auto_create_private_server_free_only"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return bool(default)


def normalize_game(raw: Any, index: int = 0) -> Dict[str, Any]:
    """Clean one game entry. Raises ValueError on a bad place_id."""
    source = raw if isinstance(raw, dict) else {}
    game_id = _text(source.get("id") or source.get("game_id")) or f"game-{index + 1}"
    place_id = _text(source.get("place_id") or source.get("placeId"))
    if place_id and not place_id.isdigit():
        raise ValueError(f"place_id must be numeric (game '{game_id}')")
    name = _text(source.get("name")) or f"Game {index + 1}"
    return {
        "id": game_id,
        "name": name,
        "place_id": place_id,
        "private_server_url": _text(source.get("private_server_url") or source.get("vip_url")),
        "auto_create_private_server_enabled": _bool(source.get("auto_create_private_server_enabled"), False),
        "auto_create_private_server_free_only": _bool(
            source.get("auto_create_private_server_free_only"), True
        ),
    }


def normalize_games(raw: Any) -> List[Dict[str, Any]]:
    """Clean a games list, dropping empties and de-duplicating ids."""
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    games: List[Dict[str, Any]] = []
    seen = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        try:
            game = normalize_game(item, len(games))
        except ValueError:
            raise
        if not (game["id"] or game["name"] or game["place_id"] or game["private_server_url"]):
            continue
        key = game["id"].lower()
        if key in seen:
            game["id"] = f"{game['id']}-{len(games) + 1}"
            key = game["id"].lower()
            if key in seen:
                continue
        seen.add(key)
        games.append(game)
    return games


def ensure_games_migrated(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One-way migration: legacy single-game keys become the first game.

    Mutates ``cfg`` in place (sets ``games``) and returns the games list.
    Never overwrites an existing non-empty games list.
    """
    games = normalize_games(cfg.get("games"))
    if games:
        cfg["games"] = games
        return games
    legacy_place = _text(cfg.get(LEGACY_PLACE_KEY))
    legacy_vip = _text(cfg.get(LEGACY_VIP_KEY))
    legacy_auto = _bool(cfg.get(LEGACY_AUTO_KEY), False)
    legacy_free = _bool(cfg.get(LEGACY_AUTO_FREE_KEY), True)
    if not (legacy_place or legacy_vip):
        cfg["games"] = []
        return []
    games = [
        {
            "id": DEFAULT_GAME_ID,
            "name": "Game 1",
            "place_id": legacy_place,
            "private_server_url": legacy_vip,
            "auto_create_private_server_enabled": legacy_auto,
            "auto_create_private_server_free_only": legacy_free,
        }
    ]
    cfg["games"] = games
    return games


def games_by_id(games: Any) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for game in normalize_games(games):
        out[game["id"].lower()] = game
    return out


def default_game(games: Any) -> Optional[Dict[str, Any]]:
    items = normalize_games(games)
    return items[0] if items else None


def get_game(games: Any, game_id: Any) -> Optional[Dict[str, Any]]:
    wanted = _text(game_id).lower()
    if not wanted:
        return None
    return games_by_id(games).get(wanted)


def game_for_account(
    game_id: Any,
    account_place_id: Any,
    games: Any,
) -> Optional[Dict[str, Any]]:
    """Find the game an account points at: id first, then matching place."""
    items = normalize_games(games)
    if not items:
        return None
    game = get_game(items, game_id)
    if game is not None:
        return game
    place = _text(account_place_id)
    if place:
        for item in items:
            if item["place_id"] and item["place_id"] == place:
                return item
    return None


def new_game_id(games: Any) -> str:
    taken = {str(game.get("id") or "").lower() for game in normalize_games(games)}
    index = len(taken) + 1
    while f"game-{index}".lower() in taken:
        index += 1
    return f"game-{index}"


def new_game_template(games: Any) -> Dict[str, Any]:
    count = len(normalize_games(games))
    return {
        "id": new_game_id(games),
        "name": f"Game {count + 1}",
        "place_id": "",
        "private_server_url": "",
        "auto_create_private_server_enabled": False,
        "auto_create_private_server_free_only": True,
    }


def effective_place(
    account_place_id: Any,
    game: Optional[Mapping[str, Any]],
    legacy_place_id: Any = "",
) -> str:
    """Account override wins, then the game, then the legacy global key."""
    account_place = _text(account_place_id)
    if account_place:
        return account_place
    if game:
        game_place = _text(game.get("place_id"))
        if game_place:
            return game_place
    return _text(legacy_place_id)


def effective_global_vip(
    game: Optional[Mapping[str, Any]],
    legacy_vip_url: Any = "",
    place_id: Any = "",
    parse_vip_place=None,
) -> str:
    """VIP link for a launch; blanked when it belongs to another place."""
    candidate = _text((game or {}).get("private_server_url")) or _text(legacy_vip_url)
    if not candidate:
        return ""
    place = _text(place_id)
    if place and parse_vip_place is not None:
        try:
            vip_place = parse_vip_place(candidate)
        except Exception:
            vip_place = ""
        if vip_place and vip_place != place:
            return ""
    return candidate


def effective_auto_flags(
    game: Optional[Mapping[str, Any]],
    legacy_enabled: Any = False,
    legacy_free_only: Any = True,
) -> Dict[str, bool]:
    if game is not None:
        return {
            "enabled": _bool(game.get("auto_create_private_server_enabled"), False),
            "free_only": _bool(game.get("auto_create_private_server_free_only"), True),
        }
    return {"enabled": _bool(legacy_enabled, False), "free_only": _bool(legacy_free_only, True)}


def describe_target(game: Optional[Mapping[str, Any]], place_id: Any = "") -> str:
    if game:
        label = _text(game.get("name")) or _text(game.get("id"))
        place = _text(place_id) or _text(game.get("place_id"))
        return f"{label} ({place})" if place else label
    place = _text(place_id)
    return f"Place {place}" if place else "no game"
