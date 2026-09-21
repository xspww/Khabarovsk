"""Multi-game management API.

- GET  /api/games                     list games (+ default id)
- POST /api/games            {game}   create or update one game
- POST /api/games/delete     {id}     delete a game (blocked while assigned)
- POST /api/account/{u}/game {game_id} assign one account ("" = unassign)
- POST /api/accounts/assign-game {usernames, game_id} bulk assign
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import HTTPException, Request

from account_hybrid import ACCOUNT_STORE

from .context import ApiContext
from .settings_state import _apply_game_defaults


def _games_snapshot(ctx: ApiContext) -> List[Dict[str, Any]]:
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


def _save_games(ctx: ApiContext, games: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    try:
        from domain.games import normalize_games
    except Exception:
        normalize_games = lambda value: list(value or [])  # noqa: E731
    clean = normalize_games(games)
    ctx.cfg_mgr.update({"games": clean})
    ctx.cfg_mgr.save()
    return clean


def _refresh_farm_targets(ctx: ApiContext) -> int:
    try:
        return int(_apply_game_defaults(ctx, ctx.farm._accounts, persist=True))
    except Exception:
        return 0


def register(app, ctx: ApiContext) -> None:
    @app.get("/api/games")
    def api_list_games():
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

    @app.post("/api/games")
    async def api_upsert_game(request: Request):
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
            found = False
            updated: List[Dict[str, Any]] = []
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
            games = updated
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
        clean = _save_games(ctx, games)
        applied = _refresh_farm_targets(ctx)
        return {"ok": True, "games": clean, "game_defaults_applied": applied}

    @app.post("/api/games/delete")
    async def api_delete_game(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected object")
        game_id = str(body.get("id") or body.get("game_id") or "").strip()
        if not game_id:
            raise HTTPException(400, "id required")
        force = bool(body.get("force", False))
        games = _games_snapshot(ctx)
        remaining = [
            item
            for item in games
            if str(item.get("id") or "").strip().lower() != game_id.lower()
        ]
        if len(remaining) == len(games):
            raise HTTPException(404, f"Game not found: {game_id}")
        assigned: List[str] = []
        try:
            for record in ACCOUNT_STORE.read_records(include_cookies=False):
                if str(record.get("game_id") or "").strip().lower() == game_id.lower():
                    assigned.append(str(record.get("username") or "?"))
        except Exception:
            assigned = []
        if assigned and not force:
            raise HTTPException(
                409,
                f"Game is assigned to {len(assigned)} account(s): "
                + ", ".join(assigned[:5])
                + (" ..." if len(assigned) > 5 else "")
                + ". Reassign them first or retry with force.",
            )
        if assigned and force:
            try:
                records = ACCOUNT_STORE.read_records(include_cookies=False)
                for record in records:
                    if str(record.get("game_id") or "").strip().lower() == game_id.lower():
                        record["game_id"] = ""
                ACCOUNT_STORE.write_records(records)
            except Exception as exc:
                raise HTTPException(500, f"Unassign failed: {exc}")
        clean = _save_games(ctx, remaining)
        applied = _refresh_farm_targets(ctx)
        try:
            from . import account_records

            account_records.replace_farm_accounts_from_store(
                ctx, ctx.cfg_mgr, ctx.farm, ACCOUNT_STORE
            )
        except Exception:
            pass
        return {
            "ok": True,
            "games": clean,
            "unassigned": assigned if force else [],
            "game_defaults_applied": applied,
        }

    def _assign_usernames(usernames: Any, game_id: str) -> Dict[str, Any]:
        wanted = str(game_id or "").strip()
        target_place = ""
        if wanted:
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
            target_place = str(match.get("place_id") or "").strip()
        names = []
        if isinstance(usernames, str):
            names = [usernames]
        elif isinstance(usernames, list):
            names = [str(name or "").strip() for name in usernames if str(name or "").strip()]
        if not names:
            raise HTTPException(400, "usernames required")
        try:
            records = ACCOUNT_STORE.read_records(include_cookies=False)
        except Exception as exc:
            raise HTTPException(500, f"Accounts unavailable: {exc}")
        by_name = {str(r.get("username") or "").strip().lower(): r for r in records}
        missing = [n for n in names if n.lower() not in by_name]
        if missing:
            raise HTTPException(404, "Account not found: " + ", ".join(missing[:5]))
        for name in names:
            record = by_name[name.lower()]
            record["game_id"] = wanted
            if not wanted:
                # Unassign ("No game"): clear stale per-account targets or
                # the old game resurrects via place_hint matching
                # (game_for_account matches by place_id) and via the
                # record.place_id fallback in build_target — the account
                # would silently rejoin the previous game.
                record["place_id"] = ""
                record["vip_links"] = []
                record["active_vip"] = ""
            else:
                # A stale explicit place_id from before multi-game (or from
                # another game) would override the picked game at launch
                # while the UI labels it as the new game. Clear it so the
                # picker stays the single source of truth.
                current_place = str(record.get("place_id") or "").strip()
                if current_place and target_place and current_place != target_place:
                    record["place_id"] = ""
                # Dropping a game must not leak its VIP into the new one.
                if str(record.get("active_vip") or "").strip():
                    record["active_vip"] = ""
        try:
            ACCOUNT_STORE.write_records(records)
        except Exception as exc:
            raise HTTPException(500, f"Save failed: {exc}")
        try:
            from . import account_records

            account_records.replace_farm_accounts_from_store(
                ctx, ctx.cfg_mgr, ctx.farm, ACCOUNT_STORE
            )
        except Exception:
            pass
        applied = _refresh_farm_targets(ctx)
        return {"ok": True, "assigned": names, "game_id": wanted, "game_defaults_applied": applied}

    @app.post("/api/account/{username}/game")
    async def api_assign_single_game(username: str, request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected object")
        return _assign_usernames([username], body.get("game_id", ""))

    @app.post("/api/accounts/assign-game")
    async def api_assign_bulk_game(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected object")
        return _assign_usernames(body.get("usernames") or [], body.get("game_id", ""))
