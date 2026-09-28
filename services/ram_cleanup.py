"""RAM Cleanup service — Python port of MemReduct memory cleaning logic.

Base on MemReduct by Henry++ (GPLv3), src/main.c::_app_memoryclean (v3.5.3).
Safe subset of REDUCT_MASK_DEFAULT (no Standby/Modified full lists, no
volume flush — those can stall a farming rig; the 5 NtSetSystemInformation
steps below reclaim the bulk with no freeze risk):

    WORKINGSET | SYSTEMFILECACHE | STANDBYPRIORITY0 | REGISTRYCACHE
    | COMBINEMEMORYLISTS

Native API mapping (verified live on Win10/11 x64):
    SystemMemoryListInformation = 80 (0x50), commands:
        MemoryEmptyWorkingSets=2, MemoryPurgeLowPriorityStandbyList=5
    System file cache via documented kernel32.SetSystemFileCacheSize(-1,-1,0)
    SystemRegistryReconciliationInformation = 155 (0x9B), NULL/0 (win8.1+)
    SystemCombinePhysicalMemoryInformation = 130 (0x82),
        struct MEMORY_COMBINE_INFORMATION_EX { HANDLE, PagesCombined, Flags }
        zero-initialized (win10+; tolerated when unsupported)

Requires admin + SeProfileSingleProcessPrivilege / SeIncreaseQuotaPrivilege.
Non-admin -> clean() returns ok=False with requires_admin=True (no crash).
OS build gating: registry (6.3+), combine (6.2+); failures are per-step
warnings, never fatal — matches Q10 toast-warning behaviour.

License: GPLv3 (derivative of MemReduct). See THIRD_PARTY_NOTICES.
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes
from typing import Any, Dict

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None  # type: ignore

# ── NtSetSystemInformation classes / commands ──
_SYS_MEMORY_LIST_INFO = 80
_SYS_COMBINE_PHYSICAL = 130
_SYS_REGISTRY_RECONCILIATION = 155

_MEM_EMPTY_WORKING_SETS = 2
_MEM_PURGE_LOW_PRIORITY_STANDBY = 5

_CLEANUP_COOLDOWN_SECONDS = 300.0  # Q8: fixed 5 min


class _MemoryCombineInfoEx(ctypes.Structure):
    _fields_ = [
        ("Handle", wintypes.HANDLE),
        ("PagesCombined", ctypes.c_size_t),
        ("Flags", wintypes.DWORD),
    ]


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class _Luid(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _LuidAndAttr(ctypes.Structure):
    _fields_ = [("Luid", _Luid), ("Attributes", wintypes.DWORD)]


class _TokenPrivileges(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wintypes.DWORD),
                ("Privileges", _LuidAndAttr * 1)]


def _enable_privilege(name: str) -> bool:
    """Enable a privilege by name on the current process token. Best-effort."""
    try:
        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.argtypes = []
        advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
        advapi32.LookupPrivilegeValueW.argtypes = [wintypes.LPWSTR, wintypes.LPCWSTR,
                                                   ctypes.POINTER(_Luid)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.HANDLE)]
        advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL
        advapi32.AdjustTokenPrivileges.argtypes = [wintypes.HANDLE, wintypes.BOOL,
                                                   ctypes.POINTER(_TokenPrivileges),
                                                   wintypes.DWORD, wintypes.LPVOID,
                                                   wintypes.LPDWORD]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        luid = _Luid()
        if not advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            return False
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                         0x20 | 0x8,  # TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY
                                         ctypes.byref(token)):
            return False
        try:
            tp = _TokenPrivileges()
            tp.PrivilegeCount = 1
            tp.Privileges[0].Luid = luid
            tp.Privileges[0].Attributes = 0x2  # SE_PRIVILEGE_ENABLED
            if not advapi32.AdjustTokenPrivileges(
                    token, False, ctypes.byref(tp), ctypes.sizeof(tp), None, None):
                return False
            # AdjustTokenPrivileges reports success even when the privilege
            # is not present in the token — GetLastError must be clean.
            return kernel32.GetLastError() == 0
        finally:
            kernel32.CloseHandle(token)
    except Exception:
        return False


def _num(value: Any, default: float) -> float:
    """float() that only falls back on None/''/invalid — never on 0."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        return float(value)
    except Exception:
        return default


def _win_version() -> tuple:
    try:
        v = sys.getwindowsversion()
        return (int(v.major), int(v.minor))
    except Exception:
        return (10, 0)


def _supports_registry_cache() -> bool:
    return _win_version() >= (6, 3)


def _supports_combine() -> bool:
    return _win_version() >= (6, 2)


def _nt_set_info(info_class: int, buffer, size: int) -> int:
    try:
        ntdll = ctypes.windll.ntdll
        fn = ntdll.NtSetSystemInformation
        fn.restype = wintypes.LONG  # NTSTATUS
        fn.argtypes = [wintypes.ULONG, wintypes.LPVOID, wintypes.ULONG]
        return int(fn(info_class, buffer, size))
    except Exception:
        return -1


def _ram_snapshot_mb() -> Dict[str, float]:
    if psutil is None:
        return {"used_mb": 0.0, "total_mb": 0.0, "percent": 0.0}
    try:
        vm = psutil.virtual_memory()
        return {
            "used_mb": float(vm.used) / (1024 * 1024),
            "total_mb": float(vm.total) / (1024 * 1024),
            "percent": float(vm.percent),
        }
    except Exception:
        return {"used_mb": 0.0, "total_mb": 0.0, "percent": 0.0}


class RamCleanupService:
    """Stateful wrapper: manual clean + auto-clean eligibility + stats."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._last_run_at = 0.0
        self._last_check_at = 0.0
        self._last_freed_mb = 0.0
        self._last_status: Dict[str, Any] = {}

    # -- stats --
    def snapshot(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        snap = _ram_snapshot_mb()
        enabled = bool(cfg.get("ram_cleanup_enabled", False))
        interval_min = max(5, min(120, int(_num(cfg.get("ram_cleanup_interval_min", 15), 15))))
        with self._lock:
            last_at = self._last_run_at
            last_check = self._last_check_at
            freed = self._last_freed_mb
        next_check_at = (last_check + interval_min * 60.0) if (enabled and last_check) else 0.0
        next_in = max(0, int(next_check_at - time.time())) if next_check_at else 0
        return {
            "ok": True,
            "enabled": enabled,
            "threshold_pct": max(50.0, min(95.0, _num(cfg.get("ram_cleanup_threshold_pct", 85.0), 85.0))),
            "interval_min": interval_min,
            "cooldown_seconds": int(_CLEANUP_COOLDOWN_SECONDS),
            "is_admin": is_admin(),
            "current": snap,
            "last_run_at": last_at,
            "last_check_at": last_check,
            "next_check_at": next_check_at,
            "next_check_in_seconds": next_in,
            "last_freed_mb": round(float(freed), 1),
            "last": dict(self._last_status),
        }

    def _check_cooldown(self, now: float) -> float:
        with self._lock:
            return max(0.0, _CLEANUP_COOLDOWN_SECONDS - (now - self._last_run_at)) \
                if self._last_run_at else 0.0

    def should_auto_clean(self, cfg: Dict[str, Any], now: float | None = None) -> Dict[str, Any]:
        now = now if now is not None else time.time()
        if not bool(cfg.get("ram_cleanup_enabled", False)):
            return {"eligible": False, "reason": "disabled"}
        with self._lock:
            self._last_check_at = now
        threshold = max(50.0, min(95.0, _num(cfg.get("ram_cleanup_threshold_pct", 85.0), 85.0)))
        snap = _ram_snapshot_mb()
        if snap["percent"] < threshold:
            return {"eligible": False, "reason": "below_threshold",
                    "percent": snap["percent"], "threshold": threshold}
        remaining = self._check_cooldown(now)
        if remaining > 0:
            return {"eligible": False, "reason": "cooldown",
                    "cooldown_remaining": round(remaining, 1),
                    "percent": snap["percent"], "threshold": threshold}
        if not is_admin():
            return {"eligible": False, "reason": "requires_admin",
                    "percent": snap["percent"], "threshold": threshold}
        return {"eligible": True, "reason": "ready",
                "percent": snap["percent"], "threshold": threshold}

    # -- core clean (MemReduct REDUCT_MASK_DEFAULT port) --
    def clean(self, source: str = "manual") -> Dict[str, Any]:
        before = _ram_snapshot_mb()
        errors: list = []
        steps: Dict[str, Any] = {}

        if os.name != "nt":
            return {"ok": False, "msg": "RAM cleanup supports Windows only",
                    "requires_admin": False, "freed_mb": 0.0}

        if not is_admin():
            return {"ok": False, "msg": "Requires admin — relaunch as administrator",
                    "requires_admin": True, "freed_mb": 0.0,
                    "current": before}

        _enable_privilege("SeProfileSingleProcessPrivilege")
        _enable_privilege("SeIncreaseQuotaPrivilege")

        # 1. Empty working sets
        try:
            cmd = ctypes.c_int(_MEM_EMPTY_WORKING_SETS)
            st = _nt_set_info(_SYS_MEMORY_LIST_INFO, ctypes.byref(cmd), ctypes.sizeof(cmd))
            steps["working_set"] = {"ntstatus": st, "ok": st == 0}
            if st != 0:
                errors.append(f"MemoryEmptyWorkingSets NTSTATUS=0x{st & 0xFFFFFFFF:08X}")
        except Exception as exc:
            steps["working_set"] = {"ok": False, "error": str(exc)}
            errors.append(f"working_set: {exc}")

        # 2. System file cache — documented kernel32 API. (The NtSet
        # classes 21/81 MemReduct uses are length-gated with
        # STATUS_INFO_LENGTH_MISMATCH on current Windows builds;
        # SetSystemFileCacheSize(-1, -1, 0) is the supported equivalent.)
        try:
            k32 = ctypes.windll.kernel32
            fn = k32.SetSystemFileCacheSize
            fn.restype = wintypes.BOOL
            fn.argtypes = [ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
            max_ws = ctypes.c_size_t(-1).value
            cache_ok = bool(fn(max_ws, max_ws, 0))
            steps["system_file_cache"] = {"ok": cache_ok}
            if not cache_ok:
                errors.append("SetSystemFileCacheSize failed")
        except Exception as exc:
            steps["system_file_cache"] = {"ok": False, "error": str(exc)}
            errors.append(f"file_cache: {exc}")

        # 3. Standby priority-0 list
        try:
            cmd = ctypes.c_int(_MEM_PURGE_LOW_PRIORITY_STANDBY)
            st = _nt_set_info(_SYS_MEMORY_LIST_INFO, ctypes.byref(cmd), ctypes.sizeof(cmd))
            steps["standby_priority0"] = {"ntstatus": st, "ok": st == 0}
            if st != 0:
                errors.append(f"PurgeLowPriorityStandby NTSTATUS=0x{st & 0xFFFFFFFF:08X}")
        except Exception as exc:
            steps["standby_priority0"] = {"ok": False, "error": str(exc)}
            errors.append(f"standby_prio0: {exc}")

        # 4. Registry cache (win8.1+)
        if _supports_registry_cache():
            try:
                st = _nt_set_info(_SYS_REGISTRY_RECONCILIATION, None, 0)
                steps["registry_cache"] = {"ntstatus": st, "ok": st == 0}
                if st != 0:
                    errors.append(f"RegistryReconciliation NTSTATUS=0x{st & 0xFFFFFFFF:08X}")
            except Exception as exc:
                steps["registry_cache"] = {"ok": False, "error": str(exc)}
                errors.append(f"registry: {exc}")
        else:
            steps["registry_cache"] = {"ok": True, "skipped": "os_too_old"}

        # 5. Combine memory lists (win10+)
        if _supports_combine():
            try:
                info = _MemoryCombineInfoEx()  # zero-initialized
                st = _nt_set_info(_SYS_COMBINE_PHYSICAL, ctypes.byref(info), ctypes.sizeof(info))
                steps["combine"] = {"ntstatus": st, "ok": st == 0,
                                    "pages_combined": int(info.PagesCombined)}
                if st != 0:
                    errors.append(f"CombineMemory NTSTATUS=0x{st & 0xFFFFFFFF:08X}")
            except Exception as exc:
                steps["combine"] = {"ok": False, "error": str(exc)}
                errors.append(f"combine: {exc}")
        else:
            steps["combine"] = {"ok": True, "skipped": "os_too_old"}

        after = _ram_snapshot_mb()
        freed = max(0.0, before["used_mb"] - after["used_mb"])
        core_ok = any(steps.get(k, {}).get("ok")
                      for k in ("working_set", "system_file_cache",
                                "standby_priority0", "combine"))
        if not core_ok:
            return {
                "ok": False,
                "msg": "Cleanup had no effect — " + ("; ".join(errors[:3]) or "all native calls failed"),
                "freed_mb": 0.0,
                "before": before,
                "after": after,
                "steps": steps,
                "warnings": errors[:8],
                "source": source,
            }
        now = time.time()
        with self._lock:
            self._last_run_at = now
            self._last_freed_mb = freed
            self._last_status = {"source": source, "freed_mb": round(freed, 1),
                                 "before": before, "after": after, "steps": steps,
                                 "warnings": errors[:8]}
        return {
            "ok": True,
            "msg": f"Cleaned {freed:.1f} MB",
            "freed_mb": round(freed, 1),
            "before": before,
            "after": after,
            "steps": steps,
            "warnings": errors[:8],
            "source": source,
        }


RAM_CLEANUP = RamCleanupService()


def normalize_ram_cleanup_settings(source: Dict[str, Any]) -> Dict[str, Any]:
    threshold = _num(source.get("ram_cleanup_threshold_pct",
                                source.get("threshold_pct", 85.0)), 85.0)
    interval = _num(source.get("ram_cleanup_interval_min",
                               source.get("interval_min", 15)), 15)
    return {
        "enabled": bool(source.get("ram_cleanup_enabled", source.get("enabled", False))),
        "threshold_pct": max(50.0, min(95.0, threshold)),
        "interval_min": max(5, min(120, int(interval))),
    }
