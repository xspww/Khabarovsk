from __future__ import annotations

import threading
import time
from typing import Any

from runtime.recovery_context import RecoveryAttemptContext
from runtime.recovery_policy import active_recovery_block_reason

_REPLACE_REASONS = {"launch_fail", "game_unavailable", "watchdog_timeout"}
_REPLACE_STATES = {"LAUNCHING", "VERIFY"}


def _int_value(value: Any) -> int:
    return int(value or 0)


def _text(value: Any) -> str:
    return str(value or "")


def _owner_details(owner: dict[str, Any]) -> dict[str, Any]:
    return {
        "owner_reason": owner.get("reason", ""),
        "owner_runtime_generation": owner.get("runtime_generation", 0),
        "owner_recovery_generation": owner.get("recovery_generation", 0),
    }


def _generation_match(
    owner: dict[str, Any],
    runtime_generation: int,
    recovery_generation: int,
) -> tuple[bool, bool]:
    same_runtime = int(owner.get("runtime_generation", -1)) == int(runtime_generation or 0)
    same_recovery = int(owner.get("recovery_generation", -1)) == int(recovery_generation or 0)
    return same_runtime, same_recovery


def _can_replace(reason: str, state_name: str) -> bool:
    return str(reason or "") in _REPLACE_REASONS and state_name in _REPLACE_STATES


def _reject_owner(owner: dict[str, Any], reject: str) -> dict[str, Any]:
    return {"accepted": False, "reject": reject, **_owner_details(owner)}


class RecoveryOwnerRegistry:
    """Owns active recovery ownership and generation-safe release checks."""

    def __init__(self):
        self._active_recoveries: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def active_count(self) -> int:
        with self._lock:
            return len(self._active_recoveries)

    def clear(self) -> int:
        with self._lock:
            count = len(self._active_recoveries)
            self._active_recoveries.clear()
            return count

    def get(self, account_key: str) -> dict[str, Any] | None:
        with self._lock:
            owner = self._active_recoveries.get(str(account_key or ""))
            return dict(owner) if owner else None

    def block_reason(self, account_key: str, ctx: RecoveryAttemptContext) -> dict[str, Any]:
        owner = self.get(account_key)
        return active_recovery_block_reason(owner, ctx)

    def _replace_owner(self, account_id: str, state_name: str) -> dict[str, Any]:
        replaced = self._active_recoveries.pop(account_id, None) or {}
        return {
            "accepted": True,
            "replaced": True,
            **_owner_details(replaced),
            "state": state_name,
        }

    def check_start(
        self,
        account_key: str,
        runtime_generation: int,
        recovery_generation: int,
        reason: str,
        current_state: Any,
        force: bool = False,
    ) -> dict[str, Any]:
        account_id = _text(account_key)
        state_name = str(getattr(current_state, "name", current_state or ""))
        with self._lock:
            owner = self._active_recoveries.get(account_id)
            if not owner or force:
                return {"accepted": True}
            same_runtime, same_recovery = _generation_match(
                owner,
                runtime_generation,
                recovery_generation,
            )
            if not (same_runtime and same_recovery):
                return _reject_owner(owner, "active_recovery_owner_exists")
            if _can_replace(reason, state_name):
                return self._replace_owner(account_id, state_name)
            return _reject_owner(owner, "active_recovery_owner_duplicate")

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
        now: float | None = None,
    ) -> dict[str, Any]:
        account_id = _text(account_key)
        started_at = time.time() if now is None else float(now)
        owner = {
            "account_id": account_id,
            "runtime_generation": _int_value(runtime_generation),
            "recovery_generation": _int_value(recovery_generation),
            "command_generation": _int_value(command_generation),
            "session_id": _text(session_id),
            "transaction_id": _text(transaction_id),
            "reason": _text(reason),
            "status": _text(status),
            "started_at": started_at,
            "bucket": _text(bucket),
            "priority": _int_value(priority),
            "token": _text(token),
        }
        with self._lock:
            self._active_recoveries[account_id] = owner
        return dict(owner)

    def release(
        self,
        account_key: str,
        runtime_generation: int | None,
        recovery_generation: int | None,
        reason: str,
    ) -> dict[str, Any]:
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
