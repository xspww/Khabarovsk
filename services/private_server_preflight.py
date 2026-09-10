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

from typing import Any, Dict, List, Optional


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
    text = str(message or "").lower()
    if "disabled for this universe" in text or "private servers are disabled" in text:
        return "vip_disabled"
    if "free-only" in text or "expectedprice" in text:
        return "vip_paid"
    if "no .rob housesecurity" in text or "no cookie" in text or "cookie" in text:
        return "no_cookie"
    return "vip_error"


def run_private_server_preflight(
    cfg: Dict[str, Any],
    accounts: List[Any],
    games: List[Dict[str, Any]],
    game_mode: str = "shared",
    cfg_mgr: Any = None,
) -> Dict[str, Any]:
    """Attempt auto-create once per place. Never raises (fail-open)."""
    outcome: Dict[str, Any] = {"created": [], "failures": [], "skipped_usernames": []}
    try:
        return _run(cfg, accounts, games, game_mode, cfg_mgr, outcome)
    except Exception as exc:
        try:
            from core import flog_kv

            flog_kv("PREFLIGHT", "private_server_preflight_failed", "warning", error=exc)
        except Exception:
            pass
        return outcome


def _run(cfg, accounts, games, game_mode, cfg_mgr, outcome):
    from domain.games import effective_auto_flags, effective_place, game_for_account

    cfg = cfg if isinstance(cfg, dict) else {}
    games = list(games or [])
    per_account = str(game_mode or "shared") == "per_account"
    legacy_place = str(cfg.get("game_place_id", "") or "").strip()
    legacy_vip = str(cfg.get("game_private_server_url", "") or "").strip()
    legacy_enabled = bool(cfg.get("auto_create_private_server_enabled", False))
    legacy_free_only = bool(cfg.get("auto_create_private_server_free_only", True))

    # place_id -> {"game": dict|None, "free_only": bool, "accounts": [Account]}
    needed: Dict[str, Dict[str, Any]] = {}
    for acc in list(accounts or []):
        try:
            game = None
            if per_account and games:
                try:
                    game = game_for_account(
                        getattr(acc, "game_id", ""), getattr(acc, "place_id", ""), games
                    )
                except Exception:
                    game = None
            flags = effective_auto_flags(game, legacy_enabled, legacy_free_only)
            if not flags.get("enabled"):
                continue
            place = str(
                effective_place(
                    getattr(acc, "place_id", ""), game,
                    legacy_place if game is None else "",
                )
            ).strip()
            if not place:
                continue
            links: List[str] = []
            try:
                links.extend(list(getattr(acc, "vip_links", []) or []))
            except Exception:
                pass
            try:
                if getattr(acc, "active_vip", ""):
                    links.append(str(acc.active_vip))
            except Exception:
                pass
            if game:
                gvip = str((game or {}).get("private_server_url") or "").strip()
                if gvip:
                    links.append(gvip)
            if game is None and legacy_vip:
                links.append(legacy_vip)
            if any(_usable_link_code(link, place) for link in links):
                continue  # Has a link: use it silently, no check, no toast.
            slot = needed.get(place)
            if slot is None:
                slot = {"game": game, "free_only": bool(flags.get("free_only", True)), "accounts": []}
                needed[place] = slot
            slot["accounts"].append(acc)
        except Exception:
            continue

    if not needed:
        return outcome

    from account_hybrid import ACCOUNT_STORE

    try:
        records = ACCOUNT_STORE.read_records(include_cookies=True)
    except Exception:
        records = []
    by_name = {}
    try:
        for record in records or []:
            by_name[str(record.get("username") or "").strip().lower()] = record
    except Exception:
        pass

    try:
        from roblox_hybrid import RobloxHTTP, validate_record_cookie_identity
        from domain.roblox_private_servers import ensure_owned_private_server
    except Exception as exc:
        for place, slot in needed.items():
            outcome["failures"].append({
                "place_id": place,
                "game_name": str((slot.get("game") or {}).get("name") or ""),
                "reason": f"Preflight unavailable: {exc}",
                "reason_code": "vip_error",
                "usernames": [a.username for a in slot["accounts"]],
            })
            outcome["skipped_usernames"].extend(
                str(a.username or "").strip().lower() for a in slot["accounts"]
            )
        return outcome

    games_patch: Dict[str, str] = {}
    legacy_patch: Optional[str] = None

    for place, slot in needed.items():
        game = slot.get("game")
        game_name = str((game or {}).get("name") or (game or {}).get("id") or "")
        usernames = [str(a.username or "") for a in slot["accounts"]]
        owner_record = None
        owner_cookie = ""
        owner_uid = ""
        for acc in slot["accounts"]:
            try:
                cookie = str(getattr(acc, "cookie", "") or "").strip()
            except Exception:
                cookie = ""
            if not cookie:
                try:
                    rec = by_name.get(str(acc.username or "").strip().lower())
                    cookie = str((rec or {}).get("cookie") or "").strip()
                    if cookie and owner_record is None:
                        owner_record = rec
                except Exception:
                    cookie = ""
            if cookie:
                if owner_record is None:
                    try:
                        owner_record = by_name.get(str(acc.username or "").strip().lower())
                    except Exception:
                        owner_record = None
                owner_cookie = cookie
                break
        if not owner_cookie:
            outcome["failures"].append({
                "place_id": place,
                "game_name": game_name,
                "reason": "No cookie available to create a private server",
                "reason_code": "no_cookie",
                "usernames": usernames,
            })
            outcome["skipped_usernames"].extend(u.strip().lower() for u in usernames)
            continue
        try:
            identity = validate_record_cookie_identity(owner_record or {}, owner_cookie, update_store=True)
        except Exception as exc:
            identity = {"ok": False, "msg": str(exc)}
        if not identity.get("ok"):
            outcome["failures"].append({
                "place_id": place,
                "game_name": game_name,
                "reason": str(identity.get("msg") or "Cookie identity check failed"),
                "reason_code": "no_cookie",
                "usernames": usernames,
            })
            outcome["skipped_usernames"].extend(u.strip().lower() for u in usernames)
            continue
        try:
            client = RobloxHTTP(owner_cookie)
            result = ensure_owned_private_server(
                client,
                owner_user_id=str(identity.get("cookie_user_id") or ""),
                place_id=place,
                free_only=bool(slot.get("free_only", True)),
                known_servers=[],
            )
        except Exception as exc:
            result = {"ok": False, "msg": str(exc)}
        if result.get("ok"):
            link = str(result.get("link") or "").strip()
            if link:
                if game is not None and not str((game or {}).get("private_server_url") or "").strip():
                    games_patch[str(game.get("id") or "")] = link
                elif game is None and not legacy_vip:
                    legacy_patch = link
            outcome["created"].append({
                "place_id": place,
                "game_name": game_name,
                "source": str(result.get("source") or ""),
                "usernames": usernames,
            })
            continue
        reason = str(result.get("msg") or "Private server setup failed")
        outcome["failures"].append({
            "place_id": place,
            "game_name": game_name,
            "reason": reason,
            "reason_code": _classify_failure(reason),
            "usernames": usernames,
        })
        outcome["skipped_usernames"].extend(u.strip().lower() for u in usernames)

    # Persist created links so launches reuse them silently.
    if (games_patch or legacy_patch) and cfg_mgr is not None:
        try:
            updates: Dict[str, Any] = {}
            if games_patch and games:
                fresh = []
                for item in games:
                    item = dict(item)
                    link = games_patch.get(str(item.get("id") or ""))
                    if link and not str(item.get("private_server_url") or "").strip():
                        item["private_server_url"] = link
                    fresh.append(item)
                updates["games"] = fresh
            if legacy_patch:
                updates["game_private_server_url"] = legacy_patch
            cfg_mgr.update(updates)
            cfg_mgr.save()
        except Exception as exc:
            try:
                from core import flog_kv

                flog_kv("PREFLIGHT", "private_server_link_save_failed", "warning", error=exc)
            except Exception:
                pass

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
        pass
    return outcome
