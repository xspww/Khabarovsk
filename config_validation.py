from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple


CONFIG_SCHEMA_VERSION = 2

_INT_RANGES: Dict[str, Tuple[int, int]] = {
    "max_retry": (1, 100),
    "max_fail_count": (1, 100),
    "crash_timeout": (1, 3600),
    "heartbeat_timeout": (1, 3600),
    "lua_wait_timeout": (1, 3600),
    "launch_verify_window": (1, 3600),
    "queue_delay_seconds": (0, 3600),
    "queue_duration_seconds": (0, 86400),
    "max_concurrent_accounts": (1, 200),
    "machine_supervisor_max_launching_accounts": (1, 200),
    "auto_close_minutes": (0, 1440),
    "auto_minimize_seconds": (1, 3600),
    "network_check_interval": (1, 3600),
    "network_debounce": (0, 3600),
    "periodic_reconcile_interval": (1, 3600),
    "queue_timeout": (1, 86400),
    "recovery_budget_max_attempts": (1, 1000),
    "recovery_budget_window_seconds": (1, 86400),
    "recovery_storm_max_active": (1, 200),
    "popup_scan_interval_seconds": (5, 3600),
    "popup_scan_max_parallel": (1, 64),
    "popup_sample_count": (1, 60),
    "runtime_invariant_suppress_seconds": (1, 3600),
    "watchdog_activity_timeout": (1, 86400),
    "watchdog_loading_grace": (1, 86400),
    "home_rejoin_grace_seconds": (1, 86400),
    "fps_limit": (15, 1000),
    "graphics_quality_level": (1, 10),
    "roblox_window_width": (320, 1920),
    "roblox_window_height": (240, 1080),
    "roblox_window_arrange_columns": (1, 32),
    "roblox_window_arrange_gap": (0, 80),
}

_FLOAT_RANGES: Dict[str, Tuple[float, float]] = {
    "recovery_confidence_threshold": (0.0, 100.0),
    "machine_supervisor_cpu_high_percent": (1.0, 100.0),
    "machine_supervisor_memory_high_percent": (1.0, 100.0),
    "popup_confidence_threshold": (0.1, 5.0),
    "popup_sample_interval_seconds": (0.05, 60.0),
    "recovery_dedupe_window_seconds": (0.1, 3600.0),
    "recovery_storm_min_spacing_seconds": (0.0, 3600.0),
    "recovery_storm_jitter_seconds": (0.0, 3600.0),
    "recovery_storm_outage_backoff_seconds": (0.0, 86400.0),
    "relaunch_loop_cooldown_seconds": (10.0, 86400.0),
    "session_conflict_window_seconds": (1.0, 3600.0),
    "orphan_sweeper_min_confidence": (0.0, 100.0),
    "recovery_restore_window": (0.0, 86400.0),
    "watchdog_cpu_low": (0.0, 100.0),
    "watchdog_ram_low": (0.0, 4096.0),
    "watchdog_hold_time": (1.0, 86400.0),
    "roblox_memory_guard_mb": (512.0, 65536.0),
    "roblox_memory_guard_hold_seconds": (5.0, 3600.0),
    "home_rejoin_hold_seconds": (1.0, 3600.0),
    "cpu_limiter_default_percent": (5.0, 95.0),
    "roblox_window_resize_interval_seconds": (1.0, 3600.0),
}


def _clamp(value: float, bounds: Tuple[float, float]) -> float:
    return max(bounds[0], min(bounds[1], value))


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return bool(default)


def _int(value: Any, default: int, bounds: Tuple[int, int] | None = None) -> int:
    try:
        parsed = int(float(value))
    except Exception:
        parsed = int(default)
    if bounds:
        parsed = int(_clamp(parsed, bounds))
    return parsed


def _float(value: Any, default: float, bounds: Tuple[float, float] | None = None) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = float(default)
    if bounds:
        parsed = float(_clamp(parsed, bounds))
    return parsed


def validate_config_payload(raw: Any, defaults: Mapping[str, Any]) -> Dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    clean: Dict[str, Any] = {}
    for key, default in defaults.items():
        value = source.get(key, default)
        if isinstance(default, bool):
            clean[key] = _bool(value, default)
        elif isinstance(default, int) and not isinstance(default, bool):
            clean[key] = _int(value, default, _INT_RANGES.get(key))
        elif isinstance(default, float):
            clean[key] = _float(value, default, _FLOAT_RANGES.get(key))
        elif isinstance(default, str):
            clean[key] = str(value or "")
        elif isinstance(default, list):
            clean[key] = value if isinstance(value, list) else list(default)
        elif isinstance(default, dict):
            clean[key] = value if isinstance(value, dict) else dict(default)
        else:
            clean[key] = value
    for key, value in source.items():
        if key not in clean and not str(key).startswith("__"):
            clean[key] = value
    clean["schema_version"] = CONFIG_SCHEMA_VERSION
    return clean
