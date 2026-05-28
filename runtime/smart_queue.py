from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

from core_logging import flog_kv


class SmartQueue:
    """
    Deduplicates recovery work by account and rejects stale runtime generations.
    Lower account priority values run first; boosted recovery reasons jump ahead.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._busy = threading.Event()
        self._closed = False
        self._stale_rejections = 0
        self._sequence = 0

    def is_busy(self) -> bool:
        return self._busy.is_set()

    def mark_busy(self):
        self._busy.set()

    def mark_free(self):
        self._busy.clear()

    def wait_until_free(self, stop: threading.Event, timeout: float = 120.0):
        deadline = time.time() + timeout
        while self._busy.is_set() and not stop.is_set():
            if time.time() > deadline:
                break
            time.sleep(0.5)

    @staticmethod
    def _is_boosted(reason: str) -> bool:
        return any(flag in str(reason or "") for flag in ("force_rejoin", "network_restored", "session_restored"))

    def push(
        self,
        acc: Any,
        reason: str = "",
        runtime_generation: Optional[int] = None,
        recovery_generation: Optional[int] = None,
        delay_seconds: float = 0.0,
    ):
        key = acc._config_username
        with self._cond:
            if self._closed:
                flog_kv(
                    "QUEUE",
                    "enqueue_rejected",
                    "warning",
                    event_type="stale_work_rejected",
                    account=acc.display_name,
                    reason=reason or "queue_closed",
                    runtime_generation=getattr(acc, "runtime_generation", 0),
                    recovery_generation=getattr(acc, "recovery_generation", 0),
                )
                return
            now = time.time()
            delay_until = now + max(0.0, float(delay_seconds or 0.0))
            due_at = max(now, delay_until, float(getattr(acc, "cooldown_until", 0.0) or 0.0))
            generation = int(
                recovery_generation
                if recovery_generation is not None
                else getattr(acc, "recovery_generation", 0) or 0
            )
            runtime_generation = int(
                runtime_generation
                if runtime_generation is not None
                else getattr(acc, "runtime_generation", 0) or 0
            )
            existing = self._entries.get(key)
            if existing:
                existing["reason"] = reason or existing.get("reason", "")
                existing["generation"] = generation
                existing["recovery_generation"] = generation
                existing["runtime_generation"] = runtime_generation
                existing["due_at"] = min(float(existing.get("due_at") or due_at), due_at)
                if self._is_boosted(reason):
                    existing["boosted"] = True
                    existing["score"] = self._calculate_score(existing["acc"], True)
                flog_kv(
                    "QUEUE",
                    "dedupe",
                    account=acc.display_name,
                    reason=reason,
                    runtime_generation=runtime_generation,
                    recovery_generation=generation,
                    due_in=f"{max(0.0, due_at - now):.1f}",
                    size=len(self._entries),
                )
                return

            boosted = self._is_boosted(reason)
            self._sequence += 1
            self._entries[key] = {
                "acc": acc,
                "reason": reason,
                "queued_at": now,
                "due_at": due_at,
                "generation": generation,
                "recovery_generation": generation,
                "runtime_generation": runtime_generation,
                "boosted": boosted,
                "score": self._calculate_score(acc, boosted),
                "sequence": self._sequence,
            }
            flog_kv(
                "QUEUE",
                "push",
                account=acc.display_name,
                reason=reason,
                priority=int(getattr(acc, "priority", 50) or 50),
                runtime_generation=runtime_generation,
                recovery_generation=generation,
                due_in=f"{max(0.0, due_at - now):.1f}",
                size=len(self._entries),
            )
            self._cond.notify_all()

    def _calculate_score(self, acc: Any, boosted: bool = False) -> float:
        base = float(int(getattr(acc, "priority", 50) or 50))
        retry_penalty = min(
            80.0,
            float(
                int(getattr(acc, "retry_count", 0) or 0) +
                int(getattr(acc, "launch_fail_count", 0) or 0) +
                int(getattr(acc, "crash_retry_count", 0) or 0)
            ) * 5.0,
        )
        boost = -1000.0 if boosted else 0.0
        return base + retry_penalty + boost

    def _entry_score(self, entry: Dict[str, Any], now: float) -> float:
        try:
            return float(entry.get("score"))
        except (TypeError, ValueError):
            return self._calculate_score(entry["acc"], bool(entry.get("boosted")))

    def _entry_due_at(self, entry: Dict[str, Any]) -> float:
        acc = entry["acc"]
        return max(
            float(entry.get("due_at") or 0.0),
            float(getattr(acc, "cooldown_until", 0.0) or 0.0),
        )

    def _snapshot_sort_key(self, entry: Dict[str, Any], now: float):
        due_at = self._entry_due_at(entry)
        queued_at = float(entry.get("queued_at") or now)
        sequence = int(entry.get("sequence") or 0)
        ready_rank = 0 if due_at <= now else 1
        score_at_due = self._entry_score(entry, max(now, due_at))
        return (ready_rank, due_at if ready_rank else 0.0, score_at_due, sequence, queued_at)

    def pop(self, timeout: float = 1.0) -> Optional[Any]:
        deadline = time.time() + timeout
        with self._cond:
            while True:
                now = time.time()
                stale_keys = [
                    key for key, entry in self._entries.items()
                    if (
                        int(entry.get("recovery_generation", entry.get("generation", 0)) or 0) != int(getattr(entry["acc"], "recovery_generation", 0) or 0)
                        or int(entry.get("runtime_generation", 0) or 0) != int(getattr(entry["acc"], "runtime_generation", 0) or 0)
                    )
                ]
                for key in stale_keys:
                    entry = self._entries.pop(key, None)
                    if entry:
                        self._stale_rejections += 1
                        flog_kv(
                            "QUEUE",
                            "stale_drop",
                            "warning",
                            event_type="stale_work_rejected",
                            account=entry["acc"].display_name,
                            queued_runtime_generation=entry.get("runtime_generation", 0),
                            current_runtime_generation=getattr(entry["acc"], "runtime_generation", 0),
                            queued_recovery_generation=entry.get("recovery_generation", entry.get("generation", 0)),
                            current_recovery_generation=getattr(entry["acc"], "recovery_generation", 0),
                            size=len(self._entries),
                            reason=entry.get("reason", ""),
                        )

                if not self._entries:
                    if self._closed:
                        return None
                    remaining = deadline - now
                    if remaining <= 0:
                        return None
                    self._cond.wait(timeout=min(0.5, remaining))
                    continue

                ready = [
                    (key, entry) for key, entry in self._entries.items()
                    if self._entry_due_at(entry) <= now
                ]
                if not ready:
                    next_due = min(self._entry_due_at(entry) for entry in self._entries.values())
                    remaining = deadline - now
                    if remaining <= 0:
                        return None
                    self._cond.wait(timeout=min(max(0.05, next_due - now), remaining, 0.5))
                    continue

                best_key, best_entry = min(
                    ready,
                    key=lambda item: (
                        self._entry_score(item[1], now),
                        int(item[1].get("sequence") or 0),
                        float(item[1].get("queued_at") or now),
                    ),
                )
                self._entries.pop(best_key, None)
                acc = best_entry["acc"]
                flog_kv(
                    "QUEUE",
                    "pop",
                    account=acc.display_name,
                    reason=best_entry.get("reason", ""),
                    runtime_generation=best_entry.get("runtime_generation", 0),
                    recovery_generation=best_entry.get("recovery_generation", best_entry.get("generation", 0)),
                    size=len(self._entries),
                    score=f"{self._entry_score(best_entry, now):.1f}",
                )
                return acc

    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def has_fresh_entry(self, acc: Any) -> bool:
        key = acc._config_username
        with self._lock:
            entry = self._entries.get(key)
            if not entry:
                return False
            return (
                int(entry.get("recovery_generation", entry.get("generation", 0)) or 0) == int(getattr(acc, "recovery_generation", 0) or 0)
                and int(entry.get("runtime_generation", 0) or 0) == int(getattr(acc, "runtime_generation", 0) or 0)
            )

    def cancel_account(self, acc_or_key: Any, reason: str = "cancel_account") -> int:
        key = str(getattr(acc_or_key, "_config_username", acc_or_key) or "")
        with self._cond:
            removed = 1 if self._entries.pop(key, None) else 0
            if removed:
                self._cond.notify_all()
                flog_kv("QUEUE", "cancel_account", account=key, reason=reason, size=len(self._entries))
            return removed

    def cancel_all(self, reason: str = "cancel_all") -> int:
        with self._cond:
            count = len(self._entries)
            self._entries.clear()
            self._closed = True
            self._busy.clear()
            self._cond.notify_all()
        flog_kv("QUEUE", "cancel_all", reason=reason, count=count)
        return count

    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            entries = []
            ordered_entries = sorted(
                self._entries.items(),
                key=lambda item: self._snapshot_sort_key(item[1], now),
            )
            for position, (key, entry) in enumerate(ordered_entries, start=1):
                acc = entry.get("acc")
                due_at = self._entry_due_at(entry)
                score = self._entry_score(entry, now)
                entries.append({
                    "account": key,
                    "display": getattr(acc, "display_name", key),
                    "reason": entry.get("reason", ""),
                    "queue_position": position,
                    "queue_sequence": int(entry.get("sequence") or 0),
                    "priority": int(getattr(acc, "priority", 50) or 50),
                    "score": round(score, 2),
                    "ready": due_at <= now,
                    "queued_at": float(entry.get("queued_at") or 0.0),
                    "age_seconds": round(max(0.0, now - float(entry.get("queued_at") or now)), 2),
                    "due_at": round(due_at, 3),
                    "due_in_seconds": round(max(0.0, due_at - now), 2),
                    "runtime_generation": int(entry.get("runtime_generation", 0) or 0),
                    "recovery_generation": int(entry.get("recovery_generation", entry.get("generation", 0)) or 0),
                    "boosted": bool(entry.get("boosted", False)),
                })
            return {
                "size": len(entries),
                "pending": len(entries),
                "busy": self._busy.is_set(),
                "closed": self._closed,
                "stale_rejections": self._stale_rejections,
                "oldest_age_seconds": max((item["age_seconds"] for item in entries), default=0.0),
                "entries": entries,
            }
