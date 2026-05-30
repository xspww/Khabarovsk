from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from domain.account_state import AccountState
from domain.public_state_mapper import LIFECYCLE_STATE, runtime_state_for_public
from domain.runtime_lifecycle import lifecycle_for_public
from domain.runtime_models import AccountRuntime


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
    import_status: str = ""
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
    recovery_budget_attempts: List[float] = field(default_factory=list)
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
    resource_pressure_since: float  = 0.0
    resource_pressure_reason: str   = ""
    process_binding_status: str     = "unbound"
    binding_decision: str           = ""
    process_binding_confidence: float = 0.0
    process_proof_level: str        = "untrusted"
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
    observed_server_type: str       = ""
    observed_private_server_id: str = ""
    observed_private_server_owner_id: str = ""
    observed_place_id: str          = ""
    observed_job_id: str            = ""
    observed_universe_id: str       = ""
    observed_server_at: float       = 0.0
    lua_last_event: str             = ""
    lua_last_event_at: float        = 0.0
    lua_in_game_at: float           = 0.0
    lua_session_id: str             = ""
    lua_launch_nonce: str           = ""
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
        self.runtime.canonical_state = lifecycle_for_public(self.state).value
        self.runtime.public_state = self.state.name
        self.runtime.desired_public_state = self.desired_state.name
        self.runtime.pid = self.pid
        self.runtime.process_identity = self.bound_process_identity
        self.runtime.bind_status = self.process_binding_status or "unbound"
        self.runtime.binding_status = self.process_binding_status or "unbound"
        self.runtime.binding_decision = self.binding_decision or ""
        self.runtime.process_binding_confidence = float(self.process_binding_confidence or self.ownership_confidence or 0.0)
        self.runtime.process_proof_level = self.process_proof_level or "untrusted"
        self.runtime.process_reject_reason = self.process_reject_reason or ""
        self.runtime.process_owner_claim = self.process_owner_claim or ""
        self.runtime.unmanaged_live_process_count = int(self.unmanaged_live_process_count or 0)
        self.runtime.unmanaged_live_pids = list(self.unmanaged_live_pids or [])
        self.runtime.adopt_candidate_pid = self.adopt_candidate_pid
        self.runtime.adopt_reject_reason = self.adopt_reject_reason or ""
        self.runtime.orphan_pid = self.orphan_pid
        self.runtime.orphan_identity = self.orphan_identity or ""
        self.runtime.orphan_confidence = float(self.orphan_confidence or 0.0)
        self.runtime.orphan_observed_at = float(self.orphan_observed_at or 0.0)
        self.runtime.orphan_verify_after = float(self.orphan_verify_after or 0.0)
        self.runtime.destination_validation = self.destination_validation or self.server_validation or "unverified"
        self.runtime.launch_intent_summary = dict(self.launch_intent_summary or (self.launch_intent or {}).get("launch_intent_summary", {}) or {})
        self.runtime.runtime_generation = int(self.runtime_generation or 0)
        self.runtime.generation = int(self.runtime_generation or 0)
        self.runtime.recovery_generation = int(self.recovery_generation or 0)
        self.runtime.command_generation = int(self.command_generation or 0)
        self.runtime.retry_count = int(self.retry_count or 0)
        self.runtime.crash_count = int(self.crash_count or 0)
        self.runtime.fail_count = int(self.fail_count or 0)
        self.runtime.recovery_budget_count = len(self.recovery_budget_attempts or [])
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
        self.runtime.observed_server_type = self.observed_server_type or ""
        self.runtime.observed_private_server_id = self.observed_private_server_id or ""
        self.runtime.observed_private_server_owner_id = self.observed_private_server_owner_id or ""
        self.runtime.observed_place_id = self.observed_place_id or ""
        self.runtime.observed_job_id = self.observed_job_id or ""
        self.runtime.observed_universe_id = self.observed_universe_id or ""
        self.runtime.observed_server_at = float(self.observed_server_at or 0.0)
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
            "import_status": self.import_status,
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
            import_status = str(d.get("import_status", "")),
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


def cookie_invalid_block_reason(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        lowered = text.lower()
        if not lowered:
            continue
        if lowered == "cookie_invalid":
            return "Invalid Cookie. Reimport the correct .ROBLOSECURITY for this account."
        has_cookie = "cookie" in lowered or ".roblosecurity" in lowered
        invalid = (
            "invalid" in lowered
            or "expired" in lowered
            or "missing" in lowered
            or "validation failed" in lowered
            or "not authenticated" in lowered
        )
        if has_cookie and invalid:
            return text
    return ""


def account_launch_block_reason(acc: Account) -> str:
    try:
        from services.captcha_guard import CAPTCHA_BLOCK_REASON, is_account_captcha_required

        if is_account_captcha_required(acc):
            return CAPTCHA_BLOCK_REASON
    except Exception:
        pass
    invalid = cookie_invalid_block_reason(
        getattr(acc, "manual_status", ""),
        getattr(acc, "import_status", ""),
        getattr(acc, "last_error", ""),
    )
    if invalid:
        return invalid
    return cookie_identity_block_reason(
        getattr(acc, "username", ""),
        getattr(acc, "cookie_username", ""),
        bool(getattr(acc, "cookie_mismatch", False)),
    )


def account_launchable(acc: Account) -> bool:
    return not account_launch_block_reason(acc)
