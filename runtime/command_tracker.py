from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple


Logger = Callable[..., None]


class RuntimeCommandTracker:
    """Tracks state-changing runtime commands and idempotency replay."""

    def __init__(
        self,
        runtime_state: Any,
        find_account: Callable[[str], Optional[Any]],
        capability: Callable[[str, str], Tuple[bool, str, Optional[Any]]],
        record_timeline: Callable[..., None],
        bump_status_revision: Callable[[], int],
        logger: Optional[Logger] = None,
        is_shutting_down: Optional[Callable[[], bool]] = None,
        idempotency_ttl: float = 300.0,
    ):
        self._runtime_state = runtime_state
        self._find_account = find_account
        self._capability = capability
        self._record_timeline = record_timeline
        self._bump_status_revision = bump_status_revision
        self._log = logger
        self._is_shutting_down = is_shutting_down or (lambda: False)
        self._idempotency_ttl = max(30.0, float(idempotency_ttl or 300.0))
        self._lock = threading.RLock()
        self._commands: Dict[str, Dict[str, Any]] = {}
        self._idempotency: Dict[str, Dict[str, Any]] = {}
        self._seq = 0
        self._generation = 0

    @property
    def generation(self) -> int:
        with self._lock:
            return int(self._generation)

    def _emit(self, name: str, level: str = "info", **fields: Any) -> None:
        if not self._log:
            return
        try:
            self._log("COMMAND", name, level, **fields)
        except TypeError:
            self._log("COMMAND", name, **fields)

    def _scope(self, idempotency_key: str = "", request_fingerprint: str = "") -> str:
        key = str(idempotency_key or "").strip()
        if not key:
            return ""
        return f"{request_fingerprint or 'runtime'}:{key}"

    def _cleanup_locked(self) -> None:
        now = time.time()
        expired = [key for key, item in self._commands.items() if float(item.get("expires_at") or 0.0) <= now]
        for key in expired:
            item = self._commands.pop(key, None)
            if not item:
                continue
            acc = self._find_account(str(item.get("account", "") or ""))
            if acc:
                with acc._lock:
                    self._runtime_state.finish_account_command(
                        acc,
                        str(item.get("command_id", "")),
                        ok=False,
                        error="expired",
                    )
            self._emit(
                "expired",
                "warning",
                key=key,
                action=item.get("action", ""),
                command_id=item.get("command_id", ""),
                account=item.get("account", ""),
            )
        expired_idem = [key for key, item in self._idempotency.items() if float(item.get("expires_at") or 0.0) <= now]
        for key in expired_idem:
            self._idempotency.pop(key, None)

    def _conflict_locked(self, key: str) -> Optional[Dict[str, Any]]:
        for existing_key, item in self._commands.items():
            if existing_key == key:
                continue
            return item
        return None

    def _duplicate(self, key: str, action: str, account: str, existing: Dict[str, Any], reason: str = "duplicate_command") -> Tuple[bool, Dict[str, Any]]:
        self._emit(
            "duplicate",
            "warning",
            key=key,
            action=action,
            command_id=existing.get("command_id", ""),
            account=account,
        )
        duplicate = dict(existing)
        duplicate["duplicate"] = True
        duplicate["accepted"] = False
        duplicate["msg"] = f"{action} already in progress"
        self._record_timeline("command_rejected", account, "warning", reason, action=action, command_id=existing.get("command_id", ""))
        return False, duplicate

    def begin(
        self,
        key: str,
        action: str,
        account: str = "",
        ttl: float = 15.0,
        idempotency_key: str = "",
        request_fingerprint: str = "",
    ) -> Tuple[bool, Dict[str, Any]]:
        scope = self._scope(idempotency_key, request_fingerprint)
        now = time.time()
        with self._lock:
            self._cleanup_locked()
            if scope:
                cached = self._idempotency.get(scope)
                if cached and cached.get("status") == "finished":
                    command = dict(cached.get("command") or {})
                    command.update({
                        "accepted": False,
                        "duplicate": True,
                        "idempotent_replay": True,
                        "response": dict(cached.get("response") or {}),
                        "msg": "Idempotent response replayed",
                    })
                    return False, command
                if cached and cached.get("command"):
                    return self._duplicate(key, action, account, dict(cached["command"]), reason="idempotent_command_inflight")
            if self._is_shutting_down() and action != "stop":
                rejected = {
                    "command_id": "",
                    "key": key,
                    "action": action,
                    "account": account,
                    "accepted": False,
                    "duplicate": False,
                    "rejected": True,
                    "msg": "Shutdown in progress",
                }
                self._emit("rejected", "warning", key=key, action=action, account=account, reason="shutdown_in_progress")
                return False, rejected
            existing = self._commands.get(key)
            if existing:
                return self._duplicate(key, action, account, existing)
            conflict = self._conflict_locked(key)
            if conflict:
                rejected = dict(conflict)
                rejected["accepted"] = False
                rejected["duplicate"] = False
                rejected["rejected"] = True
                rejected["msg"] = f"{action} blocked by inflight {conflict.get('action', 'command')}"
                self._emit(
                    "overlap_rejected",
                    "warning",
                    key=key,
                    action=action,
                    account=account,
                    blocked_by_key=conflict.get("key", ""),
                    blocked_by_action=conflict.get("action", ""),
                    blocked_by_command_id=conflict.get("command_id", ""),
                    command_generation=self._generation,
                    reason="command_inflight",
                )
                self._record_timeline("command_rejected", account, "warning", "command_inflight", action=action, blocked_by_action=conflict.get("action", ""))
                return False, rejected
            allowed, reject_reason, acc = self._capability(action, account)
            if not allowed:
                rejected = {
                    "command_id": "",
                    "key": key,
                    "action": action,
                    "account": account,
                    "accepted": False,
                    "duplicate": False,
                    "rejected": True,
                    "msg": reject_reason,
                }
                self._emit("rejected", "warning", key=key, action=action, account=account, reason=reject_reason)
                self._record_timeline("command_rejected", account, "warning", reject_reason, action=action)
                return False, rejected
            self._seq += 1
            self._generation += 1
            command = {
                "command_id": f"{int(now * 1000)}-{self._seq}",
                "key": key,
                "action": action,
                "account": account,
                "command_generation": self._generation,
                "started_at": now,
                "expires_at": now + max(1.0, float(ttl or 15.0)),
                "idempotency_key": str(idempotency_key or ""),
                "idempotency_scope": scope,
                "request_fingerprint": str(request_fingerprint or ""),
            }
            self._commands[key] = command
            if scope:
                self._idempotency[scope] = {
                    "status": "running",
                    "command": dict(command),
                    "expires_at": now + self._idempotency_ttl,
                }
            if acc:
                with acc._lock:
                    account_generation = self._runtime_state.begin_account_command(acc, command)
                    command["account_command_generation"] = account_generation
            self._bump_status_revision()
            self._emit(
                "accepted",
                key=key,
                action=action,
                command_id=command["command_id"],
                account=account,
                command_generation=command["command_generation"],
                account_command_generation=command.get("account_command_generation", ""),
            )
            self._record_timeline("command_accepted", account, "info", action, action=action, command_id=command["command_id"], command_generation=command["command_generation"])
            return True, dict(command)

    def finish(self, key: str, command_id: str, ok: bool = True, error: str = "", response: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            current = self._commands.get(key)
            if current and current.get("command_id") == command_id:
                self._commands.pop(key, None)
                acc = self._find_account(str(current.get("account", "") or ""))
                if acc:
                    with acc._lock:
                        self._runtime_state.finish_account_command(acc, command_id, ok=ok, error=error)
                scope = str(current.get("idempotency_scope") or "")
                if scope:
                    cached = self._idempotency.get(scope, {})
                    cached.update({
                        "status": "finished",
                        "command": dict(current),
                        "response": dict(response or {}),
                        "ok": bool(ok),
                        "error": str(error or ""),
                        "expires_at": time.time() + self._idempotency_ttl,
                    })
                    self._idempotency[scope] = cached
                self._bump_status_revision()
                self._emit(
                    "finished",
                    key=key,
                    command_id=command_id,
                    ok=ok,
                    error=error,
                    action=current.get("action", ""),
                    account=current.get("account", ""),
                    command_generation=current.get("command_generation", ""),
                    account_command_generation=current.get("account_command_generation", ""),
                )
                self._record_timeline("command_finished", str(current.get("account", "") or ""), "info" if ok else "warning", error or "command_finished", action=current.get("action", ""), command_id=command_id, ok=ok)
            else:
                self._emit(
                    "stale_work_rejected",
                    "warning",
                    key=key,
                    command_id=command_id,
                    current_command_id=current.get("command_id", "") if current else "",
                    ok=ok,
                    error=error,
                    reason="command_finish_mismatch",
                    command_generation=self._generation,
                    thread_name=threading.current_thread().name,
                )
                self._record_timeline("stale_work_rejected", "", "warning", "command_finish_mismatch", command_id=command_id, ok=ok, error=error)

    def cancel_for_shutdown(self) -> None:
        with self._lock:
            commands = []
            preserved: Dict[str, Dict[str, Any]] = {}
            for key, item in self._commands.items():
                if str(item.get("action", "")) == "stop":
                    preserved[key] = item
                else:
                    commands.append((key, item))
            self._commands = preserved
        for key, item in commands:
            acc = self._find_account(str(item.get("account", "") or ""))
            if acc:
                with acc._lock:
                    self._runtime_state.finish_account_command(
                        acc,
                        str(item.get("command_id", "") or ""),
                        ok=False,
                        error="shutdown",
                    )
            self._emit(
                "shutdown_cancelled",
                "warning",
                key=key,
                action=item.get("action", ""),
                command_id=item.get("command_id", ""),
                account=item.get("account", ""),
                reason="shutdown",
            )

    def command_inflight(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._cleanup_locked()
            item = self._commands.get(key)
            if not item:
                return None
            return {
                "command_id": item.get("command_id", ""),
                "action": item.get("action", ""),
                "account": item.get("account", ""),
                "command_generation": item.get("command_generation", 0),
                "account_command_generation": item.get("account_command_generation", 0),
                "age": round(max(0.0, time.time() - float(item.get("started_at") or time.time())), 2),
            }

    def any_inflight(self) -> bool:
        with self._lock:
            self._cleanup_locked()
            return bool(self._commands)
