"""HybridLauncher: validate -> guard -> VIP -> ticket -> launch orchestration."""
from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field
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
    shared_direct_vip_link,
    universe_id_for_place,
    vip_unavailable_permanently,
)
from domain.roblox_private_servers import (
    ensure_owned_private_server as _ensure_owned_private_server,
)
from services.account_hybrid import ACCOUNT_STORE, COOKIE_UNREADABLE_REASON, decrypt_cookie
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
    """Launch-side adapter over the domain helper: injects the six call-site
    arguments so callers pass five. Kept on purpose — deleting it pushes the
    wiring out to every caller instead of concentrating it here."""
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


_ERROR_FALLBACK_KEYS = ("private_server_id", "link", "join_code", "access_code", "source", "name")


def _fill_missing_fields(combined: dict[str, Any], old: dict[str, Any]) -> None:
    """Keep old values when an errored refresh blanks a field."""
    for key in _ERROR_FALLBACK_KEYS:
        if combined.get(key) in (None, "") and old.get(key) not in (None, ""):
            combined[key] = old.get(key)


def _log_private_public_fallback(
    username: str, place_id: str, reason: str, *, joined_existing_vip: bool | None = None
) -> None:
    """Log why a private-server launch fell back; logging must never raise."""
    try:
        from core import flog_kv

        fields: dict[str, Any] = {"account": username, "place_id": place_id, "reason": reason}
        if joined_existing_vip is not None:
            fields["joined_existing_vip"] = joined_existing_vip
        flog_kv("LAUNCH", "private_server_public_fallback", "warning", **fields)
    except Exception:
        "intentional: ignore exception"


def _pick_fallback_link(link_candidates: list[str], place_id: str) -> str:
    """Return the first candidate link that can still join this place."""
    for candidate in link_candidates:
        if not candidate:
            continue
        cand_place, cand_code = parse_vip_link(candidate)
        if cand_place and cand_place != str(place_id):
            continue
        if not cand_code:
            continue
        return candidate
    return ""


def _private_server_identity(server: dict[str, Any]) -> tuple[str, tuple[str, str, str]]:
    return (
        str(server.get("private_server_id") or "").strip(),
        (
            str(server.get("owner_user_id") or ""),
            str(server.get("place_id") or ""),
            str(server.get("universe_id") or ""),
        ),
    )


def _is_same_private_server(old: dict[str, Any], incoming_id: str, incoming_key: tuple[str, str, str]) -> bool:
    old_id, old_key = _private_server_identity(old)
    return (incoming_id and old_id == incoming_id) or (incoming_key[0] and old_key == incoming_key)


def _merge_private_server_record(old: dict[str, Any], server: dict[str, Any]) -> dict[str, Any]:
    combined = dict(old)
    combined.update(dict(server))
    if str(server.get("status") or "").lower() == "error":
        _fill_missing_fields(combined, old)
    return combined


def _merge_owned_private_server(records: list[dict[str, Any]], server: dict[str, Any]) -> list[dict[str, Any]]:
    incoming_id, incoming_key = _private_server_identity(server)
    merged: list[dict[str, Any]] = []
    replaced = False
    for old in records or []:
        if _is_same_private_server(old, incoming_id, incoming_key):
            merged.append(_merge_private_server_record(old, server))
            replaced = True
        else:
            merged.append(dict(old))
    if not replaced:
        merged.append(dict(server))
    return merged


def _stored_account_record(username: str) -> dict[str, Any] | None:
    """Return the stored account row for a username, or None when unreadable."""
    try:
        return next(
            (
                item
                for item in ACCOUNT_STORE.read_records(include_cookies=False)
                if str(item.get("username") or "").strip().lower() == username.lower()
            ),
            None,
        )
    except Exception:
        return None


_LAUNCH_CARRY_FIELDS = ("owned_private_servers", "place_id", "job_id", "browser_tracker_id", "global_vip_link")


def _fill_launch_fields(data: dict[str, Any], stored: dict[str, Any]) -> None:
    """Backfill launch fields the caller omitted from the stored account row."""
    for key in _LAUNCH_CARRY_FIELDS:
        if not data.get(key) and stored.get(key):
            data[key] = stored[key]


def _merge_launch_links(data: dict[str, Any], stored: dict[str, Any]) -> None:
    """Merge stored VIP links into the record, keeping first-seen order."""
    data_links = data.get("vip_links") if isinstance(data.get("vip_links"), list) else []
    stored_links = stored.get("vip_links") if isinstance(stored.get("vip_links"), list) else []
    links = list(
        dict.fromkeys(
            str(link or "").strip() for link in [*data_links, *stored_links] if str(link or "").strip()
        )
    )
    if links:
        data["vip_links"] = links


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
            # Stays lazy on purpose: launcher → process_service →
            # process_backend → launch_service → hybrid → launcher is a
            # cycle at import time.
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
            # Stays lazy on purpose: see duplicate_pids_for_tracker above.
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

    @staticmethod
    def _hydrate_launch_record(record: dict[str, Any]) -> tuple[dict[str, Any], str]:
        data = dict(record or {})
        username = str(data.get("username") or "").strip()
        if not username:
            return data, username
        stored = _stored_account_record(username)
        if not stored:
            return data, username
        _fill_launch_fields(data, stored)
        _merge_launch_links(data, stored)
        return data, username

    @staticmethod
    def _resolve_launch_cookie(data: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        cookie = str(data.get("cookie") or "")
        if not cookie and data.get("encrypted_cookie"):
            cookie = decrypt_cookie(str(data.get("encrypted_cookie") or ""), strict=False)
            if not cookie:
                return "", {"ok": False, "fatal": True, "msg": COOKIE_UNREADABLE_REASON}
        if not cookie:
            return "", {"ok": False, "msg": "No .ROBLOSECURITY cookie for account"}
        return cookie, None

    @classmethod
    def _prepare_instances(cls, browser_tracker_id: str, multi_roblox: bool) -> tuple[bool, str, dict[str, Any]] | dict[str, Any]:
        if multi_roblox:
            guard_ok, guard_detail = ensure_multi_roblox_guard()
            if not guard_ok:
                return {"ok": False, "fatal": True, "msg": f"Multi Roblox guard failed: {guard_detail}"}
            return guard_ok, guard_detail, cls.kill_duplicate_instances(browser_tracker_id)
        release_multi_roblox_guard()
        # Stays lazy on purpose: launcher → process_service → process_backend
        # → launch_service → hybrid → launcher is a cycle at import time.
        from services.process_service import ProcessManager

        killed_count = ProcessManager.kill_all_roblox_clients(wait_seconds=2.5)
        return False, "", {"ok": True, "killed": [], "count": int(killed_count), "all_instances_closed": int(killed_count)}

    @classmethod
    def launch_record(
        cls, record: dict[str, Any], target: dict[str, Any] | None = None, multi_roblox: bool = True
    ) -> dict[str, Any]:
        data, username = cls._hydrate_launch_record(record)
        cookie, cookie_error = cls._resolve_launch_cookie(data)
        if cookie_error:
            return cookie_error
        identity = validate_record_cookie_identity(data, cookie, update_store=True)
        if not identity.get("ok"):
            return {"ok": False, "fatal": True, "msg": identity.get("msg", "cookie identity mismatch"), **identity}
        browser_tracker_id = _browser_tracker_id(target, data)
        instance_setup = cls._prepare_instances(browser_tracker_id, multi_roblox)
        if isinstance(instance_setup, dict):
            return instance_setup
        guard_ok, guard_detail, close_result = instance_setup
        client = RobloxHTTP(cookie)
        plan, plan_error = cls._resolve_launch_plan(data, dict(target or {}), identity, username, client, cookie)
        if plan_error:
            return plan_error
        return cls._start_client(
            data,
            plan,
            identity,
            browser_tracker_id,
            client,
            guard_ok,
            guard_detail,
            close_result,
            multi_roblox,
        )

    @classmethod
    def _resolve_launch_plan(
        cls,
        data: dict[str, Any],
        target: dict[str, Any],
        identity: dict[str, Any],
        username: str,
        client: RobloxHTTP,
        cookie: str,
    ) -> tuple[_LaunchPlan, dict[str, Any] | None]:
        """Resolve place/job/VIP inputs into a plan, or return a fatal response."""
        inputs = _launch_inputs(data, target)
        plan = inputs.plan
        _apply_http_job_id(plan)
        auto_private = inputs.auto_private
        if _apply_shared_direct(plan, inputs.shared_direct):
            auto_private = False
        if auto_private:
            error = cls._plan_auto_private(
                plan, data, target, identity, username, client, cookie, inputs.links, inputs.global_vip_link
            )
        else:
            if not plan.vip_link:
                plan.vip_link = _first_link(inputs.links) or inputs.global_vip_link
            error = None
        if not error:
            error = cls._resolve_plan_vip(plan, cookie)
        if error:
            return plan, error
        if _missing_launch_target(plan):
            return plan, {"ok": False, "msg": "PlaceId, JobId, VIP link, or follow user is required"}
        return plan, None

    @classmethod
    def _plan_auto_private(
        cls,
        plan: _LaunchPlan,
        data: dict[str, Any],
        target: dict[str, Any],
        identity: dict[str, Any],
        username: str,
        client: RobloxHTTP,
        cookie: str,
        links: list[str],
        global_vip_link: str,
    ) -> dict[str, Any] | None:
        """Create or reuse an owned private server; returns a fatal response or None."""
        plan.auto_private_enabled = True
        owner_id = str(identity.get("cookie_user_id") or data.get("cookie_user_id") or "")
        link_candidates = _link_candidates(plan.vip_link, links, global_vip_link)
        plan.place_id = _place_from_candidates(link_candidates, plan.place_id)
        if not plan.place_id:
            return {
                "ok": False,
                "fatal": True,
                "mode": "vip",
                "vip_resolved": False,
                "msg": "Auto Create Private Server requires Place ID or a VIP link with Place ID",
            }
        free_only = bool(
            target.get(
                "auto_create_private_server_free_only",
                data.get("auto_create_private_server_free_only", True),
            )
        )
        known_private_servers = list(data.get("owned_private_servers") or [])
        # A game that had no private servers recently will not have one now
        # either. Skip the three Roblox calls and reuse the stored verdict,
        # otherwise every relaunch re-probes the same fact.
        known_private_servers = _remember_candidate_servers(
            known_private_servers, link_candidates, plan.place_id, owner_id
        )
        cached_unavailable = remembered_vip_unavailable(known_private_servers, plan.place_id)
        private_result = _ensure_private_server(
            client, owner_id, plan.place_id, free_only, known_private_servers, cached_unavailable
        )
        if not private_result.get("ok"):
            return cls._handle_private_failure(
                plan, data, identity, username, private_result, cached_unavailable, link_candidates
            )
        return cls._apply_private_success(plan, data, username, private_result, cookie)

    @classmethod
    def _handle_private_failure(
        cls,
        plan: _LaunchPlan,
        data: dict[str, Any],
        identity: dict[str, Any],
        username: str,
        private_result: dict[str, Any],
        cached_unavailable: str,
        link_candidates: list[str],
    ) -> dict[str, Any] | None:
        """Persist a failed create and either fall back to a usable link or stop."""
        failure_msg = str(private_result.get("msg") or "private server creation failed")
        if not cached_unavailable:
            cls._record_private_failure(data, identity, username, plan.place_id, private_result, failure_msg)
        # One game in the pool having no private servers must not sink the
        # whole run: with multi-game setups the other maps still work. Only
        # "VIP disabled / paid" is permanent, so give up on CREATING our own
        # room.
        #
        # Careful: a permanent create failure does NOT mean the account cannot
        # use a private server. A VIP link pasted from a friend still joins
        # that exact room even when we are not allowed to create one (paid /
        # free-only mode). So keep the best usable link and only drop to
        # public when there is genuinely nothing to join.
        if not vip_unavailable_permanently(failure_msg):
            return {
                "ok": False,
                "fatal": True,
                "mode": "vip",
                "vip_resolved": False,
                "msg": failure_msg,
                "auto_private_server": True,
            }
        plan.auto_private_enabled = False
        plan.private_fallback_reason = failure_msg
        fallback_link = _pick_fallback_link(link_candidates, plan.place_id)
        if fallback_link:
            plan.vip_link = fallback_link
            plan.vip_resolution = {}
            plan.vip_resolved = False
            _log_private_public_fallback(username, plan.place_id, failure_msg, joined_existing_vip=True)
        else:
            plan.vip_link = ""
            _log_private_public_fallback(username, plan.place_id, failure_msg)
        return None

    @classmethod
    def _record_private_failure(
        cls,
        data: dict[str, Any],
        identity: dict[str, Any],
        username: str,
        place_id: str,
        private_result: dict[str, Any],
        failure_msg: str,
    ) -> None:
        owner_id = str(identity.get("cookie_user_id") or data.get("cookie_user_id") or "")
        ACCOUNT_STORE.update_record(
            username,
            {
                "owned_private_servers": _merge_owned_private_server(
                    list(data.get("owned_private_servers") or []),
                    {
                        "owner_user_id": owner_id,
                        "place_id": place_id,
                        "universe_id": str(private_result.get("universe_id") or ""),
                        "status": "error",
                        "error": failure_msg,
                        "synced_at": time.time(),
                    },
                )
            },
        )

    @classmethod
    def _apply_private_success(
        cls,
        plan: _LaunchPlan,
        data: dict[str, Any],
        username: str,
        private_result: dict[str, Any],
        cookie: str,
    ) -> dict[str, Any] | None:
        private_server_meta = dict(private_result)
        plan.private_server_meta = private_server_meta
        plan.auto_private_enabled = True
        plan.vip_link = str(private_server_meta.get("link") or "").strip()
        plan.vip_resolution = _private_resolution(private_server_meta, plan.place_id)
        if not plan.vip_link:
            plan.vip_link = build_owned_private_server_link(plan.place_id, private_server_meta)
        error = _resolve_private_invite(plan, cookie)
        if error:
            return error
        plan.vip_resolved = True
        plan.place_id = plan.place_id or str(plan.vip_resolution.get("place_id") or "")
        _store_private_success(data, username, plan)
        return None

    @staticmethod
    def _resolve_plan_vip(plan: _LaunchPlan, cookie: str) -> dict[str, Any] | None:
        """Resolve a join code for a plain VIP link; returns a fatal response or None."""
        if not plan.vip_link:
            return None
        if not plan.vip_resolution.get("ok"):
            plan.vip_resolution = resolve_vip_access_code(cookie, plan.vip_link)
            if not plan.vip_resolution.get("ok"):
                return {
                    "ok": False,
                    "fatal": True,
                    "msg": str(plan.vip_resolution.get("msg") or "VIP invite resolve failed"),
                    "mode": "vip",
                    "vip_resolved": False,
                }
            plan.vip_resolved = True
        plan.place_id = plan.place_id or str(plan.vip_resolution.get("place_id") or "")
        return None

    @classmethod
    def _start_client(
        cls,
        data: dict[str, Any],
        plan: _LaunchPlan,
        identity: dict[str, Any],
        browser_tracker_id: str,
        client: RobloxHTTP,
        guard_ok: bool,
        guard_detail: str,
        close_result: dict[str, Any],
        multi_roblox: bool,
    ) -> dict[str, Any]:
        ok, ticket = client.get_auth_ticket()
        if not ok:
            return {"ok": False, "msg": ticket}
        launcher_url, mode, attempted_vip = build_place_launcher_url(
            place_id=plan.place_id,
            job_id=plan.job_id,
            vip_link=plan.vip_link,
            follow_user_id=plan.follow_user_id,
            browser_tracker_id=browser_tracker_id,
            vip_access_code=str(plan.vip_resolution.get("access_code") or ""),
            vip_link_code=str(plan.vip_resolution.get("link_code") or ""),
        )
        uri = build_roblox_player_uri(ticket, launcher_url, browser_tracker_id)
        open_roblox_uri(uri)
        _store_launch_record(data, plan, browser_tracker_id)
        return _launch_payload(
            plan,
            identity,
            browser_tracker_id,
            guard_ok,
            guard_detail,
            close_result,
            multi_roblox,
            mode,
            attempted_vip,
            uri,
        )


def _launch_payload(
    plan: _LaunchPlan,
    identity: dict[str, Any],
    browser_tracker_id: str,
    guard_ok: bool,
    guard_detail: str,
    close_result: dict[str, Any],
    multi_roblox: bool,
    mode: str,
    attempted_vip: bool,
    uri: str,
) -> dict[str, Any]:
    guard = {"ok": guard_ok, "detail": guard_detail} if multi_roblox else {"ok": False, "detail": "disabled"}
    return {
        "ok": True,
        "mode": mode,
        "browser_tracker_id": browser_tracker_id,
        "closed_duplicates": close_result.get("killed", []),
        "closed_instances": close_result.get("count", 0),
        "multi_roblox_guard": guard,
        "cookie_username": identity.get("cookie_username", ""),
        "cookie_user_id": identity.get("cookie_user_id", ""),
        "vip_resolved": plan.vip_resolved,
        **_vip_hash_payload(plan.vip_resolution),
        "launch_uri_preview": re.sub(r"(gameinfo:)[^+]+", r"\1[REDACTED]", uri),
        "attempted_vip": attempted_vip,
        "auto_private_server": bool(plan.auto_private_enabled),
        "private_server_public_fallback": plan.private_fallback_reason,
        **_private_server_payload(plan.private_server_meta),
        "msg": "Roblox launch requested",
    }


def _store_launch_record(data: dict[str, Any], plan: _LaunchPlan, browser_tracker_id: str) -> None:
    ACCOUNT_STORE.update_record(
        str(data.get("username") or ""),
        {
            "browser_tracker_id": browser_tracker_id,
            "last_use": time.time(),
            "place_id": plan.place_id or data.get("place_id", ""),
            "job_id": plan.job_id or data.get("job_id", ""),
        },
    )


def _vip_hash_payload(resolution: dict[str, Any]) -> dict[str, str]:
    return {
        "vip_access_code_hash": _safe_hash(str(resolution.get("access_code") or "")),
        "vip_link_code_hash": _safe_hash(str(resolution.get("link_code") or "")),
    }


def _private_server_payload(meta: dict[str, Any]) -> dict[str, str]:
    return {
        "owned_private_server_id": str(meta.get("private_server_id") or ""),
        "private_server_source": str(meta.get("source") or ""),
        "private_server_owner_user_id": str(meta.get("owner_user_id") or ""),
        "private_server_place_id": str(meta.get("place_id") or ""),
        "private_server_universe_id": str(meta.get("universe_id") or ""),
    }


@dataclass
class _LaunchPlan:
    """Resolved launch inputs; the private-server stages fill in the VIP parts."""

    place_id: str = ""
    job_id: str = ""
    vip_link: str = ""
    follow_user_id: str = ""
    vip_resolved: bool = False
    vip_resolution: dict[str, Any] = field(default_factory=dict)
    auto_private_enabled: bool = False
    private_fallback_reason: str = ""
    private_server_meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class _LaunchInputs:
    """Normalized launch inputs before VIP and private-server resolution."""

    plan: _LaunchPlan
    links: list[str]
    global_vip_link: str
    auto_private: bool
    shared_direct: str


def _pick(target: dict[str, Any], data: dict[str, Any], key: str) -> str:
    return str(target.get(key) or data.get(key) or "").strip()


def _pick_value(target: dict[str, Any], data: dict[str, Any], key: str, default: Any) -> Any:
    return target.get(key) or data.get(key) or default


def _launch_inputs(data: dict[str, Any], target: dict[str, Any]) -> _LaunchInputs:
    plan = _LaunchPlan(
        place_id=_pick(target, data, "place_id"),
        job_id=_pick(target, data, "job_id"),
        vip_link=str(target.get("vip_link") or target.get("vip_url") or "").strip(),
        follow_user_id=str(target.get("follow_user_id") or target.get("follow_user") or "").strip(),
    )
    explicit_vip_link = plan.vip_link
    links = _link_list(_pick_value(target, data, "vip_links", []))
    global_vip_link = _pick(target, data, "global_vip_link")
    auto_private = bool(
        target.get(
            "auto_create_private_server_enabled",
            data.get("auto_create_private_server_enabled", False),
        )
    )
    if not auto_private and not explicit_vip_link:
        links = []
        global_vip_link = ""
    shared_direct = _pick(target, data, "shared_direct_vip")
    return _LaunchInputs(plan, links, global_vip_link, auto_private, shared_direct)


def _apply_http_job_id(plan: _LaunchPlan) -> None:
    """A pasted private-server URL wins over the job id it was stored as."""
    if plan.job_id.startswith("http") and not plan.vip_link:
        plan.vip_link = plan.job_id
        plan.job_id = ""


def _private_resolution(meta: dict[str, Any], place_id: str) -> dict[str, Any]:
    return {
        "ok": True,
        "place_id": str(meta.get("place_id") or place_id),
        "access_code": str(meta.get("access_code") or ""),
        "link_code": str(meta.get("join_code") or meta.get("access_code") or ""),
        "source": f"owned_private_server:{meta.get('source') or 'unknown'}",
    }


def _browser_tracker_id(target: dict[str, Any] | None, data: dict[str, Any]) -> str:
    tracker_id = str((target or {}).get("browser_tracker_id") or data.get("browser_tracker_id") or "").strip()
    if tracker_id:
        return tracker_id
    return str(secrets.randbelow(75_000) + 100_000) + str(secrets.randbelow(800_000) + 100_000)


def _link_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    return list(value) if isinstance(value, list) else []


def _link_candidates(vip_link: str, links: list[str], global_vip_link: str) -> list[str]:
    return [str(vip_link or "").strip()] + [str(link or "").strip() for link in links] + [global_vip_link]


def _first_link(links: list[str]) -> str:
    return str(links[0] or "").strip() if links else ""


def _place_from_candidates(candidates: list[str], place_id: str) -> str:
    if place_id:
        return place_id
    for candidate in candidates:
        parsed_place, _link_code = parse_vip_link(candidate)
        if parsed_place:
            return parsed_place
    return ""


def _missing_launch_target(plan: _LaunchPlan) -> bool:
    return not plan.place_id and not plan.vip_link and not plan.job_id and not plan.follow_user_id


def _apply_shared_direct(plan: _LaunchPlan, shared_direct: str) -> bool:
    """Shared mode with a complete shared link joins that exact room for every account.

    Validated here as well so a stale or mismatched value can never sneak in.
    """
    if not shared_direct:
        return False
    qualified = shared_direct_vip_link(shared_direct, plan.place_id)
    if not qualified:
        return False
    plan.place_id = plan.place_id or parse_vip_components(qualified).get("place_id", "")
    plan.vip_link = qualified
    plan.vip_resolved = False
    plan.vip_resolution = {}
    return True


def _remember_candidate_servers(
    servers: list[dict[str, Any]], link_candidates: list[str], place_id: str, owner_id: str
) -> list[dict[str, Any]]:
    for candidate_link in link_candidates:
        components = parse_vip_components(candidate_link)
        candidate_place = str(components.get("place_id") or "").strip()
        candidate_link_code = str(components.get("link_code") or "").strip()
        candidate_access_code = str(components.get("access_code") or "").strip()
        if candidate_place and candidate_place != str(place_id):
            continue
        if not (candidate_link_code or candidate_access_code):
            continue
        servers.append(
            {
                "owner_user_id": owner_id,
                "place_id": str(place_id),
                "link": candidate_link,
                "join_code": candidate_link_code,
                "access_code": candidate_access_code,
            }
        )
    return servers


def _ensure_private_server(
    client: RobloxHTTP,
    owner_id: str,
    place_id: str,
    free_only: bool,
    known_servers: list[dict[str, Any]],
    cached_unavailable: str,
) -> dict[str, Any]:
    if cached_unavailable:
        return {"ok": False, "msg": cached_unavailable, "place_id": place_id}
    return ensure_owned_private_server(
        client,
        owner_user_id=owner_id,
        place_id=place_id,
        free_only=free_only,
        known_servers=known_servers,
    )


def _resolve_private_invite(plan: _LaunchPlan, cookie: str) -> dict[str, Any] | None:
    """Fill the access code for an owned-server link, or return a fatal response."""
    if plan.vip_resolution.get("access_code") or not plan.vip_link:
        return None
    resolution = resolve_vip_access_code(cookie, plan.vip_link)
    if not resolution.get("ok"):
        return {
            "ok": False,
            "fatal": True,
            "msg": str(resolution.get("msg") or "Owned private server invite resolve failed"),
            "mode": "vip",
            "vip_resolved": False,
            "auto_private_server": True,
        }
    plan.vip_resolution = resolution
    return None


def _vip_link_has_code(link: str) -> bool:
    components = parse_vip_components(link)
    return bool(components.get("link_code") or components.get("access_code"))


def _store_private_success(data: dict[str, Any], username: str, plan: _LaunchPlan) -> None:
    owned_servers = _merge_owned_private_server(
        list(data.get("owned_private_servers") or []), plan.private_server_meta
    )
    updated_vip_links = list(data.get("vip_links") or [])
    if plan.vip_link and _vip_link_has_code(plan.vip_link) and plan.vip_link not in updated_vip_links:
        updated_vip_links.insert(0, plan.vip_link)
    ACCOUNT_STORE.update_record(
        username,
        {
            "owned_private_servers": owned_servers,
            "vip_links": updated_vip_links,
            "place_id": plan.place_id or data.get("place_id", ""),
        },
    )
