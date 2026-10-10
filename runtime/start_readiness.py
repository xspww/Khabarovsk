"""START readiness decision (single owner for /api/start guards).

The route module keeps HTTP + command plumbing. Everything that decides
*whether* the farm may start — account gates, game-target resolution,
private-server preflight — lives here behind decide_start() so it is
table-testable on fake accounts without a farm, network, or HTTP layer.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from core import account_launch_block_reason, flog_kv
from runtime.account_selection import runtime_account_filter_reason


def blocked_summary(blocked: List[Dict[str, Any]]) -> str:
    if not blocked:
        return ""
    reasons = [str(item.get("reason") or "").lower() for item in blocked]
    if reasons and all("captcha" in reason for reason in reasons):
        return f"{len(blocked)} blocked by CAPTCHA"
    if reasons and all("cookie" in reason for reason in reasons):
        return f"{len(blocked)} blocked by cookie"
    return f"{len(blocked)} blocked"


def decide_start(
    accounts: List[Any],
    cfg: Dict[str, Any],
    command_id: str,
    starter: Callable[[], Any],
    cfg_mgr: Any = None,
    run_preflight: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run every START guard, call starter() only when all pass.

    Returns the final result payload in every case (same shapes the route
    used to build inline). Never raises for guard outcomes; unexpected
    starter() errors propagate to the route's Multi-Roblox-guard mapping.
    """
    accounts = list(accounts or [])
    cfg = cfg if isinstance(cfg, dict) else {}
    blocked = []
    for account in accounts:
        reason = account_launch_block_reason(account) or runtime_account_filter_reason(account, cfg)
        if reason:
            blocked.append({"username": account.username, "reason": reason})
    blocked_names = {str(item["username"]).strip().lower() for item in blocked}
    launchable_accounts = [
        a for a in accounts
        if str(a.username or "").strip().lower() not in blocked_names
    ]
    if not launchable_accounts:
        return {
            "ok": False,
            "accepted": False,
            "command_id": command_id,
            "error_code": "no_launchable_accounts",
            "msg": "No launchable accounts. Reimport the correct cookie for blocked accounts.",
            "required_action": "Resolve blocked account gates, then retry /api/start.",
            "launchable_count": 0,
            "blocked_count": len(blocked),
            "blocked": blocked,
        }
    missing_targets = []
    _mode = "shared"
    try:
        from domain.games import effective_place, ensure_games_migrated, game_for_account, normalize_game_mode

        _snap = dict(cfg) if isinstance(cfg, dict) else {}
        try:
            _mode = normalize_game_mode(_snap.get("game_mode", "shared"))
        except Exception:
            _mode = "shared"
        # Shared GAME mode ignores the pool; Per-account falls back
        # to the Shared Place ID for unassigned accounts.
        _games = ensure_games_migrated(_snap) if _mode == "per_account" else []
        _legacy_place = str(cfg.get("game_place_id", "") or "")
        _shared_vip = str(cfg.get("game_private_server_url", "") or "")
    except Exception:
        _games = []
        _legacy_place = ""
        _shared_vip = ""
    if _mode == "shared" and not _legacy_place.strip() and not _shared_vip.strip():
        # Shared mode with no Shared target: every account would
        # join nothing (or a stale per-account place). Block Start
        # with a toastable message instead of launching blindly.
        return {
            "ok": False,
            "accepted": False,
            "command_id": command_id,
            "error_code": "missing_shared_place",
            "msg": "Set the Shared Place ID (Game view) before Start.",
            "required_action": "Set a Shared Place ID or Shared Private Server URL (Game view), then retry /api/start.",
            "missing_target_count": len(launchable_accounts),
            "missing_targets": [a.username for a in launchable_accounts[:10]],
        }
    for a in launchable_accounts:
        try:
            _game = game_for_account(
                getattr(a, "game_id", ""), getattr(a, "place_id", ""), _games
            )
        except Exception:
            _game = None
        try:
            _place = str(
                effective_place(
                    getattr(a, "place_id", ""),
                    _game,
                    _legacy_place if _game is None else "",
                )
            ).strip()
        except Exception:
            _place = str(getattr(a, "place_id", "") or "").strip()
        _links = list(getattr(a, "vip_links", []) or [])
        if _game is not None and not _links:
            _gvip = str((_game or {}).get("private_server_url") or "").strip()
            if _gvip:
                _links = [_gvip]
        if not _place and not _links:
            missing_targets.append(a.username)
    if missing_targets:
        shown = ", ".join(missing_targets[:3])
        suffix = "" if len(missing_targets) <= 3 else " ..."
        return {
            "ok": False,
            "accepted": False,
            "command_id": command_id,
            "error_code": "missing_launch_target",
            "msg": f"Missing game assignment for: {shown}{suffix}",
            "required_action": "Set a Shared Place ID (Game view), assign a game to each account (Games list), or set a per-account place_id / VIP link before /api/start.",
            "missing_target_count": len(missing_targets),
            "missing_targets": missing_targets[:10],
        }
    # START-time Auto Create Private Server preflight: attempt the
    # creation once per place up front. Games with no private
    # servers at all (VIP disabled / paid) are reported but NOT
    # skipped — those accounts join the public server so a single
    # VIP-less game cannot hold back the other games. Genuine
    # errors (cookie, transient) still skip.
    preflight: dict = {
        "created": [],
        "failures": [],
        "skipped_usernames": [],
        "public_fallback": [],
    }
    try:
        runner = run_preflight
        if runner is None:
            from services.private_server_preflight import run_private_server_preflight as runner
        _pf = runner(cfg, launchable_accounts, _games, _mode, cfg_mgr)
        if isinstance(_pf, dict):
            preflight = _pf
    except Exception as exc:
        flog_kv("API", "start_preflight_error", "warning", error=exc)
    skipped_names = {
        str(name or "").strip().lower()
        for name in (preflight.get("skipped_usernames") or [])
        if str(name or "").strip()
    }
    preflight_skipped: list = []
    if skipped_names:
        kept = []
        for a in launchable_accounts:
            if str(a.username or "").strip().lower() in skipped_names:
                preflight_skipped.append(a.username)
            else:
                kept.append(a)
        launchable_accounts = kept
    if not launchable_accounts:
        shown = ", ".join(preflight_skipped[:3])
        suffix = "" if len(preflight_skipped) <= 3 else " ..."
        return {
            "ok": False,
            "accepted": False,
            "command_id": command_id,
            "error_code": "private_server_preflight_blocked",
            "msg": f"Private server unavailable for: {shown}{suffix}",
            "required_action": "Disable Auto Create Private Server for that game, paste a VIP link, or pick a game with free private servers.",
            "private_server_preflight": preflight,
            "preflight_skipped": preflight_skipped[:10],
        }
    starter()
    msg = f"Farm started: {len(launchable_accounts)}/{len(accounts)} accounts launchable"
    if blocked:
        msg += f"; {blocked_summary(blocked)}"
    if preflight_skipped:
        msg += f"; {len(preflight_skipped)} skipped (private server unavailable)"
    public_fallback = [
        str(name or "").strip()
        for name in (preflight.get("public_fallback") or [])
        if str(name or "").strip()
    ]
    if public_fallback:
        msg += f"; {len(public_fallback)} joining public server (game has no VIP)"
    return {
        "ok": True,
        "accepted": True,
        "command_id": command_id,
        "msg": msg,
        "launchable_count": len(launchable_accounts),
        "blocked_count": len(blocked),
        "blocked": blocked,
        "private_server_preflight": preflight,
        "preflight_skipped": preflight_skipped[:10],
        "preflight_public_fallback": public_fallback[:10],
    }
