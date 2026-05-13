from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

from runtime.recovery_context import RecoveryAttemptContext
from runtime.recovery_policy import active_recovery_block_reason


class RecoveryOwnerRegistry:
    """Owns active recovery ownership and generation-safe release checks."""

    def __init__(self):
        self._active_recoveries: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def active_count(self) -> int:
        with self._lock:
            return len(self._active_recoveries)

    def clear(self) -> int:
        with self._lock:
            count = len(self._active_recoveries)
            self._active_recoveries.clear()
            return count

    def get(self, account_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            owner = self._active_recoveries.get(str(account_key or ""))
            return dict(owner) if owner else None

    def block_reason(self, account_key: str, ctx: RecoveryAttemptContext) -> Dict[str, Any]:
        owner = self.get(account_key)
        return active_recovery_block_reason(owner, ctx)

    def check_start(
        self,
        account_key: str,
        runtime_generation: int,
        recovery_generation: int,
        reason: str,
        current_state: Any,
        force: bool = False,
    ) -> Dict[str, Any]:
        account_id = str(account_key or "")
        state_name = str(getattr(current_state, "name", current_state or ""))
        with self._lock:
            owner = self._active_recoveries.get(account_id)
            if not owner or force:
                return {"accepted": True}
            same_runtime = int(owner.get("runtime_generation", -1)) == int(runtime_generation or 0)
            same_recovery = int(owner.get("recovery_generation", -1)) == int(recovery_generation or 0)
            if same_runtime and same_recovery:
                if str(reason or "") in {"launch_fail", "watchdog_timeout"} and state_name in {"LAUNCHING", "VERIFY"}:
                    replaced = self._active_recoveries.pop(account_id, None) or {}
                    return {
                        "accepted": True,
                        "replaced": True,
                        "owner_reason": replaced.get("reason", ""),
                        "owner_runtime_generation": replaced.get("runtime_generation", 0),
                        "owner_recovery_generation": replaced.get("recovery_generation", 0),
                        "state": state_name,
                    }
                return {
                    "accepted": False,
                    "reject": "active_recovery_owner_duplicate",
                    "owner_reason": owner.get("reason", ""),
                    "owner_runtime_generation": owner.get("runtime_generation", 0),
                    "owner_recovery_generation": owner.get("recovery_generation", 0),
                }
            return {
                "accepted": False,
                "reject": "active_recovery_owner_exists",
                "owner_reason": owner.get("reason", ""),
                "owner_runtime_generation": owner.get("runtime_generation", 0),
                "owner_recovery_generation": owner.get("recovery_generation", 0),
            }

    def acquire(
        self,
        account_key: str,
        runtime_generation: int,
        recovery_generation: int,
        command_generation: int,
        session_id: str,
        transaction_id: str,
        reason: str,
        status: str,
        bucket: str,
        priority: int = 0,
        token: str = "",
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        account_id = str(account_key or "")
        started_at = time.time() if now is None else float(now)
        owner = {
            "account_id": account_id,
            "runtime_generation": int(runtime_generation or 0),
            "recovery_generation": int(recovery_generation or 0),
            "command_generation": int(command_generation or 0),
            "session_id": str(session_id or ""),
            "transaction_id": str(transaction_id or ""),
            "reason": str(reason or ""),
            "status": str(status or ""),
            "started_at": started_at,
            "bucket": str(bucket or ""),
            "priority": int(priority or 0),
            "token": str(token or ""),
        }
        with self._lock:
            self._active_recoveries[account_id] = owner
        return dict(owner)

    def release(
        self,
        account_key: str,
        runtime_generation: Optional[int],
        recovery_generation: Optional[int],
        reason: str,
    ) -> Dict[str, Any]:
        account_id = str(account_key or "")
        with self._lock:
            owner = self._active_recoveries.get(account_id)
            if not owner:
                return {"found": False, "released": False}
            if runtime_generation is not None and int(owner.get("runtime_generation", -1)) != int(runtime_generation):
                return {
                    "found": True,
                    "released": False,
                    "reject": "stale_runtime_generation",
                    **dict(owner),
                }
            if recovery_generation is not None and int(owner.get("recovery_generation", -1)) != int(recovery_generation):
                return {
                    "found": True,
                    "released": False,
                    "reject": "stale_recovery_generation",
                    **dict(owner),
                }
            removed = self._active_recoveries.pop(account_id, None) or {}
        return {"found": True, "released": True, "release_reason": str(reason or ""), **dict(removed)}
