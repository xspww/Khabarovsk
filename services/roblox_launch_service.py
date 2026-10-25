from __future__ import annotations

import random
import re
import time
import urllib.parse
from typing import Any

from core import Account, ServerType, account_launch_block_reason, flog, flog_kv
from domain._util import as_int as _window_int
from domain.roblox_private_servers import shared_direct_vip_link, vip_links_for_place
from services.browser_tracker import tracker_label
from services.roblox_http import open_roblox_uri
from services.roblox_launcher import HybridLauncher


def parse_vip_link(vip_url: str) -> tuple[str, str]:
    # Canonical parser lives in domain.roblox_private_servers (single owner).
    # This wrapper preserves the public import path + warning logs so
    # ProcessManager.parse_vip_link / accounts_routes callers see no change.
    if not vip_url:
        return "", ""
    try:
        from domain.roblox_private_servers import parse_vip_link as _canonical_parse
    except Exception:
        _canonical_parse = None
    try:
        if _canonical_parse is not None:
            place_id, link_code = _canonical_parse(vip_url)
        else:
            parsed = urllib.parse.urlparse(vip_url.strip())
            qs = urllib.parse.parse_qs(parsed.query)
            m = re.search(r"/games/(\d+)", parsed.path)
            place_id = m.group(1) if m else qs.get("placeId", [""])[0]
            link_code = (
                qs.get("privateServerLinkCode", [""])[0] or
                qs.get("linkCode", [""])[0] or
                qs.get("code", [""])[0]
            )
        if not place_id:
            flog("[VIP] Could not parse place_id from configured VIP link", "warning")
        if not link_code:
            flog("[VIP] No linkCode in configured VIP link", "warning")
        return place_id, link_code
    except Exception as e:
        flog(f"[VIP] parse error: {e}", "warning")
        return "", ""


def _checked_global_vip_link(global_vip: str, place_id: str) -> str:
    """Single owner for the shared-global VIP place check.

    The two shared/legacy target branches used to repeat this inline; the
    launch-target decision now leverages this one. A global link naming a
    different place is dropped, otherwise it passes through.
    """
    link = str(global_vip or "").strip()
    if not link:
        return ""
    place = str(place_id or "").strip()
    link_place = parse_vip_link(link)[0] if link else ""
    return link if (not place or not link_place or link_place == place) else ""


def _finish_shared_target(
    target: dict[str, Any],
    place_id: str,
    vip_links: list[str],
    cfg_mgr: Any,
) -> dict[str, Any]:
    """Single owner for the shared/legacy target tail.

    Both non-assigned branches resolve the place differently but finish the
    same way: filtered VIP links, place-checked global link, dashboard
    defaults. Only the assigned-game branch above stays separate — the
    game's own VIP/flags overwrite instead of setdefault there.
    """
    target.setdefault("vip_links", vip_links)
    target["place_id"] = place_id
    target.setdefault(
        "global_vip_link",
        _checked_global_vip_link(str(cfg_mgr.get("game_private_server_url", "") or ""), place_id),
    )
    target.setdefault("auto_create_private_server_enabled", cfg_mgr.get("auto_create_private_server_enabled", False))
    target.setdefault("auto_create_private_server_free_only", cfg_mgr.get("auto_create_private_server_free_only", True))
    return target


def _snapshot_dict(cfg_mgr: Any) -> dict[str, Any]:
    try:
        snap = cfg_mgr.snapshot() if hasattr(cfg_mgr, "snapshot") else {}
    except Exception:
        snap = {}
    return snap if isinstance(snap, dict) else {}


def _migrated_games(snap: dict[str, Any]) -> list[Any]:
    try:
        from domain.games import ensure_games_migrated

        return ensure_games_migrated(snap)
    except Exception:
        return []


def _game_mode(snap: dict[str, Any]) -> str:
    try:
        from domain.games import normalize_game_mode

        return normalize_game_mode(snap.get("game_mode", "shared"))
    except Exception:
        return ""


def _launch_hint(body: dict[str, Any], record: dict[str, Any], key: str) -> str:
    return str((body or {}).get(key) or (record or {}).get(key) or "")


def _game_effective_helpers() -> tuple[Any, Any, Any] | None:
    try:
        from domain.games import (
            effective_auto_flags,
            effective_global_vip,
            effective_place,
        )

        return effective_auto_flags, effective_global_vip, effective_place
    except Exception:
        return None


def _assigned_game_target(
    target: dict[str, Any],
    record: dict[str, Any],
    game: dict[str, Any],
    cfg_mgr: Any,
    helpers: tuple[Any, Any, Any],
) -> dict[str, Any]:
    effective_auto_flags, effective_global_vip, effective_place = helpers
    # Per-account mode with an assigned game: explicit per-account values
    # win and the game's own VIP/flags are authoritative.
    place_id = str(
        target.get("place_id") or effective_place(record.get("place_id"), game, "")
    ).strip()
    vip_links = vip_links_for_place(list(record.get("vip_links") or []), place_id)
    target.setdefault("vip_links", vip_links)
    target["place_id"] = place_id
    flags = effective_auto_flags(
        game,
        cfg_mgr.get("auto_create_private_server_enabled", False),
        cfg_mgr.get("auto_create_private_server_free_only", True),
    )
    # Assigned game only here (game is not None): unmatched accounts fall
    # into the "No game" branch below and stay target-less.
    global_vip = effective_global_vip(
        game,
        "",
        place_id,
        parse_vip_place=lambda url: parse_vip_link(url)[0],
    )
    # Multi-game mode: the game's own VIP/flags are authoritative. The
    # dashboard still sends legacy global values in the body, so overwrite
    # them instead of setdefault — otherwise game-1's VIP would leak into
    # every other game's manual launch.
    target["global_vip_link"] = global_vip
    if game is not None:
        target["game_id"] = game.get("id")
    target["auto_create_private_server_enabled"] = flags["enabled"]
    target["auto_create_private_server_free_only"] = flags["free_only"]
    return target


def _unassigned_game_target(
    target: dict[str, Any],
    record: dict[str, Any],
    cfg_mgr: Any,
) -> dict[str, Any]:
    # Per-account mode, unassigned account ("No game"): only an explicit
    # per-account target may launch. The Shared keys are deliberately NOT a
    # fallback here — otherwise clearing the game would silently relaunch
    # the previous/shared place.
    place_id = str(target.get("place_id") or record.get("place_id") or "").strip()
    vip_links = vip_links_for_place(list(record.get("vip_links") or []), place_id)
    target.setdefault("vip_links", vip_links)
    target["place_id"] = place_id
    # The dashboard sends the Shared global link in the body; drop it when
    # this account has no target of its own.
    target["global_vip_link"] = (
        _checked_global_vip_link(str(target.get("global_vip_link") or ""), place_id)
        if place_id
        else ""
    )
    target["shared_direct_vip"] = ""
    target.setdefault(
        "auto_create_private_server_enabled",
        cfg_mgr.get("auto_create_private_server_enabled", False),
    )
    target.setdefault(
        "auto_create_private_server_free_only",
        cfg_mgr.get("auto_create_private_server_free_only", True),
    )
    return target


def _shared_place(
    target: dict[str, Any],
    record: dict[str, Any],
    cfg_mgr: Any,
) -> str:
    return str(
        target.get("place_id")
        or cfg_mgr.get("game_place_id", "")
        or record.get("place_id")
        or ""
    ).strip()


def _window_layout_enabled(
    result: dict[str, Any],
    window_settings: dict[str, Any] | None,
) -> bool:
    if not result.get("ok"):
        return False
    if not window_settings:
        return False
    return bool(
        window_settings.get("enabled") or window_settings.get("arrange_enabled")
    )


def _arrange_roblox_windows(
    process_service: Any,
    window_settings: dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any]:
    return process_service.arrange_roblox_windows(
        int(window_settings["width"]),
        int(window_settings["height"]),
        _window_int(window_settings, "arrange_columns", 6),
        _window_int(window_settings, "arrange_gap", 0),
        _window_int(window_settings, "arrange_margin", 0),
        unlock_size=bool(window_settings.get("unlock_size_enabled", True)),
        resize=bool(window_settings.get("enabled")),
        rows=_window_int(window_settings, "arrange_rows", 4),
        reason="manual_account_launch",
        idempotency_key=idempotency_key,
    )


def _resize_roblox_windows(
    process_service: Any,
    window_settings: dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any]:
    return process_service.resize_roblox_windows(
        int(window_settings["width"]),
        int(window_settings["height"]),
        unlock_size=bool(window_settings.get("unlock_size_enabled", True)),
        reason="manual_account_launch",
        idempotency_key=idempotency_key,
    )


def _window_resize_payload(
    resize_result: dict[str, Any],
    window_settings: dict[str, Any],
) -> dict[str, Any]:
    return {
        "ok": bool(resize_result.get("ok", True)),
        "resized": _window_int(resize_result, "resized", 0),
        "arranged": _window_int(resize_result, "arranged", 0),
        "count": _window_int(resize_result, "count", 0),
        "width": window_settings["width"],
        "height": window_settings["height"],
    }


def _apply_manual_window_settings(
    result: dict[str, Any],
    window_settings: dict[str, Any],
    idempotency_key: str,
    username: str,
) -> None:
    try:
        # Stays lazy on purpose: process_service → process_backend →
        # launch_service → hybrid forms a cycle at import time.
        from services.process_service import ProcessService

        if window_settings.get("arrange_enabled"):
            resize_result = _arrange_roblox_windows(
                ProcessService,
                window_settings,
                idempotency_key,
            )
        else:
            resize_result = _resize_roblox_windows(
                ProcessService,
                window_settings,
                idempotency_key,
            )
        result["window_resize"] = _window_resize_payload(resize_result, window_settings)
    except Exception as exc:
        flog_kv(
            "WINDOW",
            "manual_launch_resize_failed",
            "warning",
            account=username,
            error=str(exc),
        )


class AccountLaunchService:
    """Boundary for manual account launch endpoints.

    Queue launch still belongs to LaunchController. This service keeps API
    routes from calling HybridLauncher or process window mutation directly.
    """

    @staticmethod
    def _resolve_game(record: dict[str, Any], body: dict[str, Any], cfg_mgr: Any):
        """Find the game entry for a launch.

        Returns (game_or_None, games_list). Empty games list means the
        legacy single-game mode where global keys still apply.
        """
        snap = _snapshot_dict(cfg_mgr)
        games = _migrated_games(snap)
        if not games:
            return None, []
        # Shared GAME mode: the pool is ignored entirely so every account
        # joins the Shared Place ID (legacy branch in build_target).
        if _game_mode(snap) != "per_account":
            return None, []
        game_id = _launch_hint(body, record, "game_id")
        place_hint = _launch_hint(body, record, "place_id")
        try:
            from domain.games import game_for_account

            return game_for_account(game_id, place_hint, games), games
        except Exception:
            return None, games

    @staticmethod
    def build_target(body: dict[str, Any], record: dict[str, Any], cfg_mgr: Any) -> dict[str, Any]:
        target = dict(body or {})
        game, games = AccountLaunchService._resolve_game(record, target, cfg_mgr)
        helpers = _game_effective_helpers()
        if helpers is None:
            game, games = None, []
        if games and game is not None:
            return _assigned_game_target(target, record, game, cfg_mgr, helpers)
        if games:
            return _unassigned_game_target(target, record, cfg_mgr)
        place_id = _shared_place(target, record, cfg_mgr)
        vip_links = vip_links_for_place(list(record.get("vip_links") or []), place_id)
        return _finish_shared_target(target, place_id, vip_links, cfg_mgr)

    @staticmethod
    def launch_record(
        record: dict[str, Any],
        body: dict[str, Any],
        cfg_mgr: Any,
        window_settings: dict[str, Any] | None = None,
        idempotency_key: str = "",
        body_hash: str = "",
    ) -> dict[str, Any]:
        username = str(record.get("username") or "")
        multi_roblox = bool(body.get("multi_roblox", cfg_mgr.get("multi_roblox_enabled", True)))
        result = HybridLauncher.launch_record(
            record,
            target=AccountLaunchService.build_target(body, record, cfg_mgr),
            multi_roblox=multi_roblox,
        )
        flog_kv(
            "LAUNCH",
            "manual_account_launch",
            account=username,
            ok=bool(result.get("ok")),
            mode=result.get("mode", ""),
            detail=result.get("msg", ""),
            launch_action="manual_account_launch",
            idempotency_key=idempotency_key,
            idempotency_body_hash=body_hash,
        )
        if _window_layout_enabled(result, window_settings):
            _apply_manual_window_settings(
                result,
                window_settings,
                idempotency_key,
                username,
            )
        return result

    @staticmethod
    def kill_duplicate_instances(
        record: dict[str, Any],
        idempotency_key: str = "",
        body_hash: str = "",
    ) -> dict[str, Any]:
        username = str(record.get("username") or "")
        tracker = str(record.get("browser_tracker_id") or "")
        result = HybridLauncher.kill_duplicate_instances(tracker)
        flog_kv(
            "LAUNCH",
            "manual_kill_duplicate",
            account=username,
            ok=bool(result.get("ok")),
            killed=result.get("killed", []),
            launch_action="kill_duplicate_instances",
            idempotency_key=idempotency_key,
            idempotency_body_hash=body_hash,
        )
        return result

def _launch_context(cls: Any, acc: Account) -> tuple[dict[str, Any] | None, str]:
    try:
        from domain.games import game_for_account, normalize_game_mode

        mode = "shared"
        try:
            mode = normalize_game_mode(getattr(cls, "GAME_MODE", "shared"))
        except Exception:
            mode = "shared"
        snap = (
            list(getattr(cls, "GAMES_SNAPSHOT", []) or [])
            if mode == "per_account"
            else []
        )
        game = game_for_account(
            getattr(acc, "game_id", ""),
            getattr(acc, "place_id", ""),
            snap,
        )
        # Per-account mode with a pool: unassigned means "No game", not
        # "use the Shared Place ID". The legacy fallback only applies to
        # Shared mode or a truly empty pool.
        pool_active = mode == "per_account" and bool(snap)
        shared = (
            ""
            if pool_active
            else str(getattr(cls, "SHARED_PLACE_ID", "") or "").strip()
        )
        return game, shared
    except Exception:
        return None, ""


def _launch_place(acc: Account, game: dict[str, Any] | None, shared: str) -> str:
    try:
        from domain.games import effective_place

        return str(
            effective_place(getattr(acc, "place_id", ""), game, shared)
        ).strip()
    except Exception:
        return str(getattr(acc, "place_id", "") or "")


def _choose_vip_url(acc: Account) -> str:
    if acc.vip_links and hasattr(acc, "_vip_tracker") and acc._vip_tracker:
        return acc._vip_tracker.pick()
    if acc.vip_links:
        return acc.active_vip or random.choice(acc.vip_links)
    return ""


def _vip_place_code(cls: Any, vip_url: str, fallback_place: str) -> tuple[str, str]:
    place_id, link_code = cls.parse_vip_link(vip_url)
    if not place_id and link_code and fallback_place:
        place_id = fallback_place
    return place_id, link_code


def _vip_launch_target(
    cls: Any,
    acc: Account,
    vip_url: str,
    place: str,
    use_public_fallback: bool,
) -> tuple[str, ServerType, str] | None:
    if not vip_url or use_public_fallback:
        return None
    place_id, link_code = _vip_place_code(cls, vip_url, place)
    if place_id and link_code:
        acc.active_vip = vip_url
        return (
            "roblox://experiences/start"
            f"?placeId={place_id}&linkCode={link_code}&launchData=",
            ServerType.VIP,
            vip_url,
        )
    if place_id and not link_code:
        acc.active_vip = ""
        return f"roblox://experiences/start?placeId={place_id}", ServerType.PUBLIC, ""
    return None


def _launch_strategy(use_public_fallback: bool, vip_url: str) -> str:
    if use_public_fallback:
        return "public_fallback"
    if vip_url:
        return "vip_preferred"
    return "public_only"


def build_launch_url(cls, acc: Account) -> tuple[str, ServerType, str]:
    game, shared = _launch_context(cls, acc)
    place = _launch_place(acc, game, shared)
    use_public_fallback = bool(
        place and acc.vip_links and int(acc.launch_fail_count or 0) >= 2
    )
    vip_url = _choose_vip_url(acc)
    vip_target = _vip_launch_target(cls, acc, vip_url, place, use_public_fallback)
    if vip_target is not None:
        return vip_target
    acc.launch_strategy = _launch_strategy(use_public_fallback, vip_url)
    if place:
        return f"roblox://experiences/start?placeId={place}", ServerType.PUBLIC, ""
    return "", ServerType.UNKNOWN, ""

def _auth_game(
    cls: Any,
    acc: Account,
    game_for_account: Any,
    normalize_game_mode: Any,
) -> tuple[dict[str, Any] | None, bool]:
    mode = "shared"
    snap: list[Any] = []
    game = None
    try:
        mode = normalize_game_mode(getattr(cls, "GAME_MODE", "shared"))
        snap = (
            list(getattr(cls, "GAMES_SNAPSHOT", []) or [])
            if mode == "per_account"
            else []
        )
        game = game_for_account(
            getattr(acc, "game_id", ""),
            getattr(acc, "place_id", ""),
            snap,
        )
    except Exception:
        game = None
    # Only Shared mode (or a legacy empty pool) falls back to the Shared
    # Place ID. In per-account mode an unmatched account is "No game" and
    # must stay target-less.
    pool_active = mode == "per_account" and bool(snap)
    return game, pool_active


def _auth_shared_fallback(cls: Any, game: Any, pool_active: bool) -> str:
    if game is not None or pool_active:
        return ""
    try:
        return str(getattr(cls, "SHARED_PLACE_ID", "") or "").strip()
    except Exception:
        return ""


def _auth_private_flags(
    cls: Any,
    game: Any,
    effective_auto_flags: Any,
) -> tuple[bool, bool]:
    if game is not None:
        # Master switch: the Game card toggle also applies here, so a pool
        # game added with the per-game flag off still auto-creates.
        flags = effective_auto_flags(
            game,
            cls.AUTO_CREATE_PRIVATE_SERVER_ENABLED,
            cls.AUTO_CREATE_PRIVATE_SERVER_FREE_ONLY,
        )
        return bool(flags["enabled"]), bool(flags["free_only"])
    return (
        bool(cls.AUTO_CREATE_PRIVATE_SERVER_ENABLED),
        bool(cls.AUTO_CREATE_PRIVATE_SERVER_FREE_ONLY),
    )


def _auth_active_vip(
    cls: Any,
    acc: Account,
    target_place: str,
    auto_private_enabled: bool,
) -> str:
    active_vip = str(acc.active_vip or "") if auto_private_enabled else ""
    if target_place and active_vip:
        active_place, _active_code = cls.parse_vip_link(active_vip)
        if active_place and active_place != target_place:
            active_vip = ""
    return active_vip


def _auth_global_vip(
    cls: Any,
    game: Any,
    target_place: str,
    pool_active: bool,
    auto_private_enabled: bool,
    effective_global_vip: Any,
) -> str:
    global_vip = ""
    if auto_private_enabled:
        if game is not None:
            try:
                global_vip = effective_global_vip(
                    game,
                    str(cls.GLOBAL_VIP_LINK or ""),
                    target_place,
                    parse_vip_place=lambda url: cls.parse_vip_link(url)[0],
                )
            except Exception:
                global_vip = ""
        else:
            global_vip = "" if pool_active else cls.GLOBAL_VIP_LINK
    global_place = cls.parse_vip_link(global_vip)[0] if global_vip else ""
    if target_place and global_place and global_place != target_place:
        global_vip = ""
    return global_vip


def _auth_shared_direct(
    cls: Any,
    game: Any,
    pool_active: bool,
    target_place: str,
) -> str:
    # Shared-direct VIP: Shared mode (game is None) with a complete shared
    # link (place + link/access code) joins that exact room for every
    # account, regardless of the auto-create toggle.
    if game is not None or pool_active:
        return ""
    return shared_direct_vip_link(str(cls.GLOBAL_VIP_LINK or ""), target_place)


def _auth_ticket_target(
    cls: Any,
    acc: Account,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from domain.games import (
        effective_auto_flags,
        effective_global_vip,
        effective_place,
        game_for_account,
        normalize_game_mode,
    )

    game, pool_active = _auth_game(cls, acc, game_for_account, normalize_game_mode)
    shared_fb = _auth_shared_fallback(cls, game, pool_active)
    target_place = str(
        effective_place(getattr(acc, "place_id", ""), game, shared_fb)
    ).strip()
    auto_private_enabled, auto_private_free_only = _auth_private_flags(
        cls,
        game,
        effective_auto_flags,
    )
    active_vip = _auth_active_vip(cls, acc, target_place, auto_private_enabled)
    vip_links = (
        vip_links_for_place(list(acc.vip_links or []), target_place)
        if auto_private_enabled
        else []
    )
    global_vip = _auth_global_vip(
        cls,
        game,
        target_place,
        pool_active,
        auto_private_enabled,
        effective_global_vip,
    )
    shared_direct_vip = _auth_shared_direct(cls, game, pool_active, target_place)
    target = {
        "place_id": target_place,
        "vip_links": vip_links,
        "vip_link": active_vip,
        "global_vip_link": global_vip,
        "shared_direct_vip": shared_direct_vip,
        "browser_tracker_id": getattr(acc, "browser_tracker_id", ""),
        "auto_create_private_server_enabled": auto_private_enabled,
        "auto_create_private_server_free_only": bool(auto_private_free_only),
    }
    record = {
        "username": acc.username,
        "alias": acc.alias,
        "cookie": acc.cookie,
        "place_id": target_place,
        "game_id": str(
            (game or {}).get("id") or getattr(acc, "game_id", "") or ""
        ),
        "vip_links": vip_links,
        "global_vip_link": global_vip,
        "shared_direct_vip": shared_direct_vip,
        "browser_tracker_id": getattr(acc, "browser_tracker_id", ""),
        "auto_create_private_server_enabled": auto_private_enabled,
        "auto_create_private_server_free_only": bool(auto_private_free_only),
    }
    return target, record


def _assign_browser_tracker(acc: Account, result: dict[str, Any]) -> None:
    acc.browser_tracker_id = str(
        result.get("browser_tracker_id")
        or getattr(acc, "browser_tracker_id", "")
        or ""
    )


def _apply_launch_mode(acc: Account, result: dict[str, Any]) -> str:
    mode = str(result.get("mode") or "")
    attempted_vip_hybrid = str(result.get("attempted_vip") or "")
    if mode == "vip":
        attempted_vip_hybrid = attempted_vip_hybrid or str(acc.active_vip or "")
        acc.server_type = ServerType.VIP
        acc.active_vip = attempted_vip_hybrid
    elif mode in {"job", "public"}:
        acc.server_type = ServerType.PUBLIC
        acc.active_vip = ""
    return attempted_vip_hybrid


def _accept_auth_ticket_launch(
    acc: Account,
    result: dict[str, Any],
) -> tuple[bool, str, str]:
    _assign_browser_tracker(acc, result)
    flog_kv(
        "LAUNCH",
        "browser_tracker_assigned",
        account=acc.display_name,
        browser_tracker_id=tracker_label(acc.browser_tracker_id),
        mode=str(result.get("mode") or ""),
    )
    attempted_vip_hybrid = _apply_launch_mode(acc, result)
    acc.last_launch_at = time.time()
    return (
        True,
        str(result.get("msg") or "Launched via auth ticket"),
        attempted_vip_hybrid,
    )


def _legacy_launch(cls: Any, acc: Account) -> tuple[bool, str, str]:
    url, server_type, attempted_vip = cls.build_launch_url(acc)
    if not url:
        return False, "ไม่มี place_id หรือ VIP link ที่ถูกต้อง", ""

    acc.server_type = server_type
    safe_url = re.sub(
        r"([?&](?:privateServerLinkCode|linkCode|code|accessCode|reservedServerAccessCode)=)[^&\s]+",
        r"\1<redacted>",
        url,
        flags=re.IGNORECASE,
    )
    flog(f"[LAUNCH] {acc.display_name} → {safe_url[:120]}")

    try:
        open_roblox_uri(cls.LOGIN_WARMUP_URL)
        flog(f"[LAUNCH] Warmup home for {acc.display_name}")
        time.sleep(cls.LOGIN_WARMUP_DELAY)
    except Exception as e:
        flog(f"[LAUNCH] warmup open failed: {e}", "warning")

    try:
        open_roblox_uri(url)
        acc.last_launch_at = time.time()
        return True, url, attempted_vip
    except Exception as e:
        flog(f"[LAUNCH] all methods failed for {acc.display_name}: {e}", "warning")
        return False, str(e), attempted_vip


def _try_auth_ticket_launch(
    cls: Any,
    acc: Account,
) -> tuple[bool, str, str] | None:
    if not str(getattr(acc, "cookie", "") or "").strip():
        return None
    try:
        target, record = _auth_ticket_target(cls, acc)
        result = HybridLauncher.launch_record(
            record,
            target=target,
            multi_roblox=bool(cls.MULTI_ROBLOX_ENABLED),
        )
        if result.get("ok"):
            return _accept_auth_ticket_launch(acc, result)
        flog(
            f"[LAUNCH] Auth-ticket launch failed for {acc.display_name}: {result.get('msg')}",
            "warning",
        )
        if result.get("fatal") or bool(cls.MULTI_ROBLOX_ENABLED):
            return False, str(result.get("msg") or "Auth-ticket launch blocked"), ""
    except Exception as e:
        flog(
            f"[LAUNCH] Auth-ticket launch path errored for {acc.display_name}: {e}",
            "warning",
        )
        if bool(cls.MULTI_ROBLOX_ENABLED):
            return False, str(e), ""
    return None


def launch(cls, acc: Account) -> tuple[bool, str, str]:
    block_reason = account_launch_block_reason(acc)
    if block_reason:
        flog(f"[LAUNCH] Blocked for {acc.display_name}: {block_reason}", "warning")
        return False, block_reason, ""

    auth_result = _try_auth_ticket_launch(cls, acc)
    if auth_result is not None:
        return auth_result
    return _legacy_launch(cls, acc)
