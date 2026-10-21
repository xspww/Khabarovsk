from __future__ import annotations

import random
import re
import time
import urllib.parse
from typing import Any

from core import Account, ServerType, account_launch_block_reason, flog, flog_kv
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
        try:
            from domain.games import ensure_games_migrated, game_for_account
        except Exception:
            return None, []
        try:
            snap = cfg_mgr.snapshot() if hasattr(cfg_mgr, "snapshot") else {}
        except Exception:
            snap = {}
        if not isinstance(snap, dict):
            snap = {}
        try:
            games = ensure_games_migrated(snap)
        except Exception:
            games = []
        if not games:
            return None, []
        try:
            from domain.games import normalize_game_mode
        except Exception:
            return None, []
        # Shared GAME mode: the pool is ignored entirely so every account
        # joins the Shared Place ID (legacy branch in build_target).
        try:
            if normalize_game_mode(snap.get("game_mode", "shared")) != "per_account":
                return None, []
        except Exception:
            return None, []
        game_id = str((body or {}).get("game_id") or (record or {}).get("game_id") or "")
        place_hint = str((body or {}).get("place_id") or (record or {}).get("place_id") or "")
        try:
            return game_for_account(game_id, place_hint, games), games
        except Exception:
            return None, games

    @staticmethod
    def build_target(body: dict[str, Any], record: dict[str, Any], cfg_mgr: Any) -> dict[str, Any]:
        target = dict(body or {})
        game, games = AccountLaunchService._resolve_game(record, target, cfg_mgr)
        try:
            from domain.games import effective_auto_flags, effective_global_vip, effective_place
        except Exception:
            game, games = None, []
        if games and game is not None:
            # Per-account mode with an assigned game: explicit per-account
            # values win and the game's own VIP/flags are authoritative.
            place_id = str(
                target.get("place_id")
                or effective_place(record.get("place_id"), game, "")
            ).strip()
            vip_links = vip_links_for_place(list(record.get("vip_links") or []), place_id)
            target.setdefault("vip_links", vip_links)
            target["place_id"] = place_id
            flags = effective_auto_flags(
                game,
                cfg_mgr.get("auto_create_private_server_enabled", False),
                cfg_mgr.get("auto_create_private_server_free_only", True),
            )
            # Assigned game only here (game is not None): unmatched accounts
            # fall into the "No game" branch below and stay target-less.
            global_vip = effective_global_vip(
                game,
                "",
                place_id,
                parse_vip_place=lambda url: parse_vip_link(url)[0],
            )
            # Multi-game mode: the game's own VIP/flags are authoritative.
            # The dashboard still sends legacy global values in the body, so
            # overwrite them instead of setdefault — otherwise game-1's VIP
            # would leak into every other game's manual launch.
            target["global_vip_link"] = global_vip
            if game is not None:
                target["game_id"] = game.get("id")
            target["auto_create_private_server_enabled"] = flags["enabled"]
            target["auto_create_private_server_free_only"] = flags["free_only"]
            return target
        if games:
            # Per-account mode, unassigned account ("No game"): only an
            # explicit per-account target may launch. The Shared keys are
            # deliberately NOT a fallback here — otherwise clearing the
            # game would silently relaunch the previous/shared place.
            place_id = str(
                target.get("place_id")
                or record.get("place_id")
                or ""
            ).strip()
            vip_links = vip_links_for_place(list(record.get("vip_links") or []), place_id)
            target.setdefault("vip_links", vip_links)
            target["place_id"] = place_id
            # The dashboard sends the Shared global link in the body; drop
            # it when this account has no target of its own.
            target["global_vip_link"] = _checked_global_vip_link(
                str(target.get("global_vip_link") or ""), place_id
            ) if place_id else ""
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
        place_id = str(target.get("place_id") or cfg_mgr.get("game_place_id", "") or record.get("place_id") or "").strip()
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
        if result.get("ok") and window_settings and (window_settings.get("enabled") or window_settings.get("arrange_enabled")):
            try:
                # Stays lazy on purpose: process_service → process_backend →
                # launch_service → hybrid forms a cycle at import time.
                from services.process_service import ProcessService

                if window_settings.get("arrange_enabled"):
                    resize_result = ProcessService.arrange_roblox_windows(
                        int(window_settings["width"]),
                        int(window_settings["height"]),
                        int(window_settings.get("arrange_columns") or 6),
                        int(window_settings.get("arrange_gap") or 0),
                        int(window_settings.get("arrange_margin") or 0),
                        unlock_size=bool(window_settings.get("unlock_size_enabled", True)),
                        resize=bool(window_settings.get("enabled")),
                        rows=int(window_settings.get("arrange_rows") or 4),
                        reason="manual_account_launch",
                        idempotency_key=idempotency_key,
                    )
                else:
                    resize_result = ProcessService.resize_roblox_windows(
                        int(window_settings["width"]),
                        int(window_settings["height"]),
                        unlock_size=bool(window_settings.get("unlock_size_enabled", True)),
                        reason="manual_account_launch",
                        idempotency_key=idempotency_key,
                    )
                result["window_resize"] = {
                    "ok": bool(resize_result.get("ok", True)),
                    "resized": int(resize_result.get("resized") or 0),
                    "arranged": int(resize_result.get("arranged") or 0),
                    "count": int(resize_result.get("count") or 0),
                    "width": window_settings["width"],
                    "height": window_settings["height"],
                }
            except Exception as exc:
                flog_kv("WINDOW", "manual_launch_resize_failed", "warning", account=username, error=str(exc))
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

def build_launch_url(cls, acc: Account) -> tuple[str, ServerType, str]:
    try:
        from domain.games import effective_place, game_for_account, normalize_game_mode

        _mode = "shared"
        try:
            _mode = normalize_game_mode(getattr(cls, "GAME_MODE", "shared"))
        except Exception:
            _mode = "shared"
        _snap = list(getattr(cls, "GAMES_SNAPSHOT", []) or []) if _mode == "per_account" else []
        _game = game_for_account(
            getattr(acc, "game_id", ""), getattr(acc, "place_id", ""),
            _snap,
        )
        # Per-account mode with a pool: unassigned means "No game", not
        # "use the Shared Place ID". The legacy fallback only applies to
        # Shared mode or a truly empty pool.
        _pool_active = _mode == "per_account" and bool(_snap)
        _shared = "" if _pool_active else str(getattr(cls, "SHARED_PLACE_ID", "") or "").strip()
    except Exception:
        _game = None
        _shared = ""
    _place = ""
    try:
        _place = str(effective_place(getattr(acc, "place_id", ""), _game, _shared)).strip()
    except Exception:
        _place = str(getattr(acc, "place_id", "") or "")
    use_public_fallback = bool(
        _place and
        acc.vip_links and
        int(acc.launch_fail_count or 0) >= 2
    )
    if acc.vip_links and hasattr(acc, '_vip_tracker') and acc._vip_tracker:
        vip_url = acc._vip_tracker.pick()
    elif acc.vip_links:
        vip_url = acc.active_vip or random.choice(acc.vip_links)
    else:
        vip_url = ""

    if vip_url and not use_public_fallback:
        place_id, link_code = cls.parse_vip_link(vip_url)
        if not place_id and link_code and _place:
            place_id = _place
        if place_id and link_code:
            url = (
                f"roblox://experiences/start"
                f"?placeId={place_id}"
                f"&linkCode={link_code}"
                f"&launchData="
            )
            acc.active_vip = vip_url
            return url, ServerType.VIP, vip_url
        elif place_id and not link_code:
            acc.active_vip = ""
            url = f"roblox://experiences/start?placeId={place_id}"
            return url, ServerType.PUBLIC, ""

    if use_public_fallback:
        acc.launch_strategy = "public_fallback"
    elif vip_url:
        acc.launch_strategy = "vip_preferred"
    else:
        acc.launch_strategy = "public_only"

    if _place:
        url = f"roblox://experiences/start?placeId={_place}"
        return url, ServerType.PUBLIC, ""

    return "", ServerType.UNKNOWN, ""

def launch(cls, acc: Account) -> tuple[bool, str, str]:
    block_reason = account_launch_block_reason(acc)
    if block_reason:
        flog(f"[LAUNCH] Blocked for {acc.display_name}: {block_reason}", "warning")
        return False, block_reason, ""

    if str(getattr(acc, "cookie", "") or "").strip():
        try:
            from domain.games import (
                effective_auto_flags,
                effective_global_vip,
                effective_place,
                game_for_account,
                normalize_game_mode,
            )

            game = None
            try:
                _mode = "shared"
                try:
                    _mode = normalize_game_mode(getattr(cls, "GAME_MODE", "shared"))
                except Exception:
                    _mode = "shared"
                _snap = list(getattr(cls, "GAMES_SNAPSHOT", []) or []) if _mode == "per_account" else []
                game = game_for_account(
                    getattr(acc, "game_id", ""), getattr(acc, "place_id", ""), _snap
                )
            except Exception:
                game = None
            # Only Shared mode (or a legacy empty pool) falls back to the
            # Shared Place ID. In per-account mode an unmatched account is
            # "No game" and must stay target-less.
            _pool_active = _mode == "per_account" and bool(_snap)
            _shared_fb = ""
            if game is None and not _pool_active:
                try:
                    _shared_fb = str(getattr(cls, "SHARED_PLACE_ID", "") or "").strip()
                except Exception:
                    _shared_fb = ""
            target_place = str(effective_place(getattr(acc, "place_id", ""), game, _shared_fb)).strip()
            if game is not None:
                # Master switch: the Game card toggle also applies here, so a
                # pool game added with the per-game flag off still auto-creates.
                flags = effective_auto_flags(
                    game,
                    cls.AUTO_CREATE_PRIVATE_SERVER_ENABLED,
                    cls.AUTO_CREATE_PRIVATE_SERVER_FREE_ONLY,
                )
                auto_private_enabled = bool(flags["enabled"])
                auto_private_free_only = bool(flags["free_only"])
            else:
                auto_private_enabled = bool(cls.AUTO_CREATE_PRIVATE_SERVER_ENABLED)
                auto_private_free_only = bool(cls.AUTO_CREATE_PRIVATE_SERVER_FREE_ONLY)
            active_vip = str(acc.active_vip or "") if auto_private_enabled else ""
            if target_place and active_vip:
                active_place, _active_code = cls.parse_vip_link(active_vip)
                if active_place and active_place != target_place:
                    active_vip = ""
            vip_links = vip_links_for_place(list(acc.vip_links or []), target_place) if auto_private_enabled else []
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
                    global_vip = "" if _pool_active else cls.GLOBAL_VIP_LINK
            global_place = cls.parse_vip_link(global_vip)[0] if global_vip else ""
            if target_place and global_place and global_place != target_place:
                global_vip = ""
            # Shared-direct VIP: Shared mode (game is None) with a complete
            # shared link (place + link/access code) joins that exact room
            # for every account, regardless of the auto-create toggle.
            shared_direct_vip = ""
            if game is None and not _pool_active:
                shared_direct_vip = shared_direct_vip_link(str(cls.GLOBAL_VIP_LINK or ""), target_place)
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
                "game_id": str((game or {}).get("id") or getattr(acc, "game_id", "") or ""),
                "vip_links": vip_links,
                "global_vip_link": global_vip,
                "shared_direct_vip": shared_direct_vip,
                "browser_tracker_id": getattr(acc, "browser_tracker_id", ""),
                "auto_create_private_server_enabled": auto_private_enabled,
                "auto_create_private_server_free_only": bool(auto_private_free_only),
            }
            result = HybridLauncher.launch_record(record, target=target, multi_roblox=bool(cls.MULTI_ROBLOX_ENABLED))
            if result.get("ok"):
                acc.browser_tracker_id = str(result.get("browser_tracker_id") or getattr(acc, "browser_tracker_id", "") or "")
                flog_kv(
                    "LAUNCH",
                    "browser_tracker_assigned",
                    account=acc.display_name,
                    browser_tracker_id=tracker_label(acc.browser_tracker_id),
                    mode=str(result.get("mode") or ""),
                )
                mode = str(result.get("mode") or "")
                attempted_vip_hybrid = str(result.get("attempted_vip") or "")
                if mode == "vip":
                    attempted_vip_hybrid = attempted_vip_hybrid or str(acc.active_vip or "")
                    acc.server_type = ServerType.VIP
                    acc.active_vip = attempted_vip_hybrid
                elif mode in {"job", "public"}:
                    acc.server_type = ServerType.PUBLIC
                    acc.active_vip = ""
                acc.last_launch_at = time.time()
                return True, str(result.get("msg") or "Launched via auth ticket"), attempted_vip_hybrid
            flog(f"[LAUNCH] Auth-ticket launch failed for {acc.display_name}: {result.get('msg')}", "warning")
            if result.get("fatal") or bool(cls.MULTI_ROBLOX_ENABLED):
                return False, str(result.get("msg") or "Auth-ticket launch blocked"), ""
        except Exception as e:
            flog(f"[LAUNCH] Auth-ticket launch path errored for {acc.display_name}: {e}", "warning")
            if bool(cls.MULTI_ROBLOX_ENABLED):
                return False, str(e), ""

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
