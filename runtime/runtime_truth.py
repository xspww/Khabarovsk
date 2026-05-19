from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

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


@dataclass(frozen=True)
class RuntimeTruthSnapshot:
    account_id: str
    truth_state: str
    confidence: float
    reasons: List[str] = field(default_factory=list)
    pid: Optional[int] = None
    process_alive: bool = False
    window_count: int = 0
    heartbeat_age_seconds: float = 0.0
    server_evidence_age_seconds: float = 0.0
    binding_status: str = ""
    public_state: str = ""
    runtime_state: str = ""

    def to_dict(self) -> Dict[str, Any]:
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
    process_alive: Optional[bool] = None,
    window_count: Optional[int] = None,
    now: Optional[float] = None,
    heartbeat_timeout: float = 120.0,
    server_evidence_timeout: float = 300.0,
) -> RuntimeTruthSnapshot:
    current = float(now if now is not None else time.time())
    account_id = str(getattr(account, "_config_username", getattr(account, "username", "")) or "")
    public_state = _state_name(getattr(account, "state", ""), "IDLE")
    runtime_state = _state_name(getattr(getattr(account, "runtime", None), "lifecycle_state", ""), "")
    pid = getattr(account, "pid", None)
    try:
        pid = int(pid) if pid else None
    except Exception:
        pid = None
    live = bool(process_alive) if process_alive is not None else bool(pid)
    windows = max(0, int(window_count or 0))
    binding_status = str(getattr(account, "process_binding_status", "") or "")
    binding_confidence = float(getattr(account, "process_binding_confidence", 0.0) or 0.0)
    process_proof_level = str(getattr(account, "process_proof_level", "") or "")
    heartbeat_age = _age(current, getattr(account, "last_activity_at", 0.0))
    server_age = _age(current, getattr(account, "observed_server_at", 0.0))
    desired = _state_name(getattr(account, "desired_state", ""), "IN_GAME")
    reasons: List[str] = []

    if (
        bool(getattr(account, "cookie_mismatch", False))
        or str(getattr(account, "last_crash_reason", "") or "") in {"captcha_required", "cookie_mismatch"}
        or str(getattr(account, "recovery_status", "") or "") == "captcha_required"
    ):
        reasons.append("auth_or_captcha_quarantine")
        return RuntimeTruthSnapshot(account_id, TRUTH_QUARANTINED, 100.0, reasons, pid, live, windows, heartbeat_age, server_age, binding_status, public_state, runtime_state)

    if desired != "IN_GAME" or public_state in {"IDLE", "READY"}:
        reasons.append("not_desired_or_idle")
        return RuntimeTruthSnapshot(account_id, TRUTH_IDLE, 100.0, reasons, pid, live, windows, heartbeat_age, server_age, binding_status, public_state, runtime_state)

    score = 0.0
    if live:
        score += 35.0
    elif public_state == "IN_GAME":
        reasons.append("in_game_without_live_process")
    else:
        reasons.append("process_not_confirmed")

    if (
        binding_status in {"verified", "adopted_visible_singleton", "restored"}
        and binding_confidence >= 45.0
        and is_at_least_process_proof(process_proof_level, PROOF_STRONG)
    ):
        score += 20.0
    elif binding_status in {"verified", "adopted_visible_singleton", "restored"}:
        reasons.append(f"process_proof_{process_proof_level or 'untrusted'}")
    elif binding_status:
        reasons.append(f"binding_{binding_status}")

    if windows > 0:
        score += 15.0
    elif live:
        reasons.append("live_without_visible_window")

    if heartbeat_age and heartbeat_age <= max(1.0, float(heartbeat_timeout or 120.0)):
        score += 15.0
    elif live:
        reasons.append("heartbeat_stale_or_missing")

    if server_age and server_age <= max(1.0, float(server_evidence_timeout or 300.0)):
        score += 15.0
    elif public_state in {"IN_GAME", "VERIFY"}:
        reasons.append("server_evidence_stale_or_missing")

    if score >= 70.0:
        truth_state = TRUTH_CONFIRMED
    else:
        truth_state = TRUTH_SUSPECT
    return RuntimeTruthSnapshot(account_id, truth_state, score, reasons, pid, live, windows, heartbeat_age, server_age, binding_status, public_state, runtime_state)
