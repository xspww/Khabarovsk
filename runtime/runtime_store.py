from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any

from domain._util import as_float as _snap_float
from domain._util import as_int as _snap_int
from domain._util import mapping_text as _snap_str


def _event_alias(event: dict[str, Any], primary: str, secondary: str) -> str:
    value = event.get(primary, event.get(secondary, ""))
    return str(value or "")


def _event_record_params(event: dict[str, Any], payload_json: str) -> tuple[Any, ...]:
    return (
        _snap_float(event, "ts", time.time()),
        _snap_str(event, "severity", "info"),
        _event_alias(event, "account", "account_id"),
        _event_alias(event, "event_type", "kind"),
        _snap_str(event, "reason"),
        _snap_str(event, "session_id"),
        _event_alias(event, "rejoin_transaction_id", "transaction_id"),
        _snap_int(event, "runtime_generation"),
        _snap_int(event, "recovery_generation"),
        _snap_int(event, "command_generation"),
        event.get("pid"),
        _snap_str(event, "runtime_state"),
        _snap_str(event, "public_state"),
        payload_json,
    )


def _event_filters(account_id: str, event_type: str, severity: str) -> tuple[list[str], list[Any]]:
    filters: list[str] = []
    params: list[Any] = []
    if account_id:
        filters.append("account_id=?")
        params.append(str(account_id or ""))
    if event_type:
        filters.append("LOWER(event_type)=LOWER(?)")
        params.append(str(event_type or ""))
    if severity:
        filters.append("LOWER(severity)=LOWER(?)")
        params.append(str(severity or ""))
    return filters, params


def _event_row(row: Any, payload: Any) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": int(data["id"]),
        "ts": _snap_float(data, "ts", 0.0),
        "severity": _snap_str(data, "severity", "info"),
        "account": _snap_str(data, "account_id"),
        "event_type": _snap_str(data, "event_type"),
        "reason": _snap_str(data, "reason"),
        "session_id": _snap_str(data, "session_id"),
        "rejoin_transaction_id": _snap_str(data, "transaction_id"),
        "runtime_generation": _snap_int(data, "runtime_generation"),
        "recovery_generation": _snap_int(data, "recovery_generation"),
        "command_generation": _snap_int(data, "command_generation"),
        "pid": data["pid"],
        "runtime_state": _snap_str(data, "runtime_state"),
        "public_state": _snap_str(data, "public_state"),
        "payload": payload if isinstance(payload, dict) else {},
    }


def _rollback_event(row: Any, reason: str, now: float) -> dict[str, Any]:
    data = dict(row)
    return {
        "ts": now,
        "severity": "warning",
        "account": _snap_str(data, "account_id"),
        "event_type": "transaction_rollback",
        "reason": reason or "backend_restart",
        "session_id": _snap_str(data, "session_id"),
        "transaction_id": _snap_str(data, "transaction_id"),
        "runtime_generation": _snap_int(data, "runtime_generation"),
        "recovery_generation": _snap_int(data, "recovery_generation"),
        "command_generation": _snap_int(data, "command_generation"),
        "payload": {"step": "rolled_back_on_restart", "previous_status": _snap_str(data, "status")},
    }


class RuntimeStore:
    """SQLite WAL store for runtime truth that must survive backend restarts."""

    def __init__(self, db_path: str, event_retention_limit: int = 10000):
        self.db_path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.RLock()
        self._event_retention_limit = max(1000, int(event_retention_limit or 10000))
        self._event_write_count = 0
        self._conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            try:
                self._conn.execute(sql, params)
                self._conn.commit()
            except sqlite3.DatabaseError:
                try:
                    self._conn.rollback()
                except sqlite3.DatabaseError:
                    "intentional: ignore exception"
                raise

    def _json(self, value: Any) -> str:
        return json.dumps(value or {}, ensure_ascii=False, default=str, separators=(",", ":"))

    def _decode_json(self, value: Any) -> Any:
        if not value:
            return {}
        try:
            return json.loads(value)
        except Exception:
            return {}

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS account_runtime_state (
                    account_id TEXT PRIMARY KEY,
                    snapshot_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_sessions (
                    session_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    account_runtime_id TEXT NOT NULL,
                    launch_nonce TEXT NOT NULL,
                    runtime_generation INTEGER NOT NULL,
                    recovery_generation INTEGER NOT NULL DEFAULT 0,
                    command_generation INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    launch_intent_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rejoin_transactions (
                    transaction_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    account_runtime_id TEXT NOT NULL,
                    launch_nonce TEXT NOT NULL,
                    runtime_generation INTEGER NOT NULL,
                    recovery_generation INTEGER NOT NULL,
                    command_generation INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    step TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    failure_reason TEXT NOT NULL DEFAULT '',
                    launch_intent_json TEXT NOT NULL DEFAULT '{}',
                    destination_evidence_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS process_bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    pid INTEGER,
                    process_identity TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    transaction_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    severity TEXT NOT NULL,
                    account_id TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    transaction_id TEXT NOT NULL DEFAULT '',
                    runtime_generation INTEGER NOT NULL DEFAULT 0,
                    recovery_generation INTEGER NOT NULL DEFAULT 0,
                    command_generation INTEGER NOT NULL DEFAULT 0,
                    pid INTEGER,
                    runtime_state TEXT NOT NULL DEFAULT '',
                    public_state TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_events_account_ts ON runtime_events(account_id, ts);
                CREATE INDEX IF NOT EXISTS idx_runtime_events_type_ts ON runtime_events(event_type, ts);
                """
            )
            self._conn.commit()

    def record_account_snapshot(self, account_id: str, snapshot: dict[str, Any]) -> None:
        now = time.time()
        self._execute(
            """
            INSERT INTO account_runtime_state(account_id, snapshot_json, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                snapshot_json=excluded.snapshot_json,
                updated_at=excluded.updated_at
            """,
            (str(account_id or ""), self._json(snapshot), now),
        )

    def record_session(self, snapshot: dict[str, Any], status: str = "active") -> None:
        now = time.time()
        self._execute(
            """
            INSERT INTO runtime_sessions(
                session_id, account_id, account_runtime_id, launch_nonce,
                runtime_generation, recovery_generation, command_generation,
                status, reason, launch_intent_json, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                status=excluded.status,
                reason=excluded.reason,
                launch_intent_json=excluded.launch_intent_json,
                updated_at=excluded.updated_at
            """,
            (
                _snap_str(snapshot, "session_id"),
                _snap_str(snapshot, "account_id"),
                _snap_str(snapshot, "account_runtime_id"),
                _snap_str(snapshot, "launch_nonce"),
                _snap_int(snapshot, "runtime_generation"),
                _snap_int(snapshot, "recovery_generation"),
                _snap_int(snapshot, "command_generation"),
                str(status or snapshot.get("status", "active") or "active"),
                _snap_str(snapshot, "reason"),
                self._json(snapshot.get("launch_intent", {})),
                _snap_float(snapshot, "created_at", now),
                now,
            ),
        )

    def record_transaction(self, snapshot: dict[str, Any]) -> None:
        self._execute(
            """
            INSERT INTO rejoin_transactions(
                transaction_id, account_id, session_id, account_runtime_id, launch_nonce,
                runtime_generation, recovery_generation, command_generation,
                status, step, reason, failure_reason, launch_intent_json,
                destination_evidence_json, created_at, updated_at, completed_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(transaction_id) DO UPDATE SET
                status=excluded.status,
                step=excluded.step,
                reason=excluded.reason,
                failure_reason=excluded.failure_reason,
                destination_evidence_json=excluded.destination_evidence_json,
                updated_at=excluded.updated_at,
                completed_at=excluded.completed_at
            """,
            (
                _snap_str(snapshot, "transaction_id"),
                _snap_str(snapshot, "account_id"),
                _snap_str(snapshot, "session_id"),
                _snap_str(snapshot, "account_runtime_id"),
                _snap_str(snapshot, "launch_nonce"),
                _snap_int(snapshot, "runtime_generation"),
                _snap_int(snapshot, "recovery_generation"),
                _snap_int(snapshot, "command_generation"),
                _snap_str(snapshot, "status", "pending"),
                _snap_str(snapshot, "step", "begin"),
                _snap_str(snapshot, "reason"),
                _snap_str(snapshot, "failure_reason"),
                self._json(snapshot.get("launch_intent", {})),
                self._json(snapshot.get("destination_evidence", {})),
                _snap_float(snapshot, "created_at", time.time()),
                _snap_float(snapshot, "updated_at", time.time()),
                _snap_float(snapshot, "completed_at", 0.0),
            ),
        )

    def record_process_binding(self, account_id: str, pid: int | None, identity: str, session_id: str, transaction_id: str, status: str, reason: str = "") -> None:
        self._execute(
            """
            INSERT INTO process_bindings(account_id, pid, process_identity, session_id, transaction_id, status, reason, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (str(account_id or ""), pid, str(identity or ""), str(session_id or ""), str(transaction_id or ""), str(status or ""), str(reason or ""), time.time()),
        )

    def record_event(self, event: dict[str, Any]) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO runtime_events(
                        ts, severity, account_id, event_type, reason, session_id, transaction_id,
                        runtime_generation, recovery_generation, command_generation, pid,
                        runtime_state, public_state, payload_json
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        * _event_record_params(event, self._json(event)),
                    ),
                )
                self._event_write_count += 1
                if self._event_write_count % 50 == 0:
                    self._conn.execute(
                        """
                        DELETE FROM runtime_events
                        WHERE id NOT IN (
                            SELECT id FROM runtime_events ORDER BY ts DESC, id DESC LIMIT ?
                        )
                        """,
                        (self._event_retention_limit,),
                    )
                self._conn.commit()
            except sqlite3.DatabaseError:
                try:
                    self._conn.rollback()
                except sqlite3.DatabaseError:
                    "intentional: ignore exception"
                raise
    def list_recent_events(
        self,
        account_id: str = "",
        limit: int = 100,
        event_type: str = "",
        severity: str = "",
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 100), 500))
        filters, params = _event_filters(account_id, event_type, severity)
        where = f" WHERE {' AND '.join(filters)}" if filters else ""
        sql = f"SELECT * FROM runtime_events{where} ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(safe_limit)
        with self._lock:
            rows = list(self._conn.execute(sql, tuple(params)).fetchall())
        return [
            _event_row(row, self._decode_json(row["payload_json"]))
            for row in rows
        ]
    def rollback_open_transactions(self, reason: str = "backend_restart") -> int:
        now = time.time()
        open_statuses = ("pending", "launching", "process_bound", "verifying", "binding_verified")
        with self._lock:
            rows = list(self._conn.execute(
                "SELECT * FROM rejoin_transactions WHERE status IN (?, ?, ?, ?, ?)",
                open_statuses,
            ).fetchall())
            if not rows:
                return 0
            self._conn.execute(
                """
                UPDATE rejoin_transactions
                SET status='rolled_back',
                    step='rolled_back_on_restart',
                    failure_reason=?,
                    updated_at=?,
                    completed_at=?
                WHERE status IN (?, ?, ?, ?, ?)
                """,
                (str(reason or "backend_restart"), now, now, *open_statuses),
            )
            self._conn.commit()
        for row in rows:
            try:
                self.record_event(_rollback_event(row, reason, now))
            except Exception:
                "intentional: ignore exception"
        return len(rows)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
