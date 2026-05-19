from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

from core import flog_kv


ResourceProbe = Callable[[], Dict[str, float]]
GuardProbe = Callable[[], Dict[str, Any]]
ProcessProbe = Callable[[], List[Dict[str, Any]]]


def _state_name(value: Any) -> str:
    return str(getattr(value, "name", value) or "").upper()


def _cfg_bool(cfg: Dict[str, Any], key: str, default: bool) -> bool:
    value = cfg.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def _cfg_float(cfg: Dict[str, Any], key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default))
    except Exception:
        return float(default)


def _cfg_int(cfg: Dict[str, Any], key: str, default: int) -> int:
    try:
        return int(float(cfg.get(key, default)))
    except Exception:
        return int(default)


@dataclass(frozen=True)
class MachineSnapshot:
    checked_at: float
    enabled: bool
    degraded: bool
    reasons: List[str] = field(default_factory=list)
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    live_roblox_processes: int = 0
    launching_accounts: int = 0
    verifying_accounts: int = 0
    in_game_accounts: int = 0
    suspect_accounts: int = 0
    queued_accounts: int = 0
    guard_state: str = ""
    guard_pid: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "enabled": self.enabled,
            "degraded": self.degraded,
            "reasons": list(self.reasons),
            "cpu_percent": round(self.cpu_percent, 1),
            "memory_percent": round(self.memory_percent, 1),
            "live_roblox_processes": self.live_roblox_processes,
            "launching_accounts": self.launching_accounts,
            "verifying_accounts": self.verifying_accounts,
            "in_game_accounts": self.in_game_accounts,
            "suspect_accounts": self.suspect_accounts,
            "queued_accounts": self.queued_accounts,
            "guard_state": self.guard_state,
            "guard_pid": self.guard_pid,
        }


@dataclass(frozen=True)
class MachineDecision:
    allowed: bool
    reason: str = ""
    retry_after_seconds: float = 0.0
    snapshot: Optional[MachineSnapshot] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "retry_after_seconds": round(float(self.retry_after_seconds or 0.0), 1),
            "snapshot": self.snapshot.to_dict() if self.snapshot else {},
        }


class MachineSupervisor:
    def __init__(
        self,
        cfg: Optional[Dict[str, Any]] = None,
        accounts: Optional[Iterable[Any]] = None,
        *,
        resource_probe: Optional[ResourceProbe] = None,
        guard_probe: Optional[GuardProbe] = None,
        process_probe: Optional[ProcessProbe] = None,
        clock: Callable[[], float] = time.time,
    ):
        self._cfg = dict(cfg or {})
        self._accounts = list(accounts or [])
        self._resource_probe = resource_probe or self._default_resource_probe
        self._guard_probe = guard_probe or self._default_guard_probe
        self._process_probe = process_probe or self._default_process_probe
        self._clock = clock
        self._lock = threading.RLock()
        self._cached_snapshot: Optional[MachineSnapshot] = None
        self._cached_at = 0.0

    def update_config(self, cfg: Dict[str, Any]) -> None:
        with self._lock:
            self._cfg = dict(cfg or {})
            self._cached_snapshot = None

    def set_accounts(self, accounts: Iterable[Any]) -> None:
        with self._lock:
            self._accounts = list(accounts or [])
            self._cached_snapshot = None

    def _enabled(self) -> bool:
        return _cfg_bool(self._cfg, "machine_supervisor_enabled", True)

    def _default_resource_probe(self) -> Dict[str, float]:
        try:
            import psutil

            return {
                "cpu_percent": float(psutil.cpu_percent(interval=0.0) or 0.0),
                "memory_percent": float(psutil.virtual_memory().percent or 0.0),
            }
        except Exception:
            return {"cpu_percent": 0.0, "memory_percent": 0.0}

    def _default_guard_probe(self) -> Dict[str, Any]:
        if not _cfg_bool(self._cfg, "multi_roblox_enabled", True):
            return {"state": "disabled", "pid": 0}
        try:
            from roblox_hybrid import multi_roblox_guard_status

            return multi_roblox_guard_status()
        except Exception as exc:
            return {"state": "unknown", "pid": 0, "detail": str(exc), "last_failure": str(exc)}

    def _default_process_probe(self) -> List[Dict[str, Any]]:
        try:
            from services.process_service import ProcessManager

            return ProcessManager.list_live_game_processes()
        except Exception:
            return []

    def snapshot(self, *, excluding: Optional[Any] = None, force: bool = False) -> MachineSnapshot:
        with self._lock:
            now = self._clock()
            if not force and excluding is None and self._cached_snapshot and (now - self._cached_at) < 2.0:
                return self._cached_snapshot
            resource = self._resource_probe()
            guard = self._guard_probe()
            live_processes = self._process_probe()
            reasons: List[str] = []
            cpu = float(resource.get("cpu_percent", 0.0) or 0.0)
            memory = float(resource.get("memory_percent", 0.0) or 0.0)
            if cpu >= _cfg_float(self._cfg, "machine_supervisor_cpu_high_percent", 96.0):
                reasons.append("cpu_pressure")
            if memory >= _cfg_float(self._cfg, "machine_supervisor_memory_high_percent", 96.0):
                reasons.append("memory_pressure")
            guard_state = str(guard.get("state") or "unknown").lower()
            if _cfg_bool(self._cfg, "multi_roblox_enabled", True) and guard_state not in {"ready", "disabled"}:
                reasons.append("multi_roblox_guard_not_ready")

            counts = {"launching": 0, "verifying": 0, "in_game": 0, "suspect": 0, "queued": 0}
            for acc in self._accounts:
                if acc is excluding:
                    continue
                state = _state_name(getattr(acc, "state", ""))
                if state == "LAUNCHING":
                    counts["launching"] += 1
                elif state == "VERIFY":
                    counts["verifying"] += 1
                elif state == "IN_GAME":
                    counts["in_game"] += 1
                elif state == "QUEUED":
                    counts["queued"] += 1
                if str(getattr(acc, "liveness_state", "") or "").lower() in {"suspect_frozen", "missing", "unbound"}:
                    counts["suspect"] += 1

            snapshot = MachineSnapshot(
                checked_at=now,
                enabled=self._enabled(),
                degraded=bool(reasons),
                reasons=reasons,
                cpu_percent=cpu,
                memory_percent=memory,
                live_roblox_processes=len(live_processes),
                launching_accounts=counts["launching"],
                verifying_accounts=counts["verifying"],
                in_game_accounts=counts["in_game"],
                suspect_accounts=counts["suspect"],
                queued_accounts=counts["queued"],
                guard_state=guard_state,
                guard_pid=int(guard.get("pid") or 0),
            )
            if excluding is None:
                self._cached_snapshot = snapshot
                self._cached_at = now
            return snapshot

    def launch_decision(self, account: Any) -> MachineDecision:
        snap = self.snapshot(excluding=account, force=True)
        with self._lock:
            cfg = dict(self._cfg)
        if not snap.enabled:
            return MachineDecision(True, "disabled", 0.0, snap)
        if "multi_roblox_guard_not_ready" in snap.reasons:
            return MachineDecision(False, "multi_roblox_guard_not_ready", 5.0, snap)
        active_launches = snap.launching_accounts + snap.verifying_accounts
        max_launching = max(1, _cfg_int(cfg, "machine_supervisor_max_launching_accounts", 1))
        if active_launches >= max_launching:
            return MachineDecision(False, "launch_wave_busy", 1.0, snap)
        max_live = max(1, _cfg_int(cfg, "max_concurrent_accounts", 40))
        if snap.live_roblox_processes >= max_live and not getattr(account, "pid", None):
            return MachineDecision(False, "live_process_capacity_reached", 5.0, snap)
        pressure = [reason for reason in snap.reasons if reason in {"cpu_pressure", "memory_pressure"}]
        if pressure:
            return MachineDecision(False, pressure[0], 10.0, snap)
        return MachineDecision(True, "allowed", 0.0, snap)

    def log_decision(self, account: Any, decision: MachineDecision, scope: str = "launch") -> None:
        if decision.allowed:
            return
        snap = decision.snapshot
        flog_kv(
            "MACHINE",
            f"{scope}_deferred",
            "warning",
            account=getattr(account, "display_name", getattr(account, "username", "")),
            reason=decision.reason,
            retry_after=f"{decision.retry_after_seconds:.1f}",
            cpu=f"{snap.cpu_percent:.1f}" if snap else "",
            memory=f"{snap.memory_percent:.1f}" if snap else "",
            live_roblox=snap.live_roblox_processes if snap else "",
            launching=snap.launching_accounts if snap else "",
            verifying=snap.verifying_accounts if snap else "",
            guard_state=snap.guard_state if snap else "",
        )
