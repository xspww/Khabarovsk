"""HybridLauncher: validate -> guard -> VIP -> ticket -> launch orchestration."""
from __future__ import annotations

import re
import secrets
import time
from typing import Any

from domain.roblox_private_servers import (
    _safe_hash,
    build_owned_private_server_link,
    build_place_launcher_url,
    build_roblox_player_uri,
    fetch_private_server_metadata,
    game_name_for_universe,
    list_my_private_servers,
    list_private_servers_for_place,
    parse_vip_components,
    parse_vip_link,
    private_servers_enabled_for_universe,
    remembered_vip_unavailable,
    resolve_vip_access_code,
    universe_id_for_place,
    vip_unavailable_permanently,
)
from domain.roblox_private_servers import (
    ensure_owned_private_server as _ensure_owned_private_server,
)
from services.account_hybrid import ACCOUNT_STORE, decrypt_cookie
from services.roblox_http import RobloxHTTP, open_roblox_uri
from services.roblox_multibox import ensure_multi_roblox_guard, release_multi_roblox_guard
from services.roblox_validation import validate_record_cookie_identity


def ensure_owned_private_server(
    client: RobloxHTTP,
    owner_user_id: str,
    place_id: str,
    free_only: bool = True,
    known_servers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return _ensure_owned_private_server(
        client,
        owner_user_id,
        place_id,
        free_only=free_only,
        known_servers=known_servers,
        universe_lookup=universe_id_for_place,
        list_my_private_servers_fn=list_my_private_servers,
        list_private_servers_for_place_fn=list_private_servers_for_place,
        fetch_private_server_metadata_fn=fetch_private_server_metadata,
        private_servers_enabled_for_universe_fn=private_servers_enabled_for_universe,
        game_name_for_universe_fn=game_name_for_universe,
    )


def _merge_owned_private_server(records: list[dict[str, Any]], server: dict[str, Any]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    incoming_id = str(server.get("private_server_id") or "").strip()
    incoming_key = (
        str(server.get("owner_user_id") or ""),
        str(server.get("place_id") or ""),
        str(server.get("universe_id") or ""),
    )
    replaced = False
    for old in records or []:
        old_id = str(old.get("private_server_id") or "").strip()
        old_key = (
            str(old.get("owner_user_id") or ""),
            str(old.get("place_id") or ""),
            str(old.get("universe_id") or ""),
        )
        if (incoming_id and old_id == incoming_id) or (incoming_key[0] and old_key == incoming_key):
            combined = dict(old)
            combined.update(dict(server))
            if str(server.get("status") or "").lower() == "error":
                for key in ("private_server_id", "link", "join_code", "access_code", "source", "name"):
                    if combined.get(key) in (None, "") and old.get(key) not in (None, ""):
                        combined[key] = old.get(key)
            merged.append(combined)
            replaced = True
        else:
            merged.append(dict(old))
    if not replaced:
        merged.append(dict(server))
    return merged


class HybridLauncher:
    @staticmethod
    def _tracker_from_cmdline(cmdline: str) -> str:
        text = str(cmdline or "")
        for pattern in (
            r"browsertrackerid[:=](\d+)",
            r"browserTrackerId=(\d+)",
            r"browsertrackerid%3a(\d+)",
            r"browserTrackerId%3D(\d+)",
            r"\-b\s+(\d+)",
        ):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return match.group(1)
        return ""

    @classmethod
    def duplicate_pids_for_tracker(cls, browser_tracker_id: str) -> list[int]:
        browser_tracker_id = str(browser_tracker_id or "").strip()
        if not browser_tracker_id:
            return []
        try:
            from services.process_service import ProcessManager
        except Exception:
            return []
        pids: list[int] = []
        for item in ProcessManager.list_live_game_processes():
            if cls._tracker_from_cmdline(str(item.get("cmdline") or "")) == browser_tracker_id:
                pid = int(item.get("pid") or 0)
                if pid:
                    pids.append(pid)
        return pids

    @classmethod
    def kill_duplicate_instances(cls, browser_tracker_id: str) -> dict[str, Any]:
        try:
            from services.process_service import ProcessManager
        except Exception as exc:
            return {"ok": False, "killed": [], "msg": str(exc)}
        killed: list[int] = []
        for pid in cls.duplicate_pids_for_tracker(browser_tracker_id):
            try:
                if ProcessManager.kill_pid(pid):
                    killed.append(pid)
            except Exception:
                continue
        return {"ok": True, "killed": killed, "count": len(killed)}

    @classmethod
    def launch_record(cls, record: dict[str, Any], target: dict[str, Any] | None = None, multi_roblox: bool = True) -> dict[str, Any]:
        data = dict(record or {})
        target = dict(target or {})
        username = str(data.get("username") or "").strip()
        if username:
            try:
                for stored in ACCOUNT_STORE.read_records(include_cookies=False):
                    if str(stored.get("username") or "").strip().lower() != username.lower():
                        continue
                    for key in ("owned_private_servers", "place_id", "job_id", "browser_tracker_id", "global_vip_link"):
                        if not data.get(key) and stored.get(key):
                            data[key] = stored.get(key)
                    stored_links = stored.get("vip_links") if isinstance(stored.get("vip_links"), list) else []
                    data_links = data.get("vip_links") if isinstance(data.get("vip_links"), list) else []
                    merged_links = []
                    for link in list(data_links or []) + list(stored_links or []):
                        text = str(link or "").strip()
                        if text and text not in merged_links:
                            merged_links.append(text)
                    if merged_links:
                        data["vip_links"] = merged_links
                    break
            except Exception:
                pass
        cookie = data.get("cookie") or ""
        if not cookie and data.get("encrypted_cookie"):
            cookie = decrypt_cookie(str(data.get("encrypted_cookie") or ""))
        if not cookie:
            return {"ok": False, "msg": "No .ROBLOSECURITY cookie for account"}
        identity = validate_record_cookie_identity(data, cookie, update_store=True)
        if not identity.get("ok"):
            return {"ok": False, "fatal": True, "msg": identity.get("msg", "cookie identity mismatch"), **identity}
        browser_tracker_id = str(target.get("browser_tracker_id") or data.get("browser_tracker_id") or "").strip()
        if not browser_tracker_id:
            browser_tracker_id = str(secrets.randbelow(75_000) + 100_000) + str(secrets.randbelow(800_000) + 100_000)
        guard_ok = False
        guard_detail = ""
        if multi_roblox:
            guard_ok, guard_detail = ensure_multi_roblox_guard()
            if not guard_ok:
                return {"ok": False, "fatal": True, "msg": f"Multi Roblox guard failed: {guard_detail}"}
            close_result = cls.kill_duplicate_instances(browser_tracker_id)
        else:
            release_multi_roblox_guard()
            from services.process_service import ProcessManager

            killed_count = ProcessManager.kill_all_roblox_clients(wait_seconds=2.5)
            close_result = {"ok": True, "killed": [], "count": int(killed_count), "all_instances_closed": int(killed_count)}
        place_id = str(target.get("place_id") or data.get("place_id") or "").strip()
        job_id = str(target.get("job_id") or data.get("job_id") or "").strip()
        explicit_vip_link = str(target.get("vip_link") or target.get("vip_url") or "").strip()
        links = target.get("vip_links") or data.get("vip_links") or []
        if isinstance(links, str):
            links = [line.strip() for line in links.splitlines() if line.strip()]
        if not isinstance(links, list):
            links = []
        global_vip_link = str(target.get("global_vip_link") or data.get("global_vip_link") or "").strip()
        vip_link = explicit_vip_link
        vip_resolved = False
        vip_resolution: dict[str, Any] = {}
        if job_id.startswith("http") and not vip_link:
            vip_link = job_id
            job_id = ""
        client = RobloxHTTP(cookie)
        private_server_meta: dict[str, Any] = {}
        private_fallback_reason = ""
        auto_private_enabled = bool(
            target.get(
                "auto_create_private_server_enabled",
                data.get("auto_create_private_server_enabled", False),
            )
        )
        if not auto_private_enabled and not explicit_vip_link:
            links = []
            global_vip_link = ""
        # Shared-direct VIP: Shared mode with a complete shared link joins
        # that exact room for every account (no per-owner ensure/create).
        # Validated again here so a stale/mismatched value can never sneak in.
        shared_direct = str(target.get("shared_direct_vip") or data.get("shared_direct_vip") or "").strip()
        if shared_direct:
            try:
                _scomp = parse_vip_components(shared_direct)
            except Exception:
                _scomp = {}
            _splace = str((_scomp or {}).get("place_id") or "").strip()
            _scode = str((_scomp or {}).get("link_code") or "").strip() or str((_scomp or {}).get("access_code") or "").strip()
            if _splace and _scode and (not place_id or _splace == place_id):
                place_id = place_id or _splace
                vip_link = shared_direct
                vip_resolved = False
                vip_resolution = {}
                auto_private_enabled = False
            else:
                shared_direct = ""
        if auto_private_enabled:
            link_candidates = [str(vip_link or "").strip()] + [str(link or "").strip() for link in links] + [global_vip_link]
            for candidate in link_candidates:
                if not place_id:
                    parsed_place, _link_code = parse_vip_link(candidate)
                    if parsed_place:
                        place_id = parsed_place
                        break
            if not place_id:
                return {
                    "ok": False,
                    "fatal": True,
                    "mode": "vip",
                    "vip_resolved": False,
                    "msg": "Auto Create Private Server requires Place ID or a VIP link with Place ID",
                }
            free_only = bool(target.get("auto_create_private_server_free_only", data.get("auto_create_private_server_free_only", True)))
            known_private_servers = list(data.get("owned_private_servers") or [])
            # A game that had no private servers recently will not have one
            # now either. Skip the three Roblox calls and reuse the stored
            # verdict, otherwise every relaunch re-probes the same fact.
            cached_unavailable = remembered_vip_unavailable(known_private_servers, place_id)
            for candidate_link in link_candidates:
                components = parse_vip_components(candidate_link)
                candidate_place = str(components.get("place_id") or "").strip()
                candidate_link_code = str(components.get("link_code") or "").strip()
                candidate_access_code = str(components.get("access_code") or "").strip()
                if candidate_place and candidate_place != str(place_id):
                    continue
                if not (candidate_link_code or candidate_access_code):
                    continue
                known_private_servers.append(
                    {
                        "owner_user_id": str(identity.get("cookie_user_id") or data.get("cookie_user_id") or ""),
                        "place_id": str(place_id),
                        "link": candidate_link,
                        "join_code": candidate_link_code,
                        "access_code": candidate_access_code,
                    }
                )
            private_result = (
                {"ok": False, "msg": cached_unavailable, "place_id": place_id}
                if cached_unavailable
                else ensure_owned_private_server(
                    client,
                    owner_user_id=str(identity.get("cookie_user_id") or data.get("cookie_user_id") or ""),
                    place_id=place_id,
                    free_only=free_only,
                    known_servers=known_private_servers,
                )
            )
            if not private_result.get("ok"):
                private_failure_msg = str(private_result.get("msg") or "private server creation failed")
                if not cached_unavailable:
                    ACCOUNT_STORE.update_record(
                        username,
                        {
                            "owned_private_servers": _merge_owned_private_server(
                                list(data.get("owned_private_servers") or []),
                                {
                                    "owner_user_id": str(identity.get("cookie_user_id") or data.get("cookie_user_id") or ""),
                                    "place_id": place_id,
                                    "universe_id": str(private_result.get("universe_id") or ""),
                                    "status": "error",
                                    "error": private_failure_msg,
                                    "synced_at": time.time(),
                                },
                            )
                        },
                    )
                # One game in the pool having no private servers must not
                # sink the whole run: with multi-game setups the other maps
                # still work. Only "VIP disabled / paid" is permanent, so
                # give up on CREATING our own room.
                #
                # Careful: a permanent create failure does NOT mean the
                # account cannot use a private server. A VIP link pasted
                # from a friend still joins that exact room even when we
                # are not allowed to create one (paid / free-only mode).
                # So keep the best usable link and only drop to public
                # when there is genuinely nothing to join.
                if vip_unavailable_permanently(private_failure_msg):
                    auto_private_enabled = False
                    fallback_link = ""
                    for candidate in link_candidates:
                        if not candidate:
                            continue
                        cand_place, cand_code = parse_vip_link(candidate)
                        if cand_place and cand_place != str(place_id):
                            continue
                        if not cand_code:
                            continue
                        fallback_link = candidate
                        break
                    if fallback_link:
                        vip_link = fallback_link
                        vip_resolution = {}
                        vip_resolved = False
                        private_fallback_reason = private_failure_msg
                        try:
                            from core import flog_kv

                            flog_kv(
                                "LAUNCH",
                                "private_server_public_fallback",
                                "warning",
                                account=username,
                                place_id=place_id,
                                reason=private_failure_msg,
                                joined_existing_vip=True,
                            )
                        except Exception:
                            pass
                    else:
                        private_fallback_reason = private_failure_msg
                        vip_link = ""
                        links = []
                        global_vip_link = ""
                        try:
                            from core import flog_kv

                            flog_kv(
                                "LAUNCH",
                                "private_server_public_fallback",
                                "warning",
                                account=username,
                                place_id=place_id,
                                reason=private_failure_msg,
                            )
                        except Exception:
                            pass
                else:
                    return {
                        "ok": False,
                        "fatal": True,
                        "mode": "vip",
                        "vip_resolved": False,
                        "msg": private_failure_msg,
                        "auto_private_server": True,
                    }
            else:
                private_server_meta = dict(private_result)
                vip_link = str(private_server_meta.get("link") or "").strip()
                vip_resolution = {
                    "ok": True,
                    "place_id": str(private_server_meta.get("place_id") or place_id),
                    "access_code": str(private_server_meta.get("access_code") or ""),
                    "link_code": str(private_server_meta.get("join_code") or private_server_meta.get("access_code") or ""),
                    "source": f"owned_private_server:{private_server_meta.get('source') or 'unknown'}",
                }
                if not vip_link:
                    vip_link = build_owned_private_server_link(place_id, private_server_meta)
                if not vip_resolution.get("access_code") and vip_link:
                    vip_resolution = resolve_vip_access_code(cookie, vip_link)
                    if not vip_resolution.get("ok"):
                        return {
                            "ok": False,
                            "fatal": True,
                            "msg": str(vip_resolution.get("msg") or "Owned private server invite resolve failed"),
                            "mode": "vip",
                            "vip_resolved": False,
                            "auto_private_server": True,
                        }
                vip_resolved = True
                place_id = place_id or str(vip_resolution.get("place_id") or "")
                owned_servers = _merge_owned_private_server(list(data.get("owned_private_servers") or []), private_server_meta)
                updated_vip_links = list(data.get("vip_links") or [])
                if vip_link:
                    components = parse_vip_components(vip_link)
                    if (components.get("link_code") or components.get("access_code")) and vip_link not in updated_vip_links:
                        updated_vip_links.insert(0, vip_link)
                ACCOUNT_STORE.update_record(
                    username,
                    {
                        "owned_private_servers": owned_servers,
                        "vip_links": updated_vip_links,
                        "place_id": place_id or data.get("place_id", ""),
                    },
                )
        elif not vip_link:
            if isinstance(links, list) and links:
                vip_link = str(links[0] or "").strip()
            if not vip_link:
                vip_link = global_vip_link
        if vip_link:
            if not vip_resolution.get("ok"):
                vip_resolution = resolve_vip_access_code(cookie, vip_link)
                if not vip_resolution.get("ok"):
                    return {"ok": False, "fatal": True, "msg": str(vip_resolution.get("msg") or "VIP invite resolve failed"), "mode": "vip", "vip_resolved": False}
                vip_resolved = True
            place_id = place_id or str(vip_resolution.get("place_id") or "")
        follow_user_id = str(target.get("follow_user_id") or target.get("follow_user") or "").strip()
        if not place_id and not vip_link and not job_id and not follow_user_id:
            return {"ok": False, "msg": "PlaceId, JobId, VIP link, or follow user is required"}
        ok, ticket = client.get_auth_ticket()
        if not ok:
            return {"ok": False, "msg": ticket}
        launcher_url, mode, attempted_vip = build_place_launcher_url(
            place_id=place_id,
            job_id=job_id,
            vip_link=vip_link,
            follow_user_id=follow_user_id,
            browser_tracker_id=browser_tracker_id,
            vip_access_code=str(vip_resolution.get("access_code") or ""),
            vip_link_code=str(vip_resolution.get("link_code") or ""),
        )
        uri = build_roblox_player_uri(ticket, launcher_url, browser_tracker_id)
        open_roblox_uri(uri)
        ACCOUNT_STORE.update_record(
            str(data.get("username") or ""),
            {
                "browser_tracker_id": browser_tracker_id,
                "last_use": time.time(),
                "place_id": place_id or data.get("place_id", ""),
                "job_id": job_id or data.get("job_id", ""),
            },
        )
        return {
            "ok": True,
            "mode": mode,
            "browser_tracker_id": browser_tracker_id,
            "closed_duplicates": close_result.get("killed", []),
            "closed_instances": close_result.get("count", 0),
            "multi_roblox_guard": {"ok": guard_ok, "detail": guard_detail} if multi_roblox else {"ok": False, "detail": "disabled"},
            "cookie_username": identity.get("cookie_username", ""),
            "cookie_user_id": identity.get("cookie_user_id", ""),
            "vip_resolved": vip_resolved,
            "vip_access_code_hash": _safe_hash(str(vip_resolution.get("access_code") or "")),
            "vip_link_code_hash": _safe_hash(str(vip_resolution.get("link_code") or "")),
            "launch_uri_preview": re.sub(r"(gameinfo:)[^+]+", r"\1[REDACTED]", uri),
            "attempted_vip": attempted_vip,
            "auto_private_server": bool(auto_private_enabled),
            "private_server_public_fallback": private_fallback_reason,
            "owned_private_server_id": str(private_server_meta.get("private_server_id") or ""),
            "private_server_source": str(private_server_meta.get("source") or ""),
            "private_server_owner_user_id": str(private_server_meta.get("owner_user_id") or ""),
            "private_server_place_id": str(private_server_meta.get("place_id") or ""),
            "private_server_universe_id": str(private_server_meta.get("universe_id") or ""),
            "msg": "Roblox launch requested",
        }
