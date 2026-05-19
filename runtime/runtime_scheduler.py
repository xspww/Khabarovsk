from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from core import flog_kv


ScheduleCallback = Callable[["RuntimeScheduledJob"], Any]


@dataclass
class RuntimeScheduledJob:
    job_key: str
    due_at: float
    reason: str = ""
    account_id: str = ""
    account: Any = None
    runtime_generation: Optional[int] = None
    recovery_generation: Optional[int] = None
    command_generation: Optional[int] = None
    interval_seconds: float = 0.0
    periodic: bool = False
    payload: Dict[str, Any] = field(default_factory=dict)
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)


class RuntimeScheduler:
    """Small runtime scheduler for recovery and maintenance jobs."""

    def __init__(
        self,
        stop: Optional[threading.Event] = None,
        state_manager: Optional[Any] = None,
        timeline: Optional[Any] = None,
        logger: Optional[Callable[..., None]] = None,
        name: str = "RuntimeScheduler",
        autostart: bool = True,
    ):
        self._external_stop = stop or threading.Event()
        self._state_manager = state_manager
        self._timeline = timeline
        self._log = logger or flog_kv
        self._name = name
        self._jobs: Dict[str, tuple[RuntimeScheduledJob, ScheduleCallback]] = {}
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._closed = False
        self._started_at = time.time()
        self._last_loop_at = 0.0
        self._last_dispatch_started_at = 0.0
        self._last_dispatch_finished_at = 0.0
        self._last_dispatch_latency_seconds = 0.0
        self._dispatch_count = 0
        self._callback_failure_count = 0
        self._stale_rejection_count = 0
        self._thread = threading.Thread(target=self._loop, daemon=True, name=name)
        if autostart:
            self._thread.start()

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self.cancel_all(reason="scheduler_stop")
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def get(self, job_key: str) -> Optional[RuntimeScheduledJob]:
        with self._lock:
            item = self._jobs.get(job_key)
            return item[0] if item else None

    def schedule_once(
        self,
        job_key: str,
        callback: ScheduleCallback,
        delay: float = 0.0,
        due_at: Optional[float] = None,
        reason: str = "",
        account: Any = None,
        account_id: str = "",
        runtime_generation: Optional[int] = None,
        recovery_generation: Optional[int] = None,
        command_generation: Optional[int] = None,
        payload: Optional[Dict[str, Any]] = None,
        now: Optional[float] = None,
    ) -> RuntimeScheduledJob:
        current = time.time() if now is None else float(now)
        due = float(due_at if due_at is not None else current + max(0.0, float(delay or 0.0)))
        job = RuntimeScheduledJob(
            job_key=job_key,
            due_at=due,
            reason=reason,
            account_id=account_id or self._account_key(account),
            account=account,
            runtime_generation=runtime_generation,
            recovery_generation=recovery_generation,
            command_generation=command_generation,
            payload=dict(payload or {}),
        )
        self._set_job(job, callback)
        return job

    def schedule_periodic(
        self,
        job_key: str,
        interval_seconds: float,
        callback: ScheduleCallback,
        reason: str = "",
        initial_delay: Optional[float] = None,
        account: Any = None,
        account_id: str = "",
        payload: Optional[Dict[str, Any]] = None,
        now: Optional[float] = None,
    ) -> RuntimeScheduledJob:
        interval = max(0.1, float(interval_seconds or 0.1))
        current = time.time() if now is None else float(now)
        delay = interval if initial_delay is None else max(0.0, float(initial_delay or 0.0))
        job = RuntimeScheduledJob(
            job_key=job_key,
            due_at=current + delay,
            reason=reason,
            account_id=account_id or self._account_key(account),
            account=account,
            interval_seconds=interval,
            periodic=True,
            payload=dict(payload or {}),
        )
        self._set_job(job, callback)
        return job

    def cancel(self, job_key: str, reason: str = "cancel") -> bool:
        with self._cond:
            item = self._jobs.pop(job_key, None)
            if item:
                self._cond.notify_all()
        if item:
            self._emit("runtime_schedule_cancelled", item[0], reason=reason)
        return bool(item)

    def cancel_account(self, account_id: str, reason: str = "cancel_account") -> int:
        wanted = str(account_id or "")
        cancelled = 0
        with self._cond:
            keys = [key for key, (job, _callback) in self._jobs.items() if job.account_id == wanted]
            for key in keys:
                job, _callback = self._jobs.pop(key)
                cancelled += 1
                self._emit("runtime_schedule_cancelled", job, reason=reason)
            if cancelled:
                self._cond.notify_all()
        return cancelled

    def cancel_all(self, reason: str = "cancel_all") -> int:
        with self._cond:
            items = list(self._jobs.values())
            self._jobs.clear()
            if items:
                self._cond.notify_all()
        for job, _callback in items:
            self._emit("runtime_schedule_cancelled", job, reason=reason)
        return len(items)

    def run_due(self, now: Optional[float] = None) -> int:
        current = time.time() if now is None else float(now)
        due_items = []
        with self._cond:
            due_keys = [
                key
                for key, (job, _callback) in sorted(self._jobs.items(), key=lambda item: item[1][0].due_at)
                if job.due_at <= current
            ]
            for key in due_keys:
                item = self._jobs.pop(key, None)
                if item:
                    due_items.append(item)
        for job, callback in due_items:
            self._dispatch(job, callback, current)
        return len(due_items)

    def snapshot(self, now: Optional[float] = None) -> Dict[str, Any]:
        current = time.time() if now is None else float(now)
        with self._lock:
            jobs = [item[0] for item in self._jobs.values()]
            next_due_at = min((job.due_at for job in jobs), default=0.0)
            overdue = [max(0.0, current - job.due_at) for job in jobs if job.due_at <= current]
            last_loop_at = float(self._last_loop_at or 0.0)
            last_dispatch_finished_at = float(self._last_dispatch_finished_at or 0.0)
            return {
                "name": self._name,
                "closed": bool(self._closed),
                "thread_alive": bool(self._thread.is_alive()),
                "external_stop": bool(self._external_stop.is_set()),
                "started_at": round(float(self._started_at or 0.0), 3),
                "pending_count": len(jobs),
                "periodic_count": sum(1 for job in jobs if job.periodic),
                "next_due_at": round(float(next_due_at or 0.0), 3),
                "next_delay_seconds": round(max(0.0, next_due_at - current), 3) if next_due_at else 0.0,
                "overdue_count": len(overdue),
                "max_overdue_seconds": round(max(overdue), 3) if overdue else 0.0,
                "last_loop_at": round(last_loop_at, 3),
                "last_loop_age_seconds": round(max(0.0, current - last_loop_at), 3) if last_loop_at else 0.0,
                "last_dispatch_started_at": round(float(self._last_dispatch_started_at or 0.0), 3),
                "last_dispatch_finished_at": round(last_dispatch_finished_at, 3),
                "last_dispatch_age_seconds": round(max(0.0, current - last_dispatch_finished_at), 3) if last_dispatch_finished_at else 0.0,
                "last_dispatch_latency_seconds": round(float(self._last_dispatch_latency_seconds or 0.0), 3),
                "dispatch_count": int(self._dispatch_count),
                "callback_failure_count": int(self._callback_failure_count),
                "stale_rejection_count": int(self._stale_rejection_count),
            }

    def _set_job(self, job: RuntimeScheduledJob, callback: ScheduleCallback) -> None:
        with self._cond:
            if self._closed:
                raise RuntimeError("RuntimeScheduler is stopped")
            self._jobs[job.job_key] = (job, callback)
            self._cond.notify_all()
        self._emit("runtime_schedule_set", job)

    def _loop(self) -> None:
        while not self._external_stop.is_set():
            with self._cond:
                self._last_loop_at = time.time()
                if self._closed:
                    break
                if not self._jobs:
                    self._cond.wait(timeout=1.0)
                    continue
                job, _callback = min(self._jobs.values(), key=lambda item: item[0].due_at)
                wait_for = job.due_at - time.time()
                if wait_for > 0:
                    self._cond.wait(timeout=min(wait_for, 5.0))
                    continue
            self.run_due()

    def _dispatch(self, job: RuntimeScheduledJob, callback: ScheduleCallback, now: float) -> None:
        if self._closed or self._external_stop.is_set():
            return
        if not self._generation_matches(job):
            with self._lock:
                self._stale_rejection_count += 1
            self._emit("runtime_schedule_stale_rejected", job, level="warning")
            return
        started = time.time()
        with self._lock:
            self._last_dispatch_started_at = started
            self._last_dispatch_latency_seconds = max(0.0, float(now) - float(job.due_at or now))
            self._dispatch_count += 1
        self._emit("runtime_schedule_due", job)
        try:
            callback(job)
        except Exception as exc:
            with self._lock:
                self._callback_failure_count += 1
            self._emit("runtime_schedule_callback_failed", job, level="error", error=str(exc))
        finally:
            with self._lock:
                self._last_dispatch_finished_at = time.time()
            if job.periodic and not self._closed and not self._external_stop.is_set():
                next_job = RuntimeScheduledJob(
                    job_key=job.job_key,
                    due_at=now + max(0.1, float(job.interval_seconds or 0.1)),
                    reason=job.reason,
                    account_id=job.account_id,
                    account=job.account,
                    interval_seconds=job.interval_seconds,
                    periodic=True,
                    payload=dict(job.payload or {}),
                )
                with self._cond:
                    self._jobs[next_job.job_key] = (next_job, callback)
                    self._cond.notify_all()

    def _generation_matches(self, job: RuntimeScheduledJob) -> bool:
        account = job.account
        if account is None:
            return True
        checks = (
            ("runtime_generation", job.runtime_generation),
            ("recovery_generation", job.recovery_generation),
            ("command_generation", job.command_generation),
        )
        for field_name, expected in checks:
            if expected is None:
                continue
            current = int(getattr(account, field_name, 0) or 0)
            if current != int(expected):
                if field_name == "runtime_generation" and self.effective_runtime_generation(job) is not None:
                    continue
                return False
        return True

    def effective_runtime_generation(self, job: RuntimeScheduledJob) -> Optional[int]:
        account = job.account
        expected = job.runtime_generation
        if account is None:
            return int(expected or 0)
        if expected is None:
            return None
        current = int(getattr(account, "runtime_generation", 0) or 0)
        if current == int(expected):
            return current
        payload = dict(job.payload or {})
        recovery_ok = job.recovery_generation is not None and int(getattr(account, "recovery_generation", 0) or 0) == int(job.recovery_generation)
        command_ok = job.command_generation is None or int(getattr(account, "command_generation", 0) or 0) == int(job.command_generation)
        if payload.get("allow_runtime_generation_drift") and recovery_ok and command_ok:
            return current
        return None

    def _emit(self, event: str, job: RuntimeScheduledJob, level: str = "info", **extra: Any) -> None:
        payload = {
            "job_key": job.job_key,
            "job_id": job.job_id,
            "due_at": job.due_at,
            "reason": job.reason,
            "account_id": job.account_id,
            "runtime_generation": job.runtime_generation,
            "recovery_generation": job.recovery_generation,
            "command_generation": job.command_generation,
            "interval_seconds": job.interval_seconds,
            "periodic": job.periodic,
            "created_at": job.created_at,
            **dict(job.payload or {}),
        }
        payload.update(extra)
        payload.setdefault("scheduler", self._name)
        payload.setdefault("delay_seconds", round(max(0.0, float(job.due_at or 0.0) - time.time()), 3))
        if self._timeline:
            try:
                snapshot = job.account.runtime_snapshot() if job.account is not None and hasattr(job.account, "runtime_snapshot") else {}
                self._timeline.record({"event_type": event, "severity": level, **payload}, account_snapshot=snapshot, account_id=job.account_id)
            except Exception:
                pass
        if self._log:
            try:
                self._log("RUNTIME", event, level, **payload)
            except TypeError:
                self._log("RUNTIME", event, **payload)

    @staticmethod
    def _account_key(account: Any) -> str:
        if account is None:
            return ""
        return str(getattr(account, "_config_username", "") or getattr(account, "username", "") or "")
