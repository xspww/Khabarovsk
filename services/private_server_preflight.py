"""START-time Auto Create Private Server preflight.

When auto-create is enabled and an account has NO usable VIP link, the
first real launch attempt would fail (game has VIP disabled, or VIP
costs Robux while free-only is on). Instead of failing mid-run, START
tries the creation once up front per distinct place:

- free game  -> a link comes back and the server record is saved on the
  owner account, so the launch path can reuse its link/join code. The
  link is deliberately NOT written into any config link field: the
  Shared "Private Server URL" and per-game link fields are the user's
  explicit "join this room together" switches. With a field empty each
  account creates/owns its own private server instead of sharing one.
- VIP disabled / paid / error -> the failure is reported so START can
  skip those accounts and toast (bottom-right notification) instead.

Accounts that already have a usable VIP link (their own, their game's,
or the Shared one) are never touched and never notified.
"""

from __future__ import annotations

import time
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
    """Attempt auto-create once per place. Never raises (fail-open).

    ``cfg_mgr`` is accepted for API compatibility. Auto-created links are
    never persisted to config; the created server is saved on the owner
    account's record instead.
    """
    outcome: dict[str, Any] = {
        "created": [],
        "failures": [],
        "skipped_usernames": [],
        "public_fallback": [],
    }
    try:
        return _run(cfg, accounts, games, game_mode, outcome)
    except Exception as exc:
        try:
            from core import flog_kv

            flog_kv("PREFLIGHT", "private_server_preflight_failed", "warning", error=exc)
        except Exception:
            "intentional: ignore exception"
        return outcome


def _find_owner_cookie(
    slot_accounts: list[Any],
    by_name: dict[str, Any],
) -> tuple[Any, str, str]:
    """Return (owner_record, cookie, username) for the first account with a cookie."""
    for acc in slot_accounts:
        try:
            username = str(getattr(acc, "username", "") or "").strip()
        except Exception:
            username = ""
        record = by_name.get(username.lower()) if username else None
        record = record if isinstance(record, dict) else {}
        try:
            raw_cookie = getattr(acc, "cookie", "")
            cookie = str(raw_cookie).strip() if raw_cookie else ""
        except Exception:
            cookie = ""
        if not cookie:
            cookie = str(record.get("cookie") or "").strip()
        if not cookie:
            continue
        return (
            record or None,
            cookie,
            str(record.get("username") or username).strip(),
        )
    return None, "", ""


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
) -> tuple[Any, str, dict[str, Any] | None, str]:
    owner_record, owner_cookie, owner_username = _find_owner_cookie(
        slot_accounts,
        by_name,
    )
    if not owner_cookie:
        return owner_record, "", None, owner_username
    try:
        identity = deps.validate_record_cookie_identity(
            owner_record or {},
            owner_cookie,
            update_store=True,
        )
    except Exception as exc:
        identity = {"ok": False, "msg": str(exc)}
    return owner_record, owner_cookie, identity, owner_username


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


_OWNED_SERVER_FIELDS = (
    "private_server_id",
    "owner_user_id",
    "place_id",
    "universe_id",
    "name",
    "active",
    "link",
    "join_code",
    "access_code",
    "source",
    "status",
    "error",
    "synced_at",
)


def _owned_server_record(
    result: dict[str, Any],
    place: str,
    owner_user_id: str,
) -> dict[str, Any] | None:
    record = {
        key: result.get(key)
        for key in _OWNED_SERVER_FIELDS
        if result.get(key) not in (None, "")
    }
    record.setdefault("owner_user_id", owner_user_id)
    record.setdefault("place_id", place)
    record.setdefault("status", "ok")
    record.setdefault("synced_at", time.time())
    if not any(
        record.get(key)
        for key in ("private_server_id", "link", "join_code", "access_code")
    ):
        return None
    return record


def _owned_servers_for(
    store: Any,
    username: str,
) -> list[dict[str, Any]] | None:
    key = str(username or "").strip().lower()
    if not key:
        return []
    try:
        records = store.read_records(include_cookies=False)
    except Exception as exc:
        _log_persist_failure(username, exc)
        return None
    for record in records or []:
        if str(record.get("username") or "").strip().lower() != key:
            continue
        value = record.get("owned_private_servers")
        return [
            dict(item)
            for item in value or []
            if isinstance(item, dict)
        ]
    return []


def _persist_created_server(
    owner_username: str,
    place: str,
    owner_user_id: str,
    result: dict[str, Any],
) -> None:
    """Save the created server on its owner so launch can reuse the join data.

    Config link fields stay untouched: they are the user's explicit sharing
    switch. Storage failures are logged and ignored so START still works.
    """
    username = str(owner_username or "").strip()
    server = _owned_server_record(result, place, owner_user_id)
    if not username or server is None:
        return
    try:
        from domain.roblox_private_servers import merge_owned_private_server
        from services.account_hybrid import ACCOUNT_STORE

        existing = _owned_servers_for(ACCOUNT_STORE, username)
        if existing is None:
            return
        ACCOUNT_STORE.update_record(
            username,
            {
                "owned_private_servers": merge_owned_private_server(
                    existing,
                    server,
                )
            },
        )
    except Exception as exc:
        _log_persist_failure(username, exc)


def _log_persist_failure(username: str, exc: Exception) -> None:
    try:
        from core import flog_kv

        flog_kv(
            "PREFLIGHT",
            "private_server_owner_record_failed",
            "warning",
            account=username,
            error=exc,
        )
    except Exception:
        "intentional: ignore exception"


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
    deps: _PreflightDeps,
    outcome: dict[str, Any],
    vip_unavailable_permanently: Callable[..., bool],
) -> None:
    info = _slot_info(slot)
    _owner_record, owner_cookie, identity, owner_username = _owner_identity(
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
        return
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
        return
    result = _create_result(
        owner_cookie,
        identity,
        place,
        slot,
        deps,
    )
    if result.get("ok"):
        _persist_created_server(
            owner_username,
            place,
            str(identity.get("cookie_user_id") or ""),
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
        return
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
        return
    _record_skipped(outcome, entry, info.usernames)


def _process_needed(
    needed: dict[str, dict[str, Any]],
    by_name: dict[str, Any],
    deps: _PreflightDeps,
    outcome: dict[str, Any],
    vip_unavailable_permanently: Callable[..., bool],
) -> None:
    for place, slot in needed.items():
        _process_slot(
            place,
            slot,
            by_name,
            deps,
            outcome,
            vip_unavailable_permanently,
        )


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


def _run(cfg, accounts, games, game_mode, outcome):
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

    _process_needed(
        needed,
        by_name,
        deps,
        outcome,
        vip_unavailable_permanently,
    )
    _log_done(outcome)
    return outcome
