from __future__ import annotations

import threading
from typing import Any, ClassVar

from services import roblox_launch_service as _launch_service
from services import roblox_liveness as _liveness
from services import roblox_processes as _processes
from services import roblox_windows as _windows

# Internal process backend used by process services.
#
# Ownership map — fragments live in four modules, they run here:
# - process identity / ownership / validation (pid owner dict, pid caches,
#   confidence thresholds): services.roblox_processes
# - window enumeration / arrange / hide / screenshots: services.roblox_windows
# - liveness scoring / orphan reconcile: services.roblox_liveness
# - VIP-aware launch URL + launch: services.roblox_launch_service
# Every fragment takes cls and reads the ClassVar state below; the binder
# lines that follow are the only seam — see tests/test_process_backend_wiring.py.
from services.resource_monitor import get_rt_monitor

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
    # Shared GAME card fallback: place every account joins unless a
    # per-account game/override supplies its own target.
    SHARED_PLACE_ID = ""
    # GAME card mode switch: "shared" (pool ignored) or "per_account".
    GAME_MODE = "shared"
    # Read-only snapshot of the configured games list (list of dicts).
    # Replaced wholesale on every launch attempt; per-account game
    # resolution reads from it so concurrent launches for different
    # games never race on GLOBAL_VIP_LINK.
    GAMES_SNAPSHOT: ClassVar[list] = []
    CONNECTION_ERROR_KEYWORDS: ClassVar[tuple] = (
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
    REJOINABLE_DISCONNECT_CODES: ClassVar[set] = {"277"}
    CONDITIONAL_REJOIN_DISCONNECT_CODES: ClassVar[set] = {"273"}
    FATAL_DISCONNECT_CODES: ClassVar[dict[str, str]] = {
        "267": "security_kick",
        "268": "unexpected_client_behavior",
    }
    _process_cache: ClassVar[dict[int, Any]] = {}
    _cache_lock = threading.Lock()
    _nr_cache: ClassVar[dict[int, tuple[float, bool]]] = {}
    _nr_cache_ttl = 2.0
    _ownership_lock = threading.Lock()
    _pid_owner: ClassVar[dict[int, str]] = {}
    HIGH_CONFIDENCE = 75.0
    MEDIUM_CONFIDENCE = 45.0

    classify_disconnect_dialog_texts = classmethod(_windows.classify_disconnect_dialog_texts)

    _same_windows_user = staticmethod(_processes._same_windows_user)
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
    _hidden_roblox_windows = classmethod(_windows._hidden_roblox_windows)
    minimize_roblox_windows = classmethod(_windows.minimize_roblox_windows)
    resize_roblox_windows = classmethod(_windows.resize_roblox_windows)
    _primary_monitor_work_area = classmethod(_windows._primary_monitor_work_area)
    arrange_roblox_windows = classmethod(_windows.arrange_roblox_windows)
    restore_roblox_window_styles = classmethod(_windows.restore_roblox_window_styles)
    unminimize_roblox_windows = classmethod(_windows.unminimize_roblox_windows)
    hide_roblox_windows = classmethod(_windows.hide_roblox_windows)
    show_roblox_windows = classmethod(_windows.show_roblox_windows)
    is_not_responding = classmethod(_windows.is_not_responding)
    inspect_disconnect_dialog = classmethod(_windows.inspect_disconnect_dialog)
    detect_connection_error = classmethod(_windows.detect_connection_error)
    _get_pid_window_rect = classmethod(_windows._get_pid_window_rect)
    _capture_pid_window_image = classmethod(_windows._capture_pid_window_image)
    _inspect_disconnect_dialog_visual = classmethod(_windows._inspect_disconnect_dialog_visual)

    parse_vip_link = staticmethod(_launch_service.parse_vip_link)
    build_launch_url = classmethod(_launch_service.build_launch_url)
    launch = classmethod(_launch_service.launch)
