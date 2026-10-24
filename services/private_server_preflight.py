"""START-time Auto Create Private Server preflight.

When auto-create is enabled and an account has NO usable VIP link, the
first real launch attempt would fail (game has VIP disabled, or VIP
costs Robux while free-only is on). Instead of failing mid-run, START
tries the creation once up front per distinct place:

- free game  -> a link comes back; it is saved onto the game (or the
  legacy global VIP key) so the launches reuse it silently.
- VIP disabled / paid / error -> the failure is reported so START can
  skip those accounts and toast (bottom-right notification) instead.

Accounts that already have a usable VIP link (their own, their game's,
or the Shared one) are never touched and never notified.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple


class _PreflightConfig(NamedTuple):
    cfg: dict[str, Any]
    games: list[dict[str, Any]]
    per_account: bool
    legacy_target: bool
    legacy_place: str
    legacy_vip: str
    legacy_enabled: bool
    legacy_free_only: bool


class _PreflightDeps(NamedTuple):
    ensure_owned_private_server: Callable[..., Any]
    roblox_http: Callable[..., Any]
    validate_record_cookie_identity: Callable[..., Any]


class _SlotInfo(NamedTuple):
    game: Any
    game_name: str
    usernames: list[str]


def _usable_link_code(link: Any, place_id: str) -> bool:
    try:
        from domain.roblox_private_servers import parse_vip_link
    except Exception:
        return False
    try:
        link_place, link_code = parse_vip_link(str(link or ""))
    except Exception:
        return False
    if not link_code:
        return False
    place = str(place_id or "").strip()
    return (not link_place) or (not place) or (link_place == place)


def _classify_failure(message: str) -> str:
    from domain.roblox_private_servers import classify_private_server_failure

    return classify_private_server_failure(message)


def run_private_server_preflight(
    cfg: dict[str, Any],
    accounts: list[Any],
    games: list[dict[str, Any]],
    game_mode: str = "shared",
    cfg_mgr: Any = None,
) -> dict[str, Any]:
    """Attempt auto-create once per place. Never raises (fail-open)."""
    outcome: dict[str, Any] = {
        "created": [],
        "failures": [],
        "skipped_usernames": [],
        "public_fallback": [],
    }
    try:
        return _run(cfg, accounts, games, game_mode, cfg_mgr, outcome)
    except Exception as exc:
        try:
            from core import flog_kv

            flog_kv("PREFLIGHT", "private_server_preflight_failed", "warning", error=exc)
        except Exception:
            "intentional: ignore exception"
        return outcome


def _find_owner_cookie(slot_accounts, by_name) -> tuple[Any, str]:
    """Return (owner_record, cookie) for the first account that has a cookie."""
    owner_record = None
    for acc in slot_accounts:
        try:
            raw_cookie = getattr(acc, "cookie", "")
            cookie = str(raw_cookie).strip() if raw_cookie else ""
            record = None
        except Exception:
            cookie = ""
            record = None
        if not cookie:
            try:
                raw_name = getattr(acc, "username", "")
                name_key = str(raw_name).strip().lower() if raw_name else ""
                record = by_name.get(name_key)
                if record is None:
                    record = {}
                raw_cookie = record.get("cookie")
                cookie = str(raw_cookie).strip() if raw_cookie else ""
            except Exception:
                cookie = ""
                record = None
        if not cookie:
            continue
        if owner_record is None:
            owner_record = record
        if owner_record is None:
            try:
                raw_name = getattr(acc, "username", "")
                owner_record = by_name.get(str(raw_name).strip().lower())
            except Exception:
                owner_record = None
        return owner_record, cookie
    return owner_record, ""


def _normalize_inputs(
    cfg: Any,
    games: Any,
    game_mode: str,
) -> _PreflightConfig:
    cfg = cfg if isinstance(cfg, dict) else {}
    games = list(games or [])
    per_account = str(game_mode or "shared") == "per_account"
    # The legacy Shared keys are only a target source when the Games pool
    # is empty. With a pool, an unassigned account is "No game".
    legacy_target = not (per_account and games)
    legacy_place = str(cfg.get("game_place_id", "") or "").strip()
    legacy_vip = str(
        cfg.get("game_private_server_url", "") or ""
    ).strip()
    legacy_enabled = bool(
        cfg.get("auto_create_private_server_enabled", False)
    )
    legacy_free_only = bool(
        cfg.get("auto_create_private_server_free_only", True)
    )
    return _PreflightConfig(
        cfg=cfg,
        games=games,
        per_account=per_account,
        legacy_target=legacy_target,
        legacy_place=legacy_place,
        legacy_vip=legacy_vip,
        legacy_enabled=legacy_enabled,
        legacy_free_only=legacy_free_only,
    )


def _base_links(acc: Any) -> list[str]:
    links: list[str] = []
    try:
        links.extend(list(getattr(acc, "vip_links", []) or []))
    except Exception:
        "intentional: ignore exception"
    try:
        if getattr(acc, "active_vip", ""):
            links.append(str(acc.active_vip))
    except Exception:
        "intentional: ignore exception"
    return links


def _game_links(game: Any) -> list[str]:
    if not game:
        return []
    game_vip = str(
        (game or {}).get("private_server_url") or ""
    ).strip()
    return [game_vip] if game_vip else []


def _legacy_links(
    game: Any,
    config: _PreflightConfig,
) -> list[str]:
    if (
        game is None
        and config.legacy_target
        and config.legacy_vip
    ):
        return [config.legacy_vip]
    return []


def _account_links(
    acc: Any,
    game: Any,
    config: _PreflightConfig,
) -> list[str]:
    links = _base_links(acc)
    links.extend(_game_links(game))
    links.extend(_legacy_links(game, config))
    return links


def _account_needed_slot(
    acc: Any,
    config: _PreflightConfig,
    game_for_account: Callable[..., Any],
    effective_auto_flags: Callable[..., Any],
    effective_place: Callable[..., Any],
) -> tuple[str, dict[str, Any]] | None:
    try:
        game = None
        if config.per_account and config.games:
            try:
                game = game_for_account(
                    getattr(acc, "game_id", ""),
                    getattr(acc, "place_id", ""),
                    config.games,
                )
            except Exception:
                game = None
        flags = effective_auto_flags(
            game,
            config.legacy_enabled,
            config.legacy_free_only,
        )
        if not flags.get("enabled"):
            return None
        place = str(
            effective_place(
                getattr(acc, "place_id", ""),
                game,
                (
                    config.legacy_place
                    if game is None and config.legacy_target
                    else ""
                ),
            )
        ).strip()
        if not place:
            return None
        links = _account_links(acc, game, config)
        if any(_usable_link_code(link, place) for link in links):
            return None
        return place, {
            "game": game,
            "free_only": bool(flags.get("free_only", True)),
            "accounts": [acc],
        }
    except Exception:
        return None


def _collect_needed(
    accounts: list[Any],
    config: _PreflightConfig,
    game_for_account: Callable[..., Any],
    effective_auto_flags: Callable[..., Any],
    effective_place: Callable[..., Any],
) -> dict[str, dict[str, Any]]:
    # place_id -> {"game": dict|None, "free_only": bool, "accounts": [Account]}
    needed: dict[str, dict[str, Any]] = {}
    for acc in list(accounts or []):
        result = _account_needed_slot(
            acc,
            config,
            game_for_account,
            effective_auto_flags,
            effective_place,
        )
        if result is None:
            continue
        place, slot = result
        existing = needed.get(place)
        if existing is None:
            needed[place] = slot
        else:
            existing["accounts"].append(acc)
    return needed


def _load_records_by_name() -> dict[str, Any]:
    from services.account_hybrid import ACCOUNT_STORE

    try:
        records = ACCOUNT_STORE.read_records(include_cookies=True)
    except Exception:
        records = []
    by_name = {}
    try:
        for record in records or []:
            by_name[
                str(record.get("username") or "").strip().lower()
            ] = record
    except Exception:
        "intentional: ignore exception"
    return by_name


def _load_preflight_deps() -> _PreflightDeps:
    from domain.roblox_private_servers import (
        ensure_owned_private_server,
    )
    from services.roblox_http import RobloxHTTP
    from services.roblox_validation import (
        validate_record_cookie_identity,
    )

    return _PreflightDeps(
        ensure_owned_private_server=(
            ensure_owned_private_server
        ),
        roblox_http=RobloxHTTP,
        validate_record_cookie_identity=(
            validate_record_cookie_identity
        ),
    )


def _record_import_failure(
    outcome: dict[str, Any],
    needed: dict[str, dict[str, Any]],
    exc: Exception,
) -> None:
    for place, slot in needed.items():
        outcome["failures"].append(
            {
                "place_id": place,
                "game_name": str(
                    (slot.get("game") or {}).get("name") or ""
                ),
                "reason": f"Preflight unavailable: {exc}",
                "reason_code": "vip_error",
                "usernames": [
                    account.username
                    for account in slot["accounts"]
                ],
            }
        )
        outcome["skipped_usernames"].extend(
            str(account.username or "").strip().lower()
            for account in slot["accounts"]
        )


def _slot_info(slot: dict[str, Any]) -> _SlotInfo:
    game = slot.get("game")
    game_name = str(
        (game or {}).get("name")
        or (game or {}).get("id")
        or ""
    )
    usernames = [
        str(account.username or "")
        for account in slot["accounts"]
    ]
    return _SlotInfo(
        game=game,
        game_name=game_name,
        usernames=usernames,
    )


def _owner_identity(
    slot_accounts: list[Any],
    by_name: dict[str, Any],
    deps: _PreflightDeps,
) -> tuple[Any, str, dict[str, Any] | None]:
    owner_record, owner_cookie = _find_owner_cookie(
        slot_accounts,
        by_name,
    )
    if not owner_cookie:
        return owner_record, "", None
    try:
        identity = deps.validate_record_cookie_identity(
            owner_record or {},
            owner_cookie,
            update_store=True,
        )
    except Exception as exc:
        identity = {"ok": False, "msg": str(exc)}
    return owner_record, owner_cookie, identity


def _create_result(
    owner_cookie: str,
    identity: dict[str, Any],
    place: str,
    slot: dict[str, Any],
    deps: _PreflightDeps,
) -> dict[str, Any]:
    try:
        client = deps.roblox_http(owner_cookie)
        return deps.ensure_owned_private_server(
            client,
            owner_user_id=str(
                identity.get("cookie_user_id") or ""
            ),
            place_id=place,
            free_only=bool(slot.get("free_only", True)),
            known_servers=[],
        )
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}


def _success_patch(
    game: Any,
    legacy_vip: str,
    result: dict[str, Any],
) -> tuple[dict[str, str] | None, str | None]:
    link = str(result.get("link") or "").strip()
    if not link:
        return None, None
    if (
        game is not None
        and not str(
            (game or {}).get("private_server_url") or ""
        ).strip()
    ):
        return {str(game.get("id") or ""): link}, None
    if game is None and not legacy_vip:
        return None, link
    return None, None


def _created_entry(
    place: str,
    game_name: str,
    result: dict[str, Any],
    usernames: list[str],
) -> dict[str, Any]:
    return {
        "place_id": place,
        "game_name": game_name,
        "source": str(result.get("source") or ""),
        "usernames": usernames,
    }


def _failure_entry(
    place: str,
    game_name: str,
    reason: str,
    reason_code: str,
    usernames: list[str],
) -> dict[str, Any]:
    return {
        "place_id": place,
        "game_name": game_name,
        "reason": reason,
        "reason_code": reason_code,
        "usernames": usernames,
    }


def _record_units(
    outcome: dict[str, Any],
    entry: dict[str, Any],
    usernames: list[str],
    target_key: str,
) -> None:
    outcome["failures"].append(entry)
    outcome[target_key].extend(
        username.strip().lower()
        for username in usernames
    )


def _record_skipped(
    outcome: dict[str, Any],
    entry: dict[str, Any],
    usernames: list[str],
) -> None:
    _record_units(
        outcome,
        entry,
        usernames,
        "skipped_usernames",
    )


def _record_public_fallback(
    outcome: dict[str, Any],
    entry: dict[str, Any],
    usernames: list[str],
) -> None:
    entry["fallback_public"] = True
    _record_units(
        outcome,
        entry,
        usernames,
        "public_fallback",
    )


def _record_no_cookie(
    outcome: dict[str, Any],
    place: str,
    game_name: str,
    usernames: list[str],
    reason: str,
) -> None:
    _record_skipped(
        outcome,
        _failure_entry(
            place,
            game_name,
            reason,
            "no_cookie",
            usernames,
        ),
        usernames,
    )


def _process_slot(
    place: str,
    slot: dict[str, Any],
    by_name: dict[str, Any],
    config: _PreflightConfig,
    deps: _PreflightDeps,
    outcome: dict[str, Any],
    vip_unavailable_permanently: Callable[..., bool],
) -> tuple[dict[str, str] | None, str | None]:
    info = _slot_info(slot)
    _owner_record, owner_cookie, identity = _owner_identity(
        slot["accounts"],
        by_name,
        deps,
    )
    if not owner_cookie:
        _record_no_cookie(
            outcome,
            place,
            info.game_name,
            info.usernames,
            "No cookie available to create a private server",
        )
        return None, None
    if not identity.get("ok"):
        _record_no_cookie(
            outcome,
            place,
            info.game_name,
            info.usernames,
            str(
                identity.get("msg")
                or "Cookie identity check failed"
            ),
        )
        return None, None
    result = _create_result(
        owner_cookie,
        identity,
        place,
        slot,
        deps,
    )
    if result.get("ok"):
        game_patch, legacy_patch = _success_patch(
            info.game,
            config.legacy_vip,
            result,
        )
        outcome["created"].append(
            _created_entry(
                place,
                info.game_name,
                result,
                info.usernames,
            )
        )
        return game_patch, legacy_patch
    reason = str(
        result.get("msg") or "Private server setup failed"
    )
    reason_code = _classify_failure(reason)
    entry = _failure_entry(
        place,
        info.game_name,
        reason,
        reason_code,
        info.usernames,
    )
    if vip_unavailable_permanently(reason):
        # VIP disabled / paid: this game simply has no private server.
        # Do NOT skip those accounts — with several games open the
        # others still work, so they join this game's public server
        # instead. Reported so the UI can explain the fallback.
        _record_public_fallback(
            outcome,
            entry,
            info.usernames,
        )
        return None, None
    _record_skipped(outcome, entry, info.usernames)
    return None, None


def _process_needed(
    needed: dict[str, dict[str, Any]],
    by_name: dict[str, Any],
    config: _PreflightConfig,
    deps: _PreflightDeps,
    outcome: dict[str, Any],
    vip_unavailable_permanently: Callable[..., bool],
) -> tuple[dict[str, str], str | None]:
    games_patch: dict[str, str] = {}
    legacy_patch: str | None = None
    for place, slot in needed.items():
        game_patch, legacy_link = _process_slot(
            place,
            slot,
            by_name,
            config,
            deps,
            outcome,
            vip_unavailable_permanently,
        )
        if game_patch:
            games_patch.update(game_patch)
        if legacy_link is not None:
            legacy_patch = legacy_link
    return games_patch, legacy_patch


def _patched_games(
    games: list[dict[str, Any]],
    games_patch: dict[str, str],
) -> list[dict[str, Any]]:
    fresh = []
    for entry in games:
        row = dict(entry)
        link = games_patch.get(str(row.get("id") or ""))
        if link and not str(
            row.get("private_server_url") or ""
        ).strip():
            row["private_server_url"] = link
        fresh.append(row)
    return fresh


def _log_save_failure(exc: Exception) -> None:
    try:
        from core import flog_kv

        flog_kv(
            "PREFLIGHT",
            "private_server_link_save_failed",
            "warning",
            error=exc,
        )
    except Exception:
        "intentional: ignore exception"


def _save_links(
    cfg_mgr: Any,
    games: list[dict[str, Any]],
    games_patch: dict[str, str],
    legacy_patch: str | None,
) -> None:
    if not (
        (games_patch or legacy_patch)
        and cfg_mgr is not None
    ):
        return
    try:
        updates: dict[str, Any] = {}
        if games_patch and games:
            updates["games"] = _patched_games(
                games,
                games_patch,
            )
        if legacy_patch:
            updates["game_private_server_url"] = legacy_patch
        cfg_mgr.update(updates)
        cfg_mgr.save()
    except Exception as exc:
        _log_save_failure(exc)


def _log_done(outcome: dict[str, Any]) -> None:
    try:
        from core import flog_kv

        flog_kv(
            "PREFLIGHT",
            "private_server_preflight_done",
            created=len(outcome["created"]),
            failures=len(outcome["failures"]),
            skipped=len(outcome["skipped_usernames"]),
        )
    except Exception:
        "intentional: ignore exception"


def _run(cfg, accounts, games, game_mode, cfg_mgr, outcome):
    from domain.games import (
        effective_auto_flags,
        effective_place,
        game_for_account,
    )
    from domain.roblox_private_servers import (
        vip_unavailable_permanently,
    )

    config = _normalize_inputs(cfg, games, game_mode)
    needed = _collect_needed(
        accounts,
        config,
        game_for_account,
        effective_auto_flags,
        effective_place,
    )
    if not needed:
        return outcome

    by_name = _load_records_by_name()
    try:
        deps = _load_preflight_deps()
    except Exception as exc:
        _record_import_failure(outcome, needed, exc)
        return outcome

    games_patch, legacy_patch = _process_needed(
        needed,
        by_name,
        config,
        deps,
        outcome,
        vip_unavailable_permanently,
    )
    _save_links(
        cfg_mgr,
        config.games,
        games_patch,
        legacy_patch,
    )
    _log_done(outcome)
    return outcome
