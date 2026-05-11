from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional


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
                    pass
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

    def record_account_snapshot(self, account_id: str, snapshot: Dict[str, Any]) -> None:
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

    def record_session(self, snapshot: Dict[str, Any], status: str = "active") -> None:
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
                str(snapshot.get("session_id", "") or ""),
                str(snapshot.get("account_id", "") or ""),
                str(snapshot.get("account_runtime_id", "") or ""),
                str(snapshot.get("launch_nonce", "") or ""),
                int(snapshot.get("runtime_generation", 0) or 0),
                int(snapshot.get("recovery_generation", 0) or 0),
                int(snapshot.get("command_generation", 0) or 0),
                str(status or snapshot.get("status", "active") or "active"),
                str(snapshot.get("reason", "") or ""),
                self._json(snapshot.get("launch_intent", {})),
                float(snapshot.get("created_at", 0.0) or now),
                now,
            ),
        )

    def record_transaction(self, snapshot: Dict[str, Any]) -> None:
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
                str(snapshot.get("transaction_id", "") or ""),
                str(snapshot.get("account_id", "") or ""),
                str(snapshot.get("session_id", "") or ""),
                str(snapshot.get("account_runtime_id", "") or ""),
                str(snapshot.get("launch_nonce", "") or ""),
                int(snapshot.get("runtime_generation", 0) or 0),
                int(snapshot.get("recovery_generation", 0) or 0),
                int(snapshot.get("command_generation", 0) or 0),
                str(snapshot.get("status", "") or "pending"),
                str(snapshot.get("step", "") or "begin"),
                str(snapshot.get("reason", "") or ""),
                str(snapshot.get("failure_reason", "") or ""),
                self._json(snapshot.get("launch_intent", {})),
                self._json(snapshot.get("destination_evidence", {})),
                float(snapshot.get("created_at", 0.0) or time.time()),
                float(snapshot.get("updated_at", 0.0) or time.time()),
                float(snapshot.get("completed_at", 0.0) or 0.0),
            ),
        )

    def record_process_binding(self, account_id: str, pid: Optional[int], identity: str, session_id: str, transaction_id: str, status: str, reason: str = "") -> None:
        self._execute(
            """
            INSERT INTO process_bindings(account_id, pid, process_identity, session_id, transaction_id, status, reason, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (str(account_id or ""), pid, str(identity or ""), str(session_id or ""), str(transaction_id or ""), str(status or ""), str(reason or ""), time.time()),
        )

    def record_event(self, event: Dict[str, Any]) -> None:
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
                        float(event.get("ts", 0.0) or time.time()),
                        str(event.get("severity", "") or "info"),
                        str(event.get("account", event.get("account_id", "")) or ""),
                        str(event.get("event_type", event.get("kind", "")) or ""),
                        str(event.get("reason", "") or ""),
                        str(event.get("session_id", "") or ""),
                        str(event.get("rejoin_transaction_id", event.get("transaction_id", "")) or ""),
                        int(event.get("runtime_generation", 0) or 0),
                        int(event.get("recovery_generation", 0) or 0),
                        int(event.get("command_generation", 0) or 0),
                        event.get("pid", None),
                        str(event.get("runtime_state", "") or ""),
                        str(event.get("public_state", "") or ""),
                        self._json(event),
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
                    pass
                raise

    def get_account_snapshot(self, account_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT snapshot_json, updated_at FROM account_runtime_state WHERE account_id=?",
                (str(account_id or ""),),
            ).fetchone()
        if not row:
            return None
        snapshot = self._decode_json(row["snapshot_json"])
        if isinstance(snapshot, dict):
            snapshot["updated_at"] = float(row["updated_at"] or 0.0)
            return snapshot
        return None

    def list_recent_events(self, account_id: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 100), 500))
        params: tuple
        if account_id:
            sql = (
                "SELECT * FROM runtime_events WHERE account_id=? "
                "ORDER BY ts DESC, id DESC LIMIT ?"
            )
            params = (str(account_id or ""), safe_limit)
        else:
            sql = "SELECT * FROM runtime_events ORDER BY ts DESC, id DESC LIMIT ?"
            params = (safe_limit,)
        with self._lock:
            rows = list(self._conn.execute(sql, params).fetchall())
        events: List[Dict[str, Any]] = []
        for row in rows:
            payload = self._decode_json(row["payload_json"])
            item = {
                "id": int(row["id"]),
                "ts": float(row["ts"] or 0.0),
                "severity": str(row["severity"] or "info"),
                "account": str(row["account_id"] or ""),
                "event_type": str(row["event_type"] or ""),
                "reason": str(row["reason"] or ""),
                "session_id": str(row["session_id"] or ""),
                "rejoin_transaction_id": str(row["transaction_id"] or ""),
                "runtime_generation": int(row["runtime_generation"] or 0),
                "recovery_generation": int(row["recovery_generation"] or 0),
                "command_generation": int(row["command_generation"] or 0),
                "pid": row["pid"],
                "runtime_state": str(row["runtime_state"] or ""),
                "public_state": str(row["public_state"] or ""),
                "payload": payload if isinstance(payload, dict) else {},
            }
            events.append(item)
        return events

    def list_recent_transactions(self, account_id: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 50), 200))
        params: tuple
        if account_id:
            sql = (
                "SELECT * FROM rejoin_transactions WHERE account_id=? "
                "ORDER BY updated_at DESC, created_at DESC LIMIT ?"
            )
            params = (str(account_id or ""), safe_limit)
        else:
            sql = "SELECT * FROM rejoin_transactions ORDER BY updated_at DESC, created_at DESC LIMIT ?"
            params = (safe_limit,)
        with self._lock:
            rows = list(self._conn.execute(sql, params).fetchall())
        transactions: List[Dict[str, Any]] = []
        for row in rows:
            transactions.append({
                "transaction_id": str(row["transaction_id"] or ""),
                "account_id": str(row["account_id"] or ""),
                "session_id": str(row["session_id"] or ""),
                "account_runtime_id": str(row["account_runtime_id"] or ""),
                "launch_nonce": str(row["launch_nonce"] or ""),
                "runtime_generation": int(row["runtime_generation"] or 0),
                "recovery_generation": int(row["recovery_generation"] or 0),
                "command_generation": int(row["command_generation"] or 0),
                "status": str(row["status"] or ""),
                "step": str(row["step"] or ""),
                "reason": str(row["reason"] or ""),
                "failure_reason": str(row["failure_reason"] or ""),
                "launch_intent": self._decode_json(row["launch_intent_json"]),
                "destination_evidence": self._decode_json(row["destination_evidence_json"]),
                "created_at": float(row["created_at"] or 0.0),
                "updated_at": float(row["updated_at"] or 0.0),
                "completed_at": float(row["completed_at"] or 0.0),
            })
        return transactions

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
                self.record_event({
                    "ts": now,
                    "severity": "warning",
                    "account": str(row["account_id"] or ""),
                    "event_type": "transaction_rollback",
                    "reason": reason or "backend_restart",
                    "session_id": str(row["session_id"] or ""),
                    "transaction_id": str(row["transaction_id"] or ""),
                    "runtime_generation": int(row["runtime_generation"] or 0),
                    "recovery_generation": int(row["recovery_generation"] or 0),
                    "command_generation": int(row["command_generation"] or 0),
                    "payload": {"step": "rolled_back_on_restart", "previous_status": str(row["status"] or "")},
                })
            except Exception:
                pass
        return len(rows)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
