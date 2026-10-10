"""Per-process trim — EmptyWorkingSet เฉพาะ PID Roblox.

ต่างจาก services/ram_cleanup.py ที่ล้างทั้งเครื่อง (global clean ต้อง admin
และสะเทือนทุกโปรเซส) ตัวนี้เลือก trim ทีละจอ:

    psapi.EmptyWorkingSet(hProcess) -> เอาเพจที่ไม่ได้ใช้ของ PID นั้น
    ออกจาก RAM ไป standby ชั่วคราว ไม่ได้ปิดโปรเซส ไม่ได้แก้ leak

กฎความปลอดภัย:
- ไม่ต้อง admin (ขอแค่ PROCESS_SET_QUOTA ของโปรเซสตัวเอง)
- trim เฉพาะ PID ที่เป็น RobloxPlayerBeta.exe และ rss >= threshold
- cooldown ราย PID (กัน fault storm) + จำกัดจำนวนต่อรอบ (ทีละ 1)
- ข้าม PID เพิ่งเปิด (<120s, กำลังโหลดแมพ) และข้ามถ้า global clean
  เพิ่งรันมา (<120s) เพื่อไม่ให้ page fault ซ้อนกัน
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from typing import Any, Dict, Iterable, List, Optional

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None  # type: ignore

ROBLOX_PROCESS_NAME = "robloxplayerbeta.exe"

TRIM_THRESHOLD_MB_DEFAULT = 1200
TRIM_THRESHOLD_MB_MIN = 256
TRIM_THRESHOLD_MB_MAX = 8192
TRIM_COOLDOWN_SEC_DEFAULT = 300
TRIM_COOLDOWN_SEC_MIN = 60
TRIM_COOLDOWN_SEC_MAX = 3600
TRIM_MAX_PER_CYCLE_DEFAULT = 1
TRIM_MAX_PER_CYCLE_MIN = 1
TRIM_MAX_PER_CYCLE_MAX = 10
TRIM_CYCLE_THROTTLE_SEC = 30.0
TRIM_MIN_PID_AGE_SEC = 120.0
TRIM_GLOBAL_CLEAN_GAP_SEC = 120.0

_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_INFORMATION = 0x0400


def _num(value: Any, default: float) -> float:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        return float(value)
    except Exception:
        return default


def normalize_process_trim_settings(source: Dict[str, Any]) -> Dict[str, Any]:
    threshold = _num(
        source.get("process_trim_threshold_mb", source.get("threshold_mb", TRIM_THRESHOLD_MB_DEFAULT)),
        TRIM_THRESHOLD_MB_DEFAULT,
    )
    cooldown = _num(
        source.get("process_trim_cooldown_sec", source.get("cooldown_sec", TRIM_COOLDOWN_SEC_DEFAULT)),
        TRIM_COOLDOWN_SEC_DEFAULT,
    )
    try:
        max_per_cycle = int(float(source.get("process_trim_max_per_cycle", source.get("max_per_cycle", TRIM_MAX_PER_CYCLE_DEFAULT))))
    except Exception:
        max_per_cycle = TRIM_MAX_PER_CYCLE_DEFAULT
    return {
        "enabled": bool(source.get("process_trim_enabled", source.get("enabled", False))),
        "threshold_mb": int(max(TRIM_THRESHOLD_MB_MIN, min(TRIM_THRESHOLD_MB_MAX, threshold))),
        "cooldown_sec": int(max(TRIM_COOLDOWN_SEC_MIN, min(TRIM_COOLDOWN_SEC_MAX, cooldown))),
        "max_per_cycle": int(max(TRIM_MAX_PER_CYCLE_MIN, min(TRIM_MAX_PER_CYCLE_MAX, max_per_cycle))),
    }


def _account_username(account: Any) -> str:
    return str(getattr(account, "_config_username", "") or getattr(account, "username", "") or "")


def _account_display(account: Any) -> str:
    return str(getattr(account, "display_name", "") or getattr(account, "display", "") or _account_username(account))


def _account_pid(account: Any) -> int:
    try:
        return int(getattr(account, "pid", 0) or 0)
    except Exception:
        return 0


def get_pid_rss_mb(pid: int) -> float:
    if psutil is None:
        return 0.0
    try:
        return float(psutil.Process(int(pid)).memory_info().rss) / (1024 * 1024)
    except Exception:
        return 0.0


def get_pid_age_sec(pid: int) -> float:
    if psutil is None:
        return 0.0
    try:
        return max(0.0, time.time() - float(psutil.Process(int(pid)).create_time()))
    except Exception:
        return 0.0


def is_roblox_pid(pid: int) -> bool:
    if psutil is None:
        return False
    try:
        proc = psutil.Process(int(pid))
        return proc.is_running() and str(proc.name() or "").lower() == ROBLOX_PROCESS_NAME
    except Exception:
        return False


def trim_pid(pid: int) -> bool:
    """Trim working set ของ PID เดียว. คืน True ถ้าสำเร็จ."""
    pid = int(pid or 0)
    if not pid or os.name != "nt":
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
        psapi.EmptyWorkingSet.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_QUERY_INFORMATION, False, int(pid)
        )
        if not handle:
            return False
        try:
            return bool(psapi.EmptyWorkingSet(handle))
        finally:
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass
    except Exception:
        return False


class ProcessTrimService:
    """Stateful: cooldown ราย PID + throttle รอบ + สถิติ."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._last_trim_at: Dict[int, float] = {}
        self._last_cycle_at = 0.0
        self._total_trimmed = 0
        self._total_freed_mb = 0.0
        self._last_result: Dict[str, Any] = {}

    def _global_clean_recent(self, now: float) -> float:
        try:
            from services.ram_cleanup import RAM_CLEANUP

            last_global = float(RAM_CLEANUP.get_last_run_at() or 0.0)
            if last_global and (now - last_global) < TRIM_GLOBAL_CLEAN_GAP_SEC:
                return max(0.0, TRIM_GLOBAL_CLEAN_GAP_SEC - (now - last_global))
        except Exception:
            pass
        return 0.0

    def snapshot(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        settings = normalize_process_trim_settings(cfg)
        with self._lock:
            last_cycle = self._last_cycle_at
            total_trimmed = self._total_trimmed
            total_freed = self._total_freed_mb
            last = dict(self._last_result)
        return {
            "ok": True,
            **settings,
            "cycle_throttle_sec": TRIM_CYCLE_THROTTLE_SEC,
            "min_pid_age_sec": TRIM_MIN_PID_AGE_SEC,
            "global_clean_gap_sec": TRIM_GLOBAL_CLEAN_GAP_SEC,
            "requires_admin": False,
            "last_cycle_at": last_cycle,
            "total_trimmed": total_trimmed,
            "total_freed_mb": round(float(total_freed), 1),
            "last": last,
        }

    def apply(self, accounts: Iterable[Any], cfg: Dict[str, Any], now: Optional[float] = None,
              bypass_throttle: bool = False) -> Dict[str, Any]:
        settings = normalize_process_trim_settings(cfg)
        now = now if now is not None else time.time()
        if not settings["enabled"]:
            return {"ok": True, **settings, "trimmed": 0, "skipped": 0, "rows": [], "reason": "disabled"}
        if os.name != "nt":
            return {"ok": False, **settings, "trimmed": 0, "reason": "windows_only", "rows": []}
        if psutil is None:
            return {"ok": False, **settings, "trimmed": 0, "reason": "psutil_unavailable", "rows": []}

        with self._lock:
            if not bypass_throttle and self._last_cycle_at and (now - self._last_cycle_at) < TRIM_CYCLE_THROTTLE_SEC:
                return {"ok": True, **settings, "trimmed": 0, "reason": "throttled", "rows": []}
            self._last_cycle_at = now

        gap_remaining = self._global_clean_recent(now)
        if gap_remaining > 0:
            return {"ok": True, **settings, "trimmed": 0, "reason": "global_clean_recent",
                    "gap_remaining_sec": round(gap_remaining, 1), "rows": []}

        threshold = float(settings["threshold_mb"])
        cooldown = float(settings["cooldown_sec"])
        candidates: List[Dict[str, Any]] = []
        skipped = 0
        for account in list(accounts or []):
            pid = _account_pid(account)
            if not pid:
                continue
            if not is_roblox_pid(pid):
                continue
            age = get_pid_age_sec(pid)
            if age < TRIM_MIN_PID_AGE_SEC:
                skipped += 1
                continue
            with self._lock:
                last_trim = float(self._last_trim_at.get(pid) or 0.0)
            if last_trim and (now - last_trim) < cooldown:
                skipped += 1
                continue
            rss = get_pid_rss_mb(pid)
            if rss < threshold:
                skipped += 1
                continue
            candidates.append({
                "account": _account_username(account),
                "display": _account_display(account),
                "pid": pid,
                "rss_mb": round(rss, 1),
            })
        candidates.sort(key=lambda r: float(r.get("rss_mb") or 0.0), reverse=True)
        targets = candidates[: int(settings["max_per_cycle"])]

        rows: List[Dict[str, Any]] = []
        for item in targets:
            pid = int(item["pid"])
            before = get_pid_rss_mb(pid)
            ok = trim_pid(pid)
            # ให้ OS มีเวลาปรับ working set ก่อนวัดหลัง
            time.sleep(0.3)
            after = get_pid_rss_mb(pid)
            freed = max(0.0, before - after) if ok else 0.0
            with self._lock:
                if ok:
                    self._last_trim_at[pid] = now
                    self._total_trimmed += 1
                    self._total_freed_mb += freed
            rows.append({
                **item,
                "ok": bool(ok),
                "before_mb": round(before, 1),
                "after_mb": round(after, 1),
                "freed_mb": round(freed, 1),
            })
        #  prune dead pids จาก cooldown map กันโตไม่จำกัด
        try:
            with self._lock:
                for pid in list(self._last_trim_at):
                    try:
                        alive = psutil.Process(int(pid)).is_running()
                    except Exception:
                        alive = False
                    if not alive:
                        self._last_trim_at.pop(pid, None)
        except Exception:
            pass

        trimmed_ok = sum(1 for r in rows if r.get("ok"))
        if rows and trimmed_ok == 0:
            reason = "trim_failed"
        else:
            reason = "ok" if rows else "below_threshold"
        result = {"ok": True, **settings, "trimmed": trimmed_ok,
                  "skipped": skipped, "candidates": len(candidates), "rows": rows,
                  "reason": reason}
        with self._lock:
            self._last_result = dict(result)
        return result

    def trim_now(self, pid: int) -> Dict[str, Any]:
        pid = int(pid or 0)
        if not pid:
            return {"ok": False, "reason": "missing_pid"}
        if os.name != "nt":
            return {"ok": False, "reason": "windows_only"}
        if not is_roblox_pid(pid):
            return {"ok": False, "reason": "not_roblox_pid", "pid": pid}
        before = get_pid_rss_mb(pid)
        ok = trim_pid(pid)
        time.sleep(0.3)
        after = get_pid_rss_mb(pid)
        freed = max(0.0, before - after) if ok else 0.0
        result: Dict[str, Any] = {"ok": bool(ok), "pid": pid, "before_mb": round(before, 1),
                "after_mb": round(after, 1), "freed_mb": round(freed, 1)}
        if not ok:
            result["reason"] = "trim_failed"
        if ok:
            with self._lock:
                self._last_trim_at[pid] = time.time()
                self._total_trimmed += 1
                self._total_freed_mb += freed
                self._last_result = {"ok": True, "trimmed": 1, "rows": [dict(result)],
                                     "reason": "manual_trim_now"}
        return result


PROCESS_TRIM = ProcessTrimService()
