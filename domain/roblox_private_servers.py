from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from domain._util import _safe_hash
from domain._util import clean_text as _clean_text
from domain._util import identity_matches as _owner_matches
from domain._util import record_text as _record_field
from infra import net_tls

USER_AGENT = "CronusLauncherHybrid/1.0"
ROBLOX_HOME = "https://www.roblox.com/"
GAMES_BASE = "https://games.roblox.com/"
APIS_BASE = "https://apis.roblox.com/"

PRIVATE_GAME_RE = re.compile(
    r"Roblox\.GameLauncher\.joinPrivateGame\(\s*(\d+)\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*\)",
    flags=re.IGNORECASE,
)
GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", flags=re.IGNORECASE)


def parse_vip_link(value: str) -> tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        return "", ""
    parsed = urllib.parse.urlparse(text)
    qs = urllib.parse.parse_qs(parsed.query)
    match = re.search(r"/games/(\d+)", parsed.path)
    place_id = match.group(1) if match else qs.get("placeId", [""])[0]
    link_code = (
        qs.get("privateServerLinkCode", [""])[0]
        or qs.get("linkCode", [""])[0]
        or qs.get("accessCode", [""])[0]
        or qs.get("code", [""])[0]
    )
    return str(place_id or ""), str(link_code or "")


def vip_links_for_place(vip_links: list[str], place_id: str) -> list[str]:
    """Single owner for the per-place VIP filter.

    Four launch-target builders used to repeat this comprehension inline
    (parsing every link twice); the launch decision now leverages this one.
    Links without a place keep passing through; only a link naming a
    *different* place is dropped. Total: never raises.
    """
    place = str(place_id or "").strip()
    if not place:
        return list(vip_links or [])
    kept: list[str] = []
    for link in vip_links or []:
        text = str(link or "").strip()
        try:
            link_place, _ = parse_vip_link(text)
        except Exception:
            link_place = ""
        if not link_place or link_place == place:
            kept.append(link)
    return kept


def _vip_component(comp: dict[str, str] | None, key: str) -> str:
    return str((comp or {}).get(key) or "").strip()


def shared_direct_vip_link(candidate: str, place_id: str) -> str:
    """Single owner for the shared-direct VIP rule.

    Shared mode with a complete shared link (place + link/access code)
    joins that exact room for every account. Returns the candidate when it
    qualifies, else "". Total: never raises.
    """
    text = str(candidate or "").strip()
    if not text:
        return ""
    try:
        comp = parse_vip_components(text)
    except Exception:
        return ""
    comp_place = _vip_component(comp, "place_id")
    code = _vip_component(comp, "link_code") or _vip_component(
        comp, "access_code"
    )
    place = str(place_id or "").strip()
    if not comp_place or not code:
        return ""
    if place and comp_place != place:
        return ""
    return text


def _first_query_value(qs: dict[str, list[str]], *keys: str) -> str:
    for key in keys:
        values = qs.get(key)
        if values:
            return str(values[0] or "")
    return ""


def _vip_query_parts(text: str) -> tuple[str, str, str]:
    parsed = urllib.parse.urlparse(text)
    qs = urllib.parse.parse_qs(parsed.query)
    match = re.search(r"/games/(\d+)", parsed.path)
    place_id = match.group(1) if match else _first_query_value(qs, "placeId")
    link_code = _first_query_value(
        qs, "privateServerLinkCode", "linkCode", "code"
    )
    access_code = _first_query_value(qs, "accessCode")
    return str(place_id or ""), link_code, access_code


def parse_vip_components(value: str) -> dict[str, str]:
    text = str(value or "").strip()
    if not text:
        return {"place_id": "", "link_code": "", "access_code": ""}
    place_id, link_code, access_code = _vip_query_parts(text)
    if not link_code and access_code and not GUID_RE.match(access_code):
        link_code = access_code
        access_code = ""
    return {
        "place_id": str(place_id or ""),
        "link_code": str(link_code or ""),
        "access_code": str(access_code or ""),
    }


def parse_vip_access_code_html(html: str) -> dict[str, str]:
    match = PRIVATE_GAME_RE.search(str(html or ""))
    if not match:
        return {"ok": False, "place_id": "", "access_code": "", "link_code": "", "msg": "joinPrivateGame marker not found"}
    return {
        "ok": True,
        "place_id": match.group(1),
        "access_code": match.group(2),
        "link_code": match.group(3),
        "msg": "ok",
    }


def _vip_invite_request(
    cookie: str, place_id: str, link_code: str, timeout: float
) -> tuple[str, int, str]:
    url = (
        f"{ROBLOX_HOME}games/{urllib.parse.quote(place_id)}"
        f"?privateServerLinkCode={urllib.parse.quote(link_code)}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 CronusLauncherHybrid/1.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": f"{ROBLOX_HOME}games/{urllib.parse.quote(place_id)}",
            "Cookie": f".ROBLOSECURITY={cookie}",
        },
    )
    try:
        with net_tls.urlopen(req, timeout=timeout) as resp:
            return (
                resp.read().decode("utf-8", errors="replace"),
                int(resp.status),
                "",
            )
    except urllib.error.HTTPError as exc:
        return "", 0, f"VIP invite resolve failed ({exc.code})"
    except Exception as exc:
        return "", 0, f"VIP invite resolve failed: {exc}"


def _invite_place_matches(parsed: dict[str, str], place_id: str) -> bool:
    return str(parsed.get("place_id") or "") == place_id


def resolve_vip_access_code(cookie: str, vip_link: str, timeout: float = 12.0) -> dict[str, Any]:
    parts = parse_vip_components(vip_link)
    place_id = parts.get("place_id", "")
    link_code = parts.get("link_code", "")
    access_code = parts.get("access_code", "")
    if access_code and GUID_RE.match(access_code):
        return {
            "ok": True,
            "place_id": place_id,
            "access_code": access_code,
            "link_code": link_code or access_code,
            "source": "query",
        }
    if not place_id or not link_code:
        return {
            "ok": False,
            "msg": "VIP link must contain placeId and privateServerLinkCode",
            "place_id": place_id,
            "link_code": link_code,
        }
    body, status, error = _vip_invite_request(
        cookie, place_id, link_code, timeout
    )
    if error:
        return {
            "ok": False,
            "msg": error,
            "place_id": place_id,
            "link_code": link_code,
        }
    parsed = parse_vip_access_code_html(body)
    if not parsed.get("ok"):
        return {
            "ok": False,
            "msg": f"VIP invite did not expose accessCode ({status})",
            "place_id": place_id,
            "link_code": link_code,
        }
    if not _invite_place_matches(parsed, place_id):
        return {
            "ok": False,
            "msg": "VIP invite placeId mismatch",
            "place_id": place_id,
            "link_code": link_code,
        }
    return {
        "ok": True,
        "place_id": place_id,
        "access_code": str(parsed.get("access_code") or ""),
        "link_code": link_code,
        "source": "invite_page",
    }


def _json_body(body: str) -> dict[str, Any]:
    try:
        parsed = json.loads(body or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {"raw": str(body or "")}


def _api_error_message(payload: dict[str, Any], fallback: str) -> str:
    try:
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            message = str(errors[0].get("message") or errors[0].get("userFacingMessage") or "").strip()
            if message:
                return message
    except Exception:
        "intentional: ignore exception"
    message = str(payload.get("message") or payload.get("error") or "").strip()
    return message or fallback


def universe_id_for_place(place_id: str, timeout: float = 8.0) -> tuple[bool, str, str]:
    place = str(place_id or "").strip()
    if not place.isdigit():
        return False, "", "Place ID is required before creating a private server"
    url = APIS_BASE + f"universes/v1/places/{urllib.parse.quote(place, safe='')}/universe"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"})
    try:
        with net_tls.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return False, "", f"Place to universe lookup failed ({exc.code})"
    except Exception as exc:
        return False, "", f"Place to universe lookup failed: {exc}"
    universe_id = str(payload.get("universeId") or "").strip() if isinstance(payload, dict) else ""
    if not universe_id:
        return False, "", "Roblox did not return a universeId for this Place ID"
    return True, universe_id, "ok"


def game_name_for_universe(client: Any, universe_id: str) -> tuple[bool, str, str]:
    uid = str(universe_id or "").strip()
    if not uid:
        return False, "", "missing universe id"
    url = GAMES_BASE + "v1/games?" + urllib.parse.urlencode({"universeIds": uid})
    status, body, _headers = client.request(url, method="GET")
    payload = _json_body(body)
    if not 200 <= status < 300:
        return False, "", _api_error_message(payload, f"Game name lookup failed ({status})")
    data = payload.get("data")
    if isinstance(data, list) and data:
        first = data[0] if isinstance(data[0], dict) else {}
        name = str(first.get("name") or first.get("sourceName") or "").strip()
        if name:
            return True, name, "ok"
    return False, "", "Roblox did not return a game name"


def _server_id(server: dict[str, Any]) -> str:
    return str(server.get("private_server_id") or server.get("privateServerId") or server.get("vipServerId") or server.get("id") or "").strip()


def _server_owner_id(server: dict[str, Any]) -> str:
    owner = server.get("owner")
    if isinstance(owner, dict):
        value = owner.get("id") or owner.get("userId")
        if value not in (None, ""):
            return str(value).strip()
    return str(server.get("owner_user_id") or server.get("ownerId") or server.get("ownerUserId") or "").strip()


def _server_place_id(server: dict[str, Any]) -> str:
    game = server.get("game")
    if isinstance(game, dict):
        value = game.get("placeId") or game.get("rootPlaceId")
        if value not in (None, ""):
            return str(value).strip()
    return str(server.get("place_id") or server.get("placeId") or "").strip()


def _server_universe_id(server: dict[str, Any]) -> str:
    game = server.get("game")
    if isinstance(game, dict):
        value = game.get("universeId")
        if value not in (None, ""):
            return str(value).strip()
    return str(server.get("universe_id") or server.get("universeId") or "").strip()


def _server_join_code(server: dict[str, Any]) -> str:
    link = server.get("link")
    if isinstance(link, dict):
        value = link.get("code") or link.get("joinCode") or link.get("privateServerLinkCode")
        if value not in (None, ""):
            return str(value).strip()
    return str(
        server.get("join_code")
        or server.get("joinCode")
        or server.get("privateServerLinkCode")
        or server.get("linkCode")
        or ""
    ).strip()


def _server_access_code(server: dict[str, Any]) -> str:
    return str(server.get("access_code") or server.get("accessCode") or "").strip()


def _server_link_value(server: dict[str, Any]) -> str:
    raw_link = (
        server.get("link")
        or server.get("join_link")
        or server.get("joinLink")
        or ""
    )
    if isinstance(raw_link, dict):
        raw_link = (
            raw_link.get("url")
            or raw_link.get("href")
            or raw_link.get("link")
            or ""
        )
    return str(raw_link or "").strip()


def _server_place(place_id: str, server: dict[str, Any]) -> str:
    return str(place_id or _server_place_id(server) or "").strip()


def _vip_link_probe(link: str) -> tuple[bool, str]:
    parts = parse_vip_components(link)
    place = str(parts.get("place_id") or "")
    link_code = str(parts.get("link_code") or "")
    access_code = str(parts.get("access_code") or "")
    complete = bool(place and (link_code or access_code))
    return complete, link_code.strip()


def _vip_place_url(place: str, key: str, value: str) -> str:
    return (
        f"{ROBLOX_HOME}games/{urllib.parse.quote(place, safe='')}"
        f"/?{key}={urllib.parse.quote(value, safe='')}"
    )


def build_owned_private_server_link(place_id: str, server: dict[str, Any]) -> str:
    link = _server_link_value(server)
    raw_link_code = ""
    if link:
        complete, raw_link_code = _vip_link_probe(link)
        if complete:
            return link
    place = _server_place(place_id, server)
    join_code = _server_join_code(server) or raw_link_code
    if place and join_code:
        return _vip_place_url(place, "privateServerLinkCode", join_code)
    access_code = _server_access_code(server)
    if place and access_code:
        return _vip_place_url(place, "accessCode", access_code)
    if link:
        return link
    return ""


def _owner_conflicts(owner_user_id: str, owner: str) -> bool:
    return bool(owner_user_id and owner and owner != str(owner_user_id))


def _id_matches(expected: str, actual: str) -> bool:
    return not expected or not actual or actual == str(expected)


def _private_server_matches_owner(
    server: dict[str, Any],
    owner_user_id: str,
    place_id: str,
    universe_id: str,
) -> bool:
    if server.get("active") is False:
        return False
    if _owner_conflicts(owner_user_id, _server_owner_id(server)):
        return False
    if not _id_matches(place_id, _server_place_id(server)):
        return False
    if not _id_matches(universe_id, _server_universe_id(server)):
        return False
    return bool(_server_id(server) or build_owned_private_server_link(place_id, server))


def _server_page_params(limit: int) -> dict[str, str]:
    return {"limit": str(max(10, min(int(limit or 100), 100)))}


def _server_page_data(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    data = payload.get("data")
    if isinstance(data, list):
        servers = [item for item in data if isinstance(item, dict)]
    else:
        servers = []
    cursor = str(payload.get("nextPageCursor") or "").strip()
    return servers, cursor


def list_my_private_servers(client: Any, limit: int = 100, max_pages: int = 5) -> tuple[bool, list[dict[str, Any]], str]:
    servers: list[dict[str, Any]] = []
    cursor = ""
    for _page in range(max(1, int(max_pages or 1))):
        params = _server_page_params(limit)
        if cursor:
            params["cursor"] = cursor
        url = GAMES_BASE + "v1/private-servers/my-private-servers?" + urllib.parse.urlencode(params)
        status, body, _headers = client.request(url, method="GET")
        payload = _json_body(body)
        if not 200 <= status < 300:
            return False, servers, _api_error_message(payload, f"Private server list failed ({status})")
        page, cursor = _server_page_data(payload)
        servers.extend(page)
        if not cursor:
            break
    return True, servers, "ok"


def list_private_servers_for_place(
    client: Any,
    place_id: str,
    limit: int = 100,
    max_pages: int = 5,
) -> tuple[bool, list[dict[str, Any]], str]:
    place = str(place_id or "").strip()
    if not place:
        return False, [], "missing place id"
    servers: list[dict[str, Any]] = []
    cursor = ""
    for _page in range(max(1, int(max_pages or 1))):
        params = {"sortOrder": "Asc", **_server_page_params(limit)}
        if cursor:
            params["cursor"] = cursor
        url = GAMES_BASE + f"v1/games/{urllib.parse.quote(place, safe='')}/private-servers?" + urllib.parse.urlencode(params)
        status, body, _headers = client.request(url, method="GET")
        payload = _json_body(body)
        if not 200 <= status < 300:
            return False, servers, _api_error_message(payload, f"Private server place list failed ({status})")
        page, cursor = _server_page_data(payload)
        servers.extend(page)
        if not cursor:
            break
    return True, servers, "ok"


def fetch_private_server_metadata(client: Any, private_server_id: str) -> tuple[bool, dict[str, Any], str]:
    sid = str(private_server_id or "").strip()
    if not sid:
        return False, {}, "missing private server id"
    status, body, _headers = client.request(GAMES_BASE + f"v1/vip-servers/{urllib.parse.quote(sid, safe='')}", method="GET")
    payload = _json_body(body)
    if 200 <= status < 300:
        return True, payload, "ok"
    return False, payload, _api_error_message(payload, f"Private server metadata failed ({status})")


def private_servers_enabled_for_universe(client: Any, universe_id: str) -> tuple[bool, bool, str]:
    uid = str(universe_id or "").strip()
    if not uid:
        return False, False, "missing universe id"
    status, body, _headers = client.request(
        GAMES_BASE + f"v1/private-servers/enabled-in-universe/{urllib.parse.quote(uid, safe='')}",
        method="GET",
    )
    payload = _json_body(body)
    if 200 <= status < 300:
        return True, bool(payload.get("privateServersEnabled", False)), "ok"
    return False, False, _api_error_message(payload, f"Private server enabled check failed ({status})")


def _private_server_record(
    server: dict[str, Any],
    place_id: str,
    universe_id: str,
    owner_user_id: str,
    source: str,
    status: str = "ok",
    error: str = "",
) -> dict[str, Any]:
    place = str(place_id or _server_place_id(server) or "").strip()
    link = build_owned_private_server_link(place, server)
    return {
        "private_server_id": _server_id(server),
        "owner_user_id": _server_owner_id(server) or str(owner_user_id or ""),
        "place_id": place,
        "universe_id": str(universe_id or _server_universe_id(server) or "").strip(),
        "name": str(server.get("name") or "").strip(),
        "active": bool(server.get("active", True)),
        "link": link,
        "join_code": _server_join_code(server),
        "access_code": _server_access_code(server),
        "source": source,
        "status": status,
        "error": error,
        "synced_at": time.time(),
    }


_ERROR_FALLBACK_KEYS = ("private_server_id", "link", "join_code", "access_code", "source", "name")


def _fill_missing_server_fields(combined: dict[str, Any], old: dict[str, Any]) -> None:
    """Keep old values when an errored refresh blanks a field."""
    for key in _ERROR_FALLBACK_KEYS:
        if combined.get(key) in (None, "") and old.get(key) not in (None, ""):
            combined[key] = old.get(key)


def _owned_server_identity(server: dict[str, Any]) -> tuple[str, tuple[str, str, str]]:
    return (
        str(server.get("private_server_id") or "").strip(),
        (
            str(server.get("owner_user_id") or ""),
            str(server.get("place_id") or ""),
            str(server.get("universe_id") or ""),
        ),
    )


def _same_owned_server(
    old: dict[str, Any],
    incoming_id: str,
    incoming_key: tuple[str, str, str],
) -> bool:
    old_id, old_key = _owned_server_identity(old)
    return bool(
        (incoming_id and old_id == incoming_id)
        or (incoming_key[0] and old_key == incoming_key)
    )


def _merge_owned_server_record(
    old: dict[str, Any],
    server: dict[str, Any],
) -> dict[str, Any]:
    combined = dict(old)
    combined.update(dict(server))
    if str(server.get("status") or "").lower() == "error":
        _fill_missing_server_fields(combined, old)
    return combined


def merge_owned_private_server(
    records: list[dict[str, Any]],
    server: dict[str, Any],
) -> list[dict[str, Any]]:
    """Merge a server record into an ``owned_private_servers`` list.

    A record is the same server when the private-server id matches, or when
    the owner/place/universe identity matches. Error refreshes keep the old
    join fields so a transient failure cannot erase a working link.
    """
    incoming_id, incoming_key = _owned_server_identity(server)
    merged: list[dict[str, Any]] = []
    replaced = False
    for old in records or []:
        if not isinstance(old, dict):
            continue
        if _same_owned_server(old, incoming_id, incoming_key):
            merged.append(_merge_owned_server_record(old, server))
            replaced = True
        else:
            merged.append(dict(old))
    if not replaced:
        merged.append(dict(server))
    return merged


def _private_server_identity_key(server: dict[str, Any], owner_user_id: str, place_id: str, universe_id: str) -> tuple[str, str, str]:
    return (
        _server_owner_id(server) or str(owner_user_id or ""),
        _server_place_id(server) or str(place_id or ""),
        _server_universe_id(server) or str(universe_id or ""),
    )


def _merge_private_server_secrets(candidate: dict[str, Any], known: dict[str, Any]) -> None:
    if not known:
        return
    for src_key, dst_key in (
        ("link", "link"),
        ("join_link", "join_link"),
        ("joinLink", "joinLink"),
        ("join_code", "join_code"),
        ("joinCode", "joinCode"),
        ("privateServerLinkCode", "privateServerLinkCode"),
        ("linkCode", "linkCode"),
        ("access_code", "access_code"),
        ("accessCode", "accessCode"),
    ):
        if candidate.get(dst_key) in (None, "") and known.get(src_key) not in (None, ""):
            candidate[dst_key] = known.get(src_key)


def _expected_server_key(
    owner_user_id: str, place_id: str, universe_id: str
) -> tuple[str, str, str]:
    return (
        str(owner_user_id or ""),
        str(place_id or ""),
        str(universe_id or ""),
    )


def _server_key_matches(
    item: dict[str, Any],
    expected: tuple[str, str, str],
    owner_user_id: str,
    place_id: str,
    universe_id: str,
) -> bool:
    item_key = _private_server_identity_key(
        item, owner_user_id, place_id, universe_id
    )
    return bool(expected[0] and item_key == expected)


def _server_has_join_data(item: dict[str, Any], place_id: str) -> bool:
    return bool(
        build_owned_private_server_link(place_id, item)
        or _server_access_code(item)
        or _server_join_code(item)
    )


def _server_matches_known(
    item: dict[str, Any],
    expected: tuple[str, str, str],
    sid: str,
    owner_user_id: str,
    place_id: str,
    universe_id: str,
) -> bool:
    if sid and _server_id(item) == sid:
        return True
    return _server_key_matches(
        item, expected, owner_user_id, place_id, universe_id
    )


def _server_matches(
    item: dict[str, Any], sid: str, owner_user_id: str
) -> bool:
    item_id = _server_id(item)
    if sid and item_id and item_id == sid:
        return True
    return _owner_matches(owner_user_id, _server_owner_id(item))


def _known_private_server_for(
    records: list[dict[str, Any]],
    private_server_id: str,
    owner_user_id: str,
    place_id: str,
    universe_id: str,
) -> dict[str, Any]:
    expected_key = _expected_server_key(owner_user_id, place_id, universe_id)
    sid = _clean_text(private_server_id)
    fallback: dict[str, Any] = {}
    for item in records or []:
        if not isinstance(item, dict):
            continue
        if not _server_matches_known(
            item, expected_key, sid, owner_user_id, place_id, universe_id
        ):
            continue
        if _server_has_join_data(item, place_id):
            return item
        if not fallback:
            fallback = item
    return fallback


def _place_private_server_for(
    servers: list[dict[str, Any]],
    private_server_id: str,
    owner_user_id: str,
) -> dict[str, Any]:
    sid = _clean_text(private_server_id)
    owner = str(owner_user_id or "").strip()
    fallback: dict[str, Any] = {}
    for item in servers or []:
        if not isinstance(item, dict):
            continue
        if not _server_matches(item, sid, owner):
            continue
        if _server_has_join_data(item, ""):
            return item
        if not fallback:
            fallback = item
    return fallback


def classify_private_server_failure(message: str) -> str:
    """Bucket a private-server failure message into a reason code.

    Shared by the START preflight and the per-launch fallback so both agree
    on what "this game simply has no private servers" means.
    """
    text = str(message or "").lower()
    if "disabled for this universe" in text or "private servers are disabled" in text:
        return "vip_disabled"
    if "free-only" in text or "expectedprice" in text:
        return "vip_paid"
    if "cookie" in text or "no .rob housesecurity" in text:
        return "no_cookie"
    return "vip_error"


def vip_unavailable_permanently(message: str) -> bool:
    """True when the game itself cannot offer a private server.

    Roblox will keep refusing forever, so retrying (or failing the launch)
    is pointless: the account is better off joining a public server.
    Transient problems (network, cookie) are NOT permanent.
    """
    return classify_private_server_failure(message) in {"vip_disabled", "vip_paid"}


# How long a remembered "this game has no private servers" verdict is
# trusted before asking Roblox again. Long enough that a rejoining farm
# does not re-probe on every single launch, short enough that enabling
# VIP servers on the game later still takes effect.
VIP_UNAVAILABLE_TTL_SECONDS = 6 * 60 * 60


def _is_unavailable_record(item: dict[str, Any], place: str) -> bool:
    if _record_field(item, "place_id") != place:
        return False
    if _record_field(item, "status").lower() != "error":
        return False
    return vip_unavailable_permanently(_record_field(item, "error"))


def _record_fresh(
    item: dict[str, Any], current: float, ttl_seconds: float
) -> bool:
    try:
        synced_at = float(item.get("synced_at") or 0.0)
    except Exception:
        synced_at = 0.0
    return bool(
        synced_at and (current - synced_at) <= float(ttl_seconds or 0.0)
    )


def remembered_vip_unavailable(
    records: list[dict[str, Any]] | None,
    place_id: Any,
    now: float | None = None,
    ttl_seconds: float = VIP_UNAVAILABLE_TTL_SECONDS,
) -> str:
    """Return a cached permanent-failure reason for this place, or "".

    Without this, every relaunch of an account on a VIP-less game would
    spend three API calls (place->universe, my-private-servers,
    enabled-in-universe) rediscovering the exact same permanent fact.
    """
    place = str(place_id or "").strip()
    if not place:
        return ""
    current = float(now if now is not None else time.time())
    for item in records or []:
        if not isinstance(item, dict):
            continue
        if not _is_unavailable_record(item, place):
            continue
        if not _record_fresh(item, current, ttl_seconds):
            continue
        return _record_field(item, "error")
    return ""


def _render_private_server_name(game_name: str, place_id: str = "") -> str:
    name = str(game_name or "").strip() or (f"Place {place_id}" if place_id else "Private Server")
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    return name[:50] or "Private Server"


def _join_data_present(record: dict[str, Any]) -> bool:
    return bool(
        record.get("link")
        or record.get("access_code")
        or record.get("join_code")
    )


def _matches_owner(
    server: dict[str, Any],
    owner_user_id: str,
    place: str,
    universe_id: str,
) -> bool:
    return _private_server_matches_owner(
        server, str(owner_user_id or ""), place, universe_id
    )


def _enrich_server_metadata(
    client: Any, candidate: dict[str, Any], sid: str, fetch_meta: Any
) -> None:
    if not sid:
        return
    meta_ok, meta, _meta_msg = fetch_meta(client, sid)
    if not meta_ok:
        return
    candidate.update(meta)
    if "privateServerId" not in candidate:
        candidate["privateServerId"] = sid


def _enrich_created_server(
    client: Any, candidate: dict[str, Any], sid: str, fetch_meta: Any
) -> None:
    if not sid:
        return
    meta_ok, meta, _meta_msg = fetch_meta(client, sid)
    if not meta_ok:
        return
    # Preserve accessCode from the create response; GET responses often omit it.
    access_code = _server_access_code(candidate)
    candidate.update(meta)
    if access_code and not _server_access_code(candidate):
        candidate["accessCode"] = access_code
    if "privateServerId" not in candidate:
        candidate["privateServerId"] = sid


def _existing_server_candidate(
    client: Any,
    server: dict[str, Any],
    place: str,
    universe_id: str,
    owner_user_id: str,
    known_servers: list[dict[str, Any]] | None,
    fetch_meta: Any,
) -> dict[str, Any]:
    candidate = dict(server)
    sid = _server_id(candidate)
    _enrich_server_metadata(client, candidate, sid, fetch_meta)
    known = _known_private_server_for(
        list(known_servers or []), sid, owner_user_id, place, universe_id
    )
    _merge_private_server_secrets(candidate, known)
    return candidate


def _load_place_servers(
    client: Any, place: str, list_place_fn: Any
) -> tuple[list[dict[str, Any]], str]:
    place_ok, place_servers, place_msg = list_place_fn(client, place)
    if not place_ok:
        return [], place_msg
    return place_servers, place_msg


def _apply_place_server_link(
    candidate: dict[str, Any],
    playable: dict[str, Any],
    place: str,
    universe_id: str,
    owner_user_id: str,
    sid: str,
) -> None:
    candidate.update(playable)
    if sid and "privateServerId" not in candidate:
        candidate["privateServerId"] = sid
    if "placeId" not in candidate:
        candidate["placeId"] = place
    if "universeId" not in candidate:
        candidate["universeId"] = universe_id
    if "ownerId" not in candidate and owner_user_id:
        candidate["ownerId"] = str(owner_user_id)


def _existing_owned_server_result(
    client: Any,
    servers: list[dict[str, Any]],
    owner_user_id: str,
    place: str,
    universe_id: str,
    known_servers: list[dict[str, Any]] | None,
    fetch_meta: Any,
    list_place_fn: Any,
) -> dict[str, Any] | None:
    place_servers_loaded = False
    place_servers: list[dict[str, Any]] = []
    place_servers_msg = ""
    for server in servers:
        if not _matches_owner(server, owner_user_id, place, universe_id):
            continue
        candidate = _existing_server_candidate(
            client,
            server,
            place,
            universe_id,
            owner_user_id,
            known_servers,
            fetch_meta,
        )
        sid = _server_id(candidate)
        if not _server_has_join_data(candidate, place):
            if not place_servers_loaded:
                place_servers_loaded = True
                place_servers, place_servers_msg = _load_place_servers(
                    client, place, list_place_fn
                )
            playable = _place_private_server_for(
                place_servers, sid, owner_user_id
            )
            if playable:
                _apply_place_server_link(
                    candidate,
                    playable,
                    place,
                    universe_id,
                    owner_user_id,
                    sid,
                )
        record = _private_server_record(
            candidate, place, universe_id, owner_user_id, source="existing"
        )
        if _join_data_present(record):
            return {"ok": True, "source": "existing", **record}
        detail = "Owned private server exists, but Roblox did not return a join link or access code"
        if place_servers_loaded and place_servers_msg and not place_servers:
            detail = f"{detail}; {place_servers_msg}"
        return {
            "ok": False,
            "fatal": True,
            "msg": detail,
            "place_id": place,
            "universe_id": universe_id,
            "private_server_id": sid,
        }
    return None


def _create_private_server(
    client: Any,
    universe_id: str,
    place: str,
    game_name: str,
    free_only: bool,
) -> tuple[bool, dict[str, Any], str]:
    payload = {
        "name": _render_private_server_name(game_name, place_id=place),
        "expectedPrice": 0,
        "isPurchaseConfirmed": True,
    }
    create_ok, created, create_msg, _headers = client.csrf_post(
        GAMES_BASE + f"v1/games/vip-servers/{urllib.parse.quote(universe_id, safe='')}",
        payload,
    )
    if create_ok:
        return True, created, ""
    reason = create_msg or "Private server creation failed"
    if free_only:
        reason += " (free-only expectedPrice=0)"
    return False, {}, reason


def _created_server_result(
    client: Any,
    created: dict[str, Any],
    place: str,
    universe_id: str,
    owner_user_id: str,
    fetch_meta: Any,
) -> dict[str, Any]:
    candidate = dict(created)
    sid = _server_id(candidate)
    _enrich_created_server(client, candidate, sid, fetch_meta)
    if "placeId" not in candidate:
        candidate["placeId"] = place
    if "universeId" not in candidate:
        candidate["universeId"] = universe_id
    if "ownerId" not in candidate and owner_user_id:
        candidate["ownerId"] = str(owner_user_id)
    record = _private_server_record(
        candidate, place, universe_id, owner_user_id, source="created"
    )
    if not _join_data_present(record):
        return {
            "ok": False,
            "fatal": True,
            "msg": "Private server was created, but Roblox did not return a join link or access code",
            **record,
        }
    return {"ok": True, "source": "created", **record}


def ensure_owned_private_server(
    client: Any,
    owner_user_id: str,
    place_id: str,
    free_only: bool = True,
    known_servers: list[dict[str, Any]] | None = None,
    *,
    universe_lookup=universe_id_for_place,
    list_my_private_servers_fn=list_my_private_servers,
    list_private_servers_for_place_fn=list_private_servers_for_place,
    fetch_private_server_metadata_fn=fetch_private_server_metadata,
    private_servers_enabled_for_universe_fn=private_servers_enabled_for_universe,
    game_name_for_universe_fn=game_name_for_universe,
) -> dict[str, Any]:
    place = str(place_id or "").strip()
    ok, universe_id, detail = universe_lookup(place)
    if not ok:
        return {"ok": False, "msg": detail, "place_id": place}

    list_ok, servers, list_msg = list_my_private_servers_fn(client)
    if not list_ok:
        return {
            "ok": False,
            "msg": list_msg,
            "place_id": place,
            "universe_id": universe_id,
        }

    existing = _existing_owned_server_result(
        client,
        servers,
        owner_user_id,
        place,
        universe_id,
        known_servers,
        fetch_private_server_metadata_fn,
        list_private_servers_for_place_fn,
    )
    if existing is not None:
        return existing

    enabled_ok, enabled, enabled_msg = private_servers_enabled_for_universe_fn(
        client, universe_id
    )
    if not enabled_ok:
        return {
            "ok": False,
            "msg": enabled_msg,
            "place_id": place,
            "universe_id": universe_id,
        }
    if not enabled:
        return {
            "ok": False,
            "msg": "Private servers are disabled for this universe",
            "place_id": place,
            "universe_id": universe_id,
        }

    name_ok, game_name, _name_msg = game_name_for_universe_fn(
        client, universe_id
    )
    if not name_ok:
        game_name = f"Place {place}"
    create_ok, created, create_msg = _create_private_server(
        client, universe_id, place, game_name, free_only
    )
    if not create_ok:
        return {
            "ok": False,
            "msg": create_msg,
            "place_id": place,
            "universe_id": universe_id,
        }
    return _created_server_result(
        client,
        created,
        place,
        universe_id,
        owner_user_id,
        fetch_private_server_metadata_fn,
    )



def _query_value(qs: dict[str, list[str]], key: str) -> str:
    return str((qs.get(key) or [""])[0] or "")


def parse_launch_destination_from_cmdline(cmdline: str) -> dict[str, Any]:
    text = str(cmdline or "")
    launcher = ""
    match = re.search(r"placelauncherurl:([^+\s]+)", text, flags=re.IGNORECASE)
    if match:
        launcher = urllib.parse.unquote(match.group(1))
    else:
        match = re.search(r"\-j\s+\"([^\"]+)\"", text, flags=re.IGNORECASE)
        if match:
            launcher = match.group(1)
    if not launcher:
        return {}
    parsed = urllib.parse.urlparse(launcher)
    qs = urllib.parse.parse_qs(parsed.query)
    request_name = _query_value(qs, "request")
    place_id = _query_value(qs, "placeId")
    link_code = _query_value(qs, "linkCode")
    access_code = _query_value(qs, "accessCode")
    job_id = _query_value(qs, "gameId")
    server_type = (
        "VIP"
        if request_name.lower() == "requestprivategame"
        else ("JOB" if request_name.lower() == "requestgamejob" else "PUBLIC")
    )
    return {
        "observed_place_id": place_id,
        "observed_server_type": server_type,
        "observed_private_link_code_hash": _safe_hash(link_code),
        "observed_access_code_hash": _safe_hash(access_code),
        "observed_job_id_hash": _safe_hash(job_id),
        "request": request_name,
        "evidence_source": "process_cmdline",
    }


def _private_launcher_result(
    source: str,
    place_id: str,
    vip_access_code: str,
    vip_link_code: str,
) -> tuple[tuple[str, str, str] | None, str]:
    parsed_place, link_code = parse_vip_link(source)
    place = place_id or parsed_place
    access_code = str(vip_access_code or link_code or "").strip()
    launch_link_code = str(vip_link_code or link_code or "").strip()
    if not (access_code and launch_link_code and place):
        return None, place
    return (
        (
            "https://assetgame.roblox.com/game/PlaceLauncher.ashx?"
            f"request=RequestPrivateGame&placeId={place}&accessCode={access_code}&linkCode={launch_link_code}",
            "vip",
            source,
        ),
        place,
    )


def build_place_launcher_url(
    place_id: str,
    job_id: str = "",
    vip_link: str = "",
    follow_user_id: str = "",
    browser_tracker_id: str = "",
    vip_access_code: str = "",
    vip_link_code: str = "",
) -> tuple[str, str, str]:
    place_id = _clean_text(place_id)
    job_id = _clean_text(job_id)
    vip_link = _clean_text(vip_link)
    follow_user_id = _clean_text(follow_user_id)
    browser_tracker_id = _clean_text(browser_tracker_id)
    if follow_user_id:
        return (
            f"https://assetgame.roblox.com/game/PlaceLauncher.ashx?request=RequestFollowUser&userId={follow_user_id}",
            "follow",
            "",
        )
    if vip_link:
        result, place_id = _private_launcher_result(
            vip_link, place_id, vip_access_code, vip_link_code
        )
        if result is not None:
            return result
    if job_id.startswith("http"):
        result, place_id = _private_launcher_result(
            job_id, place_id, vip_access_code, vip_link_code
        )
        if result is not None:
            return result
    request_name = "RequestGameJob" if job_id else "RequestGame"
    params = f"request={request_name}&browserTrackerId={browser_tracker_id}&placeId={place_id}&isPlayTogetherGame=false"
    if job_id:
        params += f"&gameId={urllib.parse.quote(job_id)}"
    return f"https://assetgame.roblox.com/game/PlaceLauncher.ashx?{params}", "job" if job_id else "public", ""


def build_roblox_player_uri(ticket: str, place_launcher_url: str, browser_tracker_id: str) -> str:
    launch_time = int(time.time() * 1000)
    encoded_launcher = urllib.parse.quote(place_launcher_url, safe="")
    return (
        f"roblox-player:1+launchmode:play+gameinfo:{ticket}+launchtime:{launch_time}"
        f"+placelauncherurl:{encoded_launcher}+browsertrackerid:{browser_tracker_id}"
        "+robloxLocale:en_us+gameLocale:en_us+channel:+LaunchExp:InApp"
    )

