from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from runtime.migration_flags import CapacityProfile, capacity_profile


ACTIVE_LAUNCH_STATES = {"LAUNCHING", "VERIFY"}


def _state_name(value: Any) -> str:
    return str(getattr(value, "name", value) or "").upper()


@dataclass(frozen=True)
class FarmCapacitySnapshot:
    checked_at: float
    profile: CapacityProfile
    total_accounts: int = 0
    desired_accounts: int = 0
    launching_accounts: int = 0
    queued_accounts: int = 0
    in_game_accounts: int = 0
    live_processes: int = 0
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "profile": self.profile.to_dict(),
            "total_accounts": self.total_accounts,
            "desired_accounts": self.desired_accounts,
            "launching_accounts": self.launching_accounts,
            "queued_accounts": self.queued_accounts,
            "in_game_accounts": self.in_game_accounts,
            "live_processes": self.live_processes,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class FarmAdmissionDecision:
    allowed: bool
    reason: str
    retry_after_seconds: float = 0.0
    snapshot: Optional[FarmCapacitySnapshot] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "retry_after_seconds": round(float(self.retry_after_seconds or 0.0), 1),
            "snapshot": self.snapshot.to_dict() if self.snapshot else {},
        }


class FarmSupervisor:
    """Global farm budget policy for shadow mode and actor admission.

    Account actors own per-account ordering. This module owns cross-account
    pressure so 5-20 accounts cannot all launch or scan at once.
    """

    def __init__(self, cfg: Optional[Dict[str, Any]] = None, *, clock=time.time):
        self._cfg = dict(cfg or {})
        self._clock = clock

    def update_config(self, cfg: Dict[str, Any]) -> None:
        self._cfg = dict(cfg or {})

    def snapshot(
        self,
        accounts: Iterable[Any],
        *,
        live_processes: int = 0,
        excluding: Optional[Any] = None,
    ) -> FarmCapacitySnapshot:
        profile = capacity_profile(self._cfg)
        counts = {
            "total": 0,
            "desired": 0,
            "launching": 0,
            "queued": 0,
            "in_game": 0,
        }
        for account in list(accounts or []):
            if account is excluding:
                continue
            counts["total"] += 1
            desired = _state_name(getattr(account, "desired_state", ""))
            state = _state_name(getattr(account, "state", ""))
            if desired == "IN_GAME":
                counts["desired"] += 1
            if state in ACTIVE_LAUNCH_STATES:
                counts["launching"] += 1
            elif state == "QUEUED":
                counts["queued"] += 1
            elif state == "IN_GAME":
                counts["in_game"] += 1
        reasons = []
        if counts["desired"] > profile.target_accounts:
            reasons.append("profile_target_exceeded")
        if counts["launching"] >= profile.max_launching:
            reasons.append("launch_capacity_busy")
        return FarmCapacitySnapshot(
            checked_at=float(self._clock()),
            profile=profile,
            total_accounts=counts["total"],
            desired_accounts=counts["desired"],
            launching_accounts=counts["launching"],
            queued_accounts=counts["queued"],
            in_game_accounts=counts["in_game"],
            live_processes=max(0, int(live_processes or 0)),
            reasons=reasons,
        )

    def admit_launch(
        self,
        account: Any,
        accounts: Iterable[Any],
        *,
        live_processes: int = 0,
    ) -> FarmAdmissionDecision:
        full_snap = self.snapshot(accounts, live_processes=live_processes)
        if full_snap.desired_accounts > full_snap.profile.target_accounts:
            return FarmAdmissionDecision(False, "profile_target_exceeded", 5.0, full_snap)
        slot_snap = self.snapshot(accounts, live_processes=live_processes, excluding=account)
        if slot_snap.launching_accounts >= slot_snap.profile.max_launching:
            return FarmAdmissionDecision(False, "launch_capacity_busy", 1.0, slot_snap)
        if slot_snap.live_processes >= slot_snap.profile.target_accounts and not getattr(account, "pid", None):
            return FarmAdmissionDecision(False, "profile_live_process_capacity_reached", 5.0, slot_snap)
        return FarmAdmissionDecision(True, "allowed", 0.0, slot_snap)
