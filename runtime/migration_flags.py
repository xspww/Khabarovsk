from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping


RUNTIME_MIGRATION_FLAGS = (
    "runtime_shadow_mode_enabled",
    "runtime_actor_enabled",
    "new_recovery_policy_enabled",
    "process_snapshot_cache_enabled",
    "lua_protocol_v2_enabled",
    "sqlite_runtime_source_enabled",
    "farm_supervisor_capacity_enabled",
    "destructive_action_gate_enforced",
)

CAPACITY_PROFILES: Dict[str, Dict[str, int]] = {
    "low": {
        "target_accounts": 5,
        "max_launching": 1,
        "popup_scan_parallel": 1,
        "process_scan_interval_seconds": 2,
    },
    "medium": {
        "target_accounts": 10,
        "max_launching": 1,
        "popup_scan_parallel": 2,
        "process_scan_interval_seconds": 2,
    },
    "high": {
        "target_accounts": 20,
        "max_launching": 2,
        "popup_scan_parallel": 3,
        "process_scan_interval_seconds": 3,
    },
}


def flag_enabled(cfg: Mapping[str, Any], name: str, default: bool = False) -> bool:
    value = cfg.get(name, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def enabled_flags(cfg: Mapping[str, Any]) -> Dict[str, bool]:
    return {name: flag_enabled(cfg, name, False) for name in RUNTIME_MIGRATION_FLAGS}


@dataclass(frozen=True)
class CapacityProfile:
    name: str
    target_accounts: int
    max_launching: int
    popup_scan_parallel: int
    process_scan_interval_seconds: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "target_accounts": self.target_accounts,
            "max_launching": self.max_launching,
            "popup_scan_parallel": self.popup_scan_parallel,
            "process_scan_interval_seconds": self.process_scan_interval_seconds,
        }


def capacity_profile(cfg: Mapping[str, Any]) -> CapacityProfile:
    name = str(cfg.get("runtime_capacity_profile") or "medium").strip().lower()
    values = CAPACITY_PROFILES.get(name) or CAPACITY_PROFILES["medium"]
    return CapacityProfile(name=name if name in CAPACITY_PROFILES else "medium", **values)
