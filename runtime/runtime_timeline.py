from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional


class RuntimeTimeline:
    """Thin structured runtime event writer over RuntimeStore and in-memory log."""

    def __init__(
        self,
        store: Any = None,
        memory_log: Optional[List[Dict[str, Any]]] = None,
        memory_lock: Optional[threading.RLock] = None,
        logger: Any = None,
        memory_limit: int = 500,
    ):
        self._store = store
        self._memory_log = memory_log
        self._memory_lock = memory_lock or threading.RLock()
        self._logger = logger
        self._memory_limit = max(50, int(memory_limit or 500))

    def _log_error(self, event_type: str, error: Exception, account_id: str = "") -> None:
        if not self._logger:
            return
        try:
            self._logger("RUNTIME", event_type, "warning", account=account_id, error=str(error))
        except Exception:
            pass

    def record(
        self,
        event: Dict[str, Any],
        account_snapshot: Optional[Dict[str, Any]] = None,
        account_id: str = "",
    ) -> Dict[str, Any]:
        item = dict(event or {})
        item.setdefault("ts", time.time())
        item.setdefault("severity", "info")
        item.setdefault("event_type", item.get("kind", "runtime_event"))
        item.setdefault("reason", "")
        item.setdefault("account", account_id or item.get("account", ""))
        item.setdefault("thread_name", threading.current_thread().name)
        item.setdefault("lifecycle_owner", item.get("lifecycle_owner", "farm"))

        if account_snapshot:
            item.setdefault("pid", account_snapshot.get("pid"))
            item.setdefault("session_id", account_snapshot.get("session_id", ""))
            item.setdefault("launch_nonce", account_snapshot.get("launch_nonce", ""))
            item.setdefault("account_runtime_id", account_snapshot.get("account_runtime_id", ""))
            item.setdefault("rejoin_transaction_id", account_snapshot.get("rejoin_transaction_id", ""))
            item.setdefault("runtime_state", account_snapshot.get("runtime_state", ""))
            item.setdefault("public_state", account_snapshot.get("public_state", ""))
            item.setdefault("runtime_generation", account_snapshot.get("runtime_generation", 0))
            item.setdefault("recovery_generation", account_snapshot.get("recovery_generation", 0))
            item.setdefault("command_generation", account_snapshot.get("command_generation", 0))

        if self._memory_log is not None:
            with self._memory_lock:
                self._memory_log.append(item)
                if len(self._memory_log) > self._memory_limit:
                    del self._memory_log[:-self._memory_limit]

        if self._store:
            try:
                self._store.record_event(item)
                if account_snapshot and (account_id or item.get("account")):
                    self._store.record_account_snapshot(account_id or str(item.get("account", "")), account_snapshot)
            except Exception as exc:
                self._log_error("store_event_failed", exc, str(account_id or item.get("account", "")))
        return item

