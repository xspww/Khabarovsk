from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ApiContext:
    cfg_mgr: Any
    farm: Any
    roblox_installer: Any
    html_ui: str
    instance_token: str
    shutdown_requested: Any
    clear_instance_state: Callable[[], None]
    get_network_fault_injector: Callable[[], Any]
    get_log_file: Callable[[], str]
    get_apply_graphics_settings_file: Callable[[], Any]
    get_apply_performance_settings_file: Callable[[], Any]
    get_apply_process_priority_to_roblox: Callable[[], Any]
