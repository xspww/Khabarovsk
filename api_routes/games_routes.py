"""Multi-game management API.

- GET  /api/games                     list games (+ default id)
- POST /api/games            {game}   create or update one game
- POST /api/games/delete     {id}     delete a game (blocked while assigned)
- POST /api/account/{u}/game {game_id} assign one account ("" = unassign)
- POST /api/accounts/assign-game {usernames, game_id} bulk assign
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from services.account_hybrid import ACCOUNT_STORE
from services.game_switch import capture_game_targets, rejoin_changed_game_targets

from .context import ApiContext
from .settings_state import _apply_game_defaults, persist_config


def _games_snapshot(ctx: ApiContext) -> list[dict[str, Any]]:
    try:
        from domain.games import ensure_games_migrated
    except Exception:
        return []
    try:
        snap = ctx.cfg_mgr.snapshot()
    except Exception:
        return []
    if not isinstance(snap, dict):
        return []
    try:
        return ensure_games_migrated(dict(snap))
    except Exception:
        return []


def _save_games(ctx: ApiContext, games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        from domain.games import normalize_games
    except Exception:
        normalize_games = lambda value: list(value or [])
    clean = normalize_games(games)
    persist_config(ctx, {"games": clean})
    return clean


def _refresh_farm_targets(ctx: ApiContext) -> int:
    try:
        applied = int(_apply_game_defaults(ctx, ctx.farm._accounts, persist=True))
    except Exception:
        applied = 0
    # Running workers keep their own config snapshot. Push the new Games
    # pool/shared keys before a rejoin, or the relaunch still resolves the
    # old place from the stale snapshot.
    try:
        apply_snapshot = getattr(ctx.farm, "apply_config_snapshot", None)
        if callable(apply_snapshot):
            apply_snapshot()
    except Exception:
        "intentional: ignore exception"
    return applied


def _capture_game_targets(ctx: ApiContext) -> dict[str, tuple[str, str]]:
    """Snapshot live launch targets before a game edit mutates accounts."""
    try:
        return capture_game_targets(ctx.farm._accounts, ctx.cfg_mgr.snapshot())
    except Exception:
        return {}


def _rejoin_changed_targets(
    ctx: ApiContext,
    before: dict[str, tuple[str, str]],
) -> tuple[list[str], list[str]]:
    """Rejoin/stop running clients after all account syncs finish."""
    try:
        result = rejoin_changed_game_targets(ctx.farm, before, ctx.cfg_mgr)
    except Exception:
        return [], []
    if isinstance(result, dict):
        return (
            list(result.get("rejoined") or []),
            list(result.get("stopped") or []),
        )
    # Backward-compatible shape for older implementations.
    return list(result or []), []


def _apply_game_update(
    games: list[dict[str, Any]],
    game_id: str,
    raw: dict[str, Any],
    normalize_game: Any,
) -> list[dict[str, Any]]:
    """Merge `raw` into the matching game and return the updated list."""
    found = False
    updated: list[dict[str, Any]] = []
    for item in games:
        if str(item.get("id") or "").strip().lower() == game_id.lower():
            merged = dict(item)
            for key in (
                "name",
                "place_id",
                "private_server_url",
                "auto_create_private_server_enabled",
                "auto_create_private_server_free_only",
            ):
                if key in raw:
                    merged[key] = raw[key]
            try:
                merged = normalize_game(merged, 0)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            merged["id"] = item.get("id") or game_id
            updated.append(merged)
            found = True
        else:
            updated.append(item)
    if not found:
        raise HTTPException(404, f"Game not found: {game_id}")
    return updated


def _assigned_game_message(assigned: list[str]) -> str:
    preview = ", ".join(assigned[:5])
    suffix = " ..." if len(assigned) > 5 else ""
    return (
        f"Game is assigned to {len(assigned)} account(s): {preview}{suffix}. "
        "Reassign them first or retry with force."
    )


def _find_assigned_usernames(game_id: str) -> list[str]:
    assigned: list[str] = []
    try:
        for record in ACCOUNT_STORE.read_records(include_cookies=False):
            if str(record.get("game_id") or "").strip().lower() == game_id.lower():
                assigned.append(str(record.get("username") or "?"))
    except Exception:
        return []
    return assigned


def _unassign_game_records(game_id: str) -> None:
    try:
        records = ACCOUNT_STORE.read_records(include_cookies=False)
        for record in records:
            if str(record.get("game_id") or "").strip().lower() != game_id.lower():
                continue
            # Full unassign: leaving the old place/VIP behind would silently
            # keep the account in the deleted game (the same resurrection
            # trap as "No game").
            record["game_id"] = ""
            record["place_id"] = ""
            record["vip_links"] = []
            record["active_vip"] = ""
        ACCOUNT_STORE.write_records(records)
    except Exception as exc:
        raise HTTPException(500, f"Unassign failed: {exc}")


def _mark_games_migrated(ctx: ApiContext) -> None:
    try:
        if not bool(ctx.cfg_mgr.get("games_migrated", False)):
            persist_config(ctx, {"games_migrated": True})
    except Exception:
        "intentional: ignore exception"


def _resync_accounts(ctx: ApiContext) -> None:
    try:
        from . import account_records

        account_records.replace_farm_accounts_from_store(ctx, ctx.cfg_mgr, ctx.farm, ACCOUNT_STORE)
    except Exception:
        "intentional: ignore exception"


def _delete_assigned_game(ctx: ApiContext, game_id: str) -> None:
    _unassign_game_records(game_id)
    _mark_games_migrated(ctx)


def _remaining_games(games: list[dict[str, Any]], game_id: str) -> list[dict[str, Any]]:
    return [
        item
        for item in games
        if str(item.get("id") or "").strip().lower() != game_id.lower()
    ]


def _api_list_games(ctx: ApiContext) -> dict[str, Any]:
    games = _games_snapshot(ctx)
    try:
        from domain.games import normalize_game_mode

        snap = ctx.cfg_mgr.snapshot()
        mode = normalize_game_mode((snap or {}).get("game_mode", "shared"))
    except Exception:
        mode = "shared"
    return {
        "ok": True,
        "games": games,
        "game_mode": mode,
        "default_game_id": str((games[0].get("id") if games else "") or ""),
        "count": len(games),
    }


async def _api_upsert_game(ctx: ApiContext, request: Request) -> dict[str, Any]:
    try:
        from domain.games import new_game_id, normalize_game, normalize_games
    except Exception as exc:
        raise HTTPException(500, f"Games unavailable: {exc}")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected object")
    raw = body.get("game") if isinstance(body.get("game"), dict) else body
    games = _games_snapshot(ctx)
    game_id = str(raw.get("id") or raw.get("game_id") or "").strip()
    if game_id:
        games = _apply_game_update(games, game_id, raw, normalize_game)
    else:
        try:
            game = normalize_game({**raw, "id": new_game_id(games)}, len(games))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if not game["place_id"] and not game["private_server_url"]:
            raise HTTPException(400, "place_id or private_server_url required")
        games.append(game)
    try:
        games = normalize_games(games)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    targets_before = _capture_game_targets(ctx)
    clean = _save_games(ctx, games)
    applied = _refresh_farm_targets(ctx)
    rejoined, stopped = _rejoin_changed_targets(ctx, targets_before)
    return {
        "ok": True,
        "games": clean,
        "game_defaults_applied": applied,
        "game_rejoined": rejoined,
        "game_stopped": stopped,
    }


async def _api_delete_game(ctx: ApiContext, request: Request) -> dict[str, Any]:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected object")
    game_id = str(body.get("id") or body.get("game_id") or "").strip()
    if not game_id:
        raise HTTPException(400, "id required")
    force = bool(body.get("force", False))
    games = _games_snapshot(ctx)
    remaining = _remaining_games(games, game_id)
    if len(remaining) == len(games):
        raise HTTPException(404, f"Game not found: {game_id}")
    assigned = _find_assigned_usernames(game_id)
    if assigned and not force:
        raise HTTPException(409, _assigned_game_message(assigned))
    targets_before = _capture_game_targets(ctx)
    if assigned and force:
        _delete_assigned_game(ctx, game_id)
    clean = _save_games(ctx, remaining)
    _resync_accounts(ctx)
    applied = _refresh_farm_targets(ctx)
    rejoined, stopped = _rejoin_changed_targets(ctx, targets_before)
    return {
        "ok": True,
        "games": clean,
        "unassigned": assigned if force else [],
        "game_defaults_applied": applied,
        "game_rejoined": rejoined,
        "game_stopped": stopped,
    }


def _assignment_target_place(ctx: ApiContext, wanted: str) -> str:
    if not wanted:
        return ""
    games = _games_snapshot(ctx)
    match = next(
        (
            item
            for item in games
            if str(item.get("id") or "").strip().lower() == wanted.lower()
        ),
        None,
    )
    if match is None:
        raise HTTPException(404, f"Game not found: {wanted}")
    return str(match.get("place_id") or "").strip()


def _normalized_usernames(usernames: Any) -> list[str]:
    if isinstance(usernames, str):
        return [usernames]
    if not isinstance(usernames, list):
        return []
    return [str(name or "").strip() for name in usernames if str(name or "").strip()]


def _records_by_name() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    try:
        records = ACCOUNT_STORE.read_records(include_cookies=False)
    except Exception as exc:
        raise HTTPException(500, f"Accounts unavailable: {exc}")
    by_name = {str(r.get("username") or "").strip().lower(): r for r in records}
    return records, by_name


def _apply_assignment(
    by_name: dict[str, dict[str, Any]],
    names: list[str],
    wanted: str,
    target_place: str,
) -> None:
    for name in names:
        record = by_name[name.lower()]
        record["game_id"] = wanted
        if not wanted:
            # Unassign ("No game"): clear stale per-account targets or the
            # old game resurrects via place_hint matching (game_for_account
            # matches by place_id) and via the record.place_id fallback in
            # build_target — the account would silently rejoin the previous
            # game.
            record["place_id"] = ""
            record["vip_links"] = []
            record["active_vip"] = ""
            continue
        # A stale explicit place_id from before multi-game (or from another
        # game) would override the picked game at launch while the UI labels
        # it as the new game. Clear it so the picker stays the single source
        # of truth.
        current_place = str(record.get("place_id") or "").strip()
        if current_place and current_place != target_place:
            record["place_id"] = ""
        # Dropping a game must not leak its VIP into the new one.
        if str(record.get("active_vip") or "").strip():
            record["active_vip"] = ""


def _write_assignment(records: list[dict[str, Any]]) -> None:
    try:
        ACCOUNT_STORE.write_records(records)
    except Exception as exc:
        raise HTTPException(500, f"Save failed: {exc}")


def _assign_usernames(ctx: ApiContext, usernames: Any, game_id: str) -> dict[str, Any]:
    wanted = str(game_id or "").strip()
    target_place = _assignment_target_place(ctx, wanted)
    names = _normalized_usernames(usernames)
    if not names:
        raise HTTPException(400, "usernames required")
    records, by_name = _records_by_name()
    missing = [n for n in names if n.lower() not in by_name]
    if missing:
        raise HTTPException(404, "Account not found: " + ", ".join(missing[:5]))
    targets_before = _capture_game_targets(ctx)
    _apply_assignment(by_name, names, wanted, target_place)
    _write_assignment(records)
    _mark_games_migrated(ctx)
    _resync_accounts(ctx)
    applied = _refresh_farm_targets(ctx)
    rejoined, stopped = _rejoin_changed_targets(ctx, targets_before)
    return {
        "ok": True,
        "assigned": names,
        "game_id": wanted,
        "game_defaults_applied": applied,
        "game_rejoined": rejoined,
        "game_stopped": stopped,
    }


async def _api_assign_single_game(
    ctx: ApiContext, username: str, request: Request
) -> dict[str, Any]:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected object")
    return _assign_usernames(ctx, [username], body.get("game_id", ""))


async def _api_assign_bulk_game(ctx: ApiContext, request: Request) -> dict[str, Any]:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected object")
    return _assign_usernames(ctx, body.get("usernames") or [], body.get("game_id", ""))


def register(app, ctx: ApiContext) -> None:
    @app.get("/api/games")
    def api_list_games():
        return _api_list_games(ctx)

    @app.post("/api/games")
    async def api_upsert_game(request: Request):
        return await _api_upsert_game(ctx, request)

    @app.post("/api/games/delete")
    async def api_delete_game(request: Request):
        return await _api_delete_game(ctx, request)

    @app.post("/api/account/{username}/game")
    async def api_assign_single_game(username: str, request: Request):
        return await _api_assign_single_game(ctx, username, request)

    @app.post("/api/accounts/assign-game")
    async def api_assign_bulk_game(request: Request):
        return await _api_assign_bulk_game(ctx, request)
