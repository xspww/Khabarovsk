"""START readiness decision (single owner for /api/start guards).

The route module keeps HTTP + command plumbing. Everything that decides
*whether* the farm may start — account gates, game-target resolution,
private-server preflight — lives here behind decide_start() so it is
table-testable on fake accounts without a farm, network, or HTTP layer.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple

from core import account_launch_block_reason, flog_kv
from runtime.account_selection import runtime_account_filter_reason


class _GameTargets(NamedTuple):
    mode: str
    games: list[Any]
    legacy_place: str
    shared_vip: str
    game_for_account: Callable[..., Any] | None
    effective_place: Callable[..., Any] | None


def blocked_summary(blocked: list[dict[str, Any]]) -> str:
    if not blocked:
        return ""
    reasons = [str(item.get("reason") or "").lower() for item in blocked]
    if reasons and all("captcha" in reason for reason in reasons):
        return f"{len(blocked)} blocked by CAPTCHA"
    if reasons and all("cookie" in reason for reason in reasons):
        return f"{len(blocked)} blocked by cookie"
    return f"{len(blocked)} blocked"


def _gate_accounts(
    accounts: list[Any],
    cfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[Any]]:
    blocked = []
    for account in accounts:
        reason = account_launch_block_reason(
            account
        ) or runtime_account_filter_reason(account, cfg)
        if reason:
            blocked.append(
                {
                    "username": account.username,
                    "reason": reason,
                }
            )
    blocked_names = {
        str(item["username"]).strip().lower()
        for item in blocked
    }
    launchable_accounts = [
        account
        for account in accounts
        if str(account.username or "").strip().lower()
        not in blocked_names
    ]
    return blocked, launchable_accounts


def _resolve_game_targets(cfg: dict[str, Any]) -> _GameTargets:
    mode = "shared"
    games: list[Any] = []
    legacy_place = ""
    shared_vip = ""
    game_for_account: Callable[..., Any] | None = None
    effective_place: Callable[..., Any] | None = None
    try:
        from domain.games import (
            effective_place as _effective_place,
        )
        from domain.games import (
            ensure_games_migrated,
            normalize_game_mode,
        )
        from domain.games import (
            game_for_account as _game_for_account,
        )

        game_for_account = _game_for_account
        effective_place = _effective_place
        snap = dict(cfg) if isinstance(cfg, dict) else {}
        try:
            mode = normalize_game_mode(
                snap.get("game_mode", "shared")
            )
        except Exception:
            mode = "shared"
        # Shared GAME mode ignores the pool. Per-account mode uses the
        # pool as the only target source: an unassigned account stays
        # target-less ("No game") even when a Shared Place ID is set.
        games = (
            ensure_games_migrated(snap)
            if mode == "per_account"
            else []
        )
        legacy_place = str(cfg.get("game_place_id", "") or "")
        shared_vip = str(
            cfg.get("game_private_server_url", "") or ""
        )
    except Exception:
        games = []
        legacy_place = ""
        shared_vip = ""
    return _GameTargets(
        mode=mode,
        games=games,
        legacy_place=legacy_place,
        shared_vip=shared_vip,
        game_for_account=game_for_account,
        effective_place=effective_place,
    )


def _account_game(
    account: Any,
    targets: _GameTargets,
) -> Any:
    resolver = targets.game_for_account
    if resolver is None:
        return None
    try:
        return resolver(
            getattr(account, "game_id", ""),
            getattr(account, "place_id", ""),
            targets.games,
        )
    except Exception:
        return None


def _account_place(
    account: Any,
    game: Any,
    targets: _GameTargets,
) -> str:
    resolver = targets.effective_place
    if resolver is None:
        return str(
            getattr(account, "place_id", "") or ""
        ).strip()
    try:
        return str(
            resolver(
                getattr(account, "place_id", ""),
                game,
                targets.legacy_place if not targets.games else "",
            )
        ).strip()
    except Exception:
        return str(
            getattr(account, "place_id", "") or ""
        ).strip()


def _account_links(account: Any, game: Any) -> list[Any]:
    links = list(getattr(account, "vip_links", []) or [])
    if game is not None and not links:
        game_vip = str(
            (game or {}).get("private_server_url") or ""
        ).strip()
        if game_vip:
            links = [game_vip]
    return links


def _shared_vip_links(
    shared_vip: str,
    legacy_place: str,
) -> list[str]:
    try:
        from domain.roblox_private_servers import (
            shared_direct_vip_link,
        )

        if shared_direct_vip_link(shared_vip, legacy_place):
            return [shared_vip]
    except Exception:
        "intentional: ignore exception"
    return []


def _account_is_missing_target(
    account: Any,
    targets: _GameTargets,
) -> bool:
    game = _account_game(account, targets)
    place = _account_place(account, game, targets)
    links = _account_links(account, game)
    if (
        not place
        and not links
        and not targets.games
        and targets.shared_vip
    ):
        links = _shared_vip_links(
            targets.shared_vip,
            targets.legacy_place,
        )
    return not place and not links


def _missing_launch_targets(
    accounts: list[Any],
    targets: _GameTargets,
) -> list[Any]:
    missing_targets = []
    for account in accounts:
        if _account_is_missing_target(account, targets):
            missing_targets.append(account.username)
    return missing_targets


def _no_launchable_result(
    command_id: str,
    blocked: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "ok": False,
        "accepted": False,
        "command_id": command_id,
        "error_code": "no_launchable_accounts",
        "msg": (
            "No launchable accounts. Reimport the correct cookie "
            "for blocked accounts."
        ),
        "required_action": (
            "Resolve blocked account gates, then retry /api/start."
        ),
        "launchable_count": 0,
        "blocked_count": len(blocked),
        "blocked": blocked,
    }


def _missing_shared_place_result(
    command_id: str,
    launchable_accounts: list[Any],
) -> dict[str, Any]:
    return {
        "ok": False,
        "accepted": False,
        "command_id": command_id,
        "error_code": "missing_shared_place",
        "msg": "Set the Shared Place ID (Game view) before Start.",
        "required_action": (
            "Set a Shared Place ID or Shared Private Server URL "
            "(Game view), then retry /api/start."
        ),
        "missing_target_count": len(launchable_accounts),
        "missing_targets": [
            account.username
            for account in launchable_accounts[:10]
        ],
    }


def _missing_launch_target_result(
    command_id: str,
    missing_targets: list[Any],
    mode: str,
) -> dict[str, Any]:
    shown = ", ".join(missing_targets[:3])
    suffix = "" if len(missing_targets) <= 3 else " ..."
    if mode == "per_account":
        required_action = (
            "Assign a game to each account (Games list) or set a "
            "per-account place_id / VIP link before /api/start."
        )
    else:
        required_action = (
            "Set a Shared Place ID (or a complete Shared Private "
            "Server URL) in the Game view, then retry /api/start."
        )
    return {
        "ok": False,
        "accepted": False,
        "command_id": command_id,
        "error_code": "missing_launch_target",
        "msg": f"Missing game assignment for: {shown}{suffix}",
        "required_action": required_action,
        "missing_target_count": len(missing_targets),
        "missing_targets": missing_targets[:10],
    }


def _run_preflight(
    cfg: dict[str, Any],
    launchable_accounts: list[Any],
    targets: _GameTargets,
    cfg_mgr: Any,
    run_preflight: Callable[..., dict[str, Any]] | None,
) -> dict[str, Any]:
    preflight: dict[str, Any] = {
        "created": [],
        "failures": [],
        "skipped_usernames": [],
        "public_fallback": [],
    }
    try:
        runner = run_preflight
        if runner is None:
            from services.private_server_preflight import (
                run_private_server_preflight as runner,
            )
        result = runner(
            cfg,
            launchable_accounts,
            targets.games,
            targets.mode,
            cfg_mgr,
        )
        if isinstance(result, dict):
            preflight = result
    except Exception as exc:
        flog_kv(
            "API",
            "start_preflight_error",
            "warning",
            error=exc,
        )
    return preflight


def _split_preflight_skipped(
    launchable_accounts: list[Any],
    preflight: dict[str, Any],
) -> tuple[list[Any], list[Any]]:
    skipped_names = {
        str(name or "").strip().lower()
        for name in (preflight.get("skipped_usernames") or [])
        if str(name or "").strip()
    }
    preflight_skipped: list[Any] = []
    if skipped_names:
        kept = []
        for account in launchable_accounts:
            if (
                str(account.username or "").strip().lower()
                in skipped_names
            ):
                preflight_skipped.append(account.username)
            else:
                kept.append(account)
        launchable_accounts = kept
    return launchable_accounts, preflight_skipped


def _preflight_blocked_result(
    command_id: str,
    preflight: dict[str, Any],
    preflight_skipped: list[Any],
) -> dict[str, Any]:
    shown = ", ".join(preflight_skipped[:3])
    suffix = "" if len(preflight_skipped) <= 3 else " ..."
    return {
        "ok": False,
        "accepted": False,
        "command_id": command_id,
        "error_code": "private_server_preflight_blocked",
        "msg": (
            f"Private server unavailable for: {shown}{suffix}"
        ),
        "required_action": (
            "Disable Auto Create Private Server for that game, "
            "paste a VIP link, or pick a game with free private "
            "servers."
        ),
        "private_server_preflight": preflight,
        "preflight_skipped": preflight_skipped[:10],
    }


def _public_fallback(preflight: dict[str, Any]) -> list[str]:
    return [
        str(name or "").strip()
        for name in (preflight.get("public_fallback") or [])
        if str(name or "").strip()
    ]


def _success_message(
    total_accounts: int,
    launchable_count: int,
    blocked: list[dict[str, Any]],
    preflight_skipped: list[Any],
    public_fallback: list[str],
) -> str:
    msg = (
        f"Farm started: {launchable_count}/{total_accounts} "
        "accounts launchable"
    )
    if blocked:
        msg += f"; {blocked_summary(blocked)}"
    if preflight_skipped:
        msg += (
            f"; {len(preflight_skipped)} skipped "
            "(private server unavailable)"
        )
    if public_fallback:
        msg += (
            f"; {len(public_fallback)} joining public server "
            "(game has no VIP)"
        )
    return msg


def _success_result(
    command_id: str,
    msg: str,
    launchable_accounts: list[Any],
    blocked: list[dict[str, Any]],
    preflight: dict[str, Any],
    preflight_skipped: list[Any],
    public_fallback: list[str],
) -> dict[str, Any]:
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


def decide_start(
    accounts: list[Any],
    cfg: dict[str, Any],
    command_id: str,
    starter: Callable[[], Any],
    cfg_mgr: Any = None,
    run_preflight: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run every START guard, call starter() only when all pass.

    Returns the final result payload in every case (same shapes the route
    used to build inline). Never raises for guard outcomes; unexpected
    starter() errors propagate to the route's Multi-Roblox-guard mapping.
    """
    accounts = list(accounts or [])
    cfg = cfg if isinstance(cfg, dict) else {}
    blocked, launchable_accounts = _gate_accounts(accounts, cfg)
    if not launchable_accounts:
        return _no_launchable_result(command_id, blocked)
    targets = _resolve_game_targets(cfg)
    if (
        targets.mode == "shared"
        and not targets.legacy_place.strip()
        and not targets.shared_vip.strip()
    ):
        # Shared mode with no Shared target: every account would
        # join nothing (or a stale per-account place). Block Start
        # with a toastable message instead of launching blindly.
        return _missing_shared_place_result(
            command_id,
            launchable_accounts,
        )
    missing_targets = _missing_launch_targets(
        launchable_accounts,
        targets,
    )
    if missing_targets:
        return _missing_launch_target_result(
            command_id,
            missing_targets,
            targets.mode,
        )
    # START-time Auto Create Private Server preflight: attempt the
    # creation once per place up front. Games with no private
    # servers at all (VIP disabled / paid) are reported but NOT
    # skipped — those accounts join the public server so a single
    # VIP-less game cannot hold back the other games. Genuine
    # errors (cookie, transient) still skip.
    preflight = _run_preflight(
        cfg,
        launchable_accounts,
        targets,
        cfg_mgr,
        run_preflight,
    )
    launchable_accounts, preflight_skipped = (
        _split_preflight_skipped(
            launchable_accounts,
            preflight,
        )
    )
    if not launchable_accounts:
        return _preflight_blocked_result(
            command_id,
            preflight,
            preflight_skipped,
        )
    starter()
    public_fallback = _public_fallback(preflight)
    msg = _success_message(
        len(accounts),
        len(launchable_accounts),
        blocked,
        preflight_skipped,
        public_fallback,
    )
    return _success_result(
        command_id,
        msg,
        launchable_accounts,
        blocked,
        preflight,
        preflight_skipped,
        public_fallback,
    )
