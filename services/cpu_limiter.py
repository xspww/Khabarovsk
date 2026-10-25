from __future__ import annotations

import ctypes
import os
import sys
import threading
from collections.abc import Iterable
from ctypes import wintypes
from typing import Any

from domain._util import account_display as _account_display
from domain._util import account_key as _account_username
from domain._util import account_pid as _account_pid

CPU_LIMIT_MIN = 5
CPU_LIMIT_MAX = 95
CPU_LIMIT_DEFAULT = 20
CPU_LIMIT_MODES = {"soft", "hard"}
ROBLOX_PROCESS_NAME = "robloxplayerbeta.exe"

_JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION = 15
_JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
_JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _JobObjectCpuRateControlInformation(ctypes.Structure):
    _fields_ = [
        ("ControlFlags", wintypes.DWORD),
        ("CpuRate", wintypes.DWORD),
    ]


def normalize_cpu_limit(value: Any, default: int = CPU_LIMIT_DEFAULT) -> int:
    try:
        limit = int(float(value))
    except Exception:
        limit = int(default)
    if limit < CPU_LIMIT_MIN or limit > CPU_LIMIT_MAX:
        raise ValueError(f"CPU limit must be between {CPU_LIMIT_MIN} and {CPU_LIMIT_MAX}")
    return limit


def normalize_cpu_mode(value: Any, default: str = "hard") -> str:
    mode = str(value or default or "hard").strip().lower().replace("_", "-").replace(" ", "-")
    if mode == "hard-cap":
        mode = "hard"
    if mode not in CPU_LIMIT_MODES:
        raise ValueError("CPU limiter mode must be soft or hard")
    return mode


def normalize_cpu_limiter_settings(source: dict[str, Any]) -> dict[str, Any]:
    raw_accounts = source.get("cpu_limiter_accounts") if "cpu_limiter_accounts" in source else source.get("accounts", {})
    accounts: dict[str, dict[str, Any]] = {}
    if isinstance(raw_accounts, list):
        iterable = raw_accounts
    elif isinstance(raw_accounts, dict):
        iterable = [
            {"username": username, **(value if isinstance(value, dict) else {})}
            for username, value in raw_accounts.items()
        ]
    else:
        iterable = []
    for item in iterable:
        if not isinstance(item, dict):
            continue
        username = str(item.get("username") or "").strip()
        if not username:
            continue
        accounts[username] = {
            "enabled": bool(item.get("enabled", False)),
            "limit_percent": normalize_cpu_limit(item.get("limit_percent", source.get("cpu_limiter_default_percent", CPU_LIMIT_DEFAULT))),
        }
    apply_all = bool(source.get("cpu_limiter_apply_all", source.get("apply_all", True)))
    if apply_all:
        accounts = {}
    return {
        "enabled": bool(source.get("cpu_limiter_enabled", source.get("enabled", False))),
        "mode": normalize_cpu_mode(source.get("cpu_limiter_mode", source.get("mode", "hard"))),
        "default_limit_percent": normalize_cpu_limit(source.get("cpu_limiter_default_percent", source.get("default_limit_percent", CPU_LIMIT_DEFAULT))),
        "apply_all": apply_all,
        "accounts": accounts,
    }


def _same_create_time(record: Any, create_time: Any) -> bool:
    if not record:
        return False
    return float(record.get("create_time") or 0.0) == float(create_time or 0.0)


def _soft_nice(proc: Any) -> Any:
    try:
        return proc.nice()
    except Exception:
        return None


def _soft_affinity(proc: Any) -> Any:
    try:
        if not hasattr(proc, "cpu_affinity"):
            return None
        return proc.cpu_affinity()
    except Exception:
        return None


def _cpu_indexes(psutil: Any) -> list[int]:
    return list(range(psutil.cpu_count(logical=True) or 1))


def _snapshot_status(row: dict[str, Any], enabled: bool, previous: dict[str, Any]) -> dict[str, str]:
    if not enabled:
        return {"status": "Off", "message": ""}
    if not row["enabled"]:
        return {"status": "Disabled", "message": ""}
    if not row["pid"]:
        return {"status": "No PID", "message": "Roblox is not running for this account."}
    return _previous_snapshot_status(row, previous)


def _previous_snapshot_status(row: dict[str, Any], previous: dict[str, Any]) -> dict[str, str]:
    if int(previous.get("pid") or 0) != int(row["pid"] or 0):
        return {"status": row["status"], "message": ""}
    return {
        "status": str(previous.get("status") or row["status"]),
        "message": str(previous.get("message") or ""),
    }


class CpuLimiter:
    def __init__(self):
        self._lock = threading.RLock()
        self._job_handles: dict[int, dict[str, Any]] = {}
        self._soft_originals: dict[int, dict[str, Any]] = {}
        self._last_rows: dict[str, dict[str, Any]] = {}

    def snapshot(self, accounts: Iterable[Any], settings: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_cpu_limiter_settings(settings)
        rows = self._build_rows(accounts, normalized, self._last_rows)
        return {
            "ok": True,
            **normalized,
            "rows": rows,
        }

    def release_all(self) -> dict[str, Any]:
        with self._lock:
            pids = sorted(set(self._job_handles) | set(self._soft_originals))
        released = 0
        for pid in pids:
            if self.release_pid(pid):
                released += 1
        with self._lock:
            self._last_rows = {}
        return {"ok": True, "released": released}

    def release_pid(self, pid: int) -> bool:
        pid = int(pid or 0)
        restored_soft = self._restore_soft(pid)
        record = None
        with self._lock:
            record = self._job_handles.pop(pid, None)
        if not record:
            return restored_soft
        handle = int(record.get("handle") or 0)
        try:
            self._set_job_cpu_rate(handle, CPU_LIMIT_MAX, enabled=False)
        except Exception:
            "intentional: ignore exception"
        self._close_handle(handle)
        return True

    def _apply_account_row(
        self,
        account: Any,
        settings: dict[str, Any],
        active_pids: set[int],
    ) -> dict[str, Any]:
        row = self._row_base(account, settings)
        username = row["username"]
        if not row["enabled"]:
            if row["pid"]:
                self.release_pid(int(row["pid"]))
            row.update({"status": "Disabled", "message": ""})
            return row
        pid = int(row["pid"] or 0)
        if not pid:
            row.update({"status": "No PID", "message": "Roblox is not running for this account."})
            return row
        if not self._is_roblox_pid(pid):
            self.release_pid(pid)
            row.update({"status": "Failed", "message": "PID is not a live Roblox process."})
            return row
        active_pids.add(pid)
        result = self._apply_pid(pid, row["limit_percent"], settings["mode"])
        row.update(result)
        with self._lock:
            self._last_rows[username] = dict(row)
        return row

    def apply(self, accounts: Iterable[Any], settings: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_cpu_limiter_settings(settings)
        account_list = list(accounts)
        if not normalized["enabled"]:
            self.release_all()
            rows = self._build_rows(account_list, normalized, {})
            return {"ok": True, **normalized, "rows": rows, "applied": 0, "fallback": 0, "failed": 0}

        rows: list[dict[str, Any]] = []
        active_pids: set[int] = set()
        for account in account_list:
            rows.append(self._apply_account_row(account, normalized, active_pids))

        with self._lock:
            for pid in list(self._job_handles):
                if pid not in active_pids:
                    self.release_pid(pid)
            self._last_rows = {row["username"]: dict(row) for row in rows}
        return {
            "ok": True,
            **normalized,
            "rows": rows,
            "applied": sum(1 for row in rows if row.get("status") == "Applied"),
            "fallback": sum(1 for row in rows if row.get("status") == "Fallback"),
            "failed": sum(1 for row in rows if row.get("status") == "Failed"),
        }

    def _row_base(self, account: Any, settings: dict[str, Any]) -> dict[str, Any]:
        username = _account_username(account)
        override = settings["accounts"].get(username, {})
        if settings["apply_all"]:
            enabled = True
            limit = normalize_cpu_limit(settings["default_limit_percent"])
        else:
            enabled = bool(override.get("enabled", False))
            limit = normalize_cpu_limit(override.get("limit_percent", settings["default_limit_percent"]))
        return {
            "username": username,
            "display": _account_display(account),
            "pid": _account_pid(account),
            "enabled": enabled,
            "limit_percent": limit,
            "mode": settings["mode"],
            "status": "Pending" if settings["enabled"] and enabled else "Disabled",
            "message": "",
        }

    def _row_for_snapshot(
        self,
        account: Any,
        settings: dict[str, Any],
        last_rows: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        row = self._row_base(account, settings)
        previous = last_rows.get(row["username"]) or {}
        row.update(_snapshot_status(row, settings["enabled"], previous))
        return row

    def _build_rows(self, accounts: Iterable[Any], settings: dict[str, Any], last_rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        return [self._row_for_snapshot(account, settings, last_rows) for account in accounts]

    def _apply_pid(self, pid: int, limit_percent: int, mode: str) -> dict[str, str]:
        if mode == "soft":
            ok, msg = self._apply_soft(pid, limit_percent)
            return {"status": "Applied" if ok else "Failed", "message": msg}
        try:
            self._apply_hard(pid, limit_percent)
            return {"status": "Applied", "message": f"Hard cap applied at {float(limit_percent):.2f}% total CPU."}
        except Exception as hard_error:
            ok, soft_msg = self._apply_soft(pid, limit_percent)
            if ok:
                return {"status": "Fallback", "message": f"Hard cap failed; soft limit applied. {hard_error}"}
            return {"status": "Failed", "message": f"Hard cap failed: {hard_error}; soft limit failed: {soft_msg}"}

    def _reuse_job(self, record: dict[str, Any] | None, limit_percent: int) -> bool:
        if not record:
            return False
        job = int(record.get("handle") or 0)
        self._set_job_cpu_rate(job, limit_percent, enabled=True)
        self._verify_job_cpu_rate(job, limit_percent)
        return True

    def _create_job_for_pid(self, kernel32: Any, pid: int, create_time: float, limit_percent: int) -> None:
        job = kernel32.CreateJobObjectW(None, f"CronusCpuLimiter_{os.getpid()}_{pid}")
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        self._set_job_cpu_rate(job, limit_percent, enabled=True)
        self._verify_job_cpu_rate(job, limit_percent)
        process = kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            int(pid),
        )
        if not process:
            self._close_handle(job)
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel32.AssignProcessToJobObject(job, process):
                error = ctypes.get_last_error()
                self._close_handle(job)
                raise ctypes.WinError(error)
        finally:
            self._close_handle(process)
        self._job_handles[pid] = {"handle": int(job), "create_time": float(create_time or 0.0)}

    def _apply_hard(self, pid: int, limit_percent: int) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Hard cap is Windows-only")
        kernel32 = self._kernel32()
        create_time = self._process_create_time(pid)
        with self._lock:
            record = self._job_handles.get(pid)
            if record and float(record.get("create_time") or 0.0) != float(create_time or 0.0):
                self._close_handle(int(record.get("handle") or 0))
                self._job_handles.pop(pid, None)
                record = None
            if self._reuse_job(record, limit_percent):
                return
            self._create_job_for_pid(kernel32, pid, create_time, limit_percent)

    def _set_job_cpu_rate(self, job_handle: int, limit_percent: int, enabled: bool) -> None:
        if not job_handle:
            raise RuntimeError("invalid job handle")
        kernel32 = self._kernel32()
        flags = (_JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | _JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP) if enabled else 0
        info = _JobObjectCpuRateControlInformation(flags, int(limit_percent) * 100)
        ok = kernel32.SetInformationJobObject(
            job_handle,
            _JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def _verify_job_cpu_rate(self, job_handle: int, limit_percent: int) -> None:
        kernel32 = self._kernel32()
        info = _JobObjectCpuRateControlInformation()
        returned = wintypes.DWORD(0)
        ok = kernel32.QueryInformationJobObject(
            job_handle,
            _JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(returned),
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        expected = int(limit_percent) * 100
        expected_flags = _JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | _JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
        if int(info.CpuRate) != expected or (int(info.ControlFlags) & expected_flags) != expected_flags:
            raise RuntimeError(f"CPU hard cap verify failed: rate={int(info.CpuRate)} flags={int(info.ControlFlags)}")

    def _capture_soft_original(self, proc: Any, pid: int, create_time: float) -> None:
        with self._lock:
            current = self._soft_originals.get(int(pid))
            if _same_create_time(current, create_time):
                return
            self._soft_originals[int(pid)] = {
                "create_time": float(create_time or 0.0),
                "nice": _soft_nice(proc),
                "affinity": _soft_affinity(proc),
            }

    def _apply_soft(self, pid: int, limit_percent: int) -> tuple[bool, str]:
        try:
            import psutil
        except ImportError:
            return False, "psutil unavailable"
        try:
            proc = psutil.Process(int(pid))
            create_time = self._process_create_time(pid)
            self._capture_soft_original(proc, pid, create_time)
            if sys.platform == "win32" and hasattr(psutil, "IDLE_PRIORITY_CLASS"):
                proc.nice(psutil.IDLE_PRIORITY_CLASS)
            cpus = _cpu_indexes(psutil)
            keep = max(1, min(len(cpus), round(len(cpus) * (int(limit_percent) / 100.0))))
            if hasattr(proc, "cpu_affinity") and cpus:
                proc.cpu_affinity(cpus[:keep])
            return True, f"Soft limit applied on {keep}/{len(cpus)} CPU cores."
        except Exception as exc:
            return False, str(exc)

    def _take_soft_original(self, pid: int) -> dict[str, Any] | None:
        with self._lock:
            return self._soft_originals.pop(int(pid or 0), None)

    def _restore_soft_values(self, proc: Any, original: dict[str, Any]) -> None:
        if original.get("nice") is not None:
            proc.nice(original["nice"])
        if original.get("affinity") and hasattr(proc, "cpu_affinity"):
            proc.cpu_affinity(original["affinity"])

    def _restore_soft(self, pid: int) -> bool:
        try:
            import psutil
        except ImportError:
            return False
        original = self._take_soft_original(pid)
        if not original:
            return False
        try:
            if not _same_create_time(original, self._process_create_time(pid)):
                return False
            self._restore_soft_values(psutil.Process(int(pid)), original)
            return True
        except Exception:
            return False

    def _is_roblox_pid(self, pid: int) -> bool:
        try:
            import psutil

            proc = psutil.Process(int(pid))
            return proc.is_running() and str(proc.name() or "").lower() == ROBLOX_PROCESS_NAME
        except Exception:
            return False

    def _process_create_time(self, pid: int) -> float:
        try:
            import psutil

            return float(psutil.Process(int(pid)).create_time())
        except Exception:
            return 0.0

    def _kernel32(self):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        return kernel32

    def _close_handle(self, handle: int) -> None:
        if not handle or sys.platform != "win32":
            return
        try:
            self._kernel32().CloseHandle(handle)
        except Exception:
            "intentional: ignore exception"


CPU_LIMITER = CpuLimiter()
