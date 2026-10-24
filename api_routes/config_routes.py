from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException, Request

from runtime.account_selection import runtime_account_allowlist
from services.executor_relauncher import EXECUTOR_NAMES
from services.game_switch import capture_game_targets, rejoin_changed_game_targets
from services.performance_settings import (
    normalize_fps_limit,
    normalize_graphics_quality,
    normalize_process_priority,
    normalize_roblox_volume,
)
from services.roblox_multibox import release_multi_roblox_guard

from .context import ApiContext
from .settings_state import (
    _apply_game_defaults,
    _cpu_limiter_settings_from_config,
    _float_setting,
    _int_setting,
    _normalize_window_size_settings,
    persist_config,
)

_ALLOWED_CONFIG_KEYS = {
    "auto_rejoin", "rejoin_delay", "max_retry", "max_fail_count",
    "executor_monitor_enabled", "executor_selected", "executor_check_interval_seconds",
    "roblox_auto_update_enabled", "executor_relaunch_enabled",
    "executor_path_volt", "executor_path_potassium", "executor_path_real", "executor_path_madium",
    "crash_timeout", "heartbeat_timeout", "launch_verify_window", "login_warmup_delay",
    "anti_spam_window", "launch_rate_interval", "account_switch_cooldown",
    "queue_delay_seconds", "max_concurrent_accounts",
    "lua_enabled", "lua_timeout_seconds",
    "game_private_server_url", "game_place_id",
    "game_mode",
    "games", "games_migrated",
    "auto_create_private_server_enabled", "auto_create_private_server_free_only",
    "block_same_server_enabled",
    "auto_close_enabled", "auto_close_minutes",
    "auto_minimize_enabled", "auto_minimize_seconds",
    "not_responding_timeout",
    "network_check_interval", "network_debounce",
    "queue_timeout", "cooldown_after_crash", "relaunch_loop_limit",
    "relaunch_loop_fatal", "relaunch_loop_cooldown_seconds",
    "connection_error_rejoin", "popup_disconnected_enabled",
    "machine_supervisor_enabled", "machine_supervisor_max_launching_accounts",
    "machine_supervisor_cpu_high_percent", "machine_supervisor_memory_high_percent",
    "roblox_memory_guard_enabled", "roblox_memory_guard_mb", "roblox_memory_guard_hold_seconds",
    "ram_cleanup_enabled", "ram_cleanup_threshold_pct", "ram_cleanup_interval_min",
    "process_trim_enabled", "process_trim_threshold_mb", "process_trim_cooldown_sec",
    "process_trim_max_per_cycle",
    "virtual_memory_mode", "virtual_memory_size_gb",
    "popup_scan_interval_seconds", "popup_scan_max_parallel",
    "connection_error_hold_time",
    "watchdog_enabled", "watchdog_cpu_low",
    "watchdog_ram_low", "watchdog_hold_time",
    "watchdog_activity_timeout", "watchdog_loading_grace",
    "recovery_storm_enabled", "recovery_storm_max_active",
    "recovery_storm_min_spacing_seconds", "recovery_storm_jitter_seconds",
    "recovery_storm_outage_backoff_seconds",
    "recovery_restore_window", "event_bus_workers", "event_bus_max_pending",
    "fps_limiter_enabled", "fps_limit", "graphics_auto_enabled", "graphics_low_enabled", "graphics_quality_level",
    "roblox_volume_muted", "roblox_volume_level",
    "auto_process_priority_enabled", "process_priority",
    "cpu_limiter_enabled", "cpu_limiter_mode", "cpu_limiter_default_percent",
    "cpu_limiter_apply_all", "cpu_limiter_accounts",
    "roblox_window_unlock_size_enabled", "roblox_window_resize_enabled", "roblox_window_size_preset", "roblox_window_width",
    "roblox_window_height", "roblox_window_resize_interval_seconds",
    "roblox_window_arrange_enabled", "roblox_window_arrange_columns", "roblox_window_arrange_rows",
    "roblox_window_arrange_gap", "roblox_window_arrange_margin", "roblox_window_hide_enabled",
    "multi_roblox_enabled", "rt_rotation_enabled",
    "runtime_account_allowlist",
    "start_on_boot", "start_farming_on_boot", "auto_update_on_boot",
    "privacy_blur_usernames", "privacy_randomize_usernames",
    "discord_presence_enabled", "discord_client_id",
    "discord_show_accounts", "discord_show_game",
    "ui_col_widths",
}

_ALLOWED_EXECUTORS = {"", "Volt", "Potassium", "Real", "Madium"}
_EXECUTOR_PATH_KEYS = {
    "executor_path_volt",
    "executor_path_potassium",
    "executor_path_real",
    "executor_path_madium",
}
_GAME_SWITCH_KEYS = {"game_mode", "game_place_id", "game_private_server_url", "games"}
_CPU_LIMITER_KEYS = (
    "cpu_limiter_enabled",
    "cpu_limiter_mode",
    "cpu_limiter_default_percent",
    "cpu_limiter_apply_all",
    "cpu_limiter_accounts",
)
_ROBLOX_WINDOW_KEYS = (
    "roblox_window_unlock_size_enabled",
    "roblox_window_resize_enabled",
    "roblox_window_size_preset",
    "roblox_window_width",
    "roblox_window_height",
    "roblox_window_resize_interval_seconds",
    "roblox_window_arrange_enabled",
    "roblox_window_arrange_columns",
    "roblox_window_arrange_rows",
    "roblox_window_arrange_gap",
    "roblox_window_arrange_margin",
    "roblox_window_hide_enabled",
)
_PLAIN_BOOL_KEYS = (
    "start_on_boot",
    "start_farming_on_boot",
    "auto_update_on_boot",
    "privacy_blur_usernames",
    "privacy_randomize_usernames",
    "discord_presence_enabled",
    "discord_show_accounts",
    "discord_show_game",
    "auto_close_enabled",
    "auto_minimize_enabled",
    "fps_limiter_enabled",
    "graphics_auto_enabled",
    "roblox_volume_muted",
    "auto_process_priority_enabled",
    "popup_disconnected_enabled",
    "relaunch_loop_fatal",
    "roblox_memory_guard_enabled",
    "ram_cleanup_enabled",
    "process_trim_enabled",
    "rt_rotation_enabled",
    "games_migrated",
    "auto_create_private_server_enabled",
    "auto_create_private_server_free_only",
    "block_same_server_enabled",
)
_INT_SETTINGS = (
    ("executor_check_interval_seconds", 30, 30, 3600),
    ("max_concurrent_accounts", 40, 1, 500),
    ("lua_timeout_seconds", 60, 1, 300),
    ("auto_close_minutes", 0, 0, 1440),
    ("auto_minimize_seconds", 10, 1, 3600),
    ("popup_scan_interval_seconds", 30, 5, 3600),
    ("popup_scan_max_parallel", 2, 1, 32),
    ("process_trim_threshold_mb", 1200, 256, 8192),
    ("process_trim_cooldown_sec", 300, 60, 3600),
    ("process_trim_max_per_cycle", 1, 1, 10),
    ("virtual_memory_size_gb", 16, 1, 64),
)
_FLOAT_SETTINGS = (
    ("relaunch_loop_cooldown_seconds", 300.0, 10.0, 86400.0),
    ("roblox_memory_guard_mb", 6144.0, 512.0, 65536.0),
    ("roblox_memory_guard_hold_seconds", 30.0, 5.0, 3600.0),
    ("ram_cleanup_threshold_pct", 85.0, 50.0, 95.0),
)


def _api_get_config(ctx: ApiContext) -> dict[str, Any]:
    snap = ctx.cfg_mgr.snapshot()
    snap.pop("accounts", None)
    snap.pop("runtime_state", None)
    return snap


def _effective_lua_early(updates: dict[str, Any], cfg_mgr) -> bool:
    try:
        if "lua_enabled" in updates:
            return bool(updates["lua_enabled"])
        return bool(cfg_mgr.get("lua_enabled", True))
    except Exception:
        return True


def _effective_lua(updates: dict[str, Any], cfg_mgr) -> bool:
    try:
        value = updates.get("lua_enabled", cfg_mgr.get("lua_enabled", True))
        if "lua_enabled" not in updates:
            try:
                snap = cfg_mgr.snapshot()
                if isinstance(snap, dict) and "lua_enabled" in snap:
                    value = snap.get("lua_enabled", True)
            except Exception:
                "intentional: ignore exception"
        return bool(value)
    except Exception:
        return True


def _normalize_executor_monitor(updates: dict[str, Any], lua_enabled: bool) -> None:
    if "executor_monitor_enabled" not in updates:
        return
    updates["executor_monitor_enabled"] = bool(updates["executor_monitor_enabled"])
    if updates["executor_monitor_enabled"] and not lua_enabled:
        raise HTTPException(400, "Enable Use Lua before using Roblox Executor")


def _normalize_roblox_auto_update(
    updates: dict[str, Any], lua_enabled: bool
) -> None:
    if "roblox_auto_update_enabled" not in updates:
        return
    updates["roblox_auto_update_enabled"] = bool(
        updates["roblox_auto_update_enabled"]
    )
    if updates["roblox_auto_update_enabled"] and not lua_enabled:
        raise HTTPException(400, "Enable Use Lua before using Roblox Executor")


def _normalize_executor_selected(updates: dict[str, Any]) -> None:
    if "executor_selected" not in updates:
        return
    selected = str(updates["executor_selected"] or "").strip()
    if selected not in _ALLOWED_EXECUTORS:
        raise HTTPException(400, "Unsupported Roblox Executor")
    updates["executor_selected"] = selected


def _relaunch_target(updates: dict[str, Any], cfg_mgr) -> tuple[str, str]:
    selected = str(
        updates.get("executor_selected", cfg_mgr.get("executor_selected", "")) or ""
    ).strip()
    path_key = f"executor_path_{selected.lower()}"
    path = str(updates.get(path_key, cfg_mgr.get(path_key, "")) or "").strip()
    return selected, path


def _normalize_executor_relaunch(
    updates: dict[str, Any], cfg_mgr, lua_enabled: bool
) -> None:
    if "executor_relaunch_enabled" not in updates:
        return
    enabled = bool(updates["executor_relaunch_enabled"])
    updates["executor_relaunch_enabled"] = enabled
    if enabled and not lua_enabled:
        raise HTTPException(400, "Enable Use Lua before using Roblox Executor")
    if not enabled:
        return
    selected, path = _relaunch_target(updates, cfg_mgr)
    if selected not in EXECUTOR_NAMES:
        raise HTTPException(
            400, "Auto Relaunch is unavailable: select a supported Roblox Executor"
        )
    if not path or not path.lower().endswith(".exe") or not os.path.isfile(path):
        raise HTTPException(
            400, "Auto Relaunch is unavailable: Browse the selected Executor .exe first"
        )


def _disable_broken_relaunch(updates: dict[str, Any], cfg_mgr) -> None:
    enabled = updates.get(
        "executor_relaunch_enabled", cfg_mgr.get("executor_relaunch_enabled", False)
    )
    if not enabled:
        return
    selected, selected_path = _relaunch_target(updates, cfg_mgr)
    if (
        selected not in EXECUTOR_NAMES
        or not selected_path
        or not os.path.isfile(selected_path)
    ):
        updates["executor_relaunch_enabled"] = False


def _normalize_executor_paths(
    updates: dict[str, Any], cfg_mgr, farm
) -> None:
    touched = _EXECUTOR_PATH_KEYS.intersection(updates)
    if not touched and "executor_selected" not in updates:
        return
    if getattr(farm, "running", False):
        raise HTTPException(
            409, "Stop Auto Rejoin before changing Executor selection or path"
        )
    for key in touched:
        value = str(updates[key] or "").strip()
        if value and (not value.lower().endswith(".exe") or not os.path.isfile(value)):
            raise HTTPException(400, f"Invalid Executor .exe path: {value}")
        updates[key] = value
    _disable_broken_relaunch(updates, cfg_mgr)


def _normalize_queue_delay(updates: dict[str, Any]) -> None:
    if "queue_delay_seconds" not in updates:
        return
    delay = _int_setting(updates["queue_delay_seconds"], 15, 0, 3600)
    updates["queue_delay_seconds"] = delay
    updates["launch_rate_interval"] = delay
    updates["account_switch_cooldown"] = delay


def _normalize_int_settings(updates: dict[str, Any]) -> None:
    for key, default, low, high in _INT_SETTINGS:
        if key in updates:
            updates[key] = _int_setting(updates[key], default, low, high)


def _normalize_float_settings(updates: dict[str, Any]) -> None:
    for key, default, low, high in _FLOAT_SETTINGS:
        if key in updates:
            updates[key] = _float_setting(updates[key], default, low, high)


def _normalize_plain_booleans(updates: dict[str, Any]) -> None:
    for key in _PLAIN_BOOL_KEYS:
        if key in updates:
            updates[key] = bool(updates[key])


def _normalize_discord_client_id(updates: dict[str, Any]) -> None:
    if "discord_client_id" not in updates:
        return
    cid = str(updates["discord_client_id"] or "").strip()
    if cid and (not cid.isdigit() or not (15 <= len(cid) <= 22)):
        raise HTTPException(400, "Invalid Discord Client ID (15-22 digits)")
    updates["discord_client_id"] = cid


def _normalize_lua_enabled(updates: dict[str, Any]) -> None:
    if "lua_enabled" in updates:
        updates["lua_enabled"] = bool(updates["lua_enabled"])


def _enforce_executor_without_lua(
    updates: dict[str, Any], lua_enabled: bool
) -> None:
    if lua_enabled:
        return
    if updates.get("executor_monitor_enabled") is True:
        raise HTTPException(400, "Enable Use Lua before using Roblox Executor")
    if updates.get("executor_relaunch_enabled") is True:
        raise HTTPException(400, "Enable Use Lua before using Roblox Executor")
    if updates.get("roblox_auto_update_enabled") is True:
        raise HTTPException(400, "Enable Use Lua before using Roblox Executor")
    # Turning Lua off forces executor features off in the same write
    # so background monitor/relaunch stops immediately.
    if updates.get("lua_enabled") is False:
        updates["executor_monitor_enabled"] = False
        updates["executor_relaunch_enabled"] = False
        updates["roblox_auto_update_enabled"] = False


def _normalize_fps(updates: dict[str, Any]) -> None:
    if "fps_limit" not in updates:
        return
    try:
        updates["fps_limit"] = normalize_fps_limit(updates["fps_limit"])
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def _normalize_graphics(updates: dict[str, Any]) -> None:
    if "graphics_low_enabled" in updates:
        updates["graphics_low_enabled"] = bool(updates["graphics_low_enabled"])
        updates["graphics_auto_enabled"] = updates["graphics_low_enabled"]
    if "graphics_quality_level" not in updates:
        return
    try:
        updates["graphics_quality_level"] = normalize_graphics_quality(
            updates["graphics_quality_level"]
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def _normalize_volume(updates: dict[str, Any]) -> None:
    if "roblox_volume_level" not in updates:
        return
    try:
        updates["roblox_volume_level"] = normalize_roblox_volume(
            updates["roblox_volume_level"]
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def _normalize_priority(updates: dict[str, Any]) -> None:
    if "process_priority" not in updates:
        return
    try:
        updates["process_priority"] = normalize_process_priority(
            updates["process_priority"]
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def _normalize_cpu_limiter(updates: dict[str, Any], ctx: ApiContext) -> None:
    if not any(key in updates for key in _CPU_LIMITER_KEYS):
        return
    cfg_mgr = ctx.cfg_mgr
    try:
        normalized = _cpu_limiter_settings_from_config(
            ctx,
            {
                "cpu_limiter_enabled": updates.get(
                    "cpu_limiter_enabled", cfg_mgr.get("cpu_limiter_enabled", False)
                ),
                "cpu_limiter_mode": updates.get(
                    "cpu_limiter_mode", cfg_mgr.get("cpu_limiter_mode", "hard")
                ),
                "cpu_limiter_default_percent": updates.get(
                    "cpu_limiter_default_percent",
                    cfg_mgr.get("cpu_limiter_default_percent", 20),
                ),
                "cpu_limiter_apply_all": updates.get(
                    "cpu_limiter_apply_all", cfg_mgr.get("cpu_limiter_apply_all", True)
                ),
                "cpu_limiter_accounts": updates.get(
                    "cpu_limiter_accounts", cfg_mgr.get("cpu_limiter_accounts", {})
                ),
            },
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    updates["cpu_limiter_enabled"] = normalized["enabled"]
    updates["cpu_limiter_mode"] = normalized["mode"]
    updates["cpu_limiter_default_percent"] = normalized["default_limit_percent"]
    updates["cpu_limiter_apply_all"] = normalized["apply_all"]
    if normalized["apply_all"]:
        normalized["accounts"] = {}
    updates["cpu_limiter_accounts"] = normalized["accounts"]


def _normalize_window_settings(updates: dict[str, Any], ctx: ApiContext) -> None:
    if not any(key in updates for key in _ROBLOX_WINDOW_KEYS):
        return
    cfg_mgr = ctx.cfg_mgr
    try:
        normalized = _normalize_window_size_settings(
            ctx,
            {
                "unlock_size_enabled": updates.get(
                    "roblox_window_unlock_size_enabled",
                    cfg_mgr.get("roblox_window_unlock_size_enabled", True),
                ),
                "enabled": updates.get(
                    "roblox_window_resize_enabled",
                    cfg_mgr.get("roblox_window_resize_enabled", False),
                ),
                "preset": updates.get(
                    "roblox_window_size_preset",
                    cfg_mgr.get("roblox_window_size_preset", "200x150"),
                ),
                "width": updates.get(
                    "roblox_window_width", cfg_mgr.get("roblox_window_width", 200)
                ),
                "height": updates.get(
                    "roblox_window_height", cfg_mgr.get("roblox_window_height", 150)
                ),
                "interval_seconds": updates.get(
                    "roblox_window_resize_interval_seconds",
                    cfg_mgr.get("roblox_window_resize_interval_seconds", 10),
                ),
                "arrange_enabled": updates.get(
                    "roblox_window_arrange_enabled",
                    cfg_mgr.get("roblox_window_arrange_enabled", False),
                ),
                "arrange_columns": updates.get(
                    "roblox_window_arrange_columns",
                    cfg_mgr.get("roblox_window_arrange_columns", 6),
                ),
                "arrange_rows": updates.get(
                    "roblox_window_arrange_rows",
                    cfg_mgr.get("roblox_window_arrange_rows", 4),
                ),
                "arrange_gap": updates.get(
                    "roblox_window_arrange_gap",
                    cfg_mgr.get("roblox_window_arrange_gap", 0),
                ),
                "arrange_margin": updates.get(
                    "roblox_window_arrange_margin",
                    cfg_mgr.get("roblox_window_arrange_margin", 0),
                ),
                "hide_enabled": updates.get(
                    "roblox_window_hide_enabled",
                    cfg_mgr.get("roblox_window_hide_enabled", False),
                ),
            },
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    updates["roblox_window_unlock_size_enabled"] = normalized["unlock_size_enabled"]
    updates["roblox_window_resize_enabled"] = normalized["enabled"]
    updates["roblox_window_size_preset"] = normalized["preset"]
    updates["roblox_window_width"] = normalized["width"]
    updates["roblox_window_height"] = normalized["height"]
    updates["roblox_window_resize_interval_seconds"] = normalized["interval_seconds"]
    updates["roblox_window_arrange_enabled"] = normalized["arrange_enabled"]
    updates["roblox_window_arrange_columns"] = normalized["arrange_columns"]
    updates["roblox_window_arrange_rows"] = normalized["arrange_rows"]
    updates["roblox_window_arrange_gap"] = normalized["arrange_gap"]
    updates["roblox_window_arrange_margin"] = normalized["arrange_margin"]
    updates["roblox_window_hide_enabled"] = normalized["hide_enabled"]


def _normalize_virtual_memory_mode(updates: dict[str, Any]) -> None:
    if "virtual_memory_mode" not in updates:
        return
    mode = str(updates["virtual_memory_mode"] or "system_managed").strip().lower()
    updates["virtual_memory_mode"] = (
        mode if mode in ("system_managed", "custom") else "system_managed"
    )


def _normalize_ui_widths(updates: dict[str, Any]) -> None:
    if "ui_col_widths" not in updates:
        return
    raw_widths = updates["ui_col_widths"]
    clean_widths = {}
    if isinstance(raw_widths, dict):
        for key in ("col1", "col2", "col3"):
            try:
                val = int(float(raw_widths.get(key)))
            except Exception:
                continue
            clean_widths[key] = max(44, min(520, val))
    updates["ui_col_widths"] = clean_widths


def _normalize_games(updates: dict[str, Any], cfg_mgr) -> None:
    if "games" not in updates:
        return
    try:
        from domain.games import normalize_games

        snap = cfg_mgr.snapshot()
        snap["games"] = updates["games"]
        updates["games"] = normalize_games(snap["games"])
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception:
        updates["games"] = []


def _normalize_game_mode(updates: dict[str, Any]) -> None:
    if "game_mode" not in updates:
        return
    try:
        from domain.games import normalize_game_mode

        updates["game_mode"] = normalize_game_mode(updates.get("game_mode"))
    except Exception:
        updates["game_mode"] = "shared"


def _normalize_trimmed_strings(
    updates: dict[str, Any], keys: tuple[str, ...]
) -> None:
    for key in keys:
        if key in updates:
            updates[key] = str(updates[key] or "").strip()


def _normalize_ram_cleanup_interval(updates: dict[str, Any]) -> None:
    if "ram_cleanup_interval_min" not in updates:
        return
    try:
        value = int(float(updates["ram_cleanup_interval_min"]))
    except Exception:
        value = 15
    updates["ram_cleanup_interval_min"] = max(5, value)


def _normalize_multi_roblox(updates: dict[str, Any]) -> None:
    if "multi_roblox_enabled" not in updates:
        return
    updates["multi_roblox_enabled"] = bool(updates["multi_roblox_enabled"])
    if not updates["multi_roblox_enabled"]:
        release_multi_roblox_guard()


def _normalize_runtime_allowlist(updates: dict[str, Any]) -> None:
    if "runtime_account_allowlist" in updates:
        updates["runtime_account_allowlist"] = runtime_account_allowlist(updates)


def _capture_targets(farm, cfg_mgr) -> dict:
    try:
        return capture_game_targets(farm._accounts, cfg_mgr.snapshot())
    except Exception:
        return {}


def _rejoin_games(farm, cfg_mgr, targets_before: dict) -> tuple[list, list]:
    if not targets_before:
        return [], []
    try:
        switched = rejoin_changed_game_targets(farm, targets_before, cfg_mgr)
        if isinstance(switched, dict):
            return (
                list(switched.get("rejoined") or []),
                list(switched.get("stopped") or []),
            )
        return list(switched or []), []
    except Exception:
        return [], []


def _apply_config_updates(
    ctx: ApiContext, updates: dict[str, Any]
) -> tuple[int, list, list]:
    cfg_mgr = ctx.cfg_mgr
    farm = ctx.farm
    targets_before = {}
    if _GAME_SWITCH_KEYS.intersection(updates):
        targets_before = _capture_targets(farm, cfg_mgr)
    persist_config(ctx, updates)
    applied_defaults = 0
    if _GAME_SWITCH_KEYS.intersection(updates):
        applied_defaults = _apply_game_defaults(ctx, farm._accounts, persist=True)
    if hasattr(farm, "apply_config_snapshot"):
        farm.apply_config_snapshot()
    game_rejoined, game_stopped = _rejoin_games(farm, cfg_mgr, targets_before)
    return applied_defaults, game_rejoined, game_stopped


def _log_config_update(
    updates: dict[str, Any], applied_defaults: int, game_rejoined: list
) -> None:
    try:
        from core import flog_kv as _flog_kv

        _flog_kv(
            "CONFIG",
            "updated",
            updated=list(updates.keys()),
            game_defaults_applied=applied_defaults,
            game_rejoined=len(game_rejoined),
        )
    except Exception:
        "intentional: ignore exception"


async def _api_set_config(ctx: ApiContext, request: Request) -> dict[str, Any]:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected object")
    updates = {key: value for key, value in body.items() if key in _ALLOWED_CONFIG_KEYS}
    # Effective Lua state for this write (request value wins, else saved).
    lua_early = _effective_lua_early(updates, ctx.cfg_mgr)
    _normalize_executor_monitor(updates, lua_early)
    _normalize_executor_selected(updates)
    _normalize_roblox_auto_update(updates, lua_early)
    _normalize_executor_relaunch(updates, ctx.cfg_mgr, lua_early)
    _normalize_executor_paths(updates, ctx.cfg_mgr, ctx.farm)
    _normalize_queue_delay(updates)
    _normalize_int_settings(updates)
    _normalize_float_settings(updates)
    _normalize_plain_booleans(updates)
    _normalize_discord_client_id(updates)
    _normalize_lua_enabled(updates)
    _enforce_executor_without_lua(updates, _effective_lua(updates, ctx.cfg_mgr))
    _normalize_fps(updates)
    _normalize_graphics(updates)
    _normalize_volume(updates)
    _normalize_priority(updates)
    _normalize_cpu_limiter(updates, ctx)
    _normalize_window_settings(updates, ctx)
    _normalize_virtual_memory_mode(updates)
    _normalize_ui_widths(updates)
    _normalize_games(updates, ctx.cfg_mgr)
    _normalize_game_mode(updates)
    _normalize_trimmed_strings(updates, ("game_place_id", "game_private_server_url"))
    _normalize_runtime_allowlist(updates)
    _normalize_multi_roblox(updates)
    _normalize_ram_cleanup_interval(updates)
    applied_defaults, game_rejoined, game_stopped = _apply_config_updates(ctx, updates)
    _log_config_update(updates, applied_defaults, game_rejoined)
    return {
        "ok": True,
        "updated": list(updates.keys()),
        "game_defaults_applied": applied_defaults,
        "game_rejoined": game_rejoined,
        "game_stopped": game_stopped,
    }


def register(app, ctx: ApiContext) -> None:
    @app.get("/api/config")
    def api_get_config():
        return _api_get_config(ctx)

    @app.post("/api/config")
    async def api_set_config(request: Request):
        return await _api_set_config(ctx, request)
