from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, NamedTuple

NETWORK_DISCONNECT = "NETWORK_DISCONNECT"
SESSION_CONFLICT = "SESSION_CONFLICT"
AUTH_FAILURE = "AUTH_FAILURE"
TELEPORT_FAILURE = "TELEPORT_FAILURE"
SERVER_FULL = "SERVER_FULL"
PROCESS_CRASH = "PROCESS_CRASH"
VISUAL_DISCONNECT = "VISUAL_DISCONNECT"


PRIORITY_INFORMATIONAL = 10
PRIORITY_LOW = 20
PRIORITY_MEDIUM = 40
PRIORITY_MEDIUM_HIGH = 60
PRIORITY_HIGH = 80
PRIORITY_CRITICAL = 100


REASON_TO_CATEGORY = {
    "network_drop": NETWORK_DISCONNECT,
    "connection_error": NETWORK_DISCONNECT,
    "idle_disconnect": NETWORK_DISCONNECT,
    "security_kick": NETWORK_DISCONNECT,
    "session_conflict": SESSION_CONFLICT,
    "account_launched_elsewhere": SESSION_CONFLICT,
    "auth_failure": AUTH_FAILURE,
    "cookie_invalid": AUTH_FAILURE,
    "cookie_missing": AUTH_FAILURE,
    "cookie_mismatch": AUTH_FAILURE,
    "banned": AUTH_FAILURE,
    "captcha_required": AUTH_FAILURE,
    "identity_locked": AUTH_FAILURE,
    "account_recovery": AUTH_FAILURE,
    "teleport_timeout": TELEPORT_FAILURE,
    "server_full": SERVER_FULL,
    "process_crash": PROCESS_CRASH,
    "pid_dead": PROCESS_CRASH,
    "not_responding": PROCESS_CRASH,
    "watchdog_timeout": PROCESS_CRASH,
    "loading_freeze": PROCESS_CRASH,
    "visual_disconnect": VISUAL_DISCONNECT,
}


CATEGORY_TO_REASON = {
    NETWORK_DISCONNECT: "network_drop",
    SESSION_CONFLICT: "session_conflict",
    AUTH_FAILURE: "auth_failure",
    TELEPORT_FAILURE: "teleport_timeout",
    SERVER_FULL: "server_full",
    PROCESS_CRASH: "process_crash",
    VISUAL_DISCONNECT: "connection_error",
}


CATEGORY_PRIORITY = {
    PROCESS_CRASH: PRIORITY_CRITICAL,
    NETWORK_DISCONNECT: PRIORITY_HIGH,
    SESSION_CONFLICT: PRIORITY_HIGH,
    VISUAL_DISCONNECT: PRIORITY_MEDIUM_HIGH,
    TELEPORT_FAILURE: PRIORITY_MEDIUM_HIGH,
    SERVER_FULL: PRIORITY_MEDIUM_HIGH,
    AUTH_FAILURE: PRIORITY_CRITICAL,
}


def normalize_disconnect_category(reason: str = "", popup_code: str = "", category: str = "") -> str:
    explicit = str(category or "").strip().upper()
    if explicit:
        return explicit
    code = str(popup_code or "").strip()
    if code == "277":
        return NETWORK_DISCONNECT
    if code == "278":
        return NETWORK_DISCONNECT
    if code == "273":
        return SESSION_CONFLICT
    if code == "267":
        return NETWORK_DISCONNECT
    if code == "268":
        return NETWORK_DISCONNECT
    reason_key = str(reason or "").strip().lower()
    return REASON_TO_CATEGORY.get(reason_key, PROCESS_CRASH if reason_key else NETWORK_DISCONNECT)


def reason_for_category(category: str, fallback: str = "") -> str:
    return CATEGORY_TO_REASON.get(str(category or "").strip().upper(), fallback or "connection_error")


def priority_for_signal(signal: str = "", category: str = "", popup_code: str = "", visual: bool = False) -> int:
    raw_signal = str(signal or "").strip().lower()
    code = str(popup_code or "").strip()
    if raw_signal in {"process_lost", "process_dead", "pid_dead"}:
        return PRIORITY_CRITICAL
    if code == "277":
        return PRIORITY_HIGH
    if code == "278":
        return PRIORITY_HIGH
    if code == "273":
        return PRIORITY_HIGH
    if visual:
        return PRIORITY_MEDIUM_HIGH
    if raw_signal in {"watchdog_timeout", "loading_freeze", "fault", "crash"}:
        return PRIORITY_MEDIUM
    return CATEGORY_PRIORITY.get(str(category or "").strip().upper(), PRIORITY_MEDIUM)


class _PayloadText(NamedTuple):
    popup_code: str
    trigger: str
    watchdog_reason: str
    cooldown_reason: str
    detail: str


def _payload_text_fields(
    payload: dict[str, Any],
    signal: str,
    reason: str,
) -> _PayloadText:
    return _PayloadText(
        popup_code=str(payload.get("popup_code") or payload.get("error_code") or ""),
        trigger=str(payload.get("trigger") or signal or reason or ""),
        watchdog_reason=str(payload.get("watchdog_reason") or ""),
        cooldown_reason=str(payload.get("cooldown_reason") or ""),
        detail=str(payload.get("detail") or payload.get("reason_msg") or ""),
    )


def _payload_numeric(
    payload: dict[str, Any],
    account: Any,
) -> tuple[float, int]:
    confidence = float(
        payload.get("popup_confidence") or payload.get("confidence") or 0.0
    )
    retry_count = int(
        payload.get("retry_count") or getattr(account, "retry_count", 0) or 0
    )
    return confidence, retry_count


def _account_id(account: Any) -> str:
    return str(
        getattr(account, "_config_username", "")
        or getattr(account, "username", "")
        or ""
    )


def _context_category(
    payload: dict[str, Any],
    reason: str,
    popup_code: str,
) -> str:
    return normalize_disconnect_category(
        reason=payload.get("reason_key") or reason,
        popup_code=popup_code,
        category=payload.get("disconnect_category") or payload.get("category") or "",
    )


def _context_priority(
    payload: dict[str, Any],
    signal: str,
    category: str,
    popup_code: str,
) -> int:
    return int(
        payload.get("signal_priority")
        or priority_for_signal(
            signal=signal,
            category=category,
            popup_code=popup_code,
            visual=bool(payload.get("visual_disconnect", False)),
        )
    )


@dataclass(frozen=True)
class RecoveryAttemptContext:
    account_id: str
    runtime_generation: int
    pid: int = 0
    trigger: str = ""
    category: str = NETWORK_DISCONNECT
    popup_code: str = ""
    popup_confidence: float = 0.0
    watchdog_reason: str = ""
    cooldown_reason: str = ""
    retry_count: int = 0
    created_at: float = field(default_factory=time.time)
    token: str = field(default_factory=lambda: uuid.uuid4().hex)
    priority: int = PRIORITY_MEDIUM
    detail: str = ""

    @property
    def signature(self) -> tuple[str, int, str]:
        return (self.account_id, int(self.runtime_generation or 0), self.category)

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "runtime_generation": self.runtime_generation,
            "pid": self.pid,
            "trigger": self.trigger,
            "category": self.category,
            "popup_code": self.popup_code,
            "popup_confidence": self.popup_confidence,
            "watchdog_reason": self.watchdog_reason,
            "cooldown_reason": self.cooldown_reason,
            "retry_count": self.retry_count,
            "created_at": self.created_at,
            "token": self.token,
            "priority": self.priority,
            "detail": self.detail,
            "signature": "|".join(str(part) for part in self.signature),
        }

    @classmethod
    def from_account_payload(
        cls,
        account: Any,
        signal: str,
        reason: str,
        payload: dict[str, Any] | None = None,
    ) -> RecoveryAttemptContext:
        payload = dict(payload or {})
        text = _payload_text_fields(payload, signal, reason)
        category = _context_category(payload, reason, text.popup_code)
        priority = _context_priority(
            payload,
            signal,
            category,
            text.popup_code,
        )
        confidence, retry_count = _payload_numeric(payload, account)
        return cls(
            account_id=_account_id(account),
            runtime_generation=int(getattr(account, "runtime_generation", 0) or 0),
            pid=int(getattr(account, "pid", 0) or 0),
            trigger=text.trigger,
            category=category,
            popup_code=text.popup_code,
            popup_confidence=confidence,
            watchdog_reason=text.watchdog_reason,
            cooldown_reason=text.cooldown_reason,
            retry_count=retry_count,
            priority=priority,
            detail=text.detail,
        )
