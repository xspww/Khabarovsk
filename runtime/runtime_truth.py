from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from services.process_proof_policy import PROOF_STRONG, is_at_least_process_proof

TRUTH_CONFIRMED = "confirmed"
TRUTH_SUSPECT = "suspect"
TRUTH_QUARANTINED = "quarantined"
TRUTH_IDLE = "idle"


def _state_name(value: Any, default: str = "") -> str:
    return str(getattr(value, "name", value) or default).upper()


def _age(now: float, ts: Any) -> float:
    try:
        value = float(ts or 0.0)
    except Exception:
        value = 0.0
    return max(0.0, now - value) if value > 0 else 0.0


def _limit(value: float, default: float) -> float:
    return max(1.0, float(value or default))


class _AccountFacts(NamedTuple):
    account_id: str
    public_state: str
    runtime_state: str
    pid: int | None
    live: bool
    windows: int
    binding_status: str
    binding_confidence: float
    process_proof_level: str
    heartbeat_age: float
    server_age: float
    desired: str


def _account_facts(
    account: Any,
    process_alive: bool | None,
    window_count: int | None,
    now: float | None,
) -> _AccountFacts:
    current = float(now if now is not None else time.time())
    account_id = str(
        getattr(account, "_config_username", getattr(account, "username", "")) or ""
    )
    public_state = _state_name(getattr(account, "state", ""), "IDLE")
    runtime_state = _state_name(
        getattr(getattr(account, "runtime", None), "lifecycle_state", ""),
        "",
    )
    pid = getattr(account, "pid", None)
    try:
        pid = int(pid) if pid else None
    except Exception:
        pid = None
    live = bool(process_alive) if process_alive is not None else bool(pid)
    windows = max(0, int(window_count or 0))
    binding_status = str(getattr(account, "process_binding_status", "") or "")
    binding_confidence = float(
        getattr(account, "process_binding_confidence", 0.0) or 0.0
    )
    process_proof_level = str(getattr(account, "process_proof_level", "") or "")
    heartbeat_age = _age(current, getattr(account, "last_activity_at", 0.0))
    server_age = _age(current, getattr(account, "observed_server_at", 0.0))
    desired = _state_name(getattr(account, "desired_state", ""), "IN_GAME")
    return _AccountFacts(
        account_id,
        public_state,
        runtime_state,
        pid,
        live,
        windows,
        binding_status,
        binding_confidence,
        process_proof_level,
        heartbeat_age,
        server_age,
        desired,
    )


def _is_quarantined(account: Any) -> bool:
    if bool(getattr(account, "cookie_mismatch", False)):
        return True
    crash_reason = str(getattr(account, "last_crash_reason", "") or "")
    if crash_reason in {
        "captcha_required",
        "cookie_mismatch",
        "identity_locked",
        "account_recovery",
    }:
        return True
    recovery_status = str(getattr(account, "recovery_status", "") or "")
    return recovery_status in {
        "captcha_required",
        "identity_locked",
        "account_recovery",
    }


def _snapshot(
    facts: _AccountFacts,
    truth_state: str,
    confidence: float,
    reasons: list[str],
) -> RuntimeTruthSnapshot:
    return RuntimeTruthSnapshot(
        facts.account_id,
        truth_state,
        confidence,
        reasons,
        facts.pid,
        facts.live,
        facts.windows,
        facts.heartbeat_age,
        facts.server_age,
        facts.binding_status,
        facts.public_state,
        facts.runtime_state,
    )


def _process_points(facts: _AccountFacts, reasons: list[str]) -> float:
    if facts.live:
        return 35.0
    if facts.public_state == "IN_GAME":
        reasons.append("in_game_without_live_process")
    else:
        reasons.append("process_not_confirmed")
    return 0.0


def _binding_points(facts: _AccountFacts, reasons: list[str]) -> float:
    verified = facts.binding_status in {
        "verified",
        "adopted_visible_singleton",
        "restored",
    }
    if (
        verified
        and facts.binding_confidence >= 45.0
        and is_at_least_process_proof(facts.process_proof_level, PROOF_STRONG)
    ):
        return 20.0
    if verified:
        reasons.append(
            f"process_proof_{facts.process_proof_level or 'untrusted'}"
        )
    elif facts.binding_status:
        reasons.append(f"binding_{facts.binding_status}")
    return 0.0


def _evidence_points(
    facts: _AccountFacts,
    heartbeat_timeout: float,
    server_evidence_timeout: float,
    reasons: list[str],
) -> float:
    points = 0.0
    if facts.windows > 0:
        points += 15.0
    elif facts.live:
        reasons.append("live_without_visible_window")

    if facts.heartbeat_age and facts.heartbeat_age <= _limit(heartbeat_timeout, 120.0):
        points += 15.0
    elif facts.live:
        reasons.append("heartbeat_stale_or_missing")

    if facts.server_age and facts.server_age <= _limit(server_evidence_timeout, 300.0):
        points += 15.0
    elif facts.public_state in {"IN_GAME", "VERIFY"}:
        reasons.append("server_evidence_stale_or_missing")
    return points


@dataclass(frozen=True)
class RuntimeTruthSnapshot:
    account_id: str
    truth_state: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    pid: int | None = None
    process_alive: bool = False
    window_count: int = 0
    heartbeat_age_seconds: float = 0.0
    server_evidence_age_seconds: float = 0.0
    binding_status: str = ""
    public_state: str = ""
    runtime_state: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "truth_state": self.truth_state,
            "confidence": round(float(self.confidence or 0.0), 1),
            "reasons": list(self.reasons),
            "pid": self.pid,
            "process_alive": self.process_alive,
            "window_count": self.window_count,
            "heartbeat_age_seconds": round(float(self.heartbeat_age_seconds or 0.0), 1),
            "server_evidence_age_seconds": round(float(self.server_evidence_age_seconds or 0.0), 1),
            "binding_status": self.binding_status,
            "public_state": self.public_state,
            "runtime_state": self.runtime_state,
        }


def build_account_truth(
    account: Any,
    *,
    process_alive: bool | None = None,
    window_count: int | None = None,
    now: float | None = None,
    heartbeat_timeout: float = 120.0,
    server_evidence_timeout: float = 300.0,
) -> RuntimeTruthSnapshot:
    facts = _account_facts(account, process_alive, window_count, now)
    reasons: list[str] = []

    if _is_quarantined(account):
        reasons.append("auth_or_captcha_quarantine")
        return _snapshot(facts, TRUTH_QUARANTINED, 100.0, reasons)

    if facts.desired != "IN_GAME" or facts.public_state in {"IDLE", "READY"}:
        reasons.append("not_desired_or_idle")
        return _snapshot(facts, TRUTH_IDLE, 100.0, reasons)

    score = (
        _process_points(facts, reasons)
        + _binding_points(facts, reasons)
        + _evidence_points(
            facts,
            heartbeat_timeout,
            server_evidence_timeout,
            reasons,
        )
    )
    truth_state = TRUTH_CONFIRMED if score >= 70.0 else TRUTH_SUSPECT
    return _snapshot(facts, truth_state, score, reasons)
