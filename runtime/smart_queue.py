from __future__ import annotations

import threading
import time
from typing import Any

from infra.core_logging import flog_kv


def _fallback_text(value: Any, default: str) -> str:
    return str(value or default)


def _priority(acc: Any) -> int:
    return int(getattr(acc, "priority", 50) or 50)


def _due_in(now: float, due_at: float) -> str:
    return f"{max(0.0, due_at - now):.1f}"


def _queue_generations(
    acc: Any,
    recovery_generation: int | None,
    runtime_generation: int | None,
) -> tuple[int, int]:
    recovery = (
        recovery_generation
        if recovery_generation is not None
        else getattr(acc, "recovery_generation", 0) or 0
    )
    runtime = (
        runtime_generation
        if runtime_generation is not None
        else getattr(acc, "runtime_generation", 0) or 0
    )
    return int(recovery), int(runtime)


def _queue_due_at(acc: Any, delay_seconds: float, now: float) -> float:
    delay_until = now + max(0.0, float(delay_seconds or 0.0))
    return max(now, delay_until, float(getattr(acc, "cooldown_until", 0.0) or 0.0))


def _entry_generation(entry: dict[str, Any]) -> tuple[int, int]:
    recovery = int(entry.get("recovery_generation", entry.get("generation", 0)) or 0)
    runtime = int(entry.get("runtime_generation", 0) or 0)
    return recovery, runtime


def _account_generation(acc: Any) -> tuple[int, int]:
    recovery = int(getattr(acc, "recovery_generation", 0) or 0)
    runtime = int(getattr(acc, "runtime_generation", 0) or 0)
    return recovery, runtime


def _entry_stale(entry: dict[str, Any]) -> bool:
    return _entry_generation(entry) != _account_generation(entry["acc"])


class SmartQueue:
    """
    Deduplicates recovery work by account and rejects stale runtime generations.
    Lower account priority values run first; boosted recovery reasons jump ahead.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._entries: dict[str, dict[str, Any]] = {}
        self._busy = threading.Event()
        self._closed = False
        self._stale_rejections = 0
        self._sequence = 0
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

    def _merge_entry(
        self,
        existing: dict[str, Any],
        acc: Any,
        reason: str,
        generation: int,
        runtime_generation: int,
        due_at: float,
        now: float,
    ) -> None:
        existing["reason"] = reason or existing.get("reason", "")
        existing["generation"] = generation
        existing["recovery_generation"] = generation
        existing["runtime_generation"] = runtime_generation
        existing["due_at"] = min(float(existing.get("due_at") or due_at), due_at)
        if self._is_boosted(reason):
            existing["boosted"] = True
            existing["score"] = self._calculate_score(existing["acc"], True)

    def push(
        self,
        acc: Any,
        reason: str = "",
        runtime_generation: int | None = None,
        recovery_generation: int | None = None,
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
                    reason=_fallback_text(reason, "queue_closed"),
                    runtime_generation=getattr(acc, "runtime_generation", 0),
                    recovery_generation=getattr(acc, "recovery_generation", 0),
                )
                return
            now = time.time()
            due_at = _queue_due_at(acc, delay_seconds, now)
            generation, runtime_generation = _queue_generations(
                acc,
                recovery_generation,
                runtime_generation,
            )
            existing = self._entries.get(key)
            if existing:
                self._merge_entry(
                    existing,
                    acc,
                    reason,
                    generation,
                    runtime_generation,
                    due_at,
                    now,
                )
                flog_kv(
                    "QUEUE",
                    "dedupe",
                    account=acc.display_name,
                    reason=reason,
                    runtime_generation=runtime_generation,
                    recovery_generation=generation,
                    due_in=_due_in(now, due_at),
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
                priority=_priority(acc),
                runtime_generation=runtime_generation,
                recovery_generation=generation,
                due_in=_due_in(now, due_at),
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

    def _entry_score(self, entry: dict[str, Any]) -> float:
        try:
            return float(entry.get("score"))
        except (TypeError, ValueError):
            return self._calculate_score(entry["acc"], bool(entry.get("boosted")))

    def _entry_due_at(self, entry: dict[str, Any]) -> float:
        acc = entry["acc"]
        return max(
            float(entry.get("due_at") or 0.0),
            float(getattr(acc, "cooldown_until", 0.0) or 0.0),
        )

    def _snapshot_sort_key(self, entry: dict[str, Any], now: float):
        due_at = self._entry_due_at(entry)
        queued_at = float(entry.get("queued_at") or now)
        sequence = int(entry.get("sequence") or 0)
        ready_rank = 0 if due_at <= now else 1
        score_at_due = self._entry_score(entry)
        return (ready_rank, due_at if ready_rank else 0.0, score_at_due, sequence, queued_at)

    def _drop_stale_locked(self) -> None:
        stale_keys = [
            key
            for key, entry in self._entries.items()
            if _entry_stale(entry)
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

    def _wait_for_change(self, deadline: float, now: float) -> bool:
        remaining = deadline - now
        if remaining <= 0:
            return False
        self._cond.wait(timeout=min(0.5, remaining))
        return True

    def _wait_for_due(self, deadline: float, now: float) -> bool:
        next_due = min(self._entry_due_at(entry) for entry in self._entries.values())
        remaining = deadline - now
        if remaining <= 0:
            return False
        self._cond.wait(timeout=min(max(0.05, next_due - now), remaining, 0.5))
        return True

    def _pop_best_locked(self, ready: list[tuple[str, dict[str, Any]]], now: float) -> Any:
        best_key, best_entry = min(
            ready,
            key=lambda item: (
                self._entry_score(item[1]),
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
            recovery_generation=best_entry.get(
                "recovery_generation",
                best_entry.get("generation", 0),
            ),
            size=len(self._entries),
            score=f"{self._entry_score(best_entry):.1f}",
        )
        return acc

    def pop(self, timeout: float = 1.0) -> Any | None:
        deadline = time.time() + timeout
        with self._cond:
            while True:
                now = time.time()
                self._drop_stale_locked()
                if not self._entries:
                    if self._closed:
                        return None
                    if not self._wait_for_change(deadline, now):
                        return None
                    continue

                ready = [
                    (key, entry)
                    for key, entry in self._entries.items()
                    if self._entry_due_at(entry) <= now
                ]
                if not ready:
                    if not self._wait_for_due(deadline, now):
                        return None
                    continue

                return self._pop_best_locked(ready, now)

    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def has_fresh_entry(self, acc: Any) -> bool:
        key = acc._config_username
        with self._lock:
            entry = self._entries.get(key)
            if not entry:
                return False
            return _entry_generation(entry) == _account_generation(acc)
    def cancel_all(self, reason: str = "cancel_all") -> int:
        with self._cond:
            count = len(self._entries)
            self._entries.clear()
            self._closed = True
            self._busy.clear()
            self._cond.notify_all()
        flog_kv("QUEUE", "cancel_all", reason=reason, count=count)
        return count

    def snapshot(self) -> dict[str, Any]:
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
                score = self._entry_score(entry)
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
