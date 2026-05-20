import os
import atexit
import contextlib
import io
import logging
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

_TEST_USER_ROOT = tempfile.mkdtemp(prefix="cronus-test-user-root-")
if "CRONUS_USER_ROOT" not in os.environ:
    os.environ["CRONUS_USER_ROOT"] = _TEST_USER_ROOT
    atexit.register(shutil.rmtree, _TEST_USER_ROOT, ignore_errors=True)
else:
    shutil.rmtree(_TEST_USER_ROOT, ignore_errors=True)

from core import Account, AccountState, EventBus, SmartQueue, StateManager
import farm as farm_module
from farm import RecoveryCoordinator, SystemMaintenance
from runtime.recovery_context import NETWORK_DISCONNECT, SESSION_CONFLICT, RecoveryAttemptContext, normalize_disconnect_category
from runtime.recovery_owner import RecoveryOwnerRegistry
from runtime.recovery_policy import kill_local_duplicate_for_session_conflict
from runtime.runtime_invariants import check_runtime_invariants
from runtime.invariant_monitor import RuntimeInvariantMonitor
from runtime.orphan_sweeper import RuntimeOrphanSweeper
from runtime.diagnostic_bundle import build_runtime_diagnostic_bundle
from runtime.runtime_health import build_public_farm_health, build_runtime_health, decide_farm_watchdog_action
from runtime.runtime_store import RuntimeStore
from runtime.runtime_timeline import RuntimeTimeline
from runtime.telemetry_view import build_runtime_telemetry
from runtime.command_tracker import RuntimeCommandTracker
from runtime.farm_lifecycle import FarmLifecycleService, _clear_manual_start_failure_gate
from runtime.runtime_scheduler import RuntimeScheduler
from runtime.runtime_state_manager import RuntimeStateManager
from services.network_fault_injector import CommandResult, NetworkFaultInjector, RULE_PREFIX
from services.process_service import ProcessService
from services.roblox_log_evidence import classify_log_line, collect_recent_log_evidence
from services.safe_rotating_log import ProcessSafeRotatingFileHandler
from process_net import ProcessManager


def auth_post(client, path, **kwargs):
    import main

    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("X-Cronus-Token", main.INSTANCE_TOKEN)
    return client.post(path, headers=headers, **kwargs)




class RuntimeHardeningBase:
    class _AlwaysOnlineNet:
        def is_online(self):
            return True


    def _make_recovery(self):
        stop = threading.Event()
        queue = SmartQueue()
        bus = EventBus()
        state_mgr = StateManager(bus)
        recovery = RecoveryCoordinator(
            queue,
            state_mgr,
            bus,
            self._AlwaysOnlineNet(),
            stop,
            {
                "auto_rejoin": True,
                "max_fail_count": 5,
                "max_retry": 10,
                "queue_delay_seconds": 1,
                "network_check_interval": 1,
            },
            accounts=[],
        )
        return recovery, queue, stop


__all__ = [name for name in globals() if not name.startswith("__")]
