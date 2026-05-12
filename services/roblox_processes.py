from __future__ import annotations

import getpass
import os
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

from core import Account, flog, flog_kv
from runtime.runtime_state_manager import RuntimeStateManager
from services.browser_tracker import extract_browser_tracker_id, tracker_matches
from services.resource_monitor import get_rt_monitor

ROBLOX_GAME_NAMES = {"robloxplayerbeta.exe"}
ROBLOX_NAMES = ROBLOX_GAME_NAMES | {"robloxplayer.exe", "roblox.exe"}

_RUNTIME_STATE = RuntimeStateManager(logger=flog_kv)
_rt_monitor = get_rt_monitor()
def _same_windows_user(process_user: str) -> bool:
    if not process_user:
        return True
    try:
        current = getpass.getuser().lower()
        user = str(process_user or "").replace("/", "\\").split("\\")[-1].lower()
        return bool(user and user == current)
    except Exception:
        return True
def get_process_identity(cls, pid: Optional[int]) -> str:
    if pid is None:
        return ""
    try:
        import psutil
        proc = psutil.Process(pid)
        created = float(proc.create_time() or 0.0)
        name = str(proc.name() or "").lower()
        exe = str(proc.exe() or "").lower()
        return f"{name}|{created:.6f}|{exe}"
    except Exception:
        return ""
def claim_pid_owner(cls, pid: Optional[int], owner_key: str):
    if not pid or not owner_key:
        return
    with cls._ownership_lock:
        cls._pid_owner[int(pid)] = str(owner_key)
def release_pid_owner(cls, pid: Optional[int], owner_key: Optional[str] = None):
    if not pid:
        return
    with cls._ownership_lock:
        current = cls._pid_owner.get(int(pid))
        if owner_key is None or current == owner_key:
            cls._pid_owner.pop(int(pid), None)
def get_pid_owner(cls, pid: Optional[int]) -> str:
    if not pid:
        return ""
    with cls._ownership_lock:
        return str(cls._pid_owner.get(int(pid)) or "")
def cleanup_stale_pid_claims(cls):
    with cls._ownership_lock:
        stale = [pid for pid in list(cls._pid_owner.keys()) if not cls.is_pid_alive(pid)]
        for pid in stale:
            cls._pid_owner.pop(pid, None)
def _iter_roblox_processes(cls, game_only: bool = False):
    try:
        import psutil
        allowed_names = ROBLOX_GAME_NAMES if game_only else ROBLOX_NAMES
        for proc in psutil.process_iter(["pid", "name", "create_time", "status"]):
            name = str(proc.info.get("name") or "").lower()
            if name in allowed_names:
                yield proc
    except ImportError:
        return
def _inspect_roblox_process(cls, proc) -> Dict[str, Any]:
    proc_info = getattr(proc, "info", {}) or {}
    name = str(proc_info.get("name") or proc.name() or "")
    name_l = name.lower()
    status = str(proc_info.get("status") or proc.status() or "")
    created = float(proc_info.get("create_time") or proc.create_time() or 0.0)
    exe = ""
    cmdline = ""
    username = ""
    exe_accessible = False
    cmdline_accessible = False
    username_accessible = False
    try:
        exe = str(proc.exe() or "")
        exe_accessible = True
    except Exception:
        exe = ""
    try:
        cmdline = " ".join(str(part) for part in (proc.cmdline() or []))
        cmdline_accessible = True
    except Exception:
        cmdline = ""
    try:
        username = str(proc.username() or "")
        username_accessible = True
    except Exception:
        username = ""
    try:
        rss_mb = float(proc.memory_info().rss / (1024 * 1024))
    except Exception:
        rss_mb = 0.0
    browser_tracker_id = extract_browser_tracker_id(cmdline)
    window_snapshot = cls._window_snapshot_for_pid(proc.pid)
    windows = int(window_snapshot.get("count") or 0)
    cpu = float(_rt_monitor.get_cpu(proc.pid))
    identity = cls.get_process_identity(proc.pid)
    exe_name = os.path.basename(exe).lower() if exe else ""
    exe_l = exe.lower()
    valid_exe = (
        exe_accessible and
        exe_name in ROBLOX_GAME_NAMES and
        ("\\roblox\\" in exe_l or "\\versions\\" in exe_l or "\\windowsapps\\" in exe_l)
    )
    valid_cmdline = cmdline_accessible and ((not cmdline) or ("roblox" in cmdline.lower()))
    valid_user = username_accessible and cls._same_windows_user(username)
    return {
        "pid": int(proc.pid),
        "name": name,
        "name_l": name_l,
        "status": status,
        "created": created,
        "exe": exe,
        "exe_name": exe_name,
        "cmdline": cmdline,
        "browser_tracker_id": browser_tracker_id,
        "username": username,
        "rss_mb": rss_mb,
        "windows": windows,
        "hwnd": int(window_snapshot.get("hwnd") or 0),
        "window_responsive": bool(window_snapshot.get("responsive")),
        "window_hung": bool(window_snapshot.get("hung")),
        "cpu": cpu,
        "identity": identity,
        "owner": cls.get_pid_owner(proc.pid),
        "valid_name": name_l in ROBLOX_GAME_NAMES,
        "valid_exe": valid_exe,
        "valid_cmdline": valid_cmdline,
        "valid_user": valid_user,
    }
def validate_game_process(
    cls,
    pid: Optional[int],
    owner_key: str = "",
    expected_identity: str = "",
    launched_after: Optional[float] = None,
    min_ram_mb: float = 20.0,
    expected_browser_tracker_id: str = "",
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "ok": False,
        "pid": pid,
        "reason": "",
        "confidence": 0.0,
        "identity": "",
        "name": "",
        "created": 0.0,
        "windows": 0,
        "hwnd": 0,
        "cpu": 0.0,
        "ram_mb": 0.0,
        "owner": "",
        "browser_tracker_id": "",
    }
    if not pid:
        result["reason"] = "missing_pid"
        return result
    try:
        import psutil
        proc = psutil.Process(int(pid))
        info = cls._inspect_roblox_process(proc)
        result.update({
            "identity": info["identity"],
            "name": info["name"],
            "created": info["created"],
            "windows": info["windows"],
            "hwnd": info["hwnd"],
            "cpu": round(float(info["cpu"]), 2),
            "ram_mb": round(float(info["rss_mb"]), 1),
            "owner": info["owner"],
            "browser_tracker_id": info["browser_tracker_id"],
        })
        if str(info["status"]).lower() == "zombie":
            result["reason"] = "zombie"
            return result
        if not info["valid_name"]:
            result["reason"] = "invalid_name"
            return result
        if not info["valid_exe"]:
            result["reason"] = f"invalid_exe:{info['exe_name']}"
            return result
        if not info["valid_cmdline"]:
            result["reason"] = "invalid_cmdline"
            return result
        if not info["valid_user"]:
            result["reason"] = "owner_user_mismatch"
            return result
        if launched_after and info["created"] and info["created"] < (float(launched_after) - 3.0):
            result["reason"] = "created_before_launch"
            return result
        owner = str(info["owner"] or "")
        if owner_key and owner and owner != owner_key:
            result["reason"] = f"owner_mismatch:{owner}"
            return result
        if expected_identity and info["identity"] and info["identity"] != expected_identity:
            result["reason"] = "identity_mismatch"
            return result
        observed_tracker = str(info.get("browser_tracker_id") or "")
        if expected_browser_tracker_id and observed_tracker and observed_tracker != str(expected_browser_tracker_id):
            result["reason"] = "browser_tracker_mismatch"
            return result
        if float(info["rss_mb"] or 0.0) < min_ram_mb and int(info["windows"] or 0) <= 0:
            result["reason"] = "low_ram"
            return result

        confidence = 30.0
        if info["valid_exe"]:
            confidence += 15.0
        if owner_key and owner == owner_key:
            confidence += 25.0
        if info["valid_user"]:
            confidence += 8.0
        if expected_identity and info["identity"] == expected_identity:
            confidence += 35.0
        if tracker_matches(expected_browser_tracker_id, observed_tracker):
            confidence += 40.0
        confidence += min(15.0, float(info["windows"]) * 7.0)
        confidence += min(12.0, float(info["rss_mb"]) / 120.0)
        confidence += min(10.0, float(info["cpu"]) * 2.0)
        result["ok"] = True
        result["reason"] = "ok"
        result["confidence"] = round(confidence, 1)
        result["confidence_level"] = cls.confidence_level(confidence)
        return result
    except ImportError:
        result["reason"] = "psutil_unavailable"
        return result
    except Exception as e:
        error_name = e.__class__.__name__.lower()
        if "nosuchprocess" in error_name:
            result["reason"] = "no_such_process"
        elif "accessdenied" in error_name:
            result["reason"] = "access_denied"
        else:
            result["reason"] = f"error:{e}"
        return result
def get_game_activity(cls, pid: Optional[int]) -> Dict[str, Any]:
    validation = cls.validate_game_process(pid, min_ram_mb=0.0)
    return {
        "alive": bool(validation.get("ok")),
        "windows": int(validation.get("windows") or 0),
        "cpu": float(validation.get("cpu") or 0.0),
        "ram_mb": float(validation.get("ram_mb") or 0.0),
        "reason": str(validation.get("reason") or ""),
    }
def get_pid_cmdline(cls, pid: Optional[int]) -> str:
    if not pid:
        return ""
    try:
        import psutil
        proc = psutil.Process(int(pid))
        return " ".join(str(part) for part in (proc.cmdline() or []))
    except Exception:
        return ""
def confidence_level(cls, confidence: float) -> str:
    value = float(confidence or 0.0)
    if value >= cls.HIGH_CONFIDENCE:
        return "HIGH_CONFIDENCE"
    if value >= cls.MEDIUM_CONFIDENCE:
        return "MEDIUM_CONFIDENCE"
    if value > 0:
        return "LOW_CONFIDENCE"
    return "UNTRUSTED"

def find_bound_game_process(
    cls,
    preferred_pid: Optional[int] = None,
    launched_after: Optional[float] = None,
    owner_key: str = "",
    expected_identity: str = "",
    expected_browser_tracker_id: str = "",
) -> Tuple[Optional[int], str]:
    try:
        import psutil
        candidates: List[Tuple[float, float, int, str]] = []
        cls.cleanup_stale_pid_claims()
        for proc in cls._iter_roblox_processes(game_only=True):
            try:
                info = cls._inspect_roblox_process(proc)
                pid = int(info["pid"])
                if preferred_pid and pid == preferred_pid and expected_identity:
                    validation = cls.validate_game_process(
                        pid,
                        owner_key=owner_key,
                        expected_identity=expected_identity,
                        launched_after=launched_after,
                        min_ram_mb=20.0,
                        expected_browser_tracker_id=expected_browser_tracker_id,
                    )
                    if not validation.get("ok"):
                        flog_kv(
                            "PROC",
                            "reject_preferred_pid",
                            "warning",
                            pid=pid,
                            reason=validation.get("reason", ""),
                        )
                        continue
                if str(info["status"]).lower() == "zombie":
                    continue
                created = float(info["created"] or 0.0)
                if launched_after and created and created < (launched_after - 3.0):
                    continue
                rss_mb = float(info["rss_mb"] or 0.0)
                if rss_mb < 50 and int(info["windows"] or 0) <= 0:
                    continue
                if not info["valid_name"] or not info["valid_exe"] or not info["valid_cmdline"] or not info["valid_user"]:
                    continue
                window_count = int(info["windows"] or 0)
                owner = str(info["owner"] or "")
                if owner and owner_key and owner != owner_key:
                    continue
                observed_tracker = str(info.get("browser_tracker_id") or "")
                if expected_browser_tracker_id and observed_tracker and observed_tracker != str(expected_browser_tracker_id):
                    continue
                identity = str(info["identity"] or "")
                score = (window_count * 100000.0) + rss_mb + created
                if owner_key and owner == owner_key:
                    score += 500000.0
                if preferred_pid and pid == preferred_pid:
                    score += 250000.0
                if expected_identity and identity == expected_identity:
                    score += 400000.0
                if tracker_matches(expected_browser_tracker_id, observed_tracker):
                    score += 650000.0
                candidates.append((score, created, pid, str(info["name"] or "")))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue

        if preferred_pid:
            for _score, _created, pid, name in candidates:
                if pid == preferred_pid:
                    return pid, name

        if not candidates:
            return None, ""

        candidates.sort(reverse=True)
        _score, _created, pid, name = candidates[0]
        return pid, name
    except ImportError:
        return None, ""

def summarize_game_presence(
    cls,
    launched_after: Optional[float] = None,
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "pids": [],
        "visible_windows": 0,
        "max_ram_mb": 0.0,
        "max_cpu": 0.0,
        "newest_created": 0.0,
    }
    try:
        import psutil
        for proc in cls._iter_roblox_processes(game_only=True):
            try:
                info = cls._inspect_roblox_process(proc)
                if not info["valid_name"] or not info["valid_exe"] or not info["valid_cmdline"] or not info["valid_user"]:
                    continue
                created = float(info["created"] or 0.0)
                if launched_after and created and created < (launched_after - 3.0):
                    continue
                status = str(info["status"] or "")
                if status == "zombie":
                    continue
                rss_mb = float(info["rss_mb"] or 0.0)
                if rss_mb < 20 and int(info["windows"] or 0) <= 0:
                    continue
                pid = int(info["pid"])
                summary["pids"].append(pid)
                summary["visible_windows"] += int(info["windows"] or 0)
                summary["max_ram_mb"] = max(float(summary["max_ram_mb"]), float(rss_mb))
                summary["max_cpu"] = max(float(summary["max_cpu"]), float(info["cpu"] or 0.0))
                summary["newest_created"] = max(float(summary["newest_created"] or 0.0), created)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue
    except ImportError:
        pass
    return summary

def list_live_game_processes(
    cls,
    launched_after: Optional[float] = None,
) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    try:
        import psutil
        for proc in cls._iter_roblox_processes(game_only=True):
            try:
                info = cls._inspect_roblox_process(proc)
                if not info["valid_name"] or not info["valid_exe"] or not info["valid_cmdline"] or not info["valid_user"]:
                    continue
                created = float(info["created"] or 0.0)
                if launched_after and created and created < (launched_after - 3.0):
                    continue
                status = str(info["status"] or "")
                if status == "zombie":
                    continue
                rss_mb = float(info["rss_mb"] or 0.0)
                if rss_mb < 50 and int(info["windows"] or 0) <= 0:
                    continue
                pid = int(info["pid"])
                entries.append({
                    "pid": pid,
                    "name": str(info["name"] or ""),
                    "created": created,
                    "rss_mb": float(rss_mb),
                    "windows": int(info["windows"] or 0),
                    "hwnd": int(info["hwnd"] or 0),
                    "cpu": float(info["cpu"] or 0.0),
                    "identity": str(info["identity"] or ""),
                    "owner": str(info["owner"] or ""),
                    "browser_tracker_id": str(info["browser_tracker_id"] or ""),
                    "exe": str(info["exe"] or ""),
                    "cmdline": str(info["cmdline"] or ""),
                    "username": str(info["username"] or ""),
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue
    except ImportError:
        return entries

    entries.sort(
        key=lambda item: (
            int(item.get("windows", 0)),
            float(item.get("rss_mb", 0.0)),
            float(item.get("created", 0.0)),
        ),
        reverse=True,
    )
    return entries

def snapshot_pids(cls) -> set:
    try:
        import psutil
        return {
            p.pid
            for p in psutil.process_iter(["pid", "name"])
            if (p.info.get("name") or "").lower() in ROBLOX_NAMES
        }
    except ImportError:
        return set()

def kill_all_roblox_clients(
    cls,
    wait_seconds: float = 4.0,
    exclude_pids: Optional[List[int]] = None,
) -> int:
    killed = 0
    try:
        import psutil
        excluded = {int(pid) for pid in (exclude_pids or []) if pid}
        victims = []
        for p in psutil.process_iter(["pid", "name"]):
            if (p.info.get("name") or "").lower() in ROBLOX_NAMES:
                if p.pid in excluded:
                    continue
                victims.append(p)
        for proc in victims:
            try:
                cls.evict_pid_cache(proc.pid)
                proc.terminate()
                killed += 1
            except Exception:
                continue
        if victims:
            gone, alive = psutil.wait_procs(victims, timeout=max(0.5, wait_seconds))
            for proc in alive:
                try:
                    proc.kill()
                except Exception:
                    pass
        return killed
    except ImportError:
        return 0
    except Exception as e:
        flog(f"[PROC] kill_all_roblox_clients error: {e}", "warning")
        return killed

def cleanup_extra_launch_processes(
    cls,
    before: set,
    keep_pids: Optional[List[int]] = None,
    launched_after: Optional[float] = None,
    wait_seconds: float = 2.0,
) -> int:
    killed = 0
    keep = {int(pid) for pid in (keep_pids or []) if pid}
    try:
        import psutil
        victims = []
        for proc in cls._iter_roblox_processes(game_only=True):
            try:
                pid = int(proc.pid)
                if pid in keep or pid in before:
                    continue
                created = float(proc.info.get("create_time") or proc.create_time())
                if launched_after and created and created < (launched_after - 3.0):
                    continue
                victims.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue

        for proc in victims:
            try:
                flog(f"[PROC] cleaning leftover launch PID {proc.pid}")
                cls.evict_pid_cache(proc.pid)
                proc.terminate()
                killed += 1
            except Exception:
                continue

        if victims:
            gone, alive = psutil.wait_procs(victims, timeout=max(0.5, wait_seconds))
            for proc in alive:
                try:
                    proc.kill()
                except Exception:
                    pass
    except ImportError:
        return 0
    except Exception as e:
        flog(f"[PROC] cleanup_extra_launch_processes error: {e}", "warning")
    return killed

def detect_new_pid(
    cls,
    before: set,
    timeout: float = 20.0,
    launched_after: Optional[float] = None,
    created_after_slack: float = 0.0,
    expected_browser_tracker_id: str = "",
) -> Optional[int]:
    try:
        deadline = time.time() + timeout
        first_seen: Dict[int, float] = {}
        settle_seconds = 2.0
        created_threshold = None
        if launched_after:
            created_threshold = float(launched_after) - max(0.0, float(created_after_slack or 0.0))
        while time.time() < deadline:
            now = time.time()
            live_new_pids = set()
            for entry in cls.list_live_game_processes(launched_after=None):
                pid = int(entry.get("pid") or 0)
                created = float(entry.get("created") or 0.0)
                if not pid or pid in before:
                    continue
                if created_threshold is not None and created and created < created_threshold:
                    continue
                live_new_pids.add(pid)
                first_seen.setdefault(pid, now)
                if (now - first_seen[pid]) >= settle_seconds:
                    validation = cls.validate_game_process(
                        pid,
                        launched_after=None,
                        min_ram_mb=20.0,
                        expected_browser_tracker_id=expected_browser_tracker_id,
                    )
                    if not validation.get("ok"):
                        flog_kv(
                            "PROC",
                            "reject_detected_pid",
                            "warning",
                            pid=pid,
                            reason=validation.get("reason", ""),
                        )
                        continue
                    flog_kv(
                        "PROC",
                        "stable_pid_detected",
                        pid=pid,
                        name=entry.get("name") or "unknown",
                        confidence=validation.get("confidence", 0.0),
                    )
                    return pid
            first_seen = {pid: ts for pid, ts in first_seen.items() if pid in live_new_pids}
            time.sleep(0.5)
    except Exception:
        pass
    return None

def is_pid_alive(cls, pid: Optional[int]) -> bool:
    if pid is None:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != "zombie"
    except Exception:
        return False

def is_bound_game_alive(
    cls,
    pid: Optional[int],
    owner_key: str = "",
    expected_identity: str = "",
    expected_browser_tracker_id: str = "",
) -> bool:
    validation = cls.validate_game_process(
        pid,
        owner_key=owner_key,
        expected_identity=expected_identity,
        min_ram_mb=0.0,
        expected_browser_tracker_id=expected_browser_tracker_id,
    )
    return bool(validation.get("ok"))

def get_pid_cpu(cls, pid: int, interval: float = 0.0) -> float:
    """ดึง CPU% จาก RealtimeResourceMonitor (realtime, non-blocking)"""
    return _rt_monitor.get_cpu(pid)

def get_pid_memory_mb(cls, pid: int) -> float:
    """ดึง RAM MB จาก RealtimeResourceMonitor (realtime, non-blocking)"""
    return _rt_monitor.get_ram(pid)

def evict_pid_cache(cls, pid: Optional[int]):
    if pid is not None:
        with cls._cache_lock:
            cls._process_cache.pop(pid, None)
            cls._nr_cache.pop(pid, None)
        cls.release_pid_owner(pid)
        _rt_monitor.unregister(pid)

def kill_pid(cls, pid: Optional[int]) -> bool:
    if not pid:
        return False
    cls.evict_pid_cache(pid)
    try:
        import psutil
        try:
            p = psutil.Process(pid)
            p.terminate()
            p.wait(timeout=3)
            return True
        except psutil.TimeoutExpired:
            p.kill()
            return True
        except psutil.NoSuchProcess:
            return True
        except (psutil.AccessDenied, psutil.ZombieProcess) as e:
            flog(f"[PROC] kill_pid access error for PID {pid}: {e}", "warning")
            return False
        except Exception as e:
            flog(f"[PROC] kill_pid error for PID {pid}: {e}", "warning")
            return False
    except ImportError:
        try:
            subprocess.call(
                ["taskkill", "/F", "/PID", str(pid)],
                creationflags=subprocess.CREATE_NO_WINDOW,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            return False
