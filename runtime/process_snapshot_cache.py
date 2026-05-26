from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from services.roblox_process_adapter import RobloxProcessAdapter


@dataclass(frozen=True)
class ProcessSnapshot:
    collected_at: float
    version: int
    processes: List[Dict[str, Any]] = field(default_factory=list)
    ttl_seconds: float = 2.0

    @property
    def count(self) -> int:
        return len(self.processes)

    def is_stale(self, now: Optional[float] = None) -> bool:
        current = time.time() if now is None else float(now)
        return (current - float(self.collected_at or 0.0)) > float(self.ttl_seconds or 0.0)

    def find_pid(self, pid: int) -> Dict[str, Any]:
        wanted = int(pid or 0)
        for item in self.processes:
            if int(item.get("pid") or 0) == wanted:
                return dict(item)
        return {}

    def to_dict(self, now: Optional[float] = None) -> Dict[str, Any]:
        return {
            "collected_at": self.collected_at,
            "version": self.version,
            "count": self.count,
            "stale": self.is_stale(now),
            "ttl_seconds": self.ttl_seconds,
        }


class ProcessSnapshotCache:
    """Read-only process snapshot cache.

    This cache is for status, ranking, and scan budgeting. Callers must still
    use RobloxProcessAdapter live validation before binding or killing a pid.
    """

    def __init__(
        self,
        adapter: Optional[RobloxProcessAdapter] = None,
        *,
        ttl_seconds: float = 2.0,
        clock=time.time,
    ):
        self._adapter = adapter or RobloxProcessAdapter()
        self._ttl_seconds = max(0.1, float(ttl_seconds or 2.0))
        self._clock = clock
        self._snapshot = ProcessSnapshot(0.0, 0, [], self._ttl_seconds)
        self._version = 0

    def snapshot(self, *, force: bool = False) -> ProcessSnapshot:
        now = float(self._clock())
        if not force and self._snapshot.processes and not self._snapshot.is_stale(now):
            return self._snapshot
        self._version += 1
        self._snapshot = ProcessSnapshot(
            collected_at=now,
            version=self._version,
            processes=self._adapter.list_live_game_processes(),
            ttl_seconds=self._ttl_seconds,
        )
        return self._snapshot

    def validate_live_for_action(self, account: Any, pid: int, *, reason: str) -> Any:
        return self._adapter.validate_binding(account, pid, reason=reason)

    def adapter(self) -> RobloxProcessAdapter:
        return self._adapter
