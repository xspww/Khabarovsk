from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from core import Account, flog_kv
from performance_settings import (
    DEFAULT_ROBLOX_SETTINGS_PATH,
    normalize_fps_limit,
    normalize_graphics_quality,
    read_fps_settings,
)
from services.cpu_limiter import CPU_LIMITER, normalize_cpu_limiter_settings
from services.process_service import ProcessManager

from .context import ApiContext

def _int_setting(value, default: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(float(value))
    except Exception:
        parsed = int(default)
    return max(min_value, min(parsed, max_value))


WINDOW_SIZE_PRESETS: Dict[str, Tuple[int, int]] = {
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


def _normalize_window_size_settings(ctx: ApiContext, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
    }


def _window_size_status(ctx: ApiContext) -> Dict[str, Any]:
    settings = _normalize_window_size_settings(ctx, {})
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
        "presets": [{"value": key, "width": value[0], "height": value[1]} for key, value in WINDOW_SIZE_PRESETS.items()],
    }


def _roblox_runtime_restart_required(ctx: ApiContext) -> Dict[str, Any]:
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
    if requires_restart:
        warning = "Close Roblox or Stop guard, then re-game for performance settings to take effect."
    return {
        "roblox_running": running,
        "roblox_pid_count": count,
        "rt_running": rt_running,
        "requires_restart": requires_restart,
        "warning": warning,
    }


def _fps_limiter_status(ctx: ApiContext, path: str = DEFAULT_ROBLOX_SETTINGS_PATH) -> Dict[str, Any]:
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
    }
    if file_status.get("framerate_cap") is not None:
        payload["fps_limit"] = int(file_status.get("framerate_cap") or configured_limit)
    return payload


def _graphics_status(ctx: ApiContext, path: str = DEFAULT_ROBLOX_SETTINGS_PATH) -> Dict[str, Any]:
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
    }
    return payload


def _cpu_limiter_settings_from_config(ctx: ApiContext, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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


def _cpu_limiter_status(ctx: ApiContext) -> Dict[str, Any]:
    settings = _cpu_limiter_settings_from_config(ctx)
    return CPU_LIMITER.snapshot(getattr(ctx.farm, "_accounts", []), settings)


def _apply_game_defaults(ctx: ApiContext, accounts_to_update: List[Account], persist: bool = False) -> int:
    cfg = ctx.cfg_mgr.snapshot()
    vip_url = str(cfg.get("game_private_server_url", "") or "").strip()
    place_id = str(cfg.get("game_place_id", "") or "").strip()
    if vip_url:
        parsed_place, _link_code = ProcessManager.parse_vip_link(vip_url)
        if parsed_place and not place_id:
            place_id = str(parsed_place)
    if not vip_url and not place_id:
        return 0

    changed = 0
    for acc in accounts_to_update:
        account_changed = False
        if place_id:
            if str(acc.place_id or "").strip() != place_id:
                acc.place_id = place_id
                account_changed = True
            filtered_links = [
                link for link in list(acc.vip_links or [])
                if not ProcessManager.parse_vip_link(str(link or "").strip())[0]
                or ProcessManager.parse_vip_link(str(link or "").strip())[0] == place_id
            ]
            active_place = ProcessManager.parse_vip_link(str(acc.active_vip or "").strip())[0]
            if active_place and active_place != place_id:
                acc.active_vip = ""
                account_changed = True
            if not vip_url and filtered_links != list(acc.vip_links or []):
                acc.vip_links = filtered_links
                account_changed = True
        if vip_url and list(acc.vip_links or []) != [vip_url]:
            acc.vip_links = [vip_url]
            account_changed = True
        if account_changed:
            changed += 1
    if changed and persist:
        ctx.cfg_mgr.save_accounts(ctx.farm._accounts)
        flog_kv("API", "game_defaults_applied", accounts=changed)
    return changed
