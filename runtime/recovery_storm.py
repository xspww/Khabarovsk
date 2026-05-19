from __future__ import annotations

import threading
import time
import zlib
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

from core import AccountState


RECOVERY_STORM_STATES = {
    AccountState.CRASH,
    AccountState.NETWORK_LOST,
    AccountState.COOLDOWN,
    AccountState.QUEUED,
    AccountState.LAUNCHING,
    AccountState.VERIFY,
}


def _bool(cfg: Dict[str, Any], key: str, default: bool) -> bool:
    value = cfg.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def _float(cfg: Dict[str, Any], key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default))
    except Exception:
        return float(default)


def _int(cfg: Dict[str, Any], key: str, default: int) -> int:
    try:
        return int(float(cfg.get(key, default)))
    except Exception:
        return int(default)


def _stable_jitter(account_key: str, reason: str, window: float) -> float:
    if window <= 0:
        return 0.0
    seed = f"{account_key}:{reason}".encode("utf-8", "ignore")
    return (zlib.crc32(seed) % 1000) / 1000.0 * window


@dataclass(frozen=True)
class RecoveryStormDecision:
    requested_delay: float
    delay_seconds: float
    reason: str
    active_recovery_count: int
    net_online: bool

    @property
    def delayed(self) -> bool:
        return self.delay_seconds > self.requested_delay + 0.05

    def to_log_fields(self) -> Dict[str, Any]:
        return {
            "requested_delay": f"{self.requested_delay:.1f}",
            "delay": f"{self.delay_seconds:.1f}",
            "storm_reason": self.reason,
            "active_recovery_count": self.active_recovery_count,
            "net_online": self.net_online,
        }


class RecoveryStormController:
    def __init__(self, cfg: Dict[str, Any], accounts: Iterable[Any], *, clock=time.time):
        self._cfg = dict(cfg or {})
        self._accounts: List[Any] = list(accounts or [])
        self._clock = clock
        self._lock = threading.Lock()
        self._next_due_at = 0.0
        self._last_decision = RecoveryStormDecision(0.0, 0.0, "disabled", 0, True)

    def update_config(self, cfg: Dict[str, Any]) -> None:
        with self._lock:
            self._cfg = dict(cfg or {})

    def set_accounts(self, accounts: Iterable[Any]) -> None:
        with self._lock:
            self._accounts = list(accounts or [])

    def enabled(self) -> bool:
        return _bool(self._cfg, "recovery_storm_enabled", False)

    def _active_recovery_count(self, accounts=None, excluding: Any = None) -> int:
        count = 0
        source = self._accounts if accounts is None else accounts
        for acc in source:
            if acc is excluding:
                continue
            state = getattr(acc, "state", None)
            desired = getattr(acc, "desired_state", AccountState.IN_GAME)
            inflight = bool(getattr(acc, "recovery_inflight", False))
            scheduled = bool(float(getattr(acc, "recovery_scheduled_at", 0.0) or 0.0) > 0.0)
            if desired == AccountState.IN_GAME and (state in RECOVERY_STORM_STATES or inflight or scheduled):
                count += 1
        return count

    def reserve_delay(self, account: Any, requested_delay: float, reason: str, *, net_online: bool = True) -> RecoveryStormDecision:
        requested = max(0.0, float(requested_delay or 0.0))
        with self._lock:
            cfg = dict(self._cfg)
            accounts = list(self._accounts)
            if not _bool(cfg, "recovery_storm_enabled", False):
                decision = RecoveryStormDecision(requested, requested, "disabled", 0, bool(net_online))
                self._last_decision = decision
                return decision

            now = self._clock()
            account_key = str(getattr(account, "_config_username", getattr(account, "username", "")) or "")
            active = self._active_recovery_count(accounts, excluding=account)
            max_active = max(1, _int(cfg, "recovery_storm_max_active", 3))
            spacing = max(0.0, _float(cfg, "recovery_storm_min_spacing_seconds", 5.0))
            jitter_window = max(0.0, _float(cfg, "recovery_storm_jitter_seconds", 3.0))
            outage_backoff = max(0.0, _float(cfg, "recovery_storm_outage_backoff_seconds", 30.0))
            due_at = now + requested
            reason_key = "normal"
            if not net_online and outage_backoff > requested:
                due_at = max(due_at, now + outage_backoff)
                reason_key = "network_outage_backoff"
            if active >= max_active:
                over = active - max_active + 1
                due_at = max(due_at, now + spacing * over)
                reason_key = "max_active_recovery"
            if self._next_due_at > due_at:
                due_at = self._next_due_at
                reason_key = "global_spacing"
            if due_at > now + requested + 0.05:
                due_at += _stable_jitter(account_key, reason, jitter_window)
            self._next_due_at = max(self._next_due_at, due_at) + spacing
            decision = RecoveryStormDecision(requested, max(0.0, due_at - now), reason_key, active, bool(net_online))
            self._last_decision = decision
            return decision

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            cfg = dict(self._cfg)
            accounts = list(self._accounts)
            return {
                "enabled": _bool(cfg, "recovery_storm_enabled", False),
                "next_due_at": self._next_due_at,
                "active_recovery_count": self._active_recovery_count(accounts),
                "last_decision": self._last_decision.to_log_fields(),
            }
