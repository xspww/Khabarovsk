from __future__ import annotations

import time
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from runtime.runtime_invariants import check_runtime_invariants, invariant_snapshot


RecordEvent = Callable[..., None]
PidValidator = Callable[[Any, int], bool]


def _account_key(account: Any) -> str:
    return str(getattr(account, "_config_username", "") or getattr(account, "username", "") or "")


class RuntimeInvariantMonitor:
    """Periodic invariant scanner that reports drift without owning runtime state."""

    def __init__(
        self,
        accounts: Iterable[Any],
        pid_validator: Optional[PidValidator] = None,
        record_event: Optional[RecordEvent] = None,
        suppress_seconds: float = 60.0,
    ):
        self._accounts = accounts
        self._pid_validator = pid_validator
        self._record_event = record_event
        self._suppress_seconds = max(1.0, float(suppress_seconds or 60.0))
        self._last_emit: Dict[Tuple[str, str], float] = {}

    def scan(self, now: Optional[float] = None) -> Dict[str, Any]:
        current = float(now if now is not None else time.time())
        checked = 0
        emitted = 0
        items = []
        for account in list(self._accounts or []):
            checked += 1
            account_key = _account_key(account)
            lock = getattr(account, "_lock", None)
            if lock:
                with lock:
                    violations = check_runtime_invariants(
                        account,
                        pid_validator=self._pid_validator,
                        now=current,
                    )
                    snapshot = invariant_snapshot(account)
            else:
                violations = check_runtime_invariants(
                    account,
                    pid_validator=self._pid_validator,
                    now=current,
                )
                snapshot = invariant_snapshot(account)
            for violation in violations:
                code = str(violation.get("code") or "runtime_invariant")
                item = {
                    "account": account_key,
                    "code": code,
                    "severity": str(violation.get("severity") or "warning"),
                    "violation": dict(violation),
                    "snapshot": dict(snapshot),
                }
                items.append(item)
                if self._should_emit(account_key, code, current):
                    emitted += 1
                    self._emit(account_key, code, item)
        return {
            "checked": checked,
            "violations": len(items),
            "emitted": emitted,
            "items": items,
        }

    def _should_emit(self, account_key: str, code: str, now: float) -> bool:
        key = (account_key, code)
        last = float(self._last_emit.get(key, 0.0) or 0.0)
        if last and now - last < self._suppress_seconds:
            return False
        self._last_emit[key] = now
        return True

    def _emit(self, account_key: str, code: str, item: Dict[str, Any]) -> None:
        if not self._record_event:
            return
        level = "error" if item["severity"] == "critical" else "warning"
        try:
            self._record_event(
                "runtime_invariant_violation",
                account_key,
                severity=level,
                reason=code,
                lifecycle_owner="runtime_invariant_monitor",
                violation=code,
                violation_detail=item["violation"],
                snapshot=item["snapshot"],
            )
        except Exception:
            return
