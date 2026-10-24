from __future__ import annotations

from typing import Any

from core import Account, flog_kv
from services.cpu_limiter import CPU_LIMITER, normalize_cpu_limiter_settings
from services.performance_settings import (
    DEFAULT_ROBLOX_SETTINGS_PATH,
    normalize_fps_limit,
    read_fps_settings,
)
from services.process_service import ProcessManager

from .context import ApiContext


def _int_setting(value, default: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(float(value))
    except Exception:
        parsed = int(default)
    return max(min_value, min(parsed, max_value))


def _float_setting(value, default: float, min_value: float, max_value: float) -> float:
    """Single owner for silent-clamp float parsing (route modules leverage this)."""
    try:
        parsed = float(value)
    except Exception:
        parsed = float(default)
    return max(min_value, min(parsed, max_value))


def persist_config(ctx: ApiContext, updates: dict[str, Any], *, apply_snapshot: bool = False, strict_snapshot: bool = False) -> None:
    """Single write path for config: update + save, then fan out to the farm.

    Route modules leverage this instead of repeating the triple. strict_snapshot
    preserves the older bare call sites where a fan-out error propagates;
    guarded (default) sites swallow it like the window/tuner routes do.
    """
    ctx.cfg_mgr.update(updates)
    ctx.cfg_mgr.save()
    if not apply_snapshot:
        return
    farm = getattr(ctx, "farm", None)
    if not hasattr(farm, "apply_config_snapshot"):
        return
    if strict_snapshot:
        farm.apply_config_snapshot()
        return
    try:
        farm.apply_config_snapshot()
    except Exception:
        "intentional: ignore exception"


WINDOW_SIZE_PRESETS: dict[str, tuple[int, int]] = {
    "200x150": (200, 150),
    "240x180": (240, 180),
    "320x240": (320, 240),
    "480x360": (480, 360),
    "640x480": (640, 480),
    "800x600": (800, 600),
    "1024x768": (1024, 768),
    "1280x720": (1280, 720),
    "1600x900": (1600, 900),
    "1920x1080": (1920, 1080),
}


def _normalize_window_size_settings(ctx: ApiContext, body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    unlock_size_enabled = bool(body.get("unlock_size_enabled", body.get("roblox_window_unlock_size_enabled", ctx.cfg_mgr.get("roblox_window_unlock_size_enabled", True))))
    enabled = bool(body.get("enabled", body.get("roblox_window_resize_enabled", ctx.cfg_mgr.get("roblox_window_resize_enabled", False))))
    preset = str(body.get("preset", body.get("roblox_window_size_preset", ctx.cfg_mgr.get("roblox_window_size_preset", "200x150"))) or "200x150")
    preset = preset.strip().lower().replace(" ", "")
    if preset != "custom" and preset not in WINDOW_SIZE_PRESETS:
        raise ValueError("Invalid window size preset")
    if preset in WINDOW_SIZE_PRESETS:
        width, height = WINDOW_SIZE_PRESETS[preset]
    else:
        width = _int_setting(body.get("width", body.get("roblox_window_width", ctx.cfg_mgr.get("roblox_window_width", 200))), 200, 80, 1920)
        height = _int_setting(body.get("height", body.get("roblox_window_height", ctx.cfg_mgr.get("roblox_window_height", 150))), 150, 60, 1080)
    interval = _int_setting(
        body.get("interval_seconds", body.get("roblox_window_resize_interval_seconds", ctx.cfg_mgr.get("roblox_window_resize_interval_seconds", 10))),
        10,
        1,
        3600,
    )
    arrange_enabled = bool(body.get("arrange_enabled", body.get("roblox_window_arrange_enabled", ctx.cfg_mgr.get("roblox_window_arrange_enabled", False))))
    arrange_columns = _int_setting(
        body.get("arrange_columns", body.get("roblox_window_arrange_columns", ctx.cfg_mgr.get("roblox_window_arrange_columns", 6))),
        6,
        1,
        32,
    )
    arrange_rows = _int_setting(
        body.get("arrange_rows", body.get("roblox_window_arrange_rows", ctx.cfg_mgr.get("roblox_window_arrange_rows", 4))),
        4,
        1,
        32,
    )
    arrange_gap = 0
    arrange_margin = _int_setting(
        body.get("arrange_margin", body.get("roblox_window_arrange_margin", ctx.cfg_mgr.get("roblox_window_arrange_margin", 0))),
        0,
        0,
        300,
    )
    auto_minimize_enabled = bool(body.get("auto_minimize_enabled", ctx.cfg_mgr.get("auto_minimize_enabled", False)))
    auto_minimize_seconds = _int_setting(
        body.get("auto_minimize_seconds", ctx.cfg_mgr.get("auto_minimize_seconds", 10)),
        10,
        1,
        3600,
    )
    hide_enabled = bool(body.get("hide_enabled", body.get("roblox_window_hide_enabled", ctx.cfg_mgr.get("roblox_window_hide_enabled", False))))
    return {
        "enabled": enabled,
        "unlock_size_enabled": unlock_size_enabled,
        "preset": preset,
        "width": int(width),
        "height": int(height),
        "interval_seconds": interval,
        "arrange_enabled": arrange_enabled,
        "arrange_columns": arrange_columns,
        "arrange_rows": arrange_rows,
        "arrange_gap": arrange_gap,
        "arrange_margin": arrange_margin,
        "auto_minimize_enabled": auto_minimize_enabled,
        "auto_minimize_seconds": auto_minimize_seconds,
        "hide_enabled": hide_enabled,
    }


def _window_size_status(ctx: ApiContext) -> dict[str, Any]:
    settings = _normalize_window_size_settings(ctx, {})
    visible_count = 0
    hidden_count = 0
    try:
        visible_count = len(ProcessManager._visible_roblox_windows(include_minimized=True) or [])
    except Exception:
        visible_count = 0
    try:
        hidden_fn = getattr(ProcessManager, "_hidden_roblox_windows", None)
        if callable(hidden_fn):
            hidden_count = len(hidden_fn() or [])
    except Exception:
        hidden_count = 0
    return {
        "ok": True,
        "enabled": settings["enabled"],
        "unlock_size_enabled": settings["unlock_size_enabled"],
        "preset": settings["preset"],
        "width": settings["width"],
        "height": settings["height"],
        "interval_seconds": settings["interval_seconds"],
        "arrange_enabled": settings["arrange_enabled"],
        "arrange_columns": settings["arrange_columns"],
        "arrange_rows": settings["arrange_rows"],
        "arrange_gap": settings["arrange_gap"],
        "arrange_margin": settings["arrange_margin"],
        "auto_minimize_enabled": settings["auto_minimize_enabled"],
        "auto_minimize_seconds": settings["auto_minimize_seconds"],
        "hide_enabled": settings["hide_enabled"],
        "visible_windows": visible_count,
        "hidden_windows": hidden_count,
        "presets": [{"value": key, "width": value[0], "height": value[1]} for key, value in WINDOW_SIZE_PRESETS.items()],
    }


def _roblox_runtime_restart_required(ctx: ApiContext) -> dict[str, Any]:
    running = False
    count = 0
    try:
        live = ProcessManager.list_live_game_processes()
        count = len(live)
        running = count > 0
    except Exception:
        live = []
    rt_running = bool(getattr(ctx.farm, "running", False))
    requires_restart = bool(running or rt_running)
    warning = ""
    title = ""
    action = ""
    if requires_restart:
        if running and rt_running:
            title = "Roblox is running"
            warning = f"Auto Rejoin is ON with {count} Roblox client(s) open. Stop Auto Rejoin or close Roblox, then change performance settings to take effect."
            action = "Stop Auto Rejoin or close Roblox first"
        elif running:
            title = "Roblox is running"
            warning = f"{count} Roblox client(s) open. Close Roblox, then change performance settings to take effect."
            action = "Close Roblox first"
        else:
            title = "Auto Rejoin is ON"
            warning = "Stop Auto Rejoin, then change performance settings to take effect."
            action = "Stop Auto Rejoin first"
    return {
        "roblox_running": running,
        "roblox_pid_count": count,
        "rt_running": rt_running,
        "requires_restart": requires_restart,
        "warning": warning,
        "warning_title": title,
        "required_action": action,
    }


def _volume_status_from_config(ctx: ApiContext) -> dict[str, Any]:
    return {
        "roblox_volume_muted": bool(ctx.cfg_mgr.get("roblox_volume_muted", False)),
        "roblox_volume_level": _int_setting(ctx.cfg_mgr.get("roblox_volume_level", 10), 10, 0, 10),
    }


def _fps_limiter_status(ctx: ApiContext, path: str = DEFAULT_ROBLOX_SETTINGS_PATH) -> dict[str, Any]:
    file_status = read_fps_settings(path)
    runtime_status = _roblox_runtime_restart_required(ctx)
    try:
        configured_limit = normalize_fps_limit(ctx.cfg_mgr.get("fps_limit", 240))
    except ValueError:
        configured_limit = 240
    graphics_enabled = bool(ctx.cfg_mgr.get("graphics_low_enabled", ctx.cfg_mgr.get("graphics_auto_enabled", False)))
    graphics_quality = _int_setting(ctx.cfg_mgr.get("graphics_quality_level", 1), 1, 1, 10)
    priority = str(ctx.cfg_mgr.get("process_priority", "low") or "low")
    payload = {
        "ok": bool(file_status.get("exists")),
        **file_status,
        **runtime_status,
        "enabled": bool(ctx.cfg_mgr.get("fps_limiter_enabled", False)),
        "fps_limit": configured_limit,
        "graphics_low_enabled": graphics_enabled,
        "graphics_auto_enabled": graphics_enabled,
        "graphics_quality_level": graphics_quality,
        "auto_process_priority_enabled": bool(ctx.cfg_mgr.get("auto_process_priority_enabled", False)),
        "process_priority": priority,
        **_volume_status_from_config(ctx),
    }
    # Only mirror the file value while the limiter is ON. When OFF the file
    # holds the uncap value (0) but the UI must keep showing the remembered
    # limit instead of snapping to 0 / stale 15.
    if payload["enabled"] and file_status.get("framerate_cap") is not None:
        payload["fps_limit"] = int(file_status.get("framerate_cap") or configured_limit)
    return payload


def _graphics_status(ctx: ApiContext, path: str = DEFAULT_ROBLOX_SETTINGS_PATH) -> dict[str, Any]:
    file_status = read_fps_settings(path)
    runtime_status = _roblox_runtime_restart_required(ctx)
    graphics_enabled = bool(ctx.cfg_mgr.get("graphics_low_enabled", ctx.cfg_mgr.get("graphics_auto_enabled", False)))
    graphics_quality = _int_setting(ctx.cfg_mgr.get("graphics_quality_level", 1), 1, 1, 10)
    priority = str(ctx.cfg_mgr.get("process_priority", "low") or "low")
    payload = {
        "ok": bool(file_status.get("exists")),
        **file_status,
        **runtime_status,
        "graphics_low_enabled": graphics_enabled,
        "graphics_auto_enabled": graphics_enabled,
        "graphics_quality_level": graphics_quality,
        "auto_process_priority_enabled": bool(ctx.cfg_mgr.get("auto_process_priority_enabled", False)),
        "process_priority": priority,
        **_volume_status_from_config(ctx),
    }
    return payload


def _cpu_limiter_settings_from_config(ctx: ApiContext, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    source = {
        "cpu_limiter_enabled": ctx.cfg_mgr.get("cpu_limiter_enabled", False),
        "cpu_limiter_mode": ctx.cfg_mgr.get("cpu_limiter_mode", "hard"),
        "cpu_limiter_default_percent": ctx.cfg_mgr.get("cpu_limiter_default_percent", 20),
        "cpu_limiter_apply_all": ctx.cfg_mgr.get("cpu_limiter_apply_all", True),
        "cpu_limiter_accounts": ctx.cfg_mgr.get("cpu_limiter_accounts", {}),
    }
    if extra:
        for key, value in extra.items():
            if key == "enabled":
                source["cpu_limiter_enabled"] = value
            elif key == "mode":
                source["cpu_limiter_mode"] = value
            elif key == "default_limit_percent":
                source["cpu_limiter_default_percent"] = value
            elif key == "apply_all":
                source["cpu_limiter_apply_all"] = value
            elif key == "accounts":
                source["cpu_limiter_accounts"] = value
            elif key in {
                "cpu_limiter_enabled",
                "cpu_limiter_mode",
                "cpu_limiter_default_percent",
                "cpu_limiter_apply_all",
                "cpu_limiter_accounts",
            }:
                source[key] = value
    return normalize_cpu_limiter_settings(source)


def _cpu_limiter_status(ctx: ApiContext) -> dict[str, Any]:
    settings = _cpu_limiter_settings_from_config(ctx)
    return CPU_LIMITER.snapshot(getattr(ctx.farm, "_accounts", []), settings)


def _ram_cleanup_status(ctx: ApiContext) -> dict[str, Any]:
    try:
        from services.ram_cleanup import RAM_CLEANUP
    except Exception as exc:
        return {"ok": False, "msg": f"ram cleanup unavailable: {exc}"}
    cfg = {
        "ram_cleanup_enabled": ctx.cfg_mgr.get("ram_cleanup_enabled", False),
        "ram_cleanup_threshold_pct": ctx.cfg_mgr.get("ram_cleanup_threshold_pct", 85.0),
        "ram_cleanup_interval_min": ctx.cfg_mgr.get("ram_cleanup_interval_min", 15),
    }
    try:
        return RAM_CLEANUP.snapshot(cfg)
    except Exception as exc:
        return {"ok": False, "msg": str(exc), **cfg}


def _process_trim_status(ctx: ApiContext) -> dict[str, Any]:
    try:
        from services.process_trim import PROCESS_TRIM
    except Exception as exc:
        return {"ok": False, "msg": f"process trim unavailable: {exc}"}
    cfg = {
        "process_trim_enabled": ctx.cfg_mgr.get("process_trim_enabled", False),
        "process_trim_threshold_mb": ctx.cfg_mgr.get("process_trim_threshold_mb", 1200),
        "process_trim_cooldown_sec": ctx.cfg_mgr.get("process_trim_cooldown_sec", 300),
        "process_trim_max_per_cycle": ctx.cfg_mgr.get("process_trim_max_per_cycle", 1),
    }
    try:
        return PROCESS_TRIM.snapshot(cfg)
    except Exception as exc:
        return {"ok": False, "msg": str(exc), **cfg}


def _virtual_memory_status(ctx: ApiContext) -> dict[str, Any]:
    try:
        from services import virtual_memory as _vm
    except Exception as exc:
        return {"ok": False, "msg": f"virtual memory unavailable: {exc}"}
    try:
        payload = _vm.status()
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}
    payload["configured_mode"] = str(ctx.cfg_mgr.get("virtual_memory_mode", "system_managed") or "system_managed")
    try:
        payload["configured_size_gb"] = int(ctx.cfg_mgr.get("virtual_memory_size_gb", 16) or 16)
    except Exception:
        payload["configured_size_gb"] = 16
    return payload


def _games_module() -> Any | None:
    try:
        from domain import games
    except Exception:
        return None
    return games


def _snapshot_config(ctx: ApiContext) -> dict[str, Any] | None:
    try:
        cfg = ctx.cfg_mgr.snapshot()
    except Exception:
        return None
    if not isinstance(cfg, dict):
        return None
    return cfg


def _games_mode(games_api: Any, cfg: dict[str, Any]) -> str:
    try:
        return games_api.normalize_game_mode(cfg.get("game_mode", "shared"))
    except Exception:
        return "shared"


def _ensure_games(games_api: Any, cfg: dict[str, Any]) -> list:
    try:
        return games_api.ensure_games_migrated(dict(cfg))
    except Exception:
        return []


def _persist_games_migrated(ctx: ApiContext) -> None:
    try:
        persist_config(ctx, {"games_migrated": True})
    except Exception:
        "intentional: ignore exception"


def _account_game_link(
    acc: Account, games_api: Any, games: list, fallback: dict | None
) -> tuple[bool, str]:
    game = games_api.game_for_account("", getattr(acc, "place_id", ""), games)
    if game is not None:
        return True, str(game.get("id") or "")
    has_place = bool(str(getattr(acc, "place_id", "") or "").strip())
    has_links = bool(list(getattr(acc, "vip_links", []) or []))
    if has_place or has_links or fallback is None:
        return False, ""
    return True, str(fallback.get("id") or "")


def _link_account_game(
    acc: Account, games_api: Any, games: list, fallback: dict | None
) -> bool:
    try:
        if str(getattr(acc, "game_id", "") or "").strip():
            return False
        found, game_id = _account_game_link(acc, games_api, games, fallback)
        if not found:
            return False
        acc.game_id = game_id
        return True
    except Exception:
        return False


def _migrate_account_games(
    ctx: ApiContext, accounts_to_update: list[Account]
) -> int:
    """One-time linking of existing accounts to games. Returns linked count.

    Links by id when present, otherwise by matching place_id. Accounts with
    no target at all are assigned the default game so farms configured
    before multi-game keep working. Runs only until ``games_migrated`` is
    set; accounts added afterwards without a game stay unassigned and are
    blocked at START with a warning (never silently join a wrong map).
    """
    games_api = _games_module()
    if games_api is None:
        return 0
    cfg = _snapshot_config(ctx)
    if cfg is None:
        return 0
    if bool(cfg.get("games_migrated", False)):
        return 0
    games = _ensure_games(games_api, cfg)
    if not games:
        return 0
    fallback = games_api.default_game(games)
    linked = 0
    for acc in accounts_to_update or []:
        if _link_account_game(acc, games_api, games, fallback):
            linked += 1
    if linked:
        _persist_games_migrated(ctx)
    return linked


def _vip_link_place(link: Any) -> str:
    return ProcessManager.parse_vip_link(str(link or "").strip())[0]


def _filter_vip_links(links: list, place_id: str) -> list:
    return [
        link
        for link in list(links or [])
        if not _vip_link_place(link) or _vip_link_place(link) == place_id
    ]


def _apply_default_vip(acc: Account, game_vip: str) -> bool:
    if game_vip and not list(acc.vip_links or []):
        acc.vip_links = [game_vip]
        return True
    return False


def _apply_game_vip(acc: Account, game: dict) -> bool:
    place_id = str(game.get("place_id") or "").strip()
    game_vip = str(game.get("private_server_url") or "").strip()
    if not place_id:
        return _apply_default_vip(acc, game_vip)
    changed = False
    filtered_links = _filter_vip_links(acc.vip_links, place_id)
    active_place = _vip_link_place(acc.active_vip)
    if active_place and active_place != place_id:
        acc.active_vip = ""
        changed = True
    if filtered_links != list(acc.vip_links or []):
        acc.vip_links = filtered_links
        changed = True
    if _apply_default_vip(acc, game_vip):
        changed = True
    return changed


def _apply_account_game(acc: Account, games_api: Any, games: list) -> bool:
    try:
        game = games_api.game_for_account(
            getattr(acc, "game_id", ""),
            getattr(acc, "place_id", ""),
            games,
        )
    except Exception:
        game = None
    if game is None:
        return False
    changed = False
    if not str(getattr(acc, "game_id", "") or "").strip():
        acc.game_id = str(game.get("id") or "")
        changed = True
    if _apply_game_vip(acc, game):
        changed = True
    return changed


def _save_game_defaults(
    ctx: ApiContext, changed: int, linked: int, persist: bool
) -> None:
    if not persist or (not changed and not linked):
        return
    ctx.cfg_mgr.save_accounts(ctx.farm._accounts)
    flog_kv(
        "API", "game_defaults_applied", accounts=changed, migrated=linked
    )


def _apply_game_defaults(
    ctx: ApiContext, accounts_to_update: list[Account], persist: bool = False
) -> int:
    games_api = _games_module()
    if games_api is None or not hasattr(games_api, "normalize_game_mode"):
        return _apply_legacy_game_defaults(ctx, accounts_to_update, persist)
    cfg = _snapshot_config(ctx)
    if cfg is None:
        cfg = {}
    mode = _games_mode(games_api, cfg)
    if mode != "per_account":
        # Shared GAME mode: the pool is ignored at launch, so there is
        # nothing to sync. Accounts keep their assignments untouched for
        # when the user switches back to Per-account.
        return 0
    games = _ensure_games(games_api, cfg)
    if not games:
        return _apply_legacy_game_defaults(
            ctx, accounts_to_update, persist, cfg=cfg
        )
    linked = _migrate_account_games(ctx, accounts_to_update)
    changed = 0
    for acc in accounts_to_update:
        if _apply_account_game(acc, games_api, games):
            changed += 1
    _save_game_defaults(ctx, changed, linked, persist)
    return changed + linked


def _legacy_targets(cfg: dict[str, Any]) -> tuple[str, str]:
    vip_url = str(cfg.get("game_private_server_url", "") or "").strip()
    place_id = str(cfg.get("game_place_id", "") or "").strip()
    if vip_url:
        parsed_place, _link_code = ProcessManager.parse_vip_link(vip_url)
        if parsed_place and not place_id:
            place_id = str(parsed_place)
    return vip_url, place_id


def _apply_legacy_vip(acc: Account, vip_url: str) -> bool:
    if vip_url and list(acc.vip_links or []) != [vip_url]:
        acc.vip_links = [vip_url]
        return True
    return False


def _apply_legacy_account(acc: Account, place_id: str, vip_url: str) -> bool:
    changed = False
    if place_id:
        if str(acc.place_id or "").strip() != place_id:
            acc.place_id = place_id
            changed = True
        filtered_links = _filter_vip_links(acc.vip_links, place_id)
        active_place = _vip_link_place(acc.active_vip)
        if active_place and active_place != place_id:
            acc.active_vip = ""
            changed = True
        if not vip_url and filtered_links != list(acc.vip_links or []):
            acc.vip_links = filtered_links
            changed = True
    if _apply_legacy_vip(acc, vip_url):
        changed = True
    return changed


def _apply_legacy_game_defaults(
    ctx: ApiContext,
    accounts_to_update: list[Account],
    persist: bool = False,
    cfg: dict[str, Any] | None = None,
) -> int:
    if cfg is None:
        cfg = _snapshot_config(ctx) or {}
    if not isinstance(cfg, dict):
        cfg = {}
    vip_url, place_id = _legacy_targets(cfg)
    if not vip_url and not place_id:
        return 0
    changed = 0
    for acc in accounts_to_update:
        if _apply_legacy_account(acc, place_id, vip_url):
            changed += 1
    if changed and persist:
        ctx.cfg_mgr.save_accounts(ctx.farm._accounts)
        flog_kv("API", "game_defaults_applied", accounts=changed)
    return changed
