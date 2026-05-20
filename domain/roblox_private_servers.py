from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


USER_AGENT = "CronusLauncherHybrid/1.0"
ROBLOX_HOME = "https://www.roblox.com/"
GAMES_BASE = "https://games.roblox.com/"
APIS_BASE = "https://apis.roblox.com/"

PRIVATE_GAME_RE = re.compile(
    r"Roblox\.GameLauncher\.joinPrivateGame\(\s*(\d+)\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*\)",
    flags=re.I,
)
GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", flags=re.I)


def _safe_hash(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def parse_vip_link(value: str) -> Tuple[str, str]:
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


def parse_vip_components(value: str) -> Dict[str, str]:
    text = str(value or "").strip()
    if not text:
        return {"place_id": "", "link_code": "", "access_code": ""}
    parsed = urllib.parse.urlparse(text)
    qs = urllib.parse.parse_qs(parsed.query)
    match = re.search(r"/games/(\d+)", parsed.path)
    place_id = match.group(1) if match else qs.get("placeId", [""])[0]
    link_code = (
        qs.get("privateServerLinkCode", [""])[0]
        or qs.get("linkCode", [""])[0]
        or qs.get("code", [""])[0]
    )
    access_code = qs.get("accessCode", [""])[0]
    if not link_code and access_code and not GUID_RE.match(access_code):
        link_code = access_code
        access_code = ""
    return {"place_id": str(place_id or ""), "link_code": str(link_code or ""), "access_code": str(access_code or "")}


def parse_vip_access_code_html(html: str) -> Dict[str, str]:
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


def resolve_vip_access_code(cookie: str, vip_link: str, timeout: float = 12.0) -> Dict[str, Any]:
    parts = parse_vip_components(vip_link)
    place_id = parts.get("place_id", "")
    link_code = parts.get("link_code", "")
    access_code = parts.get("access_code", "")
    if access_code and GUID_RE.match(access_code):
        return {"ok": True, "place_id": place_id, "access_code": access_code, "link_code": link_code or access_code, "source": "query"}
    if not place_id or not link_code:
        return {"ok": False, "msg": "VIP link must contain placeId and privateServerLinkCode", "place_id": place_id, "link_code": link_code}
    url = f"{ROBLOX_HOME}games/{urllib.parse.quote(place_id)}?privateServerLinkCode={urllib.parse.quote(link_code)}"
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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = int(resp.status)
    except urllib.error.HTTPError as exc:
        return {"ok": False, "msg": f"VIP invite resolve failed ({exc.code})", "place_id": place_id, "link_code": link_code}
    except Exception as exc:
        return {"ok": False, "msg": f"VIP invite resolve failed: {exc}", "place_id": place_id, "link_code": link_code}
    parsed = parse_vip_access_code_html(body)
    if not parsed.get("ok"):
        return {"ok": False, "msg": f"VIP invite did not expose accessCode ({status})", "place_id": place_id, "link_code": link_code}
    if str(parsed.get("place_id") or "") != place_id:
        return {"ok": False, "msg": "VIP invite placeId mismatch", "place_id": place_id, "link_code": link_code}
    return {
        "ok": True,
        "place_id": place_id,
        "access_code": str(parsed.get("access_code") or ""),
        "link_code": link_code,
        "source": "invite_page",
    }


def _json_body(body: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(body or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {"raw": str(body or "")}


def _api_error_message(payload: Dict[str, Any], fallback: str) -> str:
    try:
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            message = str(errors[0].get("message") or errors[0].get("userFacingMessage") or "").strip()
            if message:
                return message
    except Exception:
        pass
    message = str(payload.get("message") or payload.get("error") or "").strip()
    return message or fallback


def universe_id_for_place(place_id: str, timeout: float = 8.0) -> Tuple[bool, str, str]:
    place = str(place_id or "").strip()
    if not place.isdigit():
        return False, "", "Place ID is required before creating a private server"
    url = APIS_BASE + f"universes/v1/places/{urllib.parse.quote(place, safe='')}/universe"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return False, "", f"Place to universe lookup failed ({exc.code})"
    except Exception as exc:
        return False, "", f"Place to universe lookup failed: {exc}"
    universe_id = str(payload.get("universeId") or "").strip() if isinstance(payload, dict) else ""
    if not universe_id:
        return False, "", "Roblox did not return a universeId for this Place ID"
    return True, universe_id, "ok"


def game_name_for_universe(client: Any, universe_id: str) -> Tuple[bool, str, str]:
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


def _server_id(server: Dict[str, Any]) -> str:
    return str(server.get("private_server_id") or server.get("privateServerId") or server.get("vipServerId") or server.get("id") or "").strip()


def _server_owner_id(server: Dict[str, Any]) -> str:
    owner = server.get("owner")
    if isinstance(owner, dict):
        value = owner.get("id") or owner.get("userId")
        if value not in (None, ""):
            return str(value).strip()
    return str(server.get("owner_user_id") or server.get("ownerId") or server.get("ownerUserId") or "").strip()


def _server_place_id(server: Dict[str, Any]) -> str:
    game = server.get("game")
    if isinstance(game, dict):
        value = game.get("placeId") or game.get("rootPlaceId")
        if value not in (None, ""):
            return str(value).strip()
    return str(server.get("place_id") or server.get("placeId") or "").strip()


def _server_universe_id(server: Dict[str, Any]) -> str:
    game = server.get("game")
    if isinstance(game, dict):
        value = game.get("universeId")
        if value not in (None, ""):
            return str(value).strip()
    return str(server.get("universe_id") or server.get("universeId") or "").strip()


def _server_join_code(server: Dict[str, Any]) -> str:
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


def _server_access_code(server: Dict[str, Any]) -> str:
    return str(server.get("access_code") or server.get("accessCode") or "").strip()


def build_owned_private_server_link(place_id: str, server: Dict[str, Any]) -> str:
    raw_link = server.get("link") or server.get("join_link") or server.get("joinLink") or ""
    if isinstance(raw_link, dict):
        raw_link = raw_link.get("url") or raw_link.get("href") or raw_link.get("link") or ""
    link = str(raw_link or "").strip()
    if link:
        parts = parse_vip_components(link)
        if parts.get("place_id") and (parts.get("link_code") or parts.get("access_code")):
            return link
        raw_link_code = str(parts.get("link_code") or "").strip()
    else:
        raw_link_code = ""
    place = str(place_id or _server_place_id(server) or "").strip()
    join_code = _server_join_code(server) or raw_link_code
    if place and join_code:
        return f"{ROBLOX_HOME}games/{urllib.parse.quote(place, safe='')}/?privateServerLinkCode={urllib.parse.quote(join_code, safe='')}"
    access_code = _server_access_code(server)
    if place and access_code:
        return f"{ROBLOX_HOME}games/{urllib.parse.quote(place, safe='')}/?accessCode={urllib.parse.quote(access_code, safe='')}"
    if link:
        return link
    return ""


def _private_server_matches_owner(
    server: Dict[str, Any],
    owner_user_id: str,
    place_id: str,
    universe_id: str,
) -> bool:
    if server.get("active") is False:
        return False
    owner = _server_owner_id(server)
    if owner_user_id and owner and owner != str(owner_user_id):
        return False
    server_place = _server_place_id(server)
    server_universe = _server_universe_id(server)
    if place_id and server_place and server_place != str(place_id):
        return False
    if universe_id and server_universe and server_universe != str(universe_id):
        return False
    return bool(_server_id(server) or build_owned_private_server_link(place_id, server))


def list_my_private_servers(client: Any, limit: int = 100, max_pages: int = 5) -> Tuple[bool, List[Dict[str, Any]], str]:
    servers: List[Dict[str, Any]] = []
    cursor = ""
    for _page in range(max(1, int(max_pages or 1))):
        params = {"limit": str(max(10, min(int(limit or 100), 100)))}
        if cursor:
            params["cursor"] = cursor
        url = GAMES_BASE + "v1/private-servers/my-private-servers?" + urllib.parse.urlencode(params)
        status, body, _headers = client.request(url, method="GET")
        payload = _json_body(body)
        if not 200 <= status < 300:
            return False, servers, _api_error_message(payload, f"Private server list failed ({status})")
        data = payload.get("data")
        if isinstance(data, list):
            servers.extend([item for item in data if isinstance(item, dict)])
        cursor = str(payload.get("nextPageCursor") or "").strip()
        if not cursor:
            break
    return True, servers, "ok"


def list_private_servers_for_place(
    client: Any,
    place_id: str,
    limit: int = 100,
    max_pages: int = 5,
) -> Tuple[bool, List[Dict[str, Any]], str]:
    place = str(place_id or "").strip()
    if not place:
        return False, [], "missing place id"
    servers: List[Dict[str, Any]] = []
    cursor = ""
    for _page in range(max(1, int(max_pages or 1))):
        params = {
            "sortOrder": "Asc",
            "limit": str(max(10, min(int(limit or 100), 100))),
        }
        if cursor:
            params["cursor"] = cursor
        url = GAMES_BASE + f"v1/games/{urllib.parse.quote(place, safe='')}/private-servers?" + urllib.parse.urlencode(params)
        status, body, _headers = client.request(url, method="GET")
        payload = _json_body(body)
        if not 200 <= status < 300:
            return False, servers, _api_error_message(payload, f"Private server place list failed ({status})")
        data = payload.get("data")
        if isinstance(data, list):
            servers.extend([item for item in data if isinstance(item, dict)])
        cursor = str(payload.get("nextPageCursor") or "").strip()
        if not cursor:
            break
    return True, servers, "ok"


def fetch_private_server_metadata(client: Any, private_server_id: str) -> Tuple[bool, Dict[str, Any], str]:
    sid = str(private_server_id or "").strip()
    if not sid:
        return False, {}, "missing private server id"
    status, body, _headers = client.request(GAMES_BASE + f"v1/vip-servers/{urllib.parse.quote(sid, safe='')}", method="GET")
    payload = _json_body(body)
    if 200 <= status < 300:
        return True, payload, "ok"
    return False, payload, _api_error_message(payload, f"Private server metadata failed ({status})")


def private_servers_enabled_for_universe(client: Any, universe_id: str) -> Tuple[bool, bool, str]:
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
    server: Dict[str, Any],
    place_id: str,
    universe_id: str,
    owner_user_id: str,
    source: str,
    status: str = "ok",
    error: str = "",
) -> Dict[str, Any]:
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


def _private_server_identity_key(server: Dict[str, Any], owner_user_id: str, place_id: str, universe_id: str) -> Tuple[str, str, str]:
    return (
        _server_owner_id(server) or str(owner_user_id or ""),
        _server_place_id(server) or str(place_id or ""),
        _server_universe_id(server) or str(universe_id or ""),
    )


def _merge_private_server_secrets(candidate: Dict[str, Any], known: Dict[str, Any]) -> None:
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


def _known_private_server_for(
    records: List[Dict[str, Any]],
    private_server_id: str,
    owner_user_id: str,
    place_id: str,
    universe_id: str,
) -> Dict[str, Any]:
    expected_key = (str(owner_user_id or ""), str(place_id or ""), str(universe_id or ""))
    sid = str(private_server_id or "").strip()
    fallback: Dict[str, Any] = {}
    for item in records or []:
        if not isinstance(item, dict):
            continue
        item_id = _server_id(item)
        matched = False
        if sid and item_id and item_id == sid:
            matched = True
        item_key = _private_server_identity_key(item, owner_user_id, place_id, universe_id)
        if expected_key[0] and item_key == expected_key:
            matched = True
        if not matched:
            continue
        if build_owned_private_server_link(place_id, item) or _server_access_code(item) or _server_join_code(item):
            return item
        if not fallback:
            fallback = item
    return fallback


def _place_private_server_for(
    servers: List[Dict[str, Any]],
    private_server_id: str,
    owner_user_id: str,
) -> Dict[str, Any]:
    sid = str(private_server_id or "").strip()
    owner = str(owner_user_id or "").strip()
    fallback: Dict[str, Any] = {}
    for item in servers or []:
        if not isinstance(item, dict):
            continue
        item_id = _server_id(item)
        item_owner = _server_owner_id(item)
        matched = bool(sid and item_id and item_id == sid)
        if not matched and owner and item_owner and item_owner == owner:
            matched = True
        if not matched:
            continue
        if build_owned_private_server_link("", item) or _server_access_code(item) or _server_join_code(item):
            return item
        if not fallback:
            fallback = item
    return fallback


def _render_private_server_name(game_name: str, place_id: str = "") -> str:
    name = str(game_name or "").strip() or (f"Place {place_id}" if place_id else "Private Server")
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    return name[:50] or "Private Server"


def ensure_owned_private_server(
    client: Any,
    username: str,
    owner_user_id: str,
    place_id: str,
    name_template: str = "",
    free_only: bool = True,
    known_servers: Optional[List[Dict[str, Any]]] = None,
    *,
    universe_lookup=universe_id_for_place,
    list_my_private_servers_fn=list_my_private_servers,
    list_private_servers_for_place_fn=list_private_servers_for_place,
    fetch_private_server_metadata_fn=fetch_private_server_metadata,
    private_servers_enabled_for_universe_fn=private_servers_enabled_for_universe,
    game_name_for_universe_fn=game_name_for_universe,
) -> Dict[str, Any]:
    place = str(place_id or "").strip()
    ok, universe_id, detail = universe_lookup(place)
    if not ok:
        return {"ok": False, "msg": detail, "place_id": place}

    list_ok, servers, list_msg = list_my_private_servers_fn(client)
    if not list_ok:
        return {"ok": False, "msg": list_msg, "place_id": place, "universe_id": universe_id}

    place_servers_loaded = False
    place_servers: List[Dict[str, Any]] = []
    place_servers_msg = ""

    for server in servers:
        if not _private_server_matches_owner(server, str(owner_user_id or ""), place, universe_id):
            continue
        candidate = dict(server)
        sid = _server_id(candidate)
        if sid:
            meta_ok, meta, _meta_msg = fetch_private_server_metadata_fn(client, sid)
            if meta_ok:
                candidate.update(meta)
                if "privateServerId" not in candidate:
                    candidate["privateServerId"] = sid
        known = _known_private_server_for(list(known_servers or []), sid, owner_user_id, place, universe_id)
        _merge_private_server_secrets(candidate, known)
        if not (build_owned_private_server_link(place, candidate) or _server_access_code(candidate) or _server_join_code(candidate)):
            if not place_servers_loaded:
                place_servers_loaded = True
                place_ok, place_servers, place_servers_msg = list_private_servers_for_place_fn(client, place)
                if not place_ok:
                    place_servers = []
            playable = _place_private_server_for(place_servers, sid, owner_user_id)
            if playable:
                candidate.update(playable)
                if sid and "privateServerId" not in candidate:
                    candidate["privateServerId"] = sid
                if "placeId" not in candidate:
                    candidate["placeId"] = place
                if "universeId" not in candidate:
                    candidate["universeId"] = universe_id
                if "ownerId" not in candidate and owner_user_id:
                    candidate["ownerId"] = str(owner_user_id)
        record = _private_server_record(candidate, place, universe_id, owner_user_id, source="existing")
        if record.get("link") or record.get("access_code") or record.get("join_code"):
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

    enabled_ok, enabled, enabled_msg = private_servers_enabled_for_universe_fn(client, universe_id)
    if not enabled_ok:
        return {"ok": False, "msg": enabled_msg, "place_id": place, "universe_id": universe_id}
    if not enabled:
        return {"ok": False, "msg": "Private servers are disabled for this universe", "place_id": place, "universe_id": universe_id}

    name_ok, game_name, _name_msg = game_name_for_universe_fn(client, universe_id)
    if not name_ok:
        game_name = f"Place {place}"
    payload = {
        "name": _render_private_server_name(game_name, place_id=place),
        "expectedPrice": 0,
        "isPurchaseConfirmed": True,
    }
    create_ok, created, create_msg, _headers = client.csrf_post(
        GAMES_BASE + f"v1/games/vip-servers/{urllib.parse.quote(universe_id, safe='')}",
        payload,
    )
    if not create_ok:
        reason = create_msg or "Private server creation failed"
        if free_only:
            reason += " (free-only expectedPrice=0)"
        return {"ok": False, "msg": reason, "place_id": place, "universe_id": universe_id}

    candidate = dict(created)
    sid = _server_id(candidate)
    if sid:
        meta_ok, meta, _meta_msg = fetch_private_server_metadata_fn(client, sid)
        if meta_ok:
            # Preserve accessCode from the create response; GET responses often omit it.
            access_code = _server_access_code(candidate)
            candidate.update(meta)
            if access_code and not _server_access_code(candidate):
                candidate["accessCode"] = access_code
            if "privateServerId" not in candidate:
                candidate["privateServerId"] = sid
    if "placeId" not in candidate:
        candidate["placeId"] = place
    if "universeId" not in candidate:
        candidate["universeId"] = universe_id
    if "ownerId" not in candidate and owner_user_id:
        candidate["ownerId"] = str(owner_user_id)
    record = _private_server_record(candidate, place, universe_id, owner_user_id, source="created")
    if not (record.get("link") or record.get("access_code") or record.get("join_code")):
        return {
            "ok": False,
            "fatal": True,
            "msg": "Private server was created, but Roblox did not return a join link or access code",
            **record,
        }
    return {"ok": True, "source": "created", **record}



def parse_launch_destination_from_cmdline(cmdline: str) -> Dict[str, Any]:
    text = str(cmdline or "")
    launcher = ""
    match = re.search(r"placelauncherurl:([^+\s]+)", text, flags=re.I)
    if match:
        launcher = urllib.parse.unquote(match.group(1))
    else:
        match = re.search(r"\-j\s+\"([^\"]+)\"", text, flags=re.I)
        if match:
            launcher = match.group(1)
    if not launcher:
        return {}
    parsed = urllib.parse.urlparse(launcher)
    qs = urllib.parse.parse_qs(parsed.query)
    request_name = str((qs.get("request") or [""])[0] or "")
    place_id = str((qs.get("placeId") or [""])[0] or "")
    link_code = str((qs.get("linkCode") or [""])[0] or "")
    access_code = str((qs.get("accessCode") or [""])[0] or "")
    job_id = str((qs.get("gameId") or [""])[0] or "")
    server_type = "VIP" if request_name.lower() == "requestprivategame" else ("JOB" if request_name.lower() == "requestgamejob" else "PUBLIC")
    return {
        "observed_place_id": place_id,
        "observed_server_type": server_type,
        "observed_private_link_code_hash": _safe_hash(link_code),
        "observed_access_code_hash": _safe_hash(access_code),
        "observed_job_id_hash": _safe_hash(job_id),
        "request": request_name,
        "evidence_source": "process_cmdline",
    }


def build_place_launcher_url(
    place_id: str,
    job_id: str = "",
    vip_link: str = "",
    follow_user_id: str = "",
    browser_tracker_id: str = "",
    vip_access_code: str = "",
    vip_link_code: str = "",
) -> Tuple[str, str, str]:
    place_id = str(place_id or "").strip()
    job_id = str(job_id or "").strip()
    vip_link = str(vip_link or "").strip()
    follow_user_id = str(follow_user_id or "").strip()
    browser_tracker_id = str(browser_tracker_id or "").strip()
    if follow_user_id:
        return (
            f"https://assetgame.roblox.com/game/PlaceLauncher.ashx?request=RequestFollowUser&userId={follow_user_id}",
            "follow",
            "",
        )
    if vip_link:
        parsed_place, link_code = parse_vip_link(vip_link)
        place_id = place_id or parsed_place
        access_code = str(vip_access_code or link_code or "").strip()
        launch_link_code = str(vip_link_code or link_code or "").strip()
        if access_code and launch_link_code and place_id:
            return (
                "https://assetgame.roblox.com/game/PlaceLauncher.ashx?"
                f"request=RequestPrivateGame&placeId={place_id}&accessCode={access_code}&linkCode={launch_link_code}",
                "vip",
                vip_link,
            )
    if job_id.startswith("http"):
        parsed_place, link_code = parse_vip_link(job_id)
        place_id = place_id or parsed_place
        access_code = str(vip_access_code or link_code or "").strip()
        launch_link_code = str(vip_link_code or link_code or "").strip()
        if access_code and launch_link_code and place_id:
            return (
                "https://assetgame.roblox.com/game/PlaceLauncher.ashx?"
                f"request=RequestPrivateGame&placeId={place_id}&accessCode={access_code}&linkCode={launch_link_code}",
                "vip",
                job_id,
            )
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

