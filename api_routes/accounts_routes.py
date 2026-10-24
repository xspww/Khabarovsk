from __future__ import annotations

import html as html_lib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from typing import Any

from fastapi import HTTPException, Request

from core import banned_block_reason, cookie_identity_block_reason, cookie_invalid_block_reason, suspended_block_reason
from domain.roblox_private_servers import resolve_vip_access_code
from infra import net_tls
from services.account_hybrid import ACCOUNT_STORE, audit_event
from services.account_reload import emit_reload_cookie_events
from services.ban_guard import (
    BANNED_BLOCK_REASON,
    BANNED_REASON,
    SUSPENDED_BLOCK_REASON,
    clear_account_banned_hold,
    is_banned_status_text,
    is_banned_text,
    is_suspended_status_text,
    is_suspended_text,
)
from services.captcha_guard import (
    CAPTCHA_BLOCK_REASON,
    CAPTCHA_REASON,
    is_captcha_status_text,
    set_account_captcha_hold,
)
from services.roblox_launch_service import AccountLaunchService, parse_vip_link
from services.roblox_validation import validate_cookie_details
from services.verify_guard import (
    LOCKED_BLOCK_REASON,
    LOCKED_REASON,
    RECOVERY_BLOCK_REASON,
    RECOVERY_REASON,
    is_locked_status_text,
    is_locked_text,
    is_recovery_status_text,
    is_recovery_text,
    set_account_locked_hold,
    set_account_recovery_hold,
)

from . import account_records
from .context import ApiContext
from .idempotency import begin_idempotent_request, begin_idempotent_request_sync, finish_idempotent_request
from .settings_state import _normalize_window_size_settings

APP_USER_AGENT = "CronusLauncher/RT"


def _is_moderation_mark(record: dict[str, Any]) -> bool:
    """True when the record carries a Banned or Suspended mark.

    Unmark has to clear both: a suspension blocks the account like a ban but
    is not permanent, so it is tracked under its own import_status.
    """
    if not isinstance(record, dict):
        return False
    return is_banned_status_text(record.get("manual_status"), record.get("import_status")) or bool(
        is_suspended_status_text(record.get("manual_status"), record.get("import_status"))
    )


_AVATAR_CACHE: dict[str, tuple[float, str]] = {}
_AVATAR_CACHE_TTL = 300.0
# Place lookups hit 2-3 Roblox endpoints per call; cache successes so the
# dashboard, game picker and Games card all resolve instantly after first.
_PLACE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_PLACE_CACHE_TTL = 600.0
_PLACE_CACHE_LOCK = threading.Lock()


def _dead_cookie_ban_payload(
    cookie: str, username_hint: str, resolved_username: str, resolved_user_id: str
) -> tuple[dict[str, Any] | None, str, str]:
    """Resolve a dead cookie's ban state through the public API."""
    try:
        from services.roblox_identity import resolve_ban_for_dead_cookie as _resolve_dead

        _b, _name, _uid = _resolve_dead(cookie, username_hint)
    except Exception:
        _b, _name, _uid = None, "", ""
    if _name and not resolved_username:
        resolved_username = _name
    if _uid and not resolved_user_id:
        resolved_user_id = _uid
    if _b is not True:
        return None, resolved_username, resolved_user_id
    return (
        {
            "ok": False,
            "username": resolved_username or username_hint,
            "user_id": resolved_user_id,
            "banned": True,
            "suspended": False,
            "msg": "Account banned (Roblox isBanned=true)",
        },
        resolved_username,
        resolved_user_id,
    )


def _refreshed_cookie_payload(
    fresh: str, resolved_username: str, resolved_user_id: str
) -> dict[str, Any] | None:
    """Build a success payload when a login refreshed the cookie."""
    try:
        _rok, _ruser, _rdetail, _rmeta = validate_cookie_details(fresh)
    except Exception:
        return None
    if not _rok:
        return None
    rmeta = _rmeta or {}
    return {
        "ok": True,
        "username": str(next((v for v in (rmeta.get("username"), _ruser) if v), "")),
        "user_id": str(rmeta.get("user_id") or ""),
        "banned": False,
        "suspended": False,
        "refreshed": True,
        "msg": "",
    }


def _locked_cookie_payload(
    cookie: str, username_hint: str, resolved_username: str, resolved_user_id: str
) -> dict[str, Any] | None:
    """Classify a challenge-gated login; returns a locked payload or None."""
    try:
        from services.roblox_identity import reprobe_moderation_response as _reprobe_t
        from services.roblox_identity import resolve_moderated_dead_cookie as _resolve_mod_t2
    except Exception:
        return None
    try:
        _rs_t, _rb_t = _reprobe_t(cookie)
    except Exception:
        return None
    status = int(_rs_t) if _rs_t else 0
    if status != 403:
        return None
    try:
        _mv_t2, _md_t2, _mn_t2, _mu_t2 = _resolve_mod_t2(
            cookie, resolved_username or username_hint
        )
    except Exception:
        return None
    if _mv_t2 != "locked":
        return None
    return _locked_payload(
        _md_t2, _mn_t2, _mu_t2, username_hint, resolved_username, resolved_user_id
    )


def _dead_cookie_password_payload(
    cookie: str,
    username_hint: str,
    password_hint: str,
    resolved_username: str,
    resolved_user_id: str,
) -> tuple[dict[str, Any] | None, str]:
    """Ask Roblox what a dead cookie + pasted password actually needs."""
    if not password_hint:
        return None, ""
    target = resolved_username or username_hint
    if not target:
        return None, ""
    try:
        from services.credential_check import check_credentials as _check_creds

        _cc = _check_creds(target, password_hint)
    except Exception:
        return None, ""
    if not _cc:
        _cc = {}
    raw_outcome = _cc.get("outcome")
    check_outcome = str(raw_outcome) if raw_outcome else ""
    raw_fresh = _cc.get("fresh_cookie")
    fresh = str(raw_fresh) if raw_fresh else ""
    if check_outcome == "valid" and fresh:
        return _refreshed_cookie_payload(fresh, resolved_username, resolved_user_id), check_outcome
    raw_challenge = _cc.get("challenge_type")
    challenge = str(raw_challenge) if raw_challenge else ""
    if check_outcome == "unknown" and challenge:
        return (
            _locked_cookie_payload(cookie, username_hint, resolved_username, resolved_user_id),
            check_outcome,
        )
    return None, check_outcome


def _resolve_dead_cookie(
    cookie: str,
    username_hint: str,
    password_hint: str,
    resolved_username: str,
    resolved_user_id: str,
) -> tuple[dict[str, Any] | None, str, str, str]:
    """Resolve a dead cookie's real state; the password is never stored.

    Returns (early_payload_or_None, resolved_username, resolved_user_id,
    check_outcome). A non-None payload short-circuits the caller's response.
    """
    payload, resolved_username, resolved_user_id = _dead_cookie_ban_payload(
        cookie, username_hint, resolved_username, resolved_user_id
    )
    if payload is not None:
        return payload, resolved_username, resolved_user_id, ""
    payload, check_outcome = _dead_cookie_password_payload(
        cookie, username_hint, password_hint, resolved_username, resolved_user_id
    )
    return payload, resolved_username, resolved_user_id, check_outcome


def _locked_payload(
    detail: str,
    name: str,
    user_id: str,
    username_hint: str,
    resolved_username: str,
    resolved_user_id: str,
) -> dict[str, Any]:
    """Build the shared challenge-gated (Locked) account payload."""
    return {
        "ok": False,
        "username": name or resolved_username or username_hint,
        "user_id": user_id or resolved_user_id,
        "banned": False,
        "suspended": False,
        "locked": True,
        "recovery": False,
        "captcha": False,
        "bad_password": False,
        "msg": detail,
    }


def _account_data_api_records(ctx: ApiContext) -> list[dict[str, Any]]:
    return account_records.account_data_api_records(ACCOUNT_STORE, ctx.farm)


def _replace_farm_accounts_from_store(ctx: ApiContext) -> int:
    return account_records.replace_farm_accounts_from_store(
        ctx, ctx.cfg_mgr, ctx.farm, ACCOUNT_STORE
    )


def _clear_runtime_allowlist_after_reload(ctx: ApiContext) -> dict[str, Any]:
    return account_records.clear_runtime_allowlist_after_reload(ctx.cfg_mgr, ctx.farm)


def _validate_cookie_records_from_store() -> dict[str, Any]:
    return account_records.validate_cookie_records_from_store(
        ACCOUNT_STORE, validate_cookie_details, audit_event
    )


def _find_account_record(
    username: str, include_cookie: bool = True
) -> dict[str, Any] | None:
    return account_records.find_account_record(
        ACCOUNT_STORE, username, include_cookie=include_cookie
    )


def _import_cookie_validator(cookie: str):
    return account_records.import_cookie_validator(cookie, validate_cookie_details)


def _http_json(url: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"Mozilla/5.0 {APP_USER_AGENT}",
            "Accept": "application/json, text/plain, */*",
        },
    )
    with net_tls.urlopen(req, timeout=8.0) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _universe_details(place: str) -> tuple[str, dict[str, Any]]:
    details: dict[str, Any] = {}
    universe_id = ""
    try:
        universe_payload = _http_json(
            f"https://apis.roblox.com/universes/v1/places/{place}/universe"
        )
        if isinstance(universe_payload, dict):
            universe_id = str(universe_payload.get("universeId") or "")
        if universe_id:
            games_payload = _http_json(
                "https://games.roblox.com/v1/games?"
                + urllib.parse.urlencode({"universeIds": universe_id})
            )
            data = games_payload.get("data") if isinstance(games_payload, dict) else []
            if isinstance(data, list) and data:
                details = data[0] if isinstance(data[0], dict) else {}
    except Exception:
        details = {}
    return universe_id, details


def _thumbnail_url(universe_id: str, place: str) -> str:
    thumb_target = universe_id or place
    thumb_path = "games/icons" if universe_id else "places/gameicons"
    thumb_key = "universeIds" if universe_id else "placeIds"
    thumb_url = (
        "https://thumbnails.roblox.com/v1/"
        + thumb_path
        + "?"
        + urllib.parse.urlencode(
            {
                thumb_key: thumb_target,
                "size": "150x150",
                "format": "Png",
                "isCircular": "false",
            }
        )
    )
    try:
        payload = _http_json(thumb_url)
        items = payload.get("data") if isinstance(payload, dict) else []
        if isinstance(items, list) and items:
            return str(items[0].get("imageUrl") or "")
    except Exception:
        return ""
    return ""


def _int_or_none(value: Any) -> int | None:
    try:
        number = int(value)
    except Exception:
        return None
    return number if number >= 0 else None


def _place_numbers(details: dict[str, Any]) -> dict[str, int | None]:
    return {
        "playing": _int_or_none(details.get("playing")),
        "visits": _int_or_none(details.get("visits")),
        "max_players": _int_or_none(details.get("maxPlayers")),
        "favorited_count": _int_or_none(details.get("favoritedCount")),
    }


def _place_text(details: dict[str, Any]) -> tuple[str, str, str]:
    name = str(details.get("name") or details.get("sourceName") or "").strip()
    creator = (
        details.get("creator") if isinstance(details.get("creator"), dict) else {}
    )
    builder = str(
        details.get("builder")
        or details.get("creatorName")
        or creator.get("name")
        or ""
    ).strip()
    genre = str(details.get("genre") or details.get("genre1") or "").strip()
    return name, builder, genre


def _scrape_place_page(place: str, current_image: str) -> tuple[str, str]:
    try:
        req = urllib.request.Request(
            f"https://www.roblox.com/games/{place}",
            headers={
                "User-Agent": f"Mozilla/5.0 {APP_USER_AGENT}",
                "Accept": "text/html, */*",
            },
        )
        with net_tls.urlopen(req, timeout=8.0) as resp:
            page = resp.read().decode("utf-8", errors="replace")
        title_match = re.search(
            r"<title>(.*?)</title>", page, flags=re.IGNORECASE | re.DOTALL
        )
        name = ""
        if title_match:
            title = html_lib.unescape(
                re.sub(r"\s+", " ", title_match.group(1))
            ).strip()
            name = re.sub(
                r"\s*\|\s*Roblox\s*$", "", title, flags=re.IGNORECASE
            ).strip()
        image_url = current_image
        image_match = re.search(
            r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
            page,
            flags=re.IGNORECASE,
        )
        if image_match and not image_url:
            image_url = html_lib.unescape(image_match.group(1)).strip()
        return name, image_url
    except Exception as exc:
        raise HTTPException(502, f"Roblox place lookup failed: {exc}")


def _lookup_roblox_place(place_id: str) -> dict[str, Any]:
    place = str(place_id or "").strip()
    if not place.isdigit():
        raise HTTPException(400, "place_id must be numeric")
    now = time.time()
    with _PLACE_CACHE_LOCK:
        hit = _PLACE_CACHE.get(place)
        if hit and now - hit[0] < _PLACE_CACHE_TTL:
            return dict(hit[1])
    universe_id, details = _universe_details(place)
    image_url = _thumbnail_url(universe_id, place)
    name, builder, genre = _place_text(details)
    if not name:
        name, image_url = _scrape_place_page(place, image_url)
    return _store_place_cache(
        place,
        {
            "ok": True,
            "place_id": place,
            "name": name or f"Place {place}",
            "builder": builder,
            "genre": genre,
            **_place_numbers(details),
            "universe_id": universe_id,
            "image_url": image_url,
            "url": f"https://www.roblox.com/games/{place}",
        },
    )


def _store_place_cache(place: str, payload: dict[str, Any]) -> dict[str, Any]:
    with _PLACE_CACHE_LOCK:
        _PLACE_CACHE[place] = (time.time(), dict(payload))
        while len(_PLACE_CACHE) > 200:
            oldest = min(_PLACE_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _PLACE_CACHE.pop(oldest, None)
    return payload


def _api_save_cookie(
    ctx: ApiContext, username: str, request: Request
) -> dict[str, Any]:
    farm = ctx.farm
    idem = begin_idempotent_request_sync(request, "save_cookie", username)
    if idem.replay:
        return idem.response
    acc = next((a for a in farm._accounts if a.username == username), None)
    if not acc:
        raise HTTPException(404, "Account not found")
    if not str(acc.cookie or "").strip():
        result = {"ok": False, "msg": "No cookie loaded for this account"}
        finish_idempotent_request(idem, result)
        return result
    ok, cookie_username, detail, meta = validate_cookie_details(acc.cookie)
    if not ok:
        result = {"ok": False, "msg": detail}
        finish_idempotent_request(idem, result)
        return result
    mismatch = bool(cookie_username and acc.username.lower() != cookie_username.lower())
    if mismatch:
        ACCOUNT_STORE.update_record(
            username,
            {
                "cookie_username": cookie_username,
                "cookie_user_id": str(meta.get("user_id") or ""),
                "cookie_mismatch": True,
                "import_status": "cookie_mismatch",
            },
        )
        result = {
            "ok": False,
            "msg": (
                f"Cookie belongs to {cookie_username}, not {username}. "
                "Reimport the correct cookie."
            ),
        }
        finish_idempotent_request(idem, result)
        return result
    ACCOUNT_STORE.upsert_records([acc.to_dict()])
    ctx.cfg_mgr.save_accounts(farm._accounts)
    result = {
        "ok": True,
        "msg": f"Saved encrypted cookie to AccountData.json for {username}",
    }
    finish_idempotent_request(idem, result)
    return result


def _games_for_mode(snap: Any) -> list:
    from domain.games import ensure_games_migrated, normalize_game_mode

    try:
        mode = normalize_game_mode((snap or {}).get("game_mode", "shared"))
    except Exception:
        mode = "shared"
    if isinstance(snap, dict) and mode == "per_account":
        return ensure_games_migrated(dict(snap))
    return []


def _game_place(game: dict[str, Any]) -> str:
    place = str(game.get("place_id") or "").strip()
    if place:
        return place
    # Games with only a private-server link still have a place; show that map.
    return str(
        parse_vip_link(str(game.get("private_server_url") or ""))[0] or ""
    ).strip()


def _effective_place_id(
    item: dict[str, Any], games: list, game_for_account
) -> str:
    if str(item.get("place_id") or "").strip() or not games:
        return ""
    game = game_for_account(item.get("game_id"), "", games)
    if not game:
        return ""
    return _game_place(game)


def _attach_effective_places(
    records: list[dict[str, Any]], games: list, game_for_account
) -> None:
    for item in records:
        try:
            item["effective_place_id"] = _effective_place_id(
                item, games, game_for_account
            )
        except Exception:
            item["effective_place_id"] = ""


def _api_get_accounts(ctx: ApiContext) -> list[dict[str, Any]]:
    records = _account_data_api_records(ctx)
    # Assigned-game place for instant, flicker-free map display.
    # The dashboard renders this first; explicit per-account place_id,
    # observed and legacy fallbacks keep working when it is empty.
    try:
        from domain.games import game_for_account

        games = _games_for_mode(ctx.cfg_mgr.snapshot())
        _attach_effective_places(records, games, game_for_account)
    except Exception:
        "intentional: ignore exception"
    return records


def _api_open_captcha_login(
    ctx: ApiContext, username: str, request: Request
) -> dict[str, Any]:
    idem = begin_idempotent_request_sync(request, "captcha_open_login", username)
    if idem.replay:
        return idem.response
    record = _find_account_record(username, include_cookie=False)
    if not record:
        raise HTTPException(404, "Account not found")
    try:
        opened = bool(webbrowser.open("https://www.roblox.com/login", new=2))
    except Exception as exc:
        result = {
            "ok": False,
            "opened": False,
            "msg": f"Failed to open Roblox login: {exc}",
        }
        finish_idempotent_request(idem, result)
        return result
    audit_event("captcha_open_login", username=username, ok=opened, detail="manual_login")
    result = {
        "ok": opened,
        "opened": opened,
        "msg": (
            "Roblox login opened. Solve CAPTCHA manually, then Reload Cookies or "
            "import the new cookie and click Resume."
            if opened
            else "Roblox login could not be opened automatically."
        ),
    }
    finish_idempotent_request(idem, result)
    return result


def _reload_accounts_data(ctx: ApiContext) -> tuple[dict, int, dict]:
    validation = _validate_cookie_records_from_store()
    count = _replace_farm_accounts_from_store(ctx)
    emit_reload_cookie_events(ctx.farm, validation)
    allowlist_result = _clear_runtime_allowlist_after_reload(ctx)
    return validation, count, allowlist_result


def _reload_counts(validation: dict) -> dict[str, int]:
    return {
        "invalid": int(validation.get("invalid") or 0),
        "valid": len(validation.get("valid_accounts") or []),
        "captcha": int(validation.get("captcha") or 0),
        "banned": int(
            validation.get("banned")
            or len(validation.get("banned_accounts") or [])
            or 0
        ),
        "suspended": int(validation.get("suspended") or 0),
    }


def _reload_message(counts: dict[str, int], allowlist_result: dict) -> str:
    msg = f"Checked cookies: {counts['valid']} valid"
    if counts["captcha"]:
        msg += f", {counts['captcha']} need CAPTCHA"
    if counts["invalid"]:
        msg += f", marked {counts['invalid']} invalid"
    if counts["suspended"]:
        msg += f", marked {counts['suspended']} suspended"
    if counts["banned"]:
        msg += f", marked {counts['banned']} banned"
    if allowlist_result.get("allowlist_cleared"):
        msg += ", cleared account test lock"
    return msg


def _log_reload(counts: dict[str, int], count: int) -> None:
    try:
        from core import flog_kv as _flog_kv

        _flog_kv(
            "COOKIE",
            "reload_cookies",
            valid=counts["valid"],
            captcha=counts["captcha"],
            invalid=counts["invalid"],
            banned=counts["banned"],
            suspended=counts["suspended"],
            count=count,
        )
    except Exception:
        "intentional: ignore exception"


def _api_reload_accounts(ctx: ApiContext, request: Request) -> dict[str, Any]:
    idem = begin_idempotent_request_sync(request, "accounts_reload")
    if idem.replay:
        return idem.response
    try:
        validation, count, allowlist_result = _reload_accounts_data(ctx)
    except Exception as e:
        raise HTTPException(400, f"Reload failed: {e}")
    counts = _reload_counts(validation)
    msg = _reload_message(counts, allowlist_result)
    _log_reload(counts, count)
    result = {"ok": True, "count": count, "msg": msg, **validation, **allowlist_result}
    finish_idempotent_request(idem, result)
    return result


def _avatar_ids(user_ids: str) -> list[str]:
    ids: list[str] = []
    seen = set()
    for raw in re.split(r"[,\s]+", str(user_ids or "")):
        uid = raw.strip()
        if not uid.isdigit() or uid in seen:
            continue
        seen.add(uid)
        ids.append(uid)
        if len(ids) >= 100:
            break
    return ids


def _cached_avatars(ids: list[str], now: float) -> tuple[dict[str, str], list[str]]:
    avatars: dict[str, str] = {}
    to_fetch: list[str] = []
    for uid in ids:
        cached = _AVATAR_CACHE.get(uid)
        if cached and (now - cached[0]) < _AVATAR_CACHE_TTL:
            avatars[uid] = cached[1]
        else:
            to_fetch.append(uid)
    return avatars, to_fetch


def _fetch_avatars(to_fetch: list[str], now: float) -> dict[str, str]:
    avatars: dict[str, str] = {}
    url = "https://thumbnails.roblox.com/v1/users/avatar-headshot?" + urllib.parse.urlencode(
        {
            "userIds": ",".join(to_fetch),
            "size": "48x48",
            "format": "Png",
            "isCircular": "false",
        }
    )
    req = urllib.request.Request(
        url,
        headers={"User-Agent": APP_USER_AGENT, "Accept": "application/json"},
    )
    with net_tls.urlopen(req, timeout=8.0) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    for item in payload.get("data", []) if isinstance(payload, dict) else []:
        uid = str(item.get("targetId") or "")
        image_url = str(item.get("imageUrl") or "")
        if uid and image_url:
            avatars[uid] = image_url
            _AVATAR_CACHE[uid] = (now, image_url)
    return avatars


def _api_account_avatars(user_ids: str = "") -> dict[str, Any]:
    ids = _avatar_ids(user_ids)
    now = time.time()
    avatars, to_fetch = _cached_avatars(ids, now)
    if to_fetch:
        try:
            avatars.update(_fetch_avatars(to_fetch, now))
        except Exception as exc:
            return {
                "ok": False,
                "avatars": avatars,
                "missing": to_fetch,
                "msg": str(exc),
            }
    missing = [uid for uid in ids if uid not in avatars]
    return {"ok": True, "avatars": avatars, "missing": missing}


async def _api_set_accounts(ctx: ApiContext, request: Request) -> dict[str, Any]:
    body = await request.json()
    if not isinstance(body, list):
        raise HTTPException(400, "Expected array")
    try:
        ACCOUNT_STORE.replace_from_cronus_payload([dict(item) for item in body])
        count = _replace_farm_accounts_from_store(ctx)
    except Exception as e:
        raise HTTPException(400, f"Bad account payload: {e}")
    return {"ok": True, "count": count, "store": "AccountData.json"}


def _api_export_accounts(ctx: ApiContext) -> dict[str, Any]:
    return {
        "ok": True,
        "accounts": _account_data_api_records(ctx),
        "path": ACCOUNT_STORE.path,
    }


def _import_lines(lines: Any) -> list[str]:
    if isinstance(lines, str):
        return [line.strip() for line in lines.splitlines() if line.strip()]
    if isinstance(lines, list):
        return [str(line).strip() for line in lines if str(line).strip()]
    return []


def _import_body_lines(body: dict[str, Any]) -> list[str]:
    lines = body.get("lines") or body.get("text") or ""
    return _import_lines(lines)


def _import_accountdata(body: dict[str, Any]) -> dict[str, Any]:
    path = str(body.get("path") or "").strip()
    if not path or not os.path.exists(path):
        raise HTTPException(400, "path not found")
    with open(path, "rb") as f:
        records = ACCOUNT_STORE.decode_account_file_bytes(f.read())
    imported, merged = ACCOUNT_STORE.upsert_records(records)
    return {"ok": True, "imported": imported, "count": len(merged)}


def _import_json_accounts(body: dict[str, Any]) -> dict[str, Any]:
    imported, merged = ACCOUNT_STORE.upsert_records(body.get("accounts") or [])
    return {"ok": True, "imported": imported, "count": len(merged)}


def _import_accounts(body: dict[str, Any], kind: str) -> dict[str, Any]:
    if kind in {"cookie", "cookies", "roblosecurity"}:
        return ACCOUNT_STORE.import_cookie_lines(
            _import_body_lines(body), validator=_import_cookie_validator
        )
    if kind in {"accountdata", "ram", "file"}:
        return _import_accountdata(body)
    if kind in {"json", "accounts"} and isinstance(body.get("accounts"), list):
        return _import_json_accounts(body)
    raise HTTPException(400, "Unsupported import kind")


async def _api_import_accounts(
    ctx: ApiContext, request: Request
) -> dict[str, Any]:
    idem = await begin_idempotent_request(request, "accounts_import")
    if idem.replay:
        return idem.response
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected object")
    kind = str(body.get("kind") or body.get("type") or "auto").strip().lower()
    try:
        result = _import_accounts(body, kind)
        count = _replace_farm_accounts_from_store(ctx)
        result["count"] = count
        finish_idempotent_request(idem, result)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))


def _runtime_account(farm: Any, username: str) -> Any:
    return next((a for a in farm._accounts if a.username == username), None)


def _apply_locked_result(
    ctx: ApiContext, username: str, result: dict[str, Any]
) -> None:
    if result.get("ok") or not is_locked_text(result.get("msg"), result.get("detail")):
        return
    farm = ctx.farm
    runtime = _runtime_account(farm, username)
    if runtime:
        set_account_locked_hold(
            runtime,
            str(result.get("msg") or result.get("detail") or ""),
            source="manual_launch",
            runtime_writer=farm._runtime_state,
        )
        if farm._recovery:
            farm._recovery.fail_account(runtime, LOCKED_REASON, LOCKED_BLOCK_REASON)
        farm._push_event(
            "verify",
            f"Account Locked: {runtime.display_name}",
            account=runtime,
            severity="warn",
            reason=LOCKED_REASON,
        )
    result["fatal"] = True
    result["locked_required"] = True
    result["blocked_reason"] = LOCKED_BLOCK_REASON
    result["msg"] = LOCKED_BLOCK_REASON


def _apply_recovery_result(
    ctx: ApiContext, username: str, result: dict[str, Any]
) -> None:
    if result.get("ok") or not is_recovery_text(
        result.get("msg"), result.get("detail")
    ):
        return
    farm = ctx.farm
    runtime = _runtime_account(farm, username)
    if runtime:
        set_account_recovery_hold(
            runtime,
            str(result.get("msg") or result.get("detail") or ""),
            source="manual_launch",
            runtime_writer=farm._runtime_state,
        )
        if farm._recovery:
            farm._recovery.fail_account(
                runtime, RECOVERY_REASON, RECOVERY_BLOCK_REASON
            )
        farm._push_event(
            "verify",
            f"Account Recovery: {runtime.display_name}",
            account=runtime,
            severity="warn",
            reason=RECOVERY_REASON,
        )
    result["fatal"] = True
    result["recovery_required"] = True
    result["blocked_reason"] = RECOVERY_BLOCK_REASON
    result["msg"] = RECOVERY_BLOCK_REASON


def _apply_captcha_result(
    ctx: ApiContext, username: str, result: dict[str, Any]
) -> None:
    if result.get("ok") or not is_captcha_status_text(
        result.get("msg"), result.get("detail")
    ):
        return
    farm = ctx.farm
    runtime = _runtime_account(farm, username)
    if runtime:
        set_account_captcha_hold(
            runtime,
            str(result.get("msg") or ""),
            source="manual_launch",
            runtime_writer=farm._runtime_state,
        )
        if farm._recovery:
            farm._recovery.fail_account(runtime, CAPTCHA_REASON, CAPTCHA_BLOCK_REASON)
        farm._push_event(
            "captcha",
            f"Captcha required: {runtime.display_name}",
            account=runtime,
            severity="warn",
            reason=CAPTCHA_REASON,
        )
    result["fatal"] = True
    result["captcha_required"] = True
    result["blocked_reason"] = CAPTCHA_BLOCK_REASON
    result["msg"] = CAPTCHA_BLOCK_REASON


def _apply_banned_result(
    ctx: ApiContext, username: str, result: dict[str, Any]
) -> None:
    if result.get("ok") or not is_banned_text(result.get("msg"), result.get("detail")):
        return
    farm = ctx.farm
    runtime = _runtime_account(farm, username)
    if runtime:
        try:
            from services.ban_guard import set_account_banned_hold

            set_account_banned_hold(
                runtime,
                str(result.get("detail") or result.get("msg") or ""),
                source="manual_launch",
                runtime_writer=farm._runtime_state,
            )
        except Exception:
            "intentional: ignore exception"
        if farm._recovery:
            farm._recovery.fail_account(runtime, BANNED_REASON, BANNED_BLOCK_REASON)
        try:
            farm._push_event(
                "cookie",
                f"Account banned: {runtime.display_name}",
                account=runtime,
                severity="error",
                reason=BANNED_REASON,
            )
        except Exception:
            "intentional: ignore exception"
    result["fatal"] = True
    result["banned"] = True
    result["blocked_reason"] = BANNED_BLOCK_REASON
    result["msg"] = BANNED_BLOCK_REASON


def _apply_launch_holds(
    ctx: ApiContext, username: str, result: dict[str, Any]
) -> None:
    _apply_locked_result(ctx, username, result)
    _apply_recovery_result(ctx, username, result)
    _apply_captcha_result(ctx, username, result)
    _apply_banned_result(ctx, username, result)


def _moderation_flags(record: dict[str, Any]) -> tuple[str, str]:
    suspended_reason = suspended_block_reason(
        record.get("manual_status"),
        record.get("import_status"),
        record.get("last_error", ""),
        record.get("last_crash_reason", ""),
    )
    banned_reason = banned_block_reason(
        record.get("manual_status"),
        record.get("import_status"),
        record.get("last_error", ""),
        record.get("last_crash_reason", ""),
    )
    return suspended_reason, banned_reason


def _base_blocked_reason(
    record: dict[str, Any],
    mismatch_reason: str,
    suspended_reason: str,
    banned_reason: str,
) -> str:
    blocked_reason = mismatch_reason
    invalid_reason = cookie_invalid_block_reason(
        record.get("manual_status"), record.get("import_status")
    )
    if invalid_reason:
        blocked_reason = invalid_reason
    if suspended_reason:
        blocked_reason = SUSPENDED_BLOCK_REASON
    if banned_reason and not suspended_reason:
        blocked_reason = banned_reason
    return blocked_reason


def _hold_blocked_reason(
    record: dict[str, Any], blocked_reason: str, moderation_reason: str
) -> str:
    manual_status = record.get("manual_status")
    import_status = record.get("import_status")
    if is_locked_status_text(manual_status, import_status):
        if not moderation_reason:
            blocked_reason = LOCKED_BLOCK_REASON
    elif is_recovery_status_text(manual_status, import_status):
        if not moderation_reason:
            blocked_reason = RECOVERY_BLOCK_REASON
    if is_captcha_status_text(manual_status, import_status):
        if (
            not moderation_reason
            and not is_locked_status_text(manual_status, import_status)
            and not is_recovery_status_text(manual_status, import_status)
        ):
            blocked_reason = CAPTCHA_BLOCK_REASON
    return blocked_reason


def _launch_blocked_reason(
    record: dict[str, Any], username: str
) -> tuple[str, str]:
    mismatch_reason = cookie_identity_block_reason(
        str(record.get("username") or username),
        str(record.get("cookie_username") or ""),
        bool(record.get("cookie_mismatch", False)),
    )
    suspended_reason, banned_reason = _moderation_flags(record)
    blocked_reason = _base_blocked_reason(
        record, mismatch_reason, suspended_reason, banned_reason
    )
    blocked_reason = _hold_blocked_reason(
        record, blocked_reason, suspended_reason or banned_reason
    )
    return blocked_reason, mismatch_reason


async def _api_launch_account(
    ctx: ApiContext, username: str, request: Request
) -> dict[str, Any]:
    idem = await begin_idempotent_request(request, "account_launch", username)
    if idem.replay:
        return idem.response
    body = await request.json()
    if not isinstance(body, dict):
        body = {}
    record = _find_account_record(username, include_cookie=True)
    if not record:
        raise HTTPException(404, "Account not found")
    blocked_reason, mismatch_reason = _launch_blocked_reason(record, username)
    if blocked_reason:
        result = {
            "ok": False,
            "fatal": True,
            "msg": blocked_reason,
            "blocked_reason": blocked_reason,
            "cookie_mismatch": bool(mismatch_reason),
        }
        audit_event(
            "launch",
            username=username,
            ok=False,
            detail=blocked_reason,
            mode="blocked",
        )
        finish_idempotent_request(idem, result)
        return result
    window_settings = _normalize_window_size_settings(ctx, {})
    result = AccountLaunchService.launch_record(
        record,
        body,
        ctx.cfg_mgr,
        window_settings=window_settings,
        idempotency_key=idem.key,
        body_hash=idem.body_hash,
    )
    _apply_launch_holds(ctx, username, result)
    audit_event(
        "launch",
        username=username,
        ok=bool(result.get("ok")),
        detail=result.get("msg", ""),
        mode=result.get("mode", ""),
    )
    if result.get("ok"):
        _replace_farm_accounts_from_store(ctx)
    finish_idempotent_request(idem, result)
    return result


def _api_kill_duplicate(
    ctx: ApiContext, username: str, request: Request
) -> dict[str, Any]:
    idem = begin_idempotent_request_sync(request, "kill_duplicate", username)
    if idem.replay:
        return idem.response
    record = _find_account_record(username, include_cookie=False)
    if not record:
        raise HTTPException(404, "Account not found")
    result = AccountLaunchService.kill_duplicate_instances(
        record,
        idempotency_key=idem.key,
        body_hash=idem.body_hash,
    )
    audit_event(
        "kill_duplicate",
        username=username,
        ok=bool(result.get("ok")),
        killed=result.get("killed", []),
    )
    finish_idempotent_request(idem, result)
    return result


async def _api_test_vip(username: str, request: Request) -> dict[str, Any]:
    body = await request.json()
    vip_url = body.get("vip_url", "")
    if not vip_url:
        raise HTTPException(400, "vip_url required")
    place_id, link_code = parse_vip_link(vip_url)
    if not place_id:
        return {"ok": False, "msg": "Cannot parse place_id"}
    if not link_code:
        return {
            "ok": False,
            "msg": "No linkCode found - this link will join a PUBLIC server, not VIP!",
        }
    resolved: dict[str, Any] = {}
    record = _find_account_record(username, include_cookie=True)
    if record and record.get("cookie"):
        resolved = resolve_vip_access_code(str(record.get("cookie") or ""), vip_url)
    return {
        "ok": True,
        "place_id": place_id,
        "link_code": f"{link_code[:6]}...{link_code[-4:]}",
        "vip_resolved": bool(resolved.get("ok")) if resolved else False,
        "access_code_present": bool(resolved.get("access_code")) if resolved else False,
        "url": f"roblox://experiences/start?placeId={place_id}&linkCode=***",
        "msg": "VIP link valid"
        + (" and accessCode resolved" if resolved.get("ok") else ""),
    }


def _api_game_place(place_id: str) -> dict[str, Any]:
    return _lookup_roblox_place(place_id)


def _first_text(*values: Any) -> str:
    for value in values:
        if value:
            return str(value)
    return ""


def _cookie_identity(
    cookie: str, ok: bool, username: str, meta: dict[str, Any]
) -> tuple[str, str]:
    resolved_username = _first_text(meta.get("username"), username)
    resolved_user_id = _first_text(meta.get("user_id"))
    if ok:
        return resolved_username, resolved_user_id
    if resolved_username and resolved_user_id:
        return resolved_username, resolved_user_id
    # Cookie-only paste: the token embeds its owner, so the preview
    # can name the account (and resolve a 403) with no prefix typed.
    try:
        from services.roblox_identity import cookie_embedded_identity

        embedded_name, embedded_uid = cookie_embedded_identity(cookie)
    except Exception:
        return resolved_username, resolved_user_id
    if not resolved_username:
        resolved_username = embedded_name
    if not resolved_user_id:
        resolved_user_id = embedded_uid
    return resolved_username, resolved_user_id


def _cookie_hints(body: dict[str, Any]) -> tuple[str, str]:
    username_hint = _first_text(body.get("username")).strip()
    password_hint = _first_text(body.get("password"))
    return username_hint, password_hint


def _cookie_preview(
    ok: bool,
    detail: str,
    resolved_username: str,
    resolved_user_id: str,
    banned: bool,
    suspended: bool,
    blocked: bool,
    check_outcome: str,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "username": resolved_username if (ok or blocked or resolved_username) else "",
        "user_id": resolved_user_id,
        "banned": banned,
        "suspended": suspended,
        "locked": check_outcome == "locked",
        "recovery": check_outcome == "recovery",
        "captcha": check_outcome == "captcha",
        "bad_password": check_outcome == "bad_credentials",
        "msg": detail if not ok else "",
    }


def _moderated_locked_payload(
    cookie: str,
    detail: str,
    ok: bool,
    username_hint: str,
    resolved_username: str,
    resolved_user_id: str,
) -> tuple[dict[str, Any] | None, bool]:
    if ok or bool(is_suspended_text(detail)):
        return None, False
    # A bare "User is moderated" (403) fits a verification lock as well
    # as a ban. Resolve it BEFORE the banned label below so the preview
    # shows Locked instead of Banned — same rule as the import path.
    try:
        from services.roblox_identity import moderated_only_detail as _mod_only_t
        from services.roblox_identity import resolve_moderated_dead_cookie as _resolve_mod_t
    except Exception:
        _mod_only_t = lambda *a, **k: False
        _resolve_mod_t = lambda *a, **k: (None, "", "", "")
    try:
        mod_hit = bool(_mod_only_t(detail))
    except Exception:
        mod_hit = False
    if not mod_hit:
        return None, False
    try:
        mv, md, mn, mu = _resolve_mod_t(cookie, username_hint or resolved_username)
    except Exception:
        mv, md, mn, mu = None, "", "", ""
    if mv != "locked":
        return None, True
    return (
        _locked_payload(md, mn, mu, username_hint, resolved_username, resolved_user_id),
        True,
    )


def _cookie_moderation_flags(meta: dict[str, Any], detail: str) -> tuple[bool, bool]:
    suspended = bool(meta.get("is_suspended") or meta.get("suspended")) or bool(
        is_suspended_text(detail)
    )
    banned = (
        bool(meta.get("is_banned") or meta.get("banned"))
        or bool(is_banned_text(detail))
    ) and not suspended
    return banned, suspended


async def _api_test_cookie(request: Request) -> dict[str, Any]:
    body = await request.json()
    cookie = body.get("cookie", "")
    username_hint, password_hint = _cookie_hints(body)
    if not cookie:
        raise HTTPException(400, "cookie required")
    ok, username, detail, meta = validate_cookie_details(cookie)
    meta = meta or {}
    resolved_username, resolved_user_id = _cookie_identity(cookie, ok, username, meta)
    early, moderated_resolved = _moderated_locked_payload(
        cookie, detail, ok, username_hint, resolved_username, resolved_user_id
    )
    if early is not None:
        return early
    banned, suspended = _cookie_moderation_flags(meta, detail)
    blocked = banned or suspended
    check_outcome = ""
    if not ok and not blocked and not moderated_resolved:
        early, resolved_username, resolved_user_id, check_outcome = _resolve_dead_cookie(
            cookie, username_hint, password_hint, resolved_username, resolved_user_id
        )
        if early is not None:
            return early
    return _cookie_preview(
        ok,
        detail,
        resolved_username,
        resolved_user_id,
        banned,
        suspended,
        blocked,
        check_outcome,
    )


def _api_vip_tracker(ctx: ApiContext, username: str) -> dict[str, Any]:
    acc = next((a for a in ctx.farm._accounts if a.username == username), None)
    if not acc:
        raise HTTPException(404, "Account not found")
    if not acc._vip_tracker:
        return {"ok": False, "msg": "No VipTracker (no VIP links)"}
    return {"ok": True, "links": acc._vip_tracker.status()}


def _account_usernames(body: dict[str, Any]) -> list[str]:
    raw_names = body.get("usernames", [])
    if isinstance(raw_names, str):
        raw_names = [raw_names]
    if not isinstance(raw_names, list):
        raise HTTPException(400, "usernames must be a list")
    names = [str(n or "").strip() for n in raw_names if str(n or "").strip()]
    if not names:
        raise HTTPException(400, "usernames required")
    return names


def _finished_updates(username: str, finished: bool) -> dict[str, Any]:
    if finished:
        return {"manual_status": "Finished", "finished_at": time.time()}
    # Only clear a Finished mark; never wipe captcha/invalid holds.
    try:
        current = _find_account_record(username, include_cookie=False) or {}
        was_finished = (
            str(current.get("manual_status") or "").strip().lower() == "finished"
        )
    except Exception:
        was_finished = True
    updates: dict[str, Any] = {"finished_at": 0.0}
    if was_finished:
        updates["manual_status"] = ""
    return updates


def _mark_finished_records(
    names: list[str], finished: bool
) -> tuple[list[str], list[str]]:
    marked: list[str] = []
    missing: list[str] = []
    for username in names:
        updated = ACCOUNT_STORE.update_record(
            username, _finished_updates(username, finished)
        )
        if updated is None:
            missing.append(username)
            continue
        marked.append(username)
        try:
            audit_event(
                "finished_mark" if finished else "finished_clear",
                username=username,
                ok=True,
            )
        except Exception:
            "intentional: ignore exception"
    return marked, missing


def _stop_finished_accounts(farm: Any, marked: list[str]) -> list[str]:
    killed: list[str] = []
    for username in marked:
        try:
            ok, _msg = farm.kill_account_pid(username, reason="account_finished")
            if ok:
                killed.append(username)
        except Exception:
            "intentional: ignore exception"
        try:
            farm.mark_account_finished(farm._find_account(username))
        except Exception:
            continue
    return killed


def _revive_unfinished_accounts(farm: Any, marked: list[str]) -> list[str]:
    relaunched: list[str] = []
    for username in marked:
        try:
            if farm.revive_unfinished_account(username):
                relaunched.append(username)
        except Exception:
            continue
    return relaunched


def _finished_message(
    finished: bool, marked: list, relaunched: list, farm: Any
) -> str:
    if finished:
        return f"Marked Finished: {len(marked)}"
    if getattr(farm, "running", False):
        return f"Cleared Finished, relaunching: {len(relaunched)}/{len(marked)}"
    return f"Cleared Finished: {len(marked)}"


def _log_finished(
    finished: bool, marked: list, killed: list, relaunched: list
) -> None:
    try:
        from core import flog_kv

        flog_kv(
            "API",
            "accounts_finished" if finished else "accounts_unfinished",
            account=",".join(marked) or "*",
            count=len(marked),
            killed=len(killed),
            relaunched=len(relaunched),
        )
    except Exception:
        "intentional: ignore exception"


async def _api_mark_accounts_finished(
    ctx: ApiContext, request: Request
) -> dict[str, Any]:
    farm = ctx.farm
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected object")
    names = _account_usernames(body)
    finished = bool(body.get("finished", True))
    marked, missing = _mark_finished_records(names, finished)
    try:
        _replace_farm_accounts_from_store(ctx)
    except Exception:
        "intentional: ignore exception"
    killed: list[str] = []
    relaunched: list[str] = []
    if finished:
        killed = _stop_finished_accounts(farm, marked)
    elif bool(getattr(farm, "running", False)):
        relaunched = _revive_unfinished_accounts(farm, marked)
    _log_finished(finished, marked, killed, relaunched)
    return {
        "ok": True,
        "finished": finished,
        "marked": marked,
        "missing": missing,
        "killed": killed,
        "relaunched": relaunched,
        "msg": _finished_message(finished, marked, relaunched, farm),
    }


def _find_runtime_account(farm: Any, username: str) -> Any:
    if hasattr(farm, "_find_account"):
        return farm._find_account(username)
    return next(
        (
            a
            for a in farm._accounts
            if str(
                getattr(a, "_config_username", "") or getattr(a, "username", "")
            ).lower()
            == str(username).lower()
        ),
        None,
    )


def _audit_banned_clear(username: str) -> None:
    try:
        audit_event("banned_clear", username=username, ok=True)
    except Exception:
        "intentional: ignore exception"


def _clear_banned_hold(farm: Any, username: str) -> None:
    try:
        acc = _find_runtime_account(farm, username)
        if acc is not None:
            clear_account_banned_hold(
                acc, runtime_writer=getattr(farm, "_runtime_state", None)
            )
    except Exception:
        "intentional: ignore exception"


def _evaluate_unbanned(orchestrator: Any, acc2: Any) -> None:
    try:
        if orchestrator is not None and hasattr(orchestrator, "request_evaluate"):
            orchestrator.request_evaluate(acc2, trigger="manual_unban")
    except Exception:
        "intentional: ignore exception"


def _wake_worker(worker: Any) -> None:
    try:
        if worker is not None and worker.is_alive():
            worker.wake()
    except Exception:
        "intentional: ignore exception"


def _restart_worker_if_dead(
    farm: Any, acc2: Any, restart: Any, workers: dict, orchestrator: Any
) -> None:
    worker = None
    try:
        worker = workers.get(getattr(acc2, "_config_username", ""))
        alive = bool(worker is not None and worker.is_alive())
    except Exception:
        alive = False
    if not alive:
        try:
            restart(farm, acc2)
        except Exception:
            "intentional: ignore exception"
    _evaluate_unbanned(orchestrator, acc2)
    _wake_worker(worker)


def _restart_unbanned_worker(farm: Any, username: str) -> None:
    try:
        if not bool(getattr(farm, "running", False)):
            return
        from services.account_reload import _start_added_worker as restart

        try:
            acc2 = (
                farm._find_account(username) if hasattr(farm, "_find_account") else None
            )
        except Exception:
            acc2 = None
        if acc2 is None:
            return
        workers = getattr(farm, "_workers", None) or {}
        orchestrator = getattr(farm, "_runtime_orchestrator", None)
        _restart_worker_if_dead(farm, acc2, restart, workers, orchestrator)
    except Exception:
        "intentional: ignore exception"


def _restart_unbanned_workers(farm: Any, cleared: list[str]) -> None:
    try:
        if not bool(getattr(farm, "running", False)) or not cleared:
            return
        from services.account_reload import _start_added_worker as restart

        orchestrator = getattr(farm, "_runtime_orchestrator", None)
        workers = getattr(farm, "_workers", None) or {}
        for username in cleared:
            try:
                acc2 = (
                    farm._find_account(username)
                    if hasattr(farm, "_find_account")
                    else None
                )
            except Exception:
                acc2 = None
            if acc2 is None:
                continue
            _restart_worker_if_dead(farm, acc2, restart, workers, orchestrator)
    except Exception:
        "intentional: ignore exception"


def _api_unban_mark(
    ctx: ApiContext, username: str, request: Request
) -> dict[str, Any]:
    idem = begin_idempotent_request_sync(request, "unban_mark", username)
    if idem.replay:
        return idem.response
    record = _find_account_record(username, include_cookie=False)
    if not record:
        raise HTTPException(404, "Account not found")
    if not _is_moderation_mark(record):
        result = {
            "ok": True,
            "cleared": False,
            "msg": f"{username} is not marked Banned or Suspended",
        }
        finish_idempotent_request(idem, result)
        return result
    updated = ACCOUNT_STORE.update_record(
        username, {"manual_status": "", "import_status": ""}
    )
    if updated is None:
        raise HTTPException(404, "Account not found")
    _audit_banned_clear(username)
    _clear_banned_hold(ctx.farm, username)
    try:
        _replace_farm_accounts_from_store(ctx)
    except Exception:
        "intentional: ignore exception"
    # If the farm is running and the worker died on ban, restart it so
    # Unmark rejoins without requiring a full restart.
    _restart_unbanned_worker(ctx.farm, username)
    result = {
        "ok": True,
        "cleared": True,
        "msg": f"Cleared moderation mark: {username}",
    }
    finish_idempotent_request(idem, result)
    return result


def _clear_bulk_marks(
    farm: Any, names: list[str]
) -> tuple[list[str], list[str], list[str]]:
    cleared: list[str] = []
    missing: list[str] = []
    skipped: list[str] = []
    for username in names:
        record = _find_account_record(username, include_cookie=False)
        if not record:
            missing.append(username)
            continue
        if not _is_moderation_mark(record):
            skipped.append(username)
            continue
        updated = ACCOUNT_STORE.update_record(
            username, {"manual_status": "", "import_status": ""}
        )
        if updated is None:
            missing.append(username)
            continue
        cleared.append(username)
        _audit_banned_clear(username)
        _clear_banned_hold(farm, username)
    return cleared, missing, skipped


async def _api_unban_mark_bulk(
    ctx: ApiContext, request: Request
) -> dict[str, Any]:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected object")
    names = _account_usernames(body)
    cleared, missing, skipped = _clear_bulk_marks(ctx.farm, names)
    try:
        _replace_farm_accounts_from_store(ctx)
    except Exception:
        "intentional: ignore exception"
    _restart_unbanned_workers(ctx.farm, cleared)
    return {
        "ok": True,
        "cleared": cleared,
        "missing": missing,
        "skipped": skipped,
        "msg": f"Cleared moderation marks: {len(cleared)}",
    }


def _register_accounts_read(app, ctx: ApiContext) -> None:
    @app.post("/api/account/{username}/save-cookie")
    def api_save_cookie(username: str, request: Request):
        return _api_save_cookie(ctx, username, request)

    @app.get("/api/accounts")
    def api_get_accounts():
        return _api_get_accounts(ctx)

    @app.post("/api/account/{username}/captcha/open-login")
    def api_open_captcha_login(username: str, request: Request):
        return _api_open_captcha_login(ctx, username, request)

    @app.post("/api/accounts/reload")
    def api_reload_accounts(request: Request):
        return _api_reload_accounts(ctx, request)

    @app.get("/api/accounts/avatars")
    def api_account_avatars(user_ids: str = ""):
        return _api_account_avatars(user_ids)

    @app.post("/api/accounts")
    async def api_set_accounts(request: Request):
        return await _api_set_accounts(ctx, request)

    @app.get("/api/accounts/export")
    def api_export_accounts():
        return _api_export_accounts(ctx)

    @app.post("/api/accounts/import")
    async def api_import_accounts(request: Request):
        return await _api_import_accounts(ctx, request)


def _register_account_actions(app, ctx: ApiContext) -> None:
    @app.post("/api/account/{username}/launch")
    async def api_launch_account(username: str, request: Request):
        return await _api_launch_account(ctx, username, request)

    @app.post("/api/account/{username}/kill-duplicate")
    def api_kill_duplicate(username: str, request: Request):
        return _api_kill_duplicate(ctx, username, request)

    @app.post("/api/account/{username}/test-vip")
    async def api_test_vip(username: str, request: Request):
        return await _api_test_vip(username, request)

    @app.get("/api/game/place/{place_id}")
    def api_game_place(place_id: str):
        return _api_game_place(place_id)


def _register_cookie_tools(app, ctx: ApiContext) -> None:
    @app.post("/api/test-cookie")
    async def api_test_cookie(request: Request):
        return await _api_test_cookie(request)

    @app.get("/api/vip-tracker/{username}")
    def api_vip_tracker(username: str):
        return _api_vip_tracker(ctx, username)


def _register_finish_unban(app, ctx: ApiContext) -> None:
    @app.post("/api/accounts/finish")
    async def api_mark_accounts_finished(request: Request):
        return await _api_mark_accounts_finished(ctx, request)

    @app.post("/api/account/{username}/unban-mark")
    def api_unban_mark(username: str, request: Request):
        return _api_unban_mark(ctx, username, request)

    @app.post("/api/accounts/unban-mark")
    async def api_unban_mark_bulk(request: Request):
        return await _api_unban_mark_bulk(ctx, request)


def register(app, ctx: ApiContext) -> None:
    _register_accounts_read(app, ctx)
    _register_account_actions(app, ctx)
    _register_cookie_tools(app, ctx)
    _register_finish_unban(app, ctx)
