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
_MULTI_ROBLOX_HANDLES: list[tuple[str, int]] = []
_MULTI_ROBLOX_HELPER: subprocess.Popen | None = None
_MULTI_ROBLOX_STATE = "stopped"
_MULTI_ROBLOX_DETAIL = ""
_MULTI_ROBLOX_LAST_FAILURE = ""
_MULTI_ROBLOX_STARTED_AT = 0.0
_MULTI_ROBLOX_HANDLE_NAMES: list[str] = []
_MULTI_ROBLOX_GUARD_MODE = "mutex"


def _multi_roblox_log(event: str, severity: str = "info", **fields: Any) -> None:
    try:
        from core import flog_kv

        flog_kv("MULTI_ROBLOX", event, severity, **fields)
    except Exception:
        pass


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
    global _MULTI_ROBLOX_HELPER
    proc = _MULTI_ROBLOX_HELPER
    _MULTI_ROBLOX_HELPER = None
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
    global _MULTI_ROBLOX_STATE, _MULTI_ROBLOX_LAST_FAILURE, _MULTI_ROBLOX_DETAIL
    message = str(detail or "Multi Roblox guard failed").strip()
    with _MULTI_ROBLOX_LOCK:
        _MULTI_ROBLOX_STATE = "failed"
        _MULTI_ROBLOX_LAST_FAILURE = message
        _MULTI_ROBLOX_DETAIL = message
    _multi_roblox_log("guard_failed", "error", detail=message)


def multi_roblox_guard_status() -> dict[str, Any]:
    global _MULTI_ROBLOX_STATE, _MULTI_ROBLOX_LAST_FAILURE, _MULTI_ROBLOX_DETAIL
    with _MULTI_ROBLOX_LOCK:
        proc = _MULTI_ROBLOX_HELPER
        pid = int(getattr(proc, "pid", 0) or 0) if proc else 0
        state = _MULTI_ROBLOX_STATE
        detail = _MULTI_ROBLOX_DETAIL
        if proc:
            rc = proc.poll()
            if rc is not None:
                if state not in {"stopped", "failed"}:
                    state = "failed"
                    detail = f"guard helper exited rc={rc}"
                    _MULTI_ROBLOX_STATE = state
                    _MULTI_ROBLOX_DETAIL = detail
                    _MULTI_ROBLOX_LAST_FAILURE = detail
                pid = 0
        return {
            "state": state,
            "pid": pid,
            "detail": detail,
            "last_failure": _MULTI_ROBLOX_LAST_FAILURE,
            "started_at": _MULTI_ROBLOX_STARTED_AT,
            "handle_names": list(_MULTI_ROBLOX_HANDLE_NAMES),
        }


def ensure_multi_roblox_guard(timeout: float = 6.0) -> tuple[bool, str]:
    global _MULTI_ROBLOX_HELPER, _MULTI_ROBLOX_STATE, _MULTI_ROBLOX_DETAIL
    global _MULTI_ROBLOX_LAST_FAILURE, _MULTI_ROBLOX_STARTED_AT, _MULTI_ROBLOX_HANDLE_NAMES
    with _MULTI_ROBLOX_LOCK:
        if _MULTI_ROBLOX_HELPER and _MULTI_ROBLOX_HELPER.poll() is None and _MULTI_ROBLOX_STATE == "ready":
            return True, _MULTI_ROBLOX_DETAIL
        if _MULTI_ROBLOX_HELPER and _MULTI_ROBLOX_HELPER.poll() is None:
            _terminate_multi_roblox_helper_locked("restart_not_ready")
        elif _MULTI_ROBLOX_HELPER and _MULTI_ROBLOX_HELPER.poll() is not None:
            rc = _MULTI_ROBLOX_HELPER.poll()
            _MULTI_ROBLOX_LAST_FAILURE = f"guard helper exited rc={rc}"
            _MULTI_ROBLOX_HELPER = None

        if IS_COMPILED:
            guard_path = EXECUTABLE_PATH or sys.executable or "CronusLauncher.exe"
            cmd = [guard_path, "--multi-roblox-guard", _MULTI_ROBLOX_GUARD_MODE, "--parent-pid", str(os.getpid())]
        else:
            guard_path = os.path.join(APP_ROOT_DIR, "services", "multi_roblox_guard.py")
            cmd = [
                sys.executable or "python",
                guard_path,
                _MULTI_ROBLOX_GUARD_MODE,
                "--parent-pid",
                str(os.getpid()),
            ]
        try:
            _MULTI_ROBLOX_STATE = "starting"
            _MULTI_ROBLOX_DETAIL = "starting guard helper"
            _MULTI_ROBLOX_HANDLE_NAMES = []
            _MULTI_ROBLOX_HELPER = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            _MULTI_ROBLOX_STARTED_AT = time.time()
            _multi_roblox_log("guard_started", pid=_MULTI_ROBLOX_HELPER.pid, cmd=os.path.basename(guard_path))
            ready_line, read_error = _read_guard_ready_line(_MULTI_ROBLOX_HELPER, timeout)
            if read_error:
                raise RuntimeError(read_error)
            if _MULTI_ROBLOX_HELPER.poll() is not None:
                raise RuntimeError(f"guard helper exited before ready rc={_MULTI_ROBLOX_HELPER.poll()} {ready_line}".strip())
            if not ready_line.startswith("multi_roblox_guard_ready"):
                raise RuntimeError(ready_line or "guard helper did not report ready")
            detail, handle_names, has_mutex, has_event = _parse_multi_roblox_ready_line(ready_line)
            if not has_mutex:
                raise RuntimeError(f"guard helper missing Roblox singleton mutex: {ready_line}")
            if has_event:
                detail = f"{detail} unexpected_event_handle=present".strip()
                _multi_roblox_log(
                    "guard_event_handle_present",
                    "warning",
                    pid=_MULTI_ROBLOX_HELPER.pid,
                    detail=detail,
                )
            _MULTI_ROBLOX_HANDLE_NAMES = handle_names
            _MULTI_ROBLOX_STATE = "ready"
            _MULTI_ROBLOX_DETAIL = detail
            _MULTI_ROBLOX_LAST_FAILURE = ""
            _multi_roblox_log("guard_ready", pid=_MULTI_ROBLOX_HELPER.pid, detail=detail)
            return True, detail
        except Exception as exc:
            failure = str(exc)
            _MULTI_ROBLOX_LAST_FAILURE = failure
            _MULTI_ROBLOX_DETAIL = failure
            _MULTI_ROBLOX_STATE = "failed"
            _terminate_multi_roblox_helper_locked("startup_failed")
            _multi_roblox_log("guard_failed", "error", detail=failure)
            return False, failure


def release_multi_roblox_guard() -> None:
    global _MULTI_ROBLOX_HANDLES, _MULTI_ROBLOX_STATE, _MULTI_ROBLOX_DETAIL, _MULTI_ROBLOX_HANDLE_NAMES
    with _MULTI_ROBLOX_LOCK:
        helper_pid = int(getattr(_MULTI_ROBLOX_HELPER, "pid", 0) or 0) if _MULTI_ROBLOX_HELPER else 0
        _terminate_multi_roblox_helper_locked("release")
        try:
            if _MULTI_ROBLOX_HANDLES:
                from services.multi_roblox_guard import close_handles

                close_handles(_MULTI_ROBLOX_HANDLES)
        finally:
            _MULTI_ROBLOX_HANDLES = []
            _MULTI_ROBLOX_HANDLE_NAMES = []
            _MULTI_ROBLOX_STATE = "stopped"
            _MULTI_ROBLOX_DETAIL = ""
        if helper_pid:
            _multi_roblox_log("guard_stopped", pid=helper_pid)


atexit.register(release_multi_roblox_guard)
