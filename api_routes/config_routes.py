from __future__ import annotations

from fastapi import HTTPException, Request

from performance_settings import normalize_graphics_quality, normalize_process_priority
from roblox_hybrid import release_multi_roblox_guard

from .context import ApiContext
from .settings_state import (
    _apply_game_defaults,
    _cpu_limiter_settings_from_config,
    _int_setting,
    _normalize_window_size_settings,
)


def register(app, ctx: ApiContext) -> None:
    cfg_mgr = ctx.cfg_mgr
    farm = ctx.farm
    @app.get("/api/config")
    def api_get_config():
        snap = cfg_mgr.snapshot()
        snap.pop("accounts", None)
        snap.pop("runtime_state", None)
        return snap

    @app.post("/api/config")
    async def api_set_config(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected object")
        allowed = {
            "auto_rejoin", "rejoin_delay", "max_retry", "max_fail_count",
            "crash_timeout", "heartbeat_timeout", "launch_verify_window", "login_warmup_delay",
            "anti_spam_window", "launch_rate_interval", "account_switch_cooldown",
            "queue_delay_seconds", "queue_duration_seconds", "max_concurrent_accounts",
            "game_private_server_url", "game_place_id",
            "auto_create_private_server_enabled", "auto_create_private_server_free_only",
            "auto_close_enabled", "auto_close_minutes",
            "auto_minimize_enabled", "auto_minimize_seconds",
            "not_responding_timeout",
            "network_check_interval", "network_debounce",
            "queue_timeout", "cooldown_after_crash", "relaunch_loop_limit",
            "connection_error_rejoin", "popup_disconnected_enabled",
            "popup_scan_interval_seconds", "popup_scan_max_parallel",
            "connection_error_hold_time",
            "watchdog_enabled", "watchdog_cpu_low",
            "watchdog_ram_low", "watchdog_hold_time",
            "watchdog_activity_timeout", "watchdog_loading_grace",
            "recovery_restore_window", "event_bus_workers", "event_bus_max_pending",
            "fps_limiter_enabled", "fps_limit", "graphics_auto_enabled", "graphics_low_enabled", "graphics_quality_level",
            "auto_process_priority_enabled", "process_priority",
            "cpu_limiter_enabled", "cpu_limiter_mode", "cpu_limiter_default_percent",
            "cpu_limiter_apply_all", "cpu_limiter_accounts",
            "roblox_window_resize_enabled", "roblox_window_size_preset", "roblox_window_width",
            "roblox_window_height", "roblox_window_resize_interval_seconds",
            "roblox_window_arrange_enabled", "roblox_window_arrange_columns",
            "roblox_window_arrange_gap", "roblox_window_arrange_margin",
            "presence_api_enabled", "presence_poll_interval_seconds",
            "presence_cache_ttl_seconds", "presence_assist_rejoin_enabled",
            "multi_roblox_enabled", "rt_rotation_enabled",
        }
        updates = {k: v for k, v in body.items() if k in allowed}
        if "queue_delay_seconds" in updates:
            delay = _int_setting(updates["queue_delay_seconds"], 15, 0, 3600)
            updates["queue_delay_seconds"] = delay
            updates["launch_rate_interval"] = delay
            updates["account_switch_cooldown"] = delay
        if "queue_duration_seconds" in updates:
            updates["queue_duration_seconds"] = _int_setting(updates["queue_duration_seconds"], 15, 0, 86400)
        if "max_concurrent_accounts" in updates:
            updates["max_concurrent_accounts"] = _int_setting(updates["max_concurrent_accounts"], 40, 1, 500)
        if "auto_close_minutes" in updates:
            updates["auto_close_minutes"] = _int_setting(updates["auto_close_minutes"], 0, 0, 1440)
        if "auto_close_enabled" in updates:
            updates["auto_close_enabled"] = bool(updates["auto_close_enabled"])
        if "auto_minimize_enabled" in updates:
            updates["auto_minimize_enabled"] = bool(updates["auto_minimize_enabled"])
        if "auto_minimize_seconds" in updates:
            updates["auto_minimize_seconds"] = _int_setting(updates["auto_minimize_seconds"], 10, 1, 3600)
        if "fps_limiter_enabled" in updates:
            updates["fps_limiter_enabled"] = bool(updates["fps_limiter_enabled"])
        if "fps_limit" in updates:
            updates["fps_limit"] = _int_setting(updates["fps_limit"], 240, 15, 1000)
        if "graphics_auto_enabled" in updates:
            updates["graphics_auto_enabled"] = bool(updates["graphics_auto_enabled"])
        if "graphics_low_enabled" in updates:
            updates["graphics_low_enabled"] = bool(updates["graphics_low_enabled"])
            updates["graphics_auto_enabled"] = updates["graphics_low_enabled"]
        if "graphics_quality_level" in updates:
            try:
                updates["graphics_quality_level"] = normalize_graphics_quality(updates["graphics_quality_level"])
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        if "auto_process_priority_enabled" in updates:
            updates["auto_process_priority_enabled"] = bool(updates["auto_process_priority_enabled"])
        if "process_priority" in updates:
            try:
                updates["process_priority"] = normalize_process_priority(updates["process_priority"])
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        if any(k in updates for k in ("cpu_limiter_enabled", "cpu_limiter_mode", "cpu_limiter_default_percent", "cpu_limiter_apply_all", "cpu_limiter_accounts")):
            try:
                normalized_cpu = _cpu_limiter_settings_from_config(ctx, {
                    "cpu_limiter_enabled": updates.get("cpu_limiter_enabled", cfg_mgr.get("cpu_limiter_enabled", False)),
                    "cpu_limiter_mode": updates.get("cpu_limiter_mode", cfg_mgr.get("cpu_limiter_mode", "hard")),
                    "cpu_limiter_default_percent": updates.get("cpu_limiter_default_percent", cfg_mgr.get("cpu_limiter_default_percent", 20)),
                    "cpu_limiter_apply_all": updates.get("cpu_limiter_apply_all", cfg_mgr.get("cpu_limiter_apply_all", True)),
                    "cpu_limiter_accounts": updates.get("cpu_limiter_accounts", cfg_mgr.get("cpu_limiter_accounts", {})),
                })
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            updates["cpu_limiter_enabled"] = normalized_cpu["enabled"]
            updates["cpu_limiter_mode"] = normalized_cpu["mode"]
            updates["cpu_limiter_default_percent"] = normalized_cpu["default_limit_percent"]
            updates["cpu_limiter_apply_all"] = normalized_cpu["apply_all"]
            if normalized_cpu["apply_all"]:
                normalized_cpu["accounts"] = {}
            updates["cpu_limiter_accounts"] = normalized_cpu["accounts"]
        if any(k in updates for k in ("roblox_window_resize_enabled", "roblox_window_size_preset", "roblox_window_width", "roblox_window_height", "roblox_window_resize_interval_seconds", "roblox_window_arrange_enabled", "roblox_window_arrange_columns", "roblox_window_arrange_gap", "roblox_window_arrange_margin")):
            try:
                normalized_window = _normalize_window_size_settings(ctx, {
                    "enabled": updates.get("roblox_window_resize_enabled", cfg_mgr.get("roblox_window_resize_enabled", False)),
                    "preset": updates.get("roblox_window_size_preset", cfg_mgr.get("roblox_window_size_preset", "640x480")),
                    "width": updates.get("roblox_window_width", cfg_mgr.get("roblox_window_width", 640)),
                    "height": updates.get("roblox_window_height", cfg_mgr.get("roblox_window_height", 480)),
                    "interval_seconds": updates.get("roblox_window_resize_interval_seconds", cfg_mgr.get("roblox_window_resize_interval_seconds", 10)),
                    "arrange_enabled": updates.get("roblox_window_arrange_enabled", cfg_mgr.get("roblox_window_arrange_enabled", False)),
                    "arrange_columns": updates.get("roblox_window_arrange_columns", cfg_mgr.get("roblox_window_arrange_columns", 6)),
                    "arrange_gap": updates.get("roblox_window_arrange_gap", cfg_mgr.get("roblox_window_arrange_gap", 2)),
                    "arrange_margin": updates.get("roblox_window_arrange_margin", cfg_mgr.get("roblox_window_arrange_margin", 0)),
                })
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            updates["roblox_window_resize_enabled"] = normalized_window["enabled"]
            updates["roblox_window_size_preset"] = normalized_window["preset"]
            updates["roblox_window_width"] = normalized_window["width"]
            updates["roblox_window_height"] = normalized_window["height"]
            updates["roblox_window_resize_interval_seconds"] = normalized_window["interval_seconds"]
            updates["roblox_window_arrange_enabled"] = normalized_window["arrange_enabled"]
            updates["roblox_window_arrange_columns"] = normalized_window["arrange_columns"]
            updates["roblox_window_arrange_gap"] = normalized_window["arrange_gap"]
            updates["roblox_window_arrange_margin"] = normalized_window["arrange_margin"]
        if "presence_api_enabled" in updates:
            updates["presence_api_enabled"] = False
        if "popup_disconnected_enabled" in updates:
            updates["popup_disconnected_enabled"] = bool(updates["popup_disconnected_enabled"])
        if "popup_scan_interval_seconds" in updates:
            updates["popup_scan_interval_seconds"] = _int_setting(updates["popup_scan_interval_seconds"], 30, 5, 3600)
        if "popup_scan_max_parallel" in updates:
            updates["popup_scan_max_parallel"] = _int_setting(updates["popup_scan_max_parallel"], 2, 1, 32)
        if "presence_assist_rejoin_enabled" in updates:
            updates["presence_assist_rejoin_enabled"] = False
        if "presence_poll_interval_seconds" in updates:
            updates["presence_poll_interval_seconds"] = _int_setting(updates["presence_poll_interval_seconds"], 30, 10, 300)
        if "presence_cache_ttl_seconds" in updates:
            updates["presence_cache_ttl_seconds"] = _int_setting(updates["presence_cache_ttl_seconds"], 30, 10, 300)
        if "multi_roblox_enabled" in updates:
            updates["multi_roblox_enabled"] = bool(updates["multi_roblox_enabled"])
            if not updates["multi_roblox_enabled"]:
                release_multi_roblox_guard()
        if "rt_rotation_enabled" in updates:
            updates["rt_rotation_enabled"] = bool(updates["rt_rotation_enabled"])
        if "game_place_id" in updates:
            updates["game_place_id"] = str(updates["game_place_id"] or "").strip()
        if "game_private_server_url" in updates:
            updates["game_private_server_url"] = str(updates["game_private_server_url"] or "").strip()
        if "auto_create_private_server_enabled" in updates:
            updates["auto_create_private_server_enabled"] = bool(updates["auto_create_private_server_enabled"])
        if "auto_create_private_server_free_only" in updates:
            updates["auto_create_private_server_free_only"] = bool(updates["auto_create_private_server_free_only"])
        cfg_mgr.update(updates)
        cfg_mgr.save()
        applied_defaults = 0
        if "game_place_id" in updates or "game_private_server_url" in updates:
            applied_defaults = _apply_game_defaults(ctx, farm._accounts, persist=True)
        if hasattr(farm, "apply_config_snapshot"):
            farm.apply_config_snapshot()
        return {"ok": True, "updated": list(updates.keys()), "game_defaults_applied": applied_defaults}
