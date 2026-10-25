from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from domain.block_reasons import LOCKED_BLOCK_REASON
from domain.runtime_lifecycle import lifecycle_for_public
from domain.runtime_models import AccountRuntime
from domain.states import LIFECYCLE_STATE, AccountState, runtime_state_for_public

STATE_META: dict[AccountState, dict[str, str]] = {
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

def _first_value(*values: Any, default: Any = "") -> Any:
    for value in values:
        if value:
            return value
    return default


def _runtime_int(value: Any) -> int:
    return int(value or 0)


def _runtime_float(value: Any) -> float:
    return float(value or 0.0)


class ServerType(StrEnum):
    VIP     = "VIP"
    PUBLIC  = "PUBLIC"
    UNKNOWN = "UNKNOWN"

@dataclass
class Account:
    username:   str
    user_id:    str  = ""
    priority:   int  = 50
    place_id:   str  = ""
    game_id:    str  = ""
    vip_links:  list[str] = field(default_factory=list)
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
    pid:            int | None   = None
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
    in_game_since:  float | None = None
    last_crash_at:  float | None = None
    last_launch_at: float | None = None
    retry_history:  list[dict]      = field(default_factory=list)
    recovery_budget_attempts: list[float] = field(default_factory=list)
    session_valid:  bool            = False
    session_checked: bool           = False
    session_wait_started_at: float  = 0.0
    pid_missing_since: float        = 0.0
    cooldown_until: float           = 0.0
    teleport_suppress_until: float  = 0.0  # Lua-reported teleport handoff â€” don't fault until arrival
    rapid_relaunch_count: int       = 0
    last_network_lost_at: float | None = None
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
    unmanaged_live_pids: list[int]  = field(default_factory=list)
    adopt_candidate_pid: int | None = None
    adopt_reject_reason: str        = ""
    orphan_confidence: float        = 0.0
    orphan_pid: int | None       = None
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
    launch_intent: dict[str, Any]   = field(default_factory=dict)
    launch_intent_summary: dict[str, Any] = field(default_factory=dict)

    # crash reason tracking
    last_crash_reason: str = ""
    last_state_reason: str = ""
    last_state_change_at: float = field(default_factory=time.time)
    last_error: str = ""

    # VipTracker instance â€” set externally by FarmController
    _vip_tracker: Any = field(default=None, repr=False, compare=False)
    runtime: AccountRuntime = field(default_factory=AccountRuntime, repr=False, compare=False)

    # Stable identity key (never changes after init)
    _config_username: str = field(default="", repr=False, compare=False)
    _lock: Any = field(default_factory=threading.RLock, repr=False, compare=False)

    def __post_init__(self):
        if not self._config_username:
            object.__setattr__(self, "_config_username", self.username)
        self.sync_runtime("init")

    def _sync_runtime_identity(self) -> None:
        self.runtime.account_id = _first_value(self._config_username, self.username)
        self.runtime.lifecycle_state = runtime_state_for_public(self.state)
        self.runtime.canonical_state = lifecycle_for_public(self.state).value
        self.runtime.public_state = self.state.name
        self.runtime.desired_public_state = self.desired_state.name
        self.runtime.pid = self.pid
        self.runtime.process_identity = self.bound_process_identity

    def _sync_runtime_binding(self) -> None:
        self.runtime.bind_status = _first_value(self.process_binding_status, default="unbound")
        self.runtime.binding_status = _first_value(self.process_binding_status, default="unbound")
        self.runtime.binding_decision = _first_value(self.binding_decision)
        self.runtime.process_binding_confidence = float(
            _first_value(self.process_binding_confidence, self.ownership_confidence, default=0.0)
        )
        self.runtime.process_proof_level = _first_value(self.process_proof_level, default="untrusted")
        self.runtime.process_reject_reason = _first_value(self.process_reject_reason)
        self.runtime.process_owner_claim = _first_value(self.process_owner_claim)
        self.runtime.unmanaged_live_process_count = int(self.unmanaged_live_process_count or 0)
        self.runtime.unmanaged_live_pids = list(_first_value(self.unmanaged_live_pids, default=[]))
        self.runtime.adopt_candidate_pid = self.adopt_candidate_pid
        self.runtime.adopt_reject_reason = _first_value(self.adopt_reject_reason)
        self.runtime.orphan_pid = self.orphan_pid
        self.runtime.orphan_identity = _first_value(self.orphan_identity)
        self.runtime.orphan_confidence = float(_first_value(self.orphan_confidence, default=0.0))
        self.runtime.orphan_observed_at = float(_first_value(self.orphan_observed_at, default=0.0))
        self.runtime.orphan_verify_after = float(_first_value(self.orphan_verify_after, default=0.0))

    def _sync_runtime_state(self) -> None:
        self.runtime.destination_validation = _first_value(
            self.destination_validation, self.server_validation, default="unverified"
        )
        summary = self.launch_intent_summary
        if not summary:
            summary = _first_value(self.launch_intent, default={}).get("launch_intent_summary", {})
        self.runtime.launch_intent_summary = dict(summary or {})
        self.runtime.runtime_generation = _runtime_int(self.runtime_generation)
        self.runtime.generation = _runtime_int(self.runtime_generation)
        self.runtime.recovery_generation = _runtime_int(self.recovery_generation)
        self.runtime.command_generation = _runtime_int(self.command_generation)
        self.runtime.retry_count = _runtime_int(self.retry_count)
        self.runtime.crash_count = _runtime_int(self.crash_count)
        self.runtime.fail_count = _runtime_int(self.fail_count)
        self.runtime.recovery_budget_count = len(_first_value(self.recovery_budget_attempts, default=[]))
        self.runtime.cooldown_until = _runtime_float(self.cooldown_until)
        self.runtime.recovery_status = _first_value(self.recovery_status)
        self.runtime.recovery_reason = _first_value(self.last_recovery_reason, self.last_crash_reason)
        self.runtime.recovery_inflight = bool(self.recovery_inflight)
        self.runtime.recovery_active = bool(_first_value(self.recovery_inflight, self.recovery_status, default=False))

    def _sync_runtime_sources(self) -> None:
        self.runtime.liveness_state = _first_value(self.liveness_state, default="unknown")
        self.runtime.liveness_score = _runtime_float(self.liveness_score)
        self.runtime.last_heartbeat = _runtime_float(self.last_activity_at)
        self.runtime.session_id = _first_value(self.session_id)
        self.runtime.launch_nonce = _first_value(self.launch_nonce)
        self.runtime.account_runtime_id = _first_value(self.account_runtime_id)
        self.runtime.rejoin_transaction_id = _first_value(self.rejoin_transaction_id)
        self.runtime.server_validation = _first_value(self.server_validation, default="unverified")
        self.runtime.observed_server_type = _first_value(self.observed_server_type)
        self.runtime.observed_private_server_id = _first_value(self.observed_private_server_id)
        self.runtime.observed_private_server_owner_id = _first_value(self.observed_private_server_owner_id)
        self.runtime.observed_place_id = _first_value(self.observed_place_id)
        self.runtime.observed_job_id = _first_value(self.observed_job_id)
        self.runtime.observed_universe_id = _first_value(self.observed_universe_id)
        self.runtime.observed_server_at = _runtime_float(self.observed_server_at)
        self.runtime.scheduler_slot = _first_value(self.scheduler_slot)
        self.runtime.supervisor_state = _first_value(
            self.supervisor_state, LIFECYCLE_STATE.get(self.state, "STOPPED").lower()
        )

    def _sync_runtime_transaction(self, reason: str) -> None:
        self.runtime.last_transaction_status = _first_value(self.last_transaction_status)
        self.runtime.last_transaction_step = _first_value(self.last_transaction_step)
        self.runtime.last_transaction_reason = _first_value(self.last_transaction_reason)
        self.runtime.last_transaction_started_at = float(
            _first_value(self.last_transaction_started_at, self.session_started_at, default=0.0)
        )
        self.runtime.last_transaction_completed_at = float(
            _first_value(self.last_transaction_completed_at, default=0.0)
        )
        self.runtime.last_transaction_failure_reason = _first_value(self.last_transaction_failure_reason)
        self.runtime.last_transition_at = float(_first_value(self.last_state_change_at, time.time()))
        self.runtime.last_transition_reason = _first_value(reason, self.last_state_reason)
        self.runtime.current_command = _first_value(self.current_command)
        if self.current_command_id:
            self.runtime.command_inflight = {
                "command_id": self.current_command_id,
                "action": _first_value(self.current_command),
                "age": round(max(0.0, time.time() - float(_first_value(self.command_inflight_started_at, time.time()))), 2),
            }
        else:
            self.runtime.command_inflight = None
        self.runtime.last_error = _first_value(self.last_error)

    def sync_runtime(self, reason: str = "") -> AccountRuntime:
        self._sync_runtime_identity()
        self._sync_runtime_binding()
        self._sync_runtime_state()
        self._sync_runtime_sources()
        self._sync_runtime_transaction(reason)
        return self.runtime

    def runtime_snapshot(self) -> dict[str, Any]:
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
        return "â€”"

    @property
    def is_vip(self) -> bool:
        return bool(self.vip_links)

    def to_dict(self) -> dict:
        return {
            "username":  self.username,
            "user_id":   self.user_id,
            "priority":  self.priority,
            "place_id":  self.place_id,
            "game_id":   self.game_id,
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
    def from_dict(d: dict) -> Account:
        return Account(
            username  = str(d.get("username", "")),
            user_id   = str(d.get("user_id", d.get("userId", ""))),
            priority  = int(d.get("priority", 50)),
            place_id  = str(d.get("place_id", "")),
            game_id   = str(d.get("game_id", d.get("gameId", ""))),
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


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#  EVENT BUS
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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


_COOKIE_INVALID_MARKERS = ("invalid", "expired", "missing", "validation failed", "not authenticated")


def _is_cookie_invalid_text(lowered: str) -> bool:
    has_cookie = "cookie" in lowered or ".roblosecurity" in lowered
    return has_cookie and any(marker in lowered for marker in _COOKIE_INVALID_MARKERS)


def cookie_invalid_block_reason(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        lowered = text.lower()
        if not lowered:
            continue
        # Banned is a separate state â€” never report it as cookie_invalid.
        if lowered.strip() == "banned" or "account banned" in lowered:
            continue
        if lowered == "cookie_invalid":
            return "Invalid Cookie. Reimport the correct .ROBLOSECURITY for this account."
        if _is_cookie_invalid_text(lowered):
            return text
    return ""


def banned_block_reason(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        lowered = text.lower()
        if not lowered:
            continue
        if lowered.strip() == "banned":
            try:
                from services.ban_guard import BANNED_BLOCK_REASON as _BANNED_MSG
            except Exception:
                _BANNED_MSG = "Account banned (Roblox). Unmark to allow rejoin."
            return _BANNED_MSG
        if "account banned" in lowered or "roblox ban" in lowered:
            return text
    return ""


def suspended_block_reason(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        lowered = text.lower()
        if not lowered:
            continue
        if lowered.strip() == "suspended":
            try:
                from services.ban_guard import SUSPENDED_BLOCK_REASON as _SUSPENDED_MSG
            except Exception:
                _SUSPENDED_MSG = "Account suspended by Roblox. Unmark to allow rejoin."
            return _SUSPENDED_MSG
        if "suspended by roblox" in lowered or "account suspended" in lowered:
            return text
    return ""


def is_account_banned(acc: Any) -> bool:
    try:
        from services.ban_guard import is_account_banned as _is_banned

        return bool(_is_banned(acc))
    except Exception:
        return False


def is_account_suspended(acc: Any) -> bool:
    try:
        from services.ban_guard import is_account_suspended as _is_suspended

        return bool(_is_suspended(acc))
    except Exception:
        return False


def is_account_locked(acc: Any) -> bool:
    try:
        from services.verify_guard import is_account_locked as _is_locked

        return bool(_is_locked(acc))
    except Exception:
        return False


def is_account_recovery(acc: Any) -> bool:
    try:
        from services.verify_guard import is_account_recovery as _is_recovery

        return bool(_is_recovery(acc))
    except Exception:
        return False


def locked_message() -> str:
    return LOCKED_BLOCK_REASON


def _locked_text_match(values: tuple[object, ...]) -> str:
    try:
        from services.verify_guard import is_locked_status_text
    except Exception:
        return ""
    for value in values:
        text = str(value or "").strip()
        if text and is_locked_status_text(text):
            return text
    return ""


def locked_block_reason(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        lowered = text.lower()
        if not lowered:
            continue
        if lowered.strip() in {"identity_locked", "locked"}:
            return locked_message()
        if "identity_locked" in lowered:
            return text
    return _locked_text_match(values)


def _recovery_message() -> str:
    try:
        from services.verify_guard import RECOVERY_BLOCK_REASON as message
    except Exception:
        message = "Account recovery - passkey/2FA required. Complete recovery manually, then click Resume."
    return message


def _recovery_text_match(values: tuple[object, ...]) -> str:
    try:
        from services.verify_guard import is_recovery_status_text
    except Exception:
        return ""
    for value in values:
        text = str(value or "").strip()
        if text and is_recovery_status_text(text):
            return text
    return ""


def recovery_block_reason(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        lowered = text.lower()
        if not lowered:
            continue
        if lowered.strip() == "account_recovery":
            return _recovery_message()
        if "account_recovery" in lowered:
            return text
    return _recovery_text_match(values)


FINISHED_STATUS = "Finished"
SUSPENDED_BLOCK_REASON_FALLBACK = "Account suspended by Roblox. Unmark to allow rejoin."


def is_account_finished(acc: Any) -> bool:
    """User-marked done: skip launch/rejoin/recovery and ignore Lua signals."""
    try:
        if str(getattr(acc, "manual_status", "") or "").strip().lower() == "finished":
            return True
    except Exception:
        "intentional: ignore exception"
    try:
        if float(getattr(acc, "finished_at", 0.0) or 0.0) > 0:
            return True
    except Exception:
        "intentional: ignore exception"
    return False


def _suspended_launch_reason(acc: Account) -> str:
    return suspended_block_reason(
        getattr(acc, "manual_status", ""),
        getattr(acc, "import_status", ""),
        getattr(acc, "last_error", ""),
    ) or SUSPENDED_BLOCK_REASON_FALLBACK


def _banned_launch_reason(acc: Account) -> str:
    try:
        from services.ban_guard import BANNED_BLOCK_REASON
        from services.ban_guard import is_account_banned as _is_banned

        if _is_banned(acc):
            return BANNED_BLOCK_REASON
    except Exception:
        "intentional: ignore exception"
    return banned_block_reason(
        getattr(acc, "manual_status", ""),
        getattr(acc, "import_status", ""),
        getattr(acc, "last_error", ""),
        getattr(acc, "last_crash_reason", ""),
        getattr(acc, "last_recovery_reason", ""),
    )


def _manual_verification_reason(acc: Account) -> str:
    try:
        from services.verify_guard import (
            LOCKED_BLOCK_REASON,
            RECOVERY_BLOCK_REASON,
            is_account_locked,
            is_account_recovery,
        )

        if is_account_locked(acc):
            return LOCKED_BLOCK_REASON
        if is_account_recovery(acc):
            return RECOVERY_BLOCK_REASON
    except Exception:
        "intentional: ignore exception"
    return ""


def _hold_text_reason(acc: Account) -> str:
    try:
        locked = locked_block_reason(
            getattr(acc, "manual_status", ""),
            getattr(acc, "import_status", ""),
            getattr(acc, "last_error", ""),
            getattr(acc, "last_crash_reason", ""),
            getattr(acc, "last_recovery_reason", ""),
        )
        if locked:
            return locked
        return recovery_block_reason(
            getattr(acc, "manual_status", ""),
            getattr(acc, "import_status", ""),
            getattr(acc, "last_error", ""),
            getattr(acc, "last_crash_reason", ""),
            getattr(acc, "last_recovery_reason", ""),
        )
    except Exception:
        return ""


def _captcha_launch_reason(acc: Account) -> str:
    try:
        from services.captcha_guard import CAPTCHA_BLOCK_REASON, is_account_captcha_required

        if is_account_captcha_required(acc):
            return CAPTCHA_BLOCK_REASON
    except Exception:
        "intentional: ignore exception"
    return ""


def _invalid_cookie_launch_reason(acc: Account) -> str:
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


def account_launch_block_reason(acc: Account) -> str:
    if is_account_finished(acc):
        return FINISHED_STATUS
    # A suspension blocks the launch just as hard as a ban, but it reports its
    # own reason so the dashboard can label it correctly.
    if is_account_suspended(acc):
        return _suspended_launch_reason(acc)
    return (
        _banned_launch_reason(acc)
        or suspended_block_reason(
            getattr(acc, "manual_status", ""),
            getattr(acc, "import_status", ""),
            getattr(acc, "last_error", ""),
        )
        or _manual_verification_reason(acc)
        or _hold_text_reason(acc)
        or _captcha_launch_reason(acc)
        or _invalid_cookie_launch_reason(acc)
    )


def account_launchable(acc: Account) -> bool:
    return not account_launch_block_reason(acc)
