from __future__ import annotations

import threading
from typing import Any, Dict, Tuple

from services import roblox_launch_service as _launch_service
from services import roblox_liveness as _liveness
from services import roblox_processes as _processes
from services import roblox_windows as _windows

# Internal process backend used by process services.
from services.resource_monitor import RealtimeResourceMonitor, get_rt_monitor
from services.cookie_service import IsolationManager
from services.vip_tracker import VipTracker
from services.network_monitor import (
    NET_ONLINE,
    NET_DEGRADED,
    NET_OFFLINE,
    NetworkState,
    NetworkMonitor,
)

ROBLOX_GAME_NAMES = _processes.ROBLOX_GAME_NAMES
ROBLOX_NAMES = _processes.ROBLOX_NAMES
_rt_monitor = get_rt_monitor()


class ProcessManager:
    LOGIN_WARMUP_URL = "roblox://navigation/home"
    LOGIN_WARMUP_DELAY = 3.0
    MULTI_ROBLOX_ENABLED = True
    GLOBAL_VIP_LINK = ""
    AUTO_CREATE_PRIVATE_SERVER_ENABLED = False
    AUTO_CREATE_PRIVATE_SERVER_FREE_ONLY = True
    CONNECTION_ERROR_KEYWORDS = (
        "connection error",
        "lost connection",
        "disconnected",
        "reconnect",
        "failed to connect",
        "teleport failed",
        "internet connection",
        "connection lost",
        "please check your internet connection",
        "lost connection to the game server",
    )
    REJOINABLE_DISCONNECT_CODES = {"277"}
    CONDITIONAL_REJOIN_DISCONNECT_CODES = {"273"}
    FATAL_DISCONNECT_CODES = {
        "267": "security_kick",
        "268": "unexpected_client_behavior",
    }
    _process_cache: Dict[int, Any] = {}
    _cache_lock = threading.Lock()
    _nr_cache: Dict[int, Tuple[float, bool]] = {}
    _nr_cache_ttl = 2.0
    _ownership_lock = threading.Lock()
    _pid_owner: Dict[int, str] = {}
    HIGH_CONFIDENCE = 75.0
    MEDIUM_CONFIDENCE = 45.0

    classify_disconnect_dialog_texts = classmethod(_windows.classify_disconnect_dialog_texts)

    _same_windows_user = staticmethod(_processes._same_windows_user)
    extract_browser_tracker_id_from_cmdline = staticmethod(_processes.extract_browser_tracker_id)
    get_process_identity = classmethod(_processes.get_process_identity)
    claim_pid_owner = classmethod(_processes.claim_pid_owner)
    release_pid_owner = classmethod(_processes.release_pid_owner)
    get_pid_owner = classmethod(_processes.get_pid_owner)
    cleanup_stale_pid_claims = classmethod(_processes.cleanup_stale_pid_claims)
    _iter_roblox_processes = classmethod(_processes._iter_roblox_processes)
    _inspect_roblox_process = classmethod(_processes._inspect_roblox_process)
    validate_game_process = classmethod(_processes.validate_game_process)
    get_game_activity = classmethod(_processes.get_game_activity)
    get_pid_cmdline = classmethod(_processes.get_pid_cmdline)
    confidence_level = classmethod(_processes.confidence_level)
    find_bound_game_process = classmethod(_processes.find_bound_game_process)
    summarize_game_presence = classmethod(_processes.summarize_game_presence)
    list_live_game_processes = classmethod(_processes.list_live_game_processes)
    snapshot_pids = classmethod(_processes.snapshot_pids)
    kill_all_roblox_clients = classmethod(_processes.kill_all_roblox_clients)
    cleanup_extra_launch_processes = classmethod(_processes.cleanup_extra_launch_processes)
    detect_new_pid = classmethod(_processes.detect_new_pid)
    is_pid_alive = classmethod(_processes.is_pid_alive)
    is_bound_game_alive = classmethod(_processes.is_bound_game_alive)
    get_pid_cpu = classmethod(_processes.get_pid_cpu)
    get_pid_memory_mb = classmethod(_processes.get_pid_memory_mb)
    evict_pid_cache = classmethod(_processes.evict_pid_cache)
    kill_pid = classmethod(_processes.kill_pid)

    multi_signal_validate = classmethod(_liveness.multi_signal_validate)
    staged_orphan_reconcile = classmethod(_liveness.staged_orphan_reconcile)
    assess_liveness = classmethod(_liveness.assess_liveness)

    _window_snapshot_for_pid = classmethod(_windows._window_snapshot_for_pid)
    _count_visible_windows_for_pid = classmethod(_windows._count_visible_windows_for_pid)
    _visible_roblox_windows = classmethod(_windows._visible_roblox_windows)
    minimize_roblox_windows = classmethod(_windows.minimize_roblox_windows)
    resize_roblox_windows = classmethod(_windows.resize_roblox_windows)
    _primary_monitor_work_area = classmethod(_windows._primary_monitor_work_area)
    arrange_roblox_windows = classmethod(_windows.arrange_roblox_windows)
    restore_roblox_window_styles = classmethod(_windows.restore_roblox_window_styles)
    is_not_responding = classmethod(_windows.is_not_responding)
    inspect_disconnect_dialog = classmethod(_windows.inspect_disconnect_dialog)
    detect_connection_error = classmethod(_windows.detect_connection_error)
    _get_pid_window_rect = classmethod(_windows._get_pid_window_rect)
    _capture_pid_window_image = classmethod(_windows._capture_pid_window_image)
    _inspect_disconnect_dialog_visual = classmethod(_windows._inspect_disconnect_dialog_visual)

    parse_vip_link = staticmethod(_launch_service.parse_vip_link)
    build_launch_url = classmethod(_launch_service.build_launch_url)
    launch = classmethod(_launch_service.launch)
