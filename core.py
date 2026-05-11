from __future__ import annotations

import json
import os
import queue
import re
import shutil
import threading
import time
import logging
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from logging.handlers import RotatingFileHandler
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from app_paths import APP_DATA_DIR, APP_ROOT_DIR, LEGACY_APP_DATA_DIR, migrate_legacy_data_files
from domain.account_state import AccountState, RuntimeState
from domain.public_state_mapper import (
    LIFECYCLE_STATE,
    PUBLIC_TO_RUNTIME_STATE,
    RUNTIME_TO_DEFAULT_PUBLIC_STATE,
    public_state_for_runtime,
    runtime_state_for_public,
)
from domain.runtime_models import AccountRuntime
from domain.state_transitions import LIFECYCLE_ALLOWED_TRANSITIONS
from runtime.runtime_state_manager import RuntimeStateManager

# ─────────────────────────────────────────────────────────────────────────────
#  FILE LOGGER
# ─────────────────────────────────────────────────────────────────────────────
for _filename in (
    "AccountData.json",
    "account_tools_audit.jsonl",
    "account_import_pending.json",
    "roboguard_rt1.log",
    "roboguard_rt1_events.jsonl",
    "roboguard_rt1_config.json",
    "roboguard_rt1_cookies.json",
    "roboguard_rt12_accounts.txt",
    "roboguard_rt12_runtime.txt",
    "roboguard_runtime.db",
    "roboguard_runtime.db-shm",
    "roboguard_runtime.db-wal",
):
    migrate_legacy_data_files((_filename,))

LOG_FILE = os.path.join(APP_DATA_DIR, "roboguard_rt1.log")
STRUCTURED_LOG_FILE = os.path.join(APP_DATA_DIR, "roboguard_rt1_events.jsonl")
_logger = logging.getLogger("roboguard_rt1")
_logger.setLevel(logging.DEBUG)
if not _logger.handlers:
    _fh = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    _logger.addHandler(_fh)

_structured_logger = logging.getLogger("roboguard_rt1.structured")
_structured_logger.setLevel(logging.INFO)
_structured_logger.propagate = False
if not _structured_logger.handlers:
    _json_fh = RotatingFileHandler(STRUCTURED_LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    _json_fh.setFormatter(logging.Formatter("%(message)s"))
    _structured_logger.addHandler(_json_fh)

_SENSITIVE_KEYS = re.compile(r"(cookie|password|token|secret|roblosecurity|privateServerLinkCode|linkCode)", re.I)
_COOKIE_RE = re.compile(r"_\|WARNING:.*?(?=\s|$)", re.I)
_LINK_CODE_RE = re.compile(r"((?:privateServerLinkCode|linkCode)=)[^&\s]+", re.I)


def _redact_value(key: str, value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, dict):
        return {str(k): _redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_redact_value(key, item) for item in value]
    text = str(value)
    if _SENSITIVE_KEYS.search(str(key)):
        return "[REDACTED]"
    text = _COOKIE_RE.sub("[REDACTED_COOKIE]", text)
    text = _LINK_CODE_RE.sub(r"\1[REDACTED]", text)
    return text


def flog_struct(scope: str, event: str, level: str = "info", **fields):
    record = {
        "ts": round(time.time(), 3),
        "level": str(level or "info").lower(),
        "scope": str(scope or "runtime"),
        "event": str(event or ""),
        "thread": threading.current_thread().name,
    }
    for key, value in fields.items():
        record[str(key)] = _redact_value(str(key), value)
    try:
        _structured_logger.info(json.dumps(record, ensure_ascii=False, default=str, separators=(",", ":")))
    except Exception as e:
        _logger.warning("structured_log_failed error=%s", e)


def flog(msg: str, level: str = "info"):
    getattr(_logger, level, _logger.info)(msg)


def _kv_value(value: Any) -> str:
    if value is None:
        return "none"
    text = str(_redact_value("", value)).replace("\r", " ").replace("\n", " ").strip()
    if not text:
        return '""'
    if any(ch.isspace() for ch in text) or "=" in text:
        return '"' + text.replace('"', '\\"') + '"'
    return text


def flog_kv(scope: str, name: str, level: str = "info", **fields):
    flog_struct(scope, name, level, **fields)
    parts = " ".join(f"{key}={_kv_value(_redact_value(key, value))}" for key, value in fields.items())
    suffix = f" {parts}" if parts else ""
    flog(f"[{scope}] {name}{suffix}", level)


# ─────────────────────────────────────────────────────────────────────────────
#  ENUMS
# ─────────────────────────────────────────────────────────────────────────────
STATE_META: Dict[AccountState, Dict[str, str]] = {
    AccountState.IDLE:         {"label": "Idle",         "color": "#6b7280"},
    AccountState.READY:        {"label": "Ready",        "color": "#3b82f6"},
    AccountState.QUEUED:       {"label": "Queued",       "color": "#8b5cf6"},
    AccountState.LAUNCHING:    {"label": "Launching",    "color": "#f59e0b"},
    AccountState.VERIFY:       {"label": "Verifying",    "color": "#f59e0b"},
    AccountState.IN_GAME:      {"label": "In Game",      "color": "#10b981"},
    AccountState.CRASH:        {"label": "Crash",        "color": "#ef4444"},
    AccountState.FAILED:       {"label": "Failed",       "color": "#ef4444"},
    AccountState.NETWORK_LOST: {"label": "Disconnected", "color": "#f97316"},
    AccountState.COOLDOWN:     {"label": "Cooldown",     "color": "#6b7280"},
}

class ServerType(str, Enum):
    VIP     = "VIP"
    PUBLIC  = "PUBLIC"
    UNKNOWN = "UNKNOWN"

class EventName:
    STATE_CHANGE         = "state_change"
    INVALID_TRANSITION   = "invalid_transition"
    ACCOUNT_CRASH        = "account_crash"
    ACCOUNT_FAILED       = "account_failed"
    RECOVERY_REQUESTED   = "recovery_requested"
    REJOIN_SUCCESS       = "rejoin_success"
    LAUNCH_SUCCESS       = "launch_success"
    LAUNCH_FAILED        = "launch_failed"
    NETWORK_STATE_CHANGE = "network_state_change"
    NETWORK_LOST_ACCOUNT = "network_lost_account"


ALLOWED_STATE_TRANSITIONS: Dict[AccountState, Set[AccountState]] = {
    AccountState.IDLE: {
        AccountState.READY,
        AccountState.FAILED,
        AccountState.NETWORK_LOST,
    },
    AccountState.READY: {
        AccountState.QUEUED,
        AccountState.CRASH,
        AccountState.FAILED,
        AccountState.NETWORK_LOST,
        AccountState.COOLDOWN,
    },
    AccountState.QUEUED: {
        AccountState.LAUNCHING,
        AccountState.CRASH,
        AccountState.FAILED,
        AccountState.NETWORK_LOST,
        AccountState.COOLDOWN,
        AccountState.READY,
    },
    AccountState.LAUNCHING: {
        AccountState.VERIFY,
        AccountState.IN_GAME,
        AccountState.CRASH,
        AccountState.FAILED,
        AccountState.NETWORK_LOST,
        AccountState.READY,
    },
    AccountState.VERIFY: {
        AccountState.IN_GAME,
        AccountState.CRASH,
        AccountState.FAILED,
        AccountState.NETWORK_LOST,
        AccountState.READY,
    },
    AccountState.IN_GAME: {
        AccountState.CRASH,
        AccountState.FAILED,
        AccountState.NETWORK_LOST,
        AccountState.COOLDOWN,
        AccountState.READY,
    },
    AccountState.CRASH: {
        AccountState.COOLDOWN,
        AccountState.READY,
        AccountState.QUEUED,
        AccountState.FAILED,
        AccountState.NETWORK_LOST,
    },
    AccountState.FAILED: set(),
    AccountState.NETWORK_LOST: {
        AccountState.READY,
        AccountState.QUEUED,
        AccountState.FAILED,
        AccountState.COOLDOWN,
    },
    AccountState.COOLDOWN: {
        AccountState.READY,
        AccountState.QUEUED,
        AccountState.FAILED,
        AccountState.NETWORK_LOST,
    },
}

EVENT_CONTRACTS: Dict[str, Tuple[str, ...]] = {
    EventName.STATE_CHANGE: ("account", "old_state", "new_state"),
    EventName.INVALID_TRANSITION: ("account", "old_state", "new_state"),
    EventName.ACCOUNT_CRASH: ("account", "reason", "reason_msg"),
    EventName.ACCOUNT_FAILED: ("account", "reason", "reason_msg"),
    EventName.RECOVERY_REQUESTED: ("account", "reason"),
    EventName.REJOIN_SUCCESS: ("account",),
    EventName.LAUNCH_SUCCESS: ("account", "pid"),
    EventName.LAUNCH_FAILED: ("account", "reason"),
    EventName.NETWORK_STATE_CHANGE: ("old", "new"),
    EventName.NETWORK_LOST_ACCOUNT: ("account",),
}


# ─────────────────────────────────────────────────────────────────────────────
#  ACCOUNT DATA MODEL
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Account:
    username:   str
    user_id:    str  = ""
    priority:   int  = 50
    place_id:   str  = ""
    vip_links:  List[str] = field(default_factory=list)
    alias:      str  = ""
    cookie:     str  = ""
    browser_tracker_id: str = ""
    cookie_username: str = ""
    cookie_user_id: str = ""
    cookie_mismatch: bool = False
    description: str = ""
    manual_status: str = ""
    finished_at: float = 0.0

    # runtime (not persisted to config)
    state:          AccountState    = AccountState.IDLE
    desired_state:  AccountState    = AccountState.IN_GAME
    pid:            Optional[int]   = None
    server_type:    ServerType      = ServerType.UNKNOWN
    active_vip:     str             = ""
    bound_process_name: str         = ""
    bound_process_identity: str     = ""
    ownership_confidence: float     = 0.0
    retry_count:    int             = 0
    crash_count:    int             = 0
    fail_count:     int             = 0
    launch_fail_count: int          = 0
    crash_retry_count: int          = 0
    network_retry_count: int        = 0
    session_retry_count: int        = 0
    in_game_since:  Optional[float] = None
    last_crash_at:  Optional[float] = None
    last_launch_at: Optional[float] = None
    retry_history:  List[Dict]      = field(default_factory=list)
    session_valid:  bool            = False
    session_checked: bool           = False
    session_wait_started_at: float  = 0.0
    pid_missing_since: float        = 0.0
    cooldown_until: float           = 0.0
    rapid_relaunch_count: int       = 0
    last_network_lost_at: Optional[float] = None
    last_pid_change_at: float       = 0.0
    last_reconcile_at: float        = 0.0
    last_signal_confidence: float   = 0.0
    launch_strategy: str            = ""
    recovery_status: str            = ""
    last_recovery_reason: str       = ""
    recovery_scheduled_at: float    = 0.0
    recovery_generation: int        = 0
    recovery_inflight: bool         = False
    runtime_generation: int         = 0
    command_generation: int         = 0
    current_command_id: str         = ""
    current_command: str            = ""
    command_inflight_started_at: float = 0.0
    last_recovery_at: float         = 0.0
    last_rejoin_trigger: str        = ""
    last_activity_at: float         = 0.0
    last_activity_reason: str       = ""
    last_activity_cpu: float        = 0.0
    last_activity_ram_mb: float     = 0.0
    last_watchdog_classification: str = ""
    liveness_state: str             = "unknown"
    liveness_score: float           = 0.0
    liveness_suspect_since: float   = 0.0
    presence_mismatch_since: float   = 0.0
    presence_mismatch_status: str    = ""
    presence_mismatch_reason: str    = ""
    presence_rejoin_suppressed_until: float = 0.0
    last_presence_rejoin_at: float    = 0.0
    presence_rejoin_pending_clear: bool = False
    process_binding_status: str     = "unbound"
    binding_decision: str           = ""
    process_binding_confidence: float = 0.0
    process_reject_reason: str      = ""
    process_owner_claim: str        = ""
    unmanaged_live_process_count: int = 0
    unmanaged_live_pids: List[int]  = field(default_factory=list)
    adopt_candidate_pid: Optional[int] = None
    adopt_reject_reason: str        = ""
    orphan_confidence: float        = 0.0
    orphan_pid: Optional[int]       = None
    orphan_identity: str            = ""
    orphan_observed_at: float       = 0.0
    orphan_verify_after: float      = 0.0
    session_id: str                 = ""
    launch_nonce: str               = ""
    account_runtime_id: str         = ""
    rejoin_transaction_id: str      = ""
    server_validation: str          = "unverified"
    destination_validation: str     = "unverified"
    scheduler_slot: str             = ""
    supervisor_state: str           = "stopped"
    last_transaction_status: str    = ""
    last_transaction_step: str      = ""
    last_transaction_reason: str    = ""
    last_transaction_started_at: float = 0.0
    last_transaction_completed_at: float = 0.0
    last_transaction_failure_reason: str = ""
    session_started_at: float       = 0.0
    last_transaction_at: float      = 0.0
    launch_intent: Dict[str, Any]   = field(default_factory=dict)
    launch_intent_summary: Dict[str, Any] = field(default_factory=dict)

    # crash reason tracking
    last_crash_reason: str = ""
    last_state_reason: str = ""
    last_state_change_at: float = field(default_factory=time.time)
    last_error: str = ""

    # VipTracker instance — set externally by FarmController
    _vip_tracker: Any = field(default=None, repr=False, compare=False)
    runtime: AccountRuntime = field(default_factory=AccountRuntime, repr=False, compare=False)

    # Stable identity key (never changes after init)
    _config_username: str = field(default="", repr=False, compare=False)
    _lock: Any = field(default_factory=threading.RLock, repr=False, compare=False)

    def __post_init__(self):
        if not self._config_username:
            object.__setattr__(self, "_config_username", self.username)
        self.sync_runtime("init")

    def sync_runtime(self, reason: str = "") -> AccountRuntime:
        self.runtime.account_id = self._config_username or self.username
        self.runtime.lifecycle_state = runtime_state_for_public(self.state)
        self.runtime.public_state = self.state.name
        self.runtime.desired_public_state = self.desired_state.name
        self.runtime.pid = self.pid
        self.runtime.process_identity = self.bound_process_identity
        self.runtime.bind_status = self.process_binding_status or "unbound"
        self.runtime.binding_status = self.process_binding_status or "unbound"
        self.runtime.binding_decision = self.binding_decision or ""
        self.runtime.process_binding_confidence = float(self.process_binding_confidence or self.ownership_confidence or 0.0)
        self.runtime.process_reject_reason = self.process_reject_reason or ""
        self.runtime.process_owner_claim = self.process_owner_claim or ""
        self.runtime.unmanaged_live_process_count = int(self.unmanaged_live_process_count or 0)
        self.runtime.unmanaged_live_pids = list(self.unmanaged_live_pids or [])
        self.runtime.adopt_candidate_pid = self.adopt_candidate_pid
        self.runtime.adopt_reject_reason = self.adopt_reject_reason or ""
        self.runtime.destination_validation = self.destination_validation or self.server_validation or "unverified"
        self.runtime.launch_intent_summary = dict(self.launch_intent_summary or (self.launch_intent or {}).get("launch_intent_summary", {}) or {})
        self.runtime.runtime_generation = int(self.runtime_generation or 0)
        self.runtime.generation = int(self.runtime_generation or 0)
        self.runtime.recovery_generation = int(self.recovery_generation or 0)
        self.runtime.command_generation = int(self.command_generation or 0)
        self.runtime.retry_count = int(self.retry_count or 0)
        self.runtime.crash_count = int(self.crash_count or 0)
        self.runtime.fail_count = int(self.fail_count or 0)
        self.runtime.cooldown_until = float(self.cooldown_until or 0.0)
        self.runtime.recovery_status = self.recovery_status or ""
        self.runtime.recovery_reason = self.last_recovery_reason or self.last_crash_reason or ""
        self.runtime.recovery_inflight = bool(self.recovery_inflight)
        self.runtime.recovery_active = bool(self.recovery_inflight or self.recovery_status)
        self.runtime.liveness_state = self.liveness_state or "unknown"
        self.runtime.liveness_score = float(self.liveness_score or 0.0)
        self.runtime.last_heartbeat = float(self.last_activity_at or 0.0)
        self.runtime.session_id = self.session_id or ""
        self.runtime.launch_nonce = self.launch_nonce or ""
        self.runtime.account_runtime_id = self.account_runtime_id or ""
        self.runtime.rejoin_transaction_id = self.rejoin_transaction_id or ""
        self.runtime.server_validation = self.server_validation or "unverified"
        self.runtime.scheduler_slot = self.scheduler_slot or ""
        self.runtime.supervisor_state = self.supervisor_state or LIFECYCLE_STATE.get(self.state, "STOPPED").lower()
        self.runtime.last_transaction_status = self.last_transaction_status or ""
        self.runtime.last_transaction_step = self.last_transaction_step or ""
        self.runtime.last_transaction_reason = self.last_transaction_reason or ""
        self.runtime.last_transaction_started_at = float(self.last_transaction_started_at or self.session_started_at or 0.0)
        self.runtime.last_transaction_completed_at = float(self.last_transaction_completed_at or 0.0)
        self.runtime.last_transaction_failure_reason = self.last_transaction_failure_reason or ""
        self.runtime.last_transition_at = float(self.last_state_change_at or time.time())
        self.runtime.last_transition_reason = reason or self.last_state_reason or ""
        self.runtime.current_command = self.current_command or ""
        if self.current_command_id:
            self.runtime.command_inflight = {
                "command_id": self.current_command_id,
                "action": self.current_command or "",
                "age": round(max(0.0, time.time() - float(self.command_inflight_started_at or time.time())), 2),
            }
        else:
            self.runtime.command_inflight = None
        self.runtime.last_error = self.last_error or ""
        return self.runtime

    def runtime_snapshot(self) -> Dict[str, Any]:
        return self.sync_runtime().snapshot()

    @property
    def display_name(self) -> str:
        return self.alias or self.username

    @property
    def uptime_seconds(self) -> int:
        if self.in_game_since and self.state == AccountState.IN_GAME:
            return int(time.time() - self.in_game_since)
        return 0

    @property
    def uptime_str(self) -> str:
        s = self.uptime_seconds
        if s > 0:
            h, r = divmod(s, 3600)
            m, s = divmod(r, 60)
            return f"{h:02d}:{m:02d}:{s:02d}"
        return "—"

    @property
    def is_vip(self) -> bool:
        return bool(self.vip_links)

    @property
    def needs_recovery(self) -> bool:
        return self.desired_state == AccountState.IN_GAME and self.state != AccountState.IN_GAME

    def to_dict(self) -> dict:
        return {
            "username":  self.username,
            "user_id":   self.user_id,
            "priority":  self.priority,
            "place_id":  self.place_id,
            "vip_links": self.vip_links,
            "alias":     self.alias,
            "cookie":    self.cookie,
            "browser_tracker_id": self.browser_tracker_id,
            "cookie_username": self.cookie_username,
            "cookie_user_id": self.cookie_user_id,
            "cookie_mismatch": self.cookie_mismatch,
            "description": self.description,
            "manual_status": self.manual_status,
            "finished_at": self.finished_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "Account":
        return Account(
            username  = str(d.get("username", "")),
            user_id   = str(d.get("user_id", d.get("userId", ""))),
            priority  = int(d.get("priority", 50)),
            place_id  = str(d.get("place_id", "")),
            vip_links = list(d.get("vip_links", [])),
            alias     = str(d.get("alias", "")),
            cookie    = str(d.get("cookie", "")),
            browser_tracker_id = str(d.get("browser_tracker_id", d.get("BrowserTrackerID", ""))),
            cookie_username = str(d.get("cookie_username", "")),
            cookie_user_id = str(d.get("cookie_user_id", "")),
            cookie_mismatch = bool(d.get("cookie_mismatch", False)),
            description = str(d.get("description", "")),
            manual_status = str(d.get("manual_status", "")),
            finished_at = float(d.get("finished_at", 0.0) or 0.0),
        )


# ─────────────────────────────────────────────────────────────────────────────
#  EVENT BUS
# ─────────────────────────────────────────────────────────────────────────────
def cookie_identity_block_reason(
    username: str,
    cookie_username: str = "",
    cookie_mismatch: bool = False,
) -> str:
    user = str(username or "").strip()
    owner = str(cookie_username or "").strip()
    mismatch = bool(cookie_mismatch)
    if owner and user and owner.lower() != user.lower():
        return f"Cookie belongs to {owner}, not {user}. Reimport the correct .ROBLOSECURITY for this account."
    if mismatch:
        target = user or "this account"
        return f"Cookie identity mismatch for {target}. Reimport the correct .ROBLOSECURITY for this account."
    return ""


def account_launch_block_reason(acc: Account) -> str:
    return cookie_identity_block_reason(
        getattr(acc, "username", ""),
        getattr(acc, "cookie_username", ""),
        bool(getattr(acc, "cookie_mismatch", False)),
    )


def account_launchable(acc: Account) -> bool:
    return not account_launch_block_reason(acc)


class EventBus:
    def __init__(self, workers: int = 4, max_pending: int = 128):
        self._handlers: Dict[str, List[Callable]] = {}
        self._lock = threading.Lock()
        self._tasks: queue.Queue = queue.Queue(maxsize=max(1, int(max_pending or 128)))
        self._slow_handler_sec = 2.0
        self._workers: List[threading.Thread] = []
        for idx in range(max(1, int(workers or 4))):
            thread = threading.Thread(
                target=self._run_worker,
                daemon=True,
                name=f"EventBus-{idx + 1}",
            )
            thread.start()
            self._workers.append(thread)

    def on(self, event: str, handler: Callable):
        with self._lock:
            self._handlers.setdefault(event, []).append(handler)

    def off(self, event: str, handler: Callable):
        with self._lock:
            if event in self._handlers:
                self._handlers[event] = [h for h in self._handlers[event] if h is not handler]

    def _run_worker(self):
        while True:
            event, handler, kwargs = self._tasks.get()
            started = time.time()
            try:
                handler(**kwargs)
            except Exception as e:
                flog_kv("BUS", "handler_error", "warning", event=event, error=e)
            finally:
                elapsed = time.time() - started
                if elapsed >= self._slow_handler_sec:
                    flog_kv("BUS", "slow_handler", "warning", event=event, seconds=f"{elapsed:.2f}")
                self._tasks.task_done()

    def emit(self, event: str, **kwargs):
        required = EVENT_CONTRACTS.get(event)
        if required:
            missing = [key for key in required if key not in kwargs]
            if missing:
                flog_kv("BUS", "contract_violation", "warning", event=event, missing=",".join(missing))
        with self._lock:
            handlers = list(self._handlers.get(event, []))
        for h in handlers:
            try:
                self._tasks.put_nowait((event, h, dict(kwargs)))
            except queue.Full:
                flog_kv("BUS", "queue_full_drop", "warning", event=event, pending=self._tasks.qsize())


# ─────────────────────────────────────────────────────────────────────────────
#  STATE MANAGER
# ─────────────────────────────────────────────────────────────────────────────
class StateManager:
    def __init__(self, bus: EventBus):
        self._bus = bus
        self._runtime = RuntimeStateManager(logger=flog_kv)

    def transition(self, acc: Account, new_state: AccountState, force: bool = False, **kwargs) -> bool:
        with acc._lock:
            old = acc.state
            if old == new_state:
                return True

            allowed = ALLOWED_STATE_TRANSITIONS.get(old, set())
            if not force and new_state not in allowed:
                flog_kv(
                    "STATE",
                    "invalid_transition",
                    "warning",
                    event_type="invalid_transition",
                    account=acc.display_name,
                    account_id=acc._config_username,
                    old=old.name,
                    new=new_state.name,
                    reason=kwargs.get("reason", ""),
                    runtime_state=runtime_state_for_public(old).value,
                    public_state=old.name,
                    runtime_generation=acc.runtime_generation,
                    recovery_generation=acc.recovery_generation,
                    command_generation=acc.command_generation,
                    PID=acc.pid,
                    pid=acc.pid,
                    thread_name=threading.current_thread().name,
                    snapshot=acc.runtime_snapshot(),
                )
                self._bus.emit(
                    EventName.INVALID_TRANSITION,
                    account=acc,
                    old_state=old,
                    new_state=new_state,
                    **kwargs,
                )
                return False

            old_lifecycle = LIFECYCLE_STATE.get(old, "INIT")
            new_lifecycle = LIFECYCLE_STATE.get(new_state, "INIT")
            lifecycle_allowed = LIFECYCLE_ALLOWED_TRANSITIONS.get(old_lifecycle, set())
            if old_lifecycle != new_lifecycle and new_lifecycle not in lifecycle_allowed:
                flog_kv(
                    "STATE",
                    "lifecycle_jump",
                    "warning",
                    event_type="lifecycle_jump",
                    account=acc.display_name,
                    account_id=acc._config_username,
                    old=old.name,
                    new=new_state.name,
                    old_lifecycle=old_lifecycle,
                    new_lifecycle=new_lifecycle,
                    force=force,
                    reason=kwargs.get("reason", ""),
                    runtime_state=old_lifecycle,
                    public_state=old.name,
                    runtime_generation=acc.runtime_generation,
                    recovery_generation=acc.recovery_generation,
                    command_generation=acc.command_generation,
                    PID=acc.pid,
                    pid=acc.pid,
                    thread_name=threading.current_thread().name,
                    snapshot=acc.runtime_snapshot(),
                )
                if not force:
                    self._bus.emit(
                        EventName.INVALID_TRANSITION,
                        account=acc,
                        old_state=old,
                        new_state=new_state,
                        **kwargs,
                    )
                    return False

            changed = self._runtime.transition_public(
                acc,
                new_state,
                reason=str(kwargs.get("reason", "")),
                force=force,
                expected_generation=kwargs.get("expected_generation"),
                increment_generation=bool(kwargs.get("increment_generation", False) or new_state == AccountState.FAILED),
            )
            if not changed:
                self._bus.emit(
                    EventName.INVALID_TRANSITION,
                    account=acc,
                    old_state=old,
                    new_state=new_state,
                    **kwargs,
                )
                return False

            if new_state == AccountState.IN_GAME and old != AccountState.IN_GAME:
                acc.in_game_since = time.time()
            elif new_state != AccountState.IN_GAME:
                acc.in_game_since = None
            acc.sync_runtime(acc.last_state_reason)

        flog_kv(
            "STATE",
            "transition",
            event_type="transition",
            account=acc.display_name,
            account_id=acc._config_username,
            old=old.name,
            new=new_state.name,
            old_lifecycle=LIFECYCLE_STATE.get(old, "INIT"),
            new_lifecycle=LIFECYCLE_STATE.get(new_state, "INIT"),
            force=force,
            reason=kwargs.get("reason", ""),
            runtime_state=LIFECYCLE_STATE.get(new_state, "INIT"),
            public_state=new_state.name,
            runtime_generation=acc.runtime_generation,
            recovery_generation=acc.recovery_generation,
            command_generation=acc.command_generation,
            PID=acc.pid,
            pid=acc.pid,
            thread_name=threading.current_thread().name,
        )
        self._bus.emit(
            EventName.STATE_CHANGE,
            account=acc,
            old_state=old,
            new_state=new_state,
            **kwargs,
        )
        return True

    def set_desired(self, acc: Account, desired: AccountState, reason: str = ""):
        with acc._lock:
            old = acc.desired_state
            self._runtime.set_desired(acc, desired, reason or "desired_state")
        flog_kv(
            "STATE",
            "desired_transition",
            account=acc.display_name,
            old=old.name,
            new=desired.name,
            reason=reason,
            runtime_generation=acc.runtime_generation,
            recovery_generation=acc.recovery_generation,
            command_generation=acc.command_generation,
        )

    def set_cooldown(self, acc: Account, until_ts: float, reason: str = ""):
        with acc._lock:
            self._runtime.set_cooldown(acc, until_ts, reason or "cooldown")
        flog_kv(
            "STATE",
            "cooldown_set",
            account=acc.display_name,
            cooldown_left=max(0, int(acc.cooldown_until - time.time())) if acc.cooldown_until else 0,
            reason=reason,
            runtime_generation=acc.runtime_generation,
            recovery_generation=acc.recovery_generation,
            command_generation=acc.command_generation,
        )

    def set_recovery(self, acc: Account, status: str = "", reason: str = "", inflight: Optional[bool] = None):
        with acc._lock:
            self._runtime.set_recovery(acc, status=status, reason=reason, inflight=inflight)
        flog_kv(
            "STATE",
            "recovery_update",
            account=acc.display_name,
            status=acc.recovery_status,
            reason=reason,
            inflight=acc.recovery_inflight,
            generation=acc.recovery_generation,
            runtime_generation=acc.runtime_generation,
            command_generation=acc.command_generation,
        )

    def set_binding_status(self, acc: Account, status: str, reason: str = ""):
        with acc._lock:
            self._runtime.set_binding_status(acc, status, reason or "binding_status")
        flog_kv(
            "STATE",
            "process_binding_status",
            account=acc.display_name,
            pid=acc.pid or "",
            status=acc.process_binding_status,
            reason=reason,
            runtime_generation=acc.runtime_generation,
            recovery_generation=acc.recovery_generation,
            command_generation=acc.command_generation,
        )

    def clear_process_binding(self, acc: Account, reason: str = "", increment_generation: bool = False):
        with acc._lock:
            old_pid = acc.pid
            self._runtime.clear_process_binding(
                acc,
                reason or "clear_process_binding",
                increment_generation=increment_generation,
            )
        flog_kv(
            "STATE",
            "process_unbound",
            account=acc.display_name,
            pid=old_pid or "",
            reason=reason,
            runtime_generation=acc.runtime_generation,
            recovery_generation=acc.recovery_generation,
            command_generation=acc.command_generation,
        )

    def bind_process(
        self,
        acc: Account,
        pid: int,
        identity: str,
        status: str = "verified",
        process_name: str = "RobloxPlayerBeta.exe",
        confidence: float = 100.0,
        reason: str = "",
        increment_generation: bool = True,
    ):
        with acc._lock:
            old_pid = acc.pid
            self._runtime.bind_process(
                acc,
                int(pid),
                process_name or "RobloxPlayerBeta.exe",
                str(identity or ""),
                reason or "bind_process",
                confidence=float(confidence or 0.0),
                increment_generation=increment_generation,
            )
            if status and acc.process_binding_status != status:
                self._runtime.set_binding_status(acc, str(status), reason or "bind_process_status")
            acc.last_reconcile_at = time.time()
            if acc.state == AccountState.IN_GAME and not acc.in_game_since:
                acc.in_game_since = time.time()
                acc.sync_runtime(reason or "bind_process_ingame")
        flog_kv(
            "STATE",
            "process_bind_verified",
            account=acc.display_name,
            pid=pid,
            old_pid=old_pid or "",
            identity=identity,
            status=status,
            confidence=f"{float(confidence or 0.0):.1f}",
            reason=reason,
            runtime_generation=acc.runtime_generation,
            recovery_generation=acc.recovery_generation,
            command_generation=acc.command_generation,
        )


# ─────────────────────────────────────────────────────────────────────────────
#  SIMPLE FIFO QUEUE  (Smart Queue ถูกลบออกตาม spec)
# ─────────────────────────────────────────────────────────────────────────────
class SmartQueue:
    """
    Simple FIFO queue — ลบ Smart Queue / priority scoring ออกแล้ว
    คง interface เดิม (push/pop/mark_busy/mark_free) เพื่อ backward compat
    """
    def __init__(self):
        self._lock     = threading.Lock()
        self._cond     = threading.Condition(self._lock)
        self._queue: deque = deque()
        self._busy     = threading.Event()

    def push(self, acc: Account, reason: str = ""):
        with self._cond:
            # ป้องกัน duplicate
            if acc not in self._queue:
                self._queue.append(acc)
                flog(f"[QUEUE] push {acc.display_name} ({reason}) — size={len(self._queue)}")
                self._cond.notify_all()

    def push(self, acc: Account, reason: str = ""):
        with self._cond:
            if acc in self._queue:
                return

            boosted = any(flag in reason for flag in ("force_rejoin", "network_restored", "session_restored"))
            insert_at = len(self._queue)
            new_pri = int(getattr(acc, "priority", 50) or 50)

            if boosted:
                insert_at = 0
            else:
                for idx, queued in enumerate(self._queue):
                    queued_pri = int(getattr(queued, "priority", 50) or 50)
                    if new_pri < queued_pri:
                        insert_at = idx
                        break

            self._queue.insert(insert_at, acc)
            flog(
                f"[QUEUE] push {acc.display_name} ({reason}) - size={len(self._queue)} "
                f"priority={new_pri} idx={insert_at}"
            )
            self._cond.notify_all()

    def pop(self, timeout: float = 1.0) -> Optional[Account]:
        deadline = time.time() + timeout
        with self._cond:
            while not self._queue:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=min(0.5, remaining))
            return self._queue.popleft()

    def is_busy(self) -> bool:
        return self._busy.is_set()

    def mark_busy(self):
        self._busy.set()

    def mark_free(self):
        self._busy.clear()

    def wait_until_free(self, stop: threading.Event, timeout: float = 120.0):
        deadline = time.time() + timeout
        while self._busy.is_set() and not stop.is_set():
            if time.time() > deadline:
                break
            time.sleep(0.5)

    def size(self) -> int:
        with self._lock:
            return len(self._queue)

    def __init__(self):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._busy = threading.Event()
        self._closed = False
        self._stale_rejections = 0

    @staticmethod
    def _is_boosted(reason: str) -> bool:
        return any(flag in str(reason or "") for flag in ("force_rejoin", "network_restored", "session_restored"))

    def push(
        self,
        acc: Account,
        reason: str = "",
        runtime_generation: Optional[int] = None,
        recovery_generation: Optional[int] = None,
    ):
        key = acc._config_username
        with self._cond:
            if self._closed:
                flog_kv(
                    "QUEUE",
                    "enqueue_rejected",
                    "warning",
                    event_type="stale_work_rejected",
                    account=acc.display_name,
                    reason=reason or "queue_closed",
                    runtime_generation=getattr(acc, "runtime_generation", 0),
                    recovery_generation=getattr(acc, "recovery_generation", 0),
                )
                return
            now = time.time()
            due_at = max(now, float(getattr(acc, "cooldown_until", 0.0) or 0.0))
            generation = int(
                recovery_generation
                if recovery_generation is not None
                else getattr(acc, "recovery_generation", 0) or 0
            )
            runtime_generation = int(
                runtime_generation
                if runtime_generation is not None
                else getattr(acc, "runtime_generation", 0) or 0
            )
            existing = self._entries.get(key)
            if existing:
                existing["reason"] = reason or existing.get("reason", "")
                existing["generation"] = generation
                existing["recovery_generation"] = generation
                existing["runtime_generation"] = runtime_generation
                existing["due_at"] = min(float(existing.get("due_at") or due_at), due_at)
                if self._is_boosted(reason):
                    existing["boosted"] = True
                flog_kv(
                    "QUEUE",
                    "dedupe",
                    account=acc.display_name,
                    reason=reason,
                    runtime_generation=runtime_generation,
                    recovery_generation=generation,
                    due_in=f"{max(0.0, due_at - now):.1f}",
                    size=len(self._entries),
                )
                return

            self._entries[key] = {
                "acc": acc,
                "reason": reason,
                "queued_at": now,
                "due_at": due_at,
                "generation": generation,
                "recovery_generation": generation,
                "runtime_generation": runtime_generation,
                "boosted": self._is_boosted(reason),
            }
            flog_kv(
                "QUEUE",
                "push",
                account=acc.display_name,
                reason=reason,
                priority=int(getattr(acc, "priority", 50) or 50),
                runtime_generation=runtime_generation,
                recovery_generation=generation,
                due_in=f"{max(0.0, due_at - now):.1f}",
                size=len(self._entries),
            )
            self._cond.notify_all()

    def _entry_score(self, entry: Dict[str, Any], now: float) -> float:
        acc = entry["acc"]
        base = float(int(getattr(acc, "priority", 50) or 50))
        retry_penalty = min(
            80.0,
            float(
                int(getattr(acc, "retry_count", 0) or 0) +
                int(getattr(acc, "launch_fail_count", 0) or 0) +
                int(getattr(acc, "crash_retry_count", 0) or 0)
            ) * 5.0,
        )
        aging_credit = min(40.0, max(0.0, now - float(entry.get("queued_at") or now)) / 15.0)
        boost = -1000.0 if entry.get("boosted") else 0.0
        return base + retry_penalty - aging_credit + boost

    def _entry_due_at(self, entry: Dict[str, Any]) -> float:
        acc = entry["acc"]
        return max(
            float(entry.get("due_at") or 0.0),
            float(getattr(acc, "cooldown_until", 0.0) or 0.0),
        )

    def pop(self, timeout: float = 1.0) -> Optional[Account]:
        deadline = time.time() + timeout
        with self._cond:
            while True:
                now = time.time()
                stale_keys = [
                    key for key, entry in self._entries.items()
                    if (
                        int(entry.get("recovery_generation", entry.get("generation", 0)) or 0) != int(getattr(entry["acc"], "recovery_generation", 0) or 0)
                        or int(entry.get("runtime_generation", 0) or 0) != int(getattr(entry["acc"], "runtime_generation", 0) or 0)
                    )
                ]
                for key in stale_keys:
                    entry = self._entries.pop(key, None)
                    if entry:
                        self._stale_rejections += 1
                        flog_kv(
                            "QUEUE",
                            "stale_drop",
                            "warning",
                            event_type="stale_work_rejected",
                            account=entry["acc"].display_name,
                            queued_runtime_generation=entry.get("runtime_generation", 0),
                            current_runtime_generation=getattr(entry["acc"], "runtime_generation", 0),
                            queued_recovery_generation=entry.get("recovery_generation", entry.get("generation", 0)),
                            current_recovery_generation=getattr(entry["acc"], "recovery_generation", 0),
                            size=len(self._entries),
                            reason=entry.get("reason", ""),
                        )

                if not self._entries:
                    if self._closed:
                        return None
                    remaining = deadline - now
                    if remaining <= 0:
                        return None
                    self._cond.wait(timeout=min(0.5, remaining))
                    continue

                ready = [
                    (key, entry) for key, entry in self._entries.items()
                    if self._entry_due_at(entry) <= now
                ]
                if not ready:
                    next_due = min(self._entry_due_at(entry) for entry in self._entries.values())
                    remaining = deadline - now
                    if remaining <= 0:
                        return None
                    self._cond.wait(timeout=min(max(0.05, next_due - now), remaining, 0.5))
                    continue

                best_key, best_entry = min(
                    ready,
                    key=lambda item: (self._entry_score(item[1], now), float(item[1].get("queued_at") or now)),
                )
                self._entries.pop(best_key, None)
                acc = best_entry["acc"]
                flog_kv(
                    "QUEUE",
                    "pop",
                    account=acc.display_name,
                    reason=best_entry.get("reason", ""),
                    runtime_generation=best_entry.get("runtime_generation", 0),
                    recovery_generation=best_entry.get("recovery_generation", best_entry.get("generation", 0)),
                    size=len(self._entries),
                    score=f"{self._entry_score(best_entry, now):.1f}",
                )
                return acc

    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def cancel_account(self, acc_or_key: Any, reason: str = "cancel_account") -> int:
        key = str(getattr(acc_or_key, "_config_username", acc_or_key) or "")
        with self._cond:
            removed = 1 if self._entries.pop(key, None) else 0
            if removed:
                self._cond.notify_all()
                flog_kv("QUEUE", "cancel_account", account=key, reason=reason, size=len(self._entries))
            return removed

    def cancel_all(self, reason: str = "cancel_all") -> int:
        with self._cond:
            count = len(self._entries)
            self._entries.clear()
            self._closed = True
            self._busy.clear()
            self._cond.notify_all()
        flog_kv("QUEUE", "cancel_all", reason=reason, count=count)
        return count

    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            entries = []
            for key, entry in self._entries.items():
                acc = entry.get("acc")
                entries.append({
                    "account": key,
                    "display": getattr(acc, "display_name", key),
                    "reason": entry.get("reason", ""),
                    "queued_at": float(entry.get("queued_at") or 0.0),
                    "age_seconds": round(max(0.0, now - float(entry.get("queued_at") or now)), 2),
                    "due_in_seconds": round(max(0.0, self._entry_due_at(entry) - now), 2),
                    "runtime_generation": int(entry.get("runtime_generation", 0) or 0),
                    "recovery_generation": int(entry.get("recovery_generation", entry.get("generation", 0)) or 0),
                    "boosted": bool(entry.get("boosted", False)),
                })
            return {
                "size": len(entries),
                "pending": len(entries),
                "busy": self._busy.is_set(),
                "closed": self._closed,
                "stale_rejections": self._stale_rejections,
                "oldest_age_seconds": max((item["age_seconds"] for item in entries), default=0.0),
                "entries": entries,
            }


# ─────────────────────────────────────────────────────────────────────────────
#  GLOBAL LAUNCH LIMITER
# ─────────────────────────────────────────────────────────────────────────────
class GlobalLaunchLimiter:
    def __init__(self, interval: float = 6.0):
        self.interval = interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self, stop: Optional[threading.Event] = None):
        with self._lock:
            now   = time.time()
            delta = self.interval - (now - self._last)
            if delta > 0:
                if stop:
                    stop.wait(timeout=delta)
                else:
                    time.sleep(delta)
            self._last = time.time()


# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG MANAGER
# ─────────────────────────────────────────────────────────────────────────────
CONFIG_FILE = os.path.join(APP_DATA_DIR, "roboguard_rt1_config.json")
COOKIE_STORE_FILE = os.path.join(APP_DATA_DIR, "roboguard_rt1_cookies.json")
ACCOUNTS_TEXT_FILE = os.path.join(APP_DATA_DIR, "roboguard_rt12_accounts.txt")
RUNTIME_TEXT_FILE = os.path.join(APP_DATA_DIR, "roboguard_rt12_runtime.txt")

DEFAULTS: Dict[str, Any] = {
    "auto_rejoin":              True,
    "rejoin_delay":             5,
    "max_retry":                10,
    "max_fail_count":           5,
    "crash_timeout":            30,
    "heartbeat_timeout":        60,
    "launch_verify_window":     25,
    "login_warmup_delay":       6,
    "anti_spam_window":         6,
    "launch_rate_interval":     6,
    "account_switch_cooldown":  10,
    "queue_delay_seconds":      15,
    "queue_duration_seconds":   15,
    "max_concurrent_accounts":  40,
    "game_private_server_url":  "",
    "game_place_id":            "",
    "auto_create_private_server_enabled": False,
    "auto_create_private_server_free_only": True,
    "auto_close_enabled":       False,
    "auto_close_minutes":       0,
    "auto_minimize_enabled":    False,
    "auto_minimize_seconds":    10,
    "not_responding_timeout":   30,
    "network_check_interval":   5,
    "network_debounce":         5,
    "periodic_reconcile_interval": 15,
    "queue_timeout":            90,
    "cooldown_after_crash":     5,
    "relaunch_loop_window":     45,
    "relaunch_loop_limit":      3,
    "launch_public_fallback_threshold": 2,
    "recovery_confidence_threshold": 45.0,
    "connection_error_rejoin":  True,
    "popup_disconnected_enabled": True,
    "connection_error_hold_time": 3,
    "popup_startup_grace_seconds": 8,
    "popup_confidence_threshold": 1.0,
    "popup_sample_count": 6,
    "popup_sample_interval_seconds": 0.25,
    "recovery_dedupe_window_seconds": 3,
    "session_conflict_window_seconds": 90,
    "recovery_restore_window":  3600,
    "watchdog_activity_timeout": 180,
    "watchdog_loading_grace":   90,
    "event_bus_workers":        4,
    "event_bus_max_pending":    128,
    # ── Roblox Watchdog (ใหม่ RT.1.0) ──
    "watchdog_enabled":         True,
    "watchdog_cpu_low":         0.9,   # % CPU ต่ำกว่านี้ = ผิดปกติ
    "watchdog_ram_low":         90.0,  # MB RAM ต่ำกว่านี้ = ผิดปกติ
    "watchdog_hold_time":       60,    # วิ รอยืนยันก่อน kill+rejoin
    "fps_limiter_enabled":      False,
    "fps_limit":                240,
    "graphics_auto_enabled":    False,
    "graphics_low_enabled":     False,
    "graphics_quality_level":   1,
    "auto_process_priority_enabled": False,
    "process_priority":         "low",
    "cpu_limiter_enabled":      False,
    "cpu_limiter_mode":         "hard",
    "cpu_limiter_default_percent": 20,
    "cpu_limiter_apply_all":    True,
    "cpu_limiter_accounts":     {},
    "roblox_window_resize_enabled": False,
    "roblox_window_size_preset": "640x480",
    "roblox_window_width":      640,
    "roblox_window_height":     480,
    "roblox_window_resize_interval_seconds": 10,
    "roblox_window_arrange_enabled": False,
    "roblox_window_arrange_columns": 6,
    "roblox_window_arrange_gap": 2,
    "roblox_window_arrange_margin": 0,
    "presence_api_enabled":     False,
    "presence_poll_interval_seconds": 30,
    "presence_cache_ttl_seconds": 30,
    "presence_assist_rejoin_enabled": True,
    "presence_rejoin_cooldown_seconds": 10,
    "multi_roblox_enabled": True,
    "rt_rotation_enabled": False,
    "use_ram_account_manager":  False,
    "ram_launch_via_api":       True,
    "ram_auto_launch":          True,
    "ram_host":                 "localhost",
    "ram_port":                 7963,
    "ram_password":             "",
    "ram_path":                 os.path.join(
        os.path.expanduser("~"),
        "Documents",
        "acc",
        "Roblox Account Manager.exe",
    ),
    "accounts":                 [],
    "runtime_state":            {},
}

class ConfigManager:
    def __init__(self):
        self._cfg: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._io_lock = threading.RLock()
        self.load()

    def load(self):
        raw = self._read_text_json(CONFIG_FILE, {})

        # Migration from old config filenames/keys
        if "zombie_timeout" in raw and "not_responding_timeout" not in raw:
            raw["not_responding_timeout"] = raw["zombie_timeout"]
        if "auto_close_minutes" not in raw and "auto_close_seconds" in raw:
            try:
                seconds = max(0.0, float(raw.get("auto_close_seconds") or 0))
                raw["auto_close_minutes"] = int((seconds + 59) // 60) if seconds > 0 else 0
            except Exception:
                raw["auto_close_minutes"] = 0
        raw["use_ram_account_manager"] = False
        raw["ram_launch_via_api"] = False
        raw["ram_auto_launch"] = False

        with self._lock:
            self._cfg = {k: raw.get(k, v) for k, v in DEFAULTS.items()}

    def save(self):
        with self._lock:
            data = dict(self._cfg)
        data.pop("accounts", None)
        data.pop("runtime_state", None)
        data.setdefault("schema_version", 1)
        self._write_text_json(CONFIG_FILE, data)

    def get(self, key: str, default=None) -> Any:
        with self._lock:
            return self._cfg.get(key, default if default is not None else DEFAULTS.get(key))

    def update(self, updates: Dict[str, Any]):
        with self._lock:
            self._cfg.update(updates)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            snap = dict(self._cfg)
        snap["use_ram_account_manager"] = False
        snap["ram_launch_via_api"] = False
        snap["ram_auto_launch"] = False
        return snap

    def _read_text_json(self, path: str, fallback):
        if not os.path.exists(path):
            return fallback
        backup_path = f"{path}.bak"
        try:
            with self._io_lock:
                with open(path, "r", encoding="utf-8") as f:
                    body = f.read().strip()
                if not body:
                    return fallback
                return json.loads(body)
        except Exception as e:
            flog(f"Text store load error ({path}): {e}", "warning")
            if os.path.exists(backup_path):
                try:
                    with self._io_lock:
                        with open(backup_path, "r", encoding="utf-8") as f:
                            body = f.read().strip()
                        if body:
                            recovered = json.loads(body)
                            flog_kv("CONFIG", "json_recovered_from_backup", "warning", path=path)
                            return recovered
                except Exception as backup_error:
                    flog(f"Text store backup load error ({backup_path}): {backup_error}", "warning")
            return fallback

    def _write_text_json(self, path: str, payload):
        tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        backup_path = f"{path}.bak"
        try:
            with self._io_lock:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                if os.path.exists(path):
                    try:
                        shutil.copy2(path, backup_path)
                    except Exception as backup_error:
                        flog_kv("CONFIG", "json_backup_failed", "warning", path=path, error=backup_error)
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(json.dumps(payload, indent=2, ensure_ascii=False))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, path)
        except Exception as e:
            flog(f"Text store save error ({path}): {e}", "warning")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

    def _use_ram_cookie_source(self) -> bool:
        return False

    def _load_cookie_store(self) -> Dict[str, str]:
        if self._use_ram_cookie_source():
            return {}
        raw = self._read_text_json(COOKIE_STORE_FILE, {})
        if isinstance(raw, dict):
            return {
                str(k).strip().lower(): str(v or "").strip()
                for k, v in raw.items()
                if str(k).strip()
            }
        return {}

    def save_cookies(self, accounts: List[Account]):
        if self._use_ram_cookie_source():
            return
        cookie_map: Dict[str, str] = {}
        for acc in accounts:
            username = str(acc.username or "").strip().lower()
            cookie = str(acc.cookie or "").strip()
            if username and cookie:
                cookie_map[username] = cookie
        self._write_text_json(COOKIE_STORE_FILE, cookie_map)

    def get_accounts(self) -> List[Account]:
        raw = self._read_text_json(ACCOUNTS_TEXT_FILE, None)
        if raw is None:
            with self._lock:
                raw = self._cfg.get("accounts", [])
            if raw:
                self._write_text_json(ACCOUNTS_TEXT_FILE, raw)
        use_ram_cookie_source = False
        cookie_store = {} if use_ram_cookie_source else self._load_cookie_store()
        accounts = []
        for d in raw:
            try:
                acc = Account.from_dict(d)
                saved_cookie = cookie_store.get(str(acc.username or "").strip().lower(), "")
                if saved_cookie and not acc.cookie:
                    acc.cookie = saved_cookie
                accounts.append(acc)
            except Exception as e:
                flog(f"Account parse error: {e}", "warning")
        return accounts

    def save_accounts(self, accounts: List[Account]):
        payload = []
        for a in accounts:
            item = a.to_dict()
            item.pop("cookie", None)
            item["cookie_present"] = bool(str(getattr(a, "cookie", "") or "").strip())
            payload.append(item)
        self.update({"accounts": []})
        self.save()
        self._write_text_json(ACCOUNTS_TEXT_FILE, payload)

    def save_runtime(self, accounts: List[Account]):
        state = {"__schema_version": 1, "__saved_at": time.time()}
        saved_at = time.time()
        for a in accounts:
            runtime_snapshot = a.runtime_snapshot()
            entry: Dict[str, Any] = {
                "runtime": runtime_snapshot,
                "runtime_state": runtime_snapshot.get("runtime_state", RuntimeState.STOPPED.value),
                "runtime_generation": runtime_snapshot.get("runtime_generation", 0),
                "command_generation": runtime_snapshot.get("command_generation", 0),
                "retry":             a.retry_count,
                "fail":              a.fail_count,
                "crash":             a.crash_count,
                "launch_fail_count": a.launch_fail_count,
                "crash_retry_count": a.crash_retry_count,
                "network_retry_count": a.network_retry_count,
                "session_retry_count": a.session_retry_count,
                "last_crash_reason": a.last_crash_reason,
                "cooldown_until":    a.cooldown_until,
                "rapid_relaunch_count": a.rapid_relaunch_count,
                "last_network_lost_at": a.last_network_lost_at,
                "recovery_status":   a.recovery_status,
                "last_recovery_reason": a.last_recovery_reason,
                "recovery_scheduled_at": a.recovery_scheduled_at,
                "recovery_generation": a.recovery_generation,
                "recovery_active": bool(a.recovery_inflight or a.recovery_status),
                "last_recovery_at": a.last_recovery_at,
                "bound_pid": a.pid,
                "bound_process_name": a.bound_process_name,
                "bound_process_identity": a.bound_process_identity,
                "last_pid_change_at": a.last_pid_change_at,
                "last_relaunch_at": a.last_launch_at,
                "process_binding_status": a.process_binding_status,
                "binding_decision": a.binding_decision,
                "process_binding_confidence": a.process_binding_confidence,
                "process_reject_reason": a.process_reject_reason,
                "process_owner_claim": a.process_owner_claim,
                "unmanaged_live_process_count": a.unmanaged_live_process_count,
                "unmanaged_live_pids": list(a.unmanaged_live_pids or []),
                "adopt_candidate_pid": a.adopt_candidate_pid,
                "adopt_reject_reason": a.adopt_reject_reason,
                "liveness_state": a.liveness_state,
                "liveness_score": a.liveness_score,
                "session_id": a.session_id,
                "launch_nonce": a.launch_nonce,
                "account_runtime_id": a.account_runtime_id,
                "rejoin_transaction_id": a.rejoin_transaction_id,
                "server_validation": a.server_validation,
                "destination_validation": a.destination_validation,
                "scheduler_slot": a.scheduler_slot,
                "supervisor_state": a.supervisor_state,
                "last_transaction_status": a.last_transaction_status,
                "last_transaction_step": a.last_transaction_step,
                "last_transaction_reason": a.last_transaction_reason,
                "last_transaction_started_at": a.last_transaction_started_at,
                "last_transaction_completed_at": a.last_transaction_completed_at,
                "last_transaction_failure_reason": a.last_transaction_failure_reason,
                "session_started_at": a.session_started_at,
                "last_transaction_at": a.last_transaction_at,
                "launch_intent": a.launch_intent,
                "launch_intent_summary": a.launch_intent_summary,
                "runtime_saved_at":  saved_at,
            }
            if a._vip_tracker:
                try:
                    entry["vip_scores"] = a._vip_tracker.status()
                except Exception:
                    pass
            state[a._config_username] = entry
        self.update({"runtime_state": {}})
        self.save()
        self._write_text_json(RUNTIME_TEXT_FILE, state)
        flog_kv("RUNTIME", "saved", accounts=len(accounts), saved_at=f"{saved_at:.3f}")

    def restore_runtime(self, accounts: List[Account]):
        state = self._read_text_json(RUNTIME_TEXT_FILE, None)
        if state is None:
            state = self.get("runtime_state", {})
        now = time.time()
        restore_window = max(0.0, float(self.get("recovery_restore_window", 3600) or 3600))
        for a in accounts:
            key = a._config_username
            if key in state:
                s = state[key]
                saved_at = float(s.get("runtime_saved_at") or 0.0)
                fresh = bool(saved_at and (now - saved_at) <= restore_window)
                a.crash_count = int(s.get("crash", 0) or 0)
                a.last_crash_reason = str(s.get("last_crash_reason", "") or "")
                if fresh:
                    a.retry_count = int(s.get("retry", 0) or 0)
                    a.fail_count = int(s.get("fail",  0) or 0)
                    a.launch_fail_count = int(s.get("launch_fail_count", 0) or 0)
                    a.crash_retry_count = int(s.get("crash_retry_count", 0) or 0)
                    a.network_retry_count = int(s.get("network_retry_count", 0) or 0)
                    a.session_retry_count = int(s.get("session_retry_count", 0) or 0)
                    a.cooldown_until = max(0.0, float(s.get("cooldown_until", 0.0) or 0.0))
                    if a.cooldown_until <= now:
                        a.cooldown_until = 0.0
                    a.rapid_relaunch_count = int(s.get("rapid_relaunch_count", 0) or 0)
                    last_network = s.get("last_network_lost_at")
                    a.last_network_lost_at = float(last_network) if last_network else None
                    a.recovery_status = str(s.get("recovery_status", "") or "")
                    if a.recovery_status in {"recovering", "queued", "launch_backoff", "due"}:
                        a.recovery_status = "restored"
                    a.last_recovery_reason = str(s.get("last_recovery_reason", "") or "")
                    a.recovery_scheduled_at = max(0.0, float(s.get("recovery_scheduled_at", 0.0) or 0.0))
                    if a.recovery_scheduled_at and a.recovery_scheduled_at <= now:
                        a.recovery_scheduled_at = 0.0
                    a.recovery_generation = int(s.get("recovery_generation", 0) or 0)
                    a.runtime_generation = int(s.get("runtime_generation", 0) or 0)
                    a.command_generation = int(s.get("command_generation", 0) or 0)
                    a.current_command_id = ""
                    a.current_command = ""
                    a.command_inflight_started_at = 0.0
                    a.last_recovery_at = max(0.0, float(s.get("last_recovery_at", 0.0) or 0.0))
                    a.pid = int(s.get("bound_pid") or 0) or None
                    a.bound_process_name = str(s.get("bound_process_name", "") or "")
                    a.bound_process_identity = str(s.get("bound_process_identity", "") or "")
                    a.last_pid_change_at = max(0.0, float(s.get("last_pid_change_at", 0.0) or 0.0))
                    a.last_launch_at = max(0.0, float(s.get("last_relaunch_at", 0.0) or 0.0)) or None
                    a.process_binding_status = str(s.get("process_binding_status", "") or "restored")
                    a.binding_decision = str(s.get("binding_decision", "") or "")
                    a.process_binding_confidence = float(s.get("process_binding_confidence", 0.0) or 0.0)
                    a.process_reject_reason = str(s.get("process_reject_reason", "") or "")
                    a.process_owner_claim = str(s.get("process_owner_claim", "") or "")
                    a.unmanaged_live_process_count = int(s.get("unmanaged_live_process_count", 0) or 0)
                    live_pids = s.get("unmanaged_live_pids", [])
                    a.unmanaged_live_pids = list(live_pids) if isinstance(live_pids, list) else []
                    a.adopt_candidate_pid = int(s.get("adopt_candidate_pid") or 0) or None
                    a.adopt_reject_reason = str(s.get("adopt_reject_reason", "") or "")
                    a.liveness_state = str(s.get("liveness_state", "") or "unknown")
                    a.liveness_score = float(s.get("liveness_score", 0.0) or 0.0)
                    a.session_id = str(s.get("session_id", "") or "")
                    a.launch_nonce = str(s.get("launch_nonce", "") or "")
                    a.account_runtime_id = str(s.get("account_runtime_id", "") or "")
                    a.rejoin_transaction_id = str(s.get("rejoin_transaction_id", "") or "")
                    a.server_validation = str(s.get("server_validation", "") or "restored")
                    a.destination_validation = str(s.get("destination_validation", "") or a.server_validation or "restored")
                    a.scheduler_slot = str(s.get("scheduler_slot", "") or "")
                    a.supervisor_state = str(s.get("supervisor_state", "") or "restored")
                    if a.supervisor_state in {"transaction_pending", "launching", "rejoining", "process_bound", "verifying"}:
                        a.supervisor_state = "restored"
                    a.last_transaction_status = str(s.get("last_transaction_status", "") or "")
                    a.last_transaction_step = str(s.get("last_transaction_step", "") or "")
                    a.last_transaction_failure_reason = str(s.get("last_transaction_failure_reason", "") or "")
                    if a.last_transaction_status in {"pending", "launching", "process_bound", "verifying", "binding_verified"}:
                        a.last_transaction_status = "rolled_back_on_restart"
                        a.last_transaction_step = "rolled_back_on_restart"
                        a.last_transaction_failure_reason = "backend_restart"
                    a.last_transaction_reason = str(s.get("last_transaction_reason", "") or "")
                    a.last_transaction_started_at = max(0.0, float(s.get("last_transaction_started_at", 0.0) or s.get("session_started_at", 0.0) or 0.0))
                    a.last_transaction_completed_at = max(0.0, float(s.get("last_transaction_completed_at", 0.0) or 0.0))
                    a.session_started_at = max(0.0, float(s.get("session_started_at", 0.0) or 0.0))
                    a.last_transaction_at = max(0.0, float(s.get("last_transaction_at", 0.0) or 0.0))
                    launch_intent = s.get("launch_intent", {})
                    a.launch_intent = launch_intent if isinstance(launch_intent, dict) else {}
                    launch_intent_summary = s.get("launch_intent_summary", {})
                    if not isinstance(launch_intent_summary, dict):
                        launch_intent_summary = {}
                    a.launch_intent_summary = launch_intent_summary or dict(a.launch_intent.get("launch_intent_summary", {}) or {})
                else:
                    a.retry_count = 0
                    a.fail_count = 0
                    a.launch_fail_count = 0
                    a.crash_retry_count = 0
                    a.network_retry_count = 0
                    a.session_retry_count = 0
                    a.cooldown_until = 0.0
                    a.rapid_relaunch_count = 0
                    a.last_network_lost_at = None
                    a.recovery_status = ""
                    a.last_recovery_reason = ""
                    a.recovery_scheduled_at = 0.0
                    a.recovery_generation = 0
                    a.binding_decision = ""
                    a.process_binding_confidence = 0.0
                    a.process_reject_reason = ""
                    a.process_owner_claim = ""
                    a.unmanaged_live_process_count = 0
                    a.unmanaged_live_pids = []
                    a.adopt_candidate_pid = None
                    a.adopt_reject_reason = ""
                    a.destination_validation = "unverified"
                    a.launch_intent = {}
                    a.launch_intent_summary = {}
                    a.runtime_generation = 0
                    a.command_generation = 0
                    a.current_command_id = ""
                    a.current_command = ""
                    a.command_inflight_started_at = 0.0
                    a.pid = None
                    a.bound_process_name = ""
                    a.bound_process_identity = ""
                    a.last_pid_change_at = 0.0
                    a.last_launch_at = None
                    a.process_binding_status = "unbound"
                    a.liveness_state = "unknown"
                    a.liveness_score = 0.0
                    a.session_id = ""
                    a.launch_nonce = ""
                    a.account_runtime_id = ""
                    a.rejoin_transaction_id = ""
                    a.server_validation = "unverified"
                    a.scheduler_slot = ""
                    a.supervisor_state = "stopped"
                    a.last_transaction_status = ""
                    a.last_transaction_step = ""
                    a.last_transaction_reason = ""
                    a.last_transaction_started_at = 0.0
                    a.last_transaction_completed_at = 0.0
                    a.last_transaction_failure_reason = ""
                    a.session_started_at = 0.0
                    a.last_transaction_at = 0.0
                    a.launch_intent = {}
                a.sync_runtime("restore_runtime")
                if a._vip_tracker and "vip_scores" in s:
                    try:
                        now = time.time()
                        with a._vip_tracker._lock:
                            for item in s["vip_scores"]:
                                link = item.get("link")
                                if not link or link not in a._vip_tracker._scores:
                                    continue
                                if "score" in item:
                                    a._vip_tracker._scores[link] = float(item["score"])
                                remaining = int(item.get("blacklist_remaining", 0) or 0)
                                if remaining > 0:
                                    a._vip_tracker._blacklist[link] = now + remaining
                    except Exception:
                        pass
                flog_kv(
                    "RUNTIME",
                    "restored",
                    account=a.display_name,
                    fresh=fresh,
                    crash=a.crash_count,
                    fail=a.fail_count,
                    retry=a.retry_count,
                    cooldown_left=max(0, int(a.cooldown_until - time.time())) if a.cooldown_until else 0,
                    reason=a.last_recovery_reason or a.last_crash_reason,
                )
                if not fresh:
                    flog_kv(
                        "STATE",
                        "forced_reset",
                        account=a.display_name,
                        account_id=a._config_username,
                        runtime_generation=a.runtime_generation,
                        recovery_generation=a.recovery_generation,
                        command_generation=a.command_generation,
                        runtime_state=a.runtime.lifecycle_state.value,
                        public_state=a.state.name,
                        PID=a.pid or "",
                        reason="restore_runtime_expired",
                        thread_name=threading.current_thread().name,
                    )
