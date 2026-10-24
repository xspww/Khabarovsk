"""Multi-roblox guard subprocess owner (Win32 singleton handles)."""
from __future__ import annotations

import atexit
import os
import subprocess
import sys
import threading
import time
from typing import Any

from infra.app_paths import APP_ROOT_DIR, EXECUTABLE_PATH, IS_COMPILED

_MULTI_ROBLOX_LOCK = threading.RLock()
_MULTI_ROBLOX_GUARD_MODE = "mutex"


class _MultiRobloxState:
    """Mutable runtime state for the multi-Roblox guard helper process."""

    def __init__(self) -> None:
        self.handles: list[tuple[str, int]] = []
        self.helper: subprocess.Popen | None = None
        self.state = "stopped"
        self.detail = ""
        self.last_failure = ""
        self.started_at = 0.0
        self.handle_names: list[str] = []

    def helper_pid(self) -> int:
        return int(getattr(self.helper, "pid", 0) or 0) if self.helper else 0

    def fail(self, detail: str) -> None:
        self.state = "failed"
        self.last_failure = detail
        self.detail = detail

    def begin_start(self) -> None:
        self.state = "starting"
        self.detail = "starting guard helper"
        self.handle_names = []

    def mark_ready(self, detail: str, handle_names: list[str]) -> None:
        self.handle_names = handle_names
        self.state = "ready"
        self.detail = detail
        self.last_failure = ""

    def stop(self) -> None:
        self.handles = []
        self.handle_names = []
        self.state = "stopped"
        self.detail = ""


_MULTI_ROBLOX = _MultiRobloxState()


def _multi_roblox_log(event: str, severity: str = "info", **fields: Any) -> None:
    try:
        from core import flog_kv

        flog_kv("MULTI_ROBLOX", event, severity, **fields)
    except Exception:
        "intentional: ignore exception"


def _read_guard_ready_line(proc: subprocess.Popen, timeout: float) -> tuple[str, str]:
    box: dict[str, str] = {}

    def _reader() -> None:
        try:
            if proc.stdout is None:
                box["line"] = ""
                return
            box["line"] = str(proc.stdout.readline() or "").strip()
        except Exception as exc:
            box["error"] = str(exc)

    thread = threading.Thread(target=_reader, daemon=True, name="MultiRobloxGuardReadyReader")
    thread.start()
    thread.join(max(0.5, float(timeout or 5.0)))
    if thread.is_alive():
        return "", "ready timeout"
    return box.get("line", ""), box.get("error", "")


def _parse_multi_roblox_ready_line(ready_line: str) -> tuple[str, list[str], bool, bool]:
    detail = str(ready_line or "").replace("multi_roblox_guard_ready", "", 1).strip()
    handle_part = detail.split(" pid=", 1)[0].strip()
    handle_names = [name for name in handle_part.split(",") if name]
    has_mutex = any("ROBLOX_singletonMutex" in name for name in handle_names)
    has_event = any("ROBLOX_singletonEvent" in name for name in handle_names)
    return detail, handle_names, has_mutex, has_event


def _terminate_multi_roblox_helper_locked(reason: str = "release") -> None:
    proc = _MULTI_ROBLOX.helper
    _MULTI_ROBLOX.helper = None
    if not proc:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3.0)
    except Exception as exc:
        _multi_roblox_log("guard_stop_error", "warning", pid=getattr(proc, "pid", ""), reason=reason, error=str(exc))


def record_multi_roblox_guard_failure(detail: str) -> None:
    message = str(detail or "Multi Roblox guard failed").strip()
    with _MULTI_ROBLOX_LOCK:
        _MULTI_ROBLOX.fail(message)
    _multi_roblox_log("guard_failed", "error", detail=message)


def _guard_status_snapshot_locked() -> tuple[int, str, str]:
    proc = _MULTI_ROBLOX.helper
    pid = _MULTI_ROBLOX.helper_pid()
    state = _MULTI_ROBLOX.state
    detail = _MULTI_ROBLOX.detail
    if not proc:
        return pid, state, detail
    rc = proc.poll()
    if rc is None:
        return pid, state, detail
    if state not in {"stopped", "failed"}:
        state = "failed"
        detail = f"guard helper exited rc={rc}"
        _MULTI_ROBLOX.fail(detail)
    return 0, state, detail


def multi_roblox_guard_status() -> dict[str, Any]:
    with _MULTI_ROBLOX_LOCK:
        pid, state, detail = _guard_status_snapshot_locked()
        return {
            "state": state,
            "pid": pid,
            "detail": detail,
            "last_failure": _MULTI_ROBLOX.last_failure,
            "started_at": _MULTI_ROBLOX.started_at,
            "handle_names": list(_MULTI_ROBLOX.handle_names),
        }


def _guard_ready_result_locked() -> tuple[bool, str] | None:
    helper = _MULTI_ROBLOX.helper
    if helper and helper.poll() is None and _MULTI_ROBLOX.state == "ready":
        return True, _MULTI_ROBLOX.detail
    if helper and helper.poll() is None:
        _terminate_multi_roblox_helper_locked("restart_not_ready")
    elif helper and helper.poll() is not None:
        _MULTI_ROBLOX.last_failure = f"guard helper exited rc={helper.poll()}"
        _MULTI_ROBLOX.helper = None
    return None


def _guard_command() -> tuple[str, list[str]]:
    if IS_COMPILED:
        guard_path = EXECUTABLE_PATH or sys.executable or "CronusLauncher.exe"
        return guard_path, [
            guard_path,
            "--multi-roblox-guard",
            _MULTI_ROBLOX_GUARD_MODE,
            "--parent-pid",
            str(os.getpid()),
        ]
    guard_path = os.path.join(APP_ROOT_DIR, "services", "multi_roblox_guard.py")
    return guard_path, [
        sys.executable or "python",
        guard_path,
        _MULTI_ROBLOX_GUARD_MODE,
        "--parent-pid",
        str(os.getpid()),
    ]


def _guard_ready_problem(proc: subprocess.Popen, ready_line: str, read_error: str) -> str:
    if read_error:
        return read_error
    if proc.poll() is not None:
        return f"guard helper exited before ready rc={proc.poll()} {ready_line}".strip()
    if not ready_line.startswith("multi_roblox_guard_ready"):
        return ready_line or "guard helper did not report ready"
    return ""


def _start_guard_helper_locked(guard_path: str, cmd: list[str], timeout: float) -> str:
    _MULTI_ROBLOX.begin_start()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _MULTI_ROBLOX.helper = proc
    _MULTI_ROBLOX.started_at = time.time()
    _multi_roblox_log("guard_started", pid=proc.pid, cmd=os.path.basename(guard_path))
    ready_line, read_error = _read_guard_ready_line(proc, timeout)
    problem = _guard_ready_problem(proc, ready_line, read_error)
    if problem:
        raise RuntimeError(problem)
    return ready_line


def _finish_guard_ready_locked(ready_line: str) -> tuple[bool, str]:
    detail, handle_names, has_mutex, has_event = _parse_multi_roblox_ready_line(ready_line)
    if not has_mutex:
        raise RuntimeError(f"guard helper missing Roblox singleton mutex: {ready_line}")
    if has_event:
        detail = f"{detail} unexpected_event_handle=present".strip()
        _multi_roblox_log(
            "guard_event_handle_present",
            "warning",
            pid=_MULTI_ROBLOX.helper_pid(),
            detail=detail,
        )
    _MULTI_ROBLOX.mark_ready(detail, handle_names)
    _multi_roblox_log("guard_ready", pid=_MULTI_ROBLOX.helper_pid(), detail=detail)
    return True, detail


def ensure_multi_roblox_guard(timeout: float = 6.0) -> tuple[bool, str]:
    with _MULTI_ROBLOX_LOCK:
        cached = _guard_ready_result_locked()
        if cached is not None:
            return cached
        guard_path, cmd = _guard_command()
        try:
            ready_line = _start_guard_helper_locked(guard_path, cmd, timeout)
            return _finish_guard_ready_locked(ready_line)
        except Exception as exc:
            failure = str(exc)
            _MULTI_ROBLOX.fail(failure)
            _terminate_multi_roblox_helper_locked("startup_failed")
            _multi_roblox_log("guard_failed", "error", detail=failure)
            return False, failure


def release_multi_roblox_guard() -> None:
    with _MULTI_ROBLOX_LOCK:
        helper_pid = _MULTI_ROBLOX.helper_pid()
        _terminate_multi_roblox_helper_locked("release")
        try:
            if _MULTI_ROBLOX.handles:
                from services.multi_roblox_guard import close_handles

                close_handles(_MULTI_ROBLOX.handles)
        finally:
            _MULTI_ROBLOX.stop()
        if helper_pid:
            _multi_roblox_log("guard_stopped", pid=helper_pid)


atexit.register(release_multi_roblox_guard)
