from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


def _bool(raw: Dict[str, Any], key: str, default: bool = False) -> bool:
    value = raw.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def _int(raw: Dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(raw.get(key, default))
    except Exception:
        return int(default)


def _float(raw: Dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(raw.get(key, default))
    except Exception:
        return float(default)


def _str(raw: Dict[str, Any], key: str, default: str = "") -> str:
    return str(raw.get(key, default) or "")


@dataclass(frozen=True)
class GameConfig:
    place_id: str = ""
    private_server_url: str = ""
    auto_create_private_server_enabled: bool = False
    auto_create_private_server_free_only: bool = True
    multi_roblox_enabled: bool = True


@dataclass(frozen=True)
class QueueConfig:
    max_concurrent_accounts: int = 40
    delay_seconds: int = 15
    duration_seconds: int = 15
    auto_close_enabled: bool = False
    auto_close_minutes: int = 0


@dataclass(frozen=True)
class PopupDetectorConfig:
    enabled: bool = True
    scan_interval_seconds: int = 30
    max_parallel: int = 2
    startup_grace_seconds: int = 8
    confidence_threshold: float = 1.0
    sample_count: int = 6
    sample_interval_seconds: float = 0.25


@dataclass(frozen=True)
class PerformanceConfig:
    fps_limiter_enabled: bool = False
    fps_limit: int = 240
    graphics_low_enabled: bool = False
    graphics_quality_level: int = 1
    auto_process_priority_enabled: bool = False
    process_priority: str = "low"
    cpu_limiter_enabled: bool = False
    cpu_limiter_mode: str = "hard"
    cpu_limiter_default_percent: float = 20.0
    cpu_limiter_apply_all: bool = True
    cpu_limiter_accounts: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WindowConfig:
    resize_enabled: bool = False
    size_preset: str = "640x480"
    width: int = 640
    height: int = 480
    resize_interval_seconds: int = 10
    arrange_enabled: bool = False
    arrange_columns: int = 6
    arrange_gap: int = 2
    arrange_margin: int = 0


@dataclass(frozen=True)
class ArgusConfigSections:
    game: GameConfig
    queue: QueueConfig
    popup_detector: PopupDetectorConfig
    performance: PerformanceConfig
    window: WindowConfig


def build_config_sections(raw: Dict[str, Any]) -> ArgusConfigSections:
    data = dict(raw or {})
    return ArgusConfigSections(
        game=GameConfig(
            place_id=_str(data, "game_place_id"),
            private_server_url=_str(data, "game_private_server_url"),
            auto_create_private_server_enabled=_bool(data, "auto_create_private_server_enabled", False),
            auto_create_private_server_free_only=_bool(data, "auto_create_private_server_free_only", True),
            multi_roblox_enabled=_bool(data, "multi_roblox_enabled", True),
        ),
        queue=QueueConfig(
            max_concurrent_accounts=_int(data, "max_concurrent_accounts", 40),
            delay_seconds=_int(data, "queue_delay_seconds", 15),
            duration_seconds=_int(data, "queue_duration_seconds", 15),
            auto_close_enabled=_bool(data, "auto_close_enabled", False),
            auto_close_minutes=_int(data, "auto_close_minutes", 0),
        ),
        popup_detector=PopupDetectorConfig(
            enabled=_bool(data, "popup_disconnected_enabled", True),
            scan_interval_seconds=_int(data, "popup_scan_interval_seconds", 30),
            max_parallel=_int(data, "popup_scan_max_parallel", 2),
            startup_grace_seconds=_int(data, "popup_startup_grace_seconds", 8),
            confidence_threshold=_float(data, "popup_confidence_threshold", 1.0),
            sample_count=_int(data, "popup_sample_count", 6),
            sample_interval_seconds=_float(data, "popup_sample_interval_seconds", 0.25),
        ),
        performance=PerformanceConfig(
            fps_limiter_enabled=_bool(data, "fps_limiter_enabled", False),
            fps_limit=_int(data, "fps_limit", 240),
            graphics_low_enabled=_bool(data, "graphics_low_enabled", False),
            graphics_quality_level=_int(data, "graphics_quality_level", 1),
            auto_process_priority_enabled=_bool(data, "auto_process_priority_enabled", False),
            process_priority=_str(data, "process_priority", "low"),
            cpu_limiter_enabled=_bool(data, "cpu_limiter_enabled", False),
            cpu_limiter_mode=_str(data, "cpu_limiter_mode", "hard"),
            cpu_limiter_default_percent=_float(data, "cpu_limiter_default_percent", 20.0),
            cpu_limiter_apply_all=_bool(data, "cpu_limiter_apply_all", True),
            cpu_limiter_accounts=dict(data.get("cpu_limiter_accounts") or {}),
        ),
        window=WindowConfig(
            resize_enabled=_bool(data, "roblox_window_resize_enabled", False),
            size_preset=_str(data, "roblox_window_size_preset", "640x480"),
            width=_int(data, "roblox_window_width", 640),
            height=_int(data, "roblox_window_height", 480),
            resize_interval_seconds=_int(data, "roblox_window_resize_interval_seconds", 10),
            arrange_enabled=_bool(data, "roblox_window_arrange_enabled", False),
            arrange_columns=_int(data, "roblox_window_arrange_columns", 6),
            arrange_gap=_int(data, "roblox_window_arrange_gap", 2),
            arrange_margin=_int(data, "roblox_window_arrange_margin", 0),
        ),
    )
