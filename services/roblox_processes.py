from __future__ import annotations

import getpass
import os
import subprocess
import time
from typing import Any

from core import flog, flog_kv
from runtime.runtime_state_manager import RuntimeStateManager
from services.browser_tracker import extract_browser_tracker_id, tracker_matches
from services.process_proof_policy import score_resource_signals
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
def get_process_identity(cls, pid: int | None) -> str:
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
def claim_pid_owner(cls, pid: int | None, owner_key: str):
    if not pid or not owner_key:
        return
    with cls._ownership_lock:
        cls._pid_owner[int(pid)] = str(owner_key)
def release_pid_owner(cls, pid: int | None, owner_key: str | None = None):
    if not pid:
        return
    with cls._ownership_lock:
        current = cls._pid_owner.get(int(pid))
        if owner_key is None or current == owner_key:
            cls._pid_owner.pop(int(pid), None)
def get_pid_owner(cls, pid: int | None) -> str:
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
def _process_exe(proc) -> tuple[str, bool]:
    try:
        return str(proc.exe() or ""), True
    except Exception:
        return "", False


def _process_cmdline(proc) -> tuple[str, bool]:
    try:
        return " ".join(str(part) for part in (proc.cmdline() or [])), True
    except Exception:
        return "", False


def _process_username(proc) -> tuple[str, bool]:
    try:
        return str(proc.username() or ""), True
    except Exception:
        return "", False


def _process_rss_mb(proc) -> float:
    try:
        return float(proc.memory_info().rss / (1024 * 1024))
    except Exception:
        return 0.0


def _valid_exe(exe: str, accessible: bool) -> bool:
    if not accessible:
        return False
    exe_name = os.path.basename(exe).lower() if exe else ""
    if exe_name not in ROBLOX_GAME_NAMES:
        return False
    exe_l = exe.lower()
    return (
        "\\roblox\\" in exe_l
        or "\\versions\\" in exe_l
        or "\\windowsapps\\" in exe_l
    )


def _valid_cmdline(cmdline: str, accessible: bool) -> bool:
    if not accessible:
        return False
    if not cmdline:
        return True
    return "roblox" in cmdline.lower()


def _process_header(proc) -> dict[str, Any]:
    proc_info = getattr(proc, "info", {}) or {}
    name = str(proc_info.get("name") or proc.name() or "")
    status = str(proc_info.get("status") or proc.status() or "")
    created = float(proc_info.get("create_time") or proc.create_time() or 0.0)
    return {
        "name": name,
        "name_l": name.lower(),
        "status": status,
        "created": created,
    }


def _inspect_roblox_process(cls, proc) -> dict[str, Any]:
    header = _process_header(proc)
    exe, exe_accessible = _process_exe(proc)
    cmdline, cmdline_accessible = _process_cmdline(proc)
    username, username_accessible = _process_username(proc)
    rss_mb = _process_rss_mb(proc)
    browser_tracker_id = extract_browser_tracker_id(cmdline)
    window_snapshot = cls._window_snapshot_for_pid(proc.pid)
    windows = int(window_snapshot.get("count") or 0)
    return {
        "pid": int(proc.pid),
        "name": header["name"],
        "name_l": header["name_l"],
        "status": header["status"],
        "created": header["created"],
        "exe": exe,
        "exe_name": os.path.basename(exe).lower() if exe else "",
        "cmdline": cmdline,
        "browser_tracker_id": browser_tracker_id,
        "username": username,
        "rss_mb": rss_mb,
        "windows": windows,
        "hwnd": int(window_snapshot.get("hwnd") or 0),
        "window_responsive": bool(window_snapshot.get("responsive")),
        "window_hung": bool(window_snapshot.get("hung")),
        "cpu": float(_rt_monitor.get_cpu(proc.pid)),
        "identity": cls.get_process_identity(proc.pid),
        "owner": cls.get_pid_owner(proc.pid),
        "valid_name": header["name_l"] in ROBLOX_GAME_NAMES,
        "valid_exe": _valid_exe(exe, exe_accessible),
        "valid_cmdline": _valid_cmdline(cmdline, cmdline_accessible),
        "valid_user": bool(
            username_accessible and cls._same_windows_user(username)
        ),
    }
def _validation_result(pid: int | None) -> dict[str, Any]:
    return {
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


def _basic_validation_reason(info: dict[str, Any]) -> str:
    if str(info["status"]).lower() == "zombie":
        return "zombie"
    if not info["valid_name"]:
        return "invalid_name"
    if not info["valid_exe"]:
        return f"invalid_exe:{info['exe_name']}"
    if not info["valid_cmdline"]:
        return "invalid_cmdline"
    if not info["valid_user"]:
        return "owner_user_mismatch"
    return ""


def _launch_time_reason(info: dict[str, Any], launched_after: float | None) -> str:
    if not launched_after:
        return ""
    if not info["created"]:
        return ""
    if info["created"] < (float(launched_after) - 3.0):
        return "created_before_launch"
    return ""


def _owner_identity_reason(
    info: dict[str, Any], owner_key: str, expected_identity: str
) -> str:
    owner = str(info["owner"] or "")
    if owner_key and owner and owner != owner_key:
        return f"owner_mismatch:{owner}"
    if expected_identity and info["identity"] and info["identity"] != expected_identity:
        return "identity_mismatch"
    return ""


def _tracker_identity_reason(
    info: dict[str, Any], expected_browser_tracker_id: str
) -> str:
    observed_tracker = str(info.get("browser_tracker_id") or "")
    if (
        expected_browser_tracker_id
        and observed_tracker
        and observed_tracker != str(expected_browser_tracker_id)
    ):
        return "browser_tracker_mismatch"
    return ""


def _identity_validation_reason(
    info: dict[str, Any],
    owner_key: str,
    expected_identity: str,
    expected_browser_tracker_id: str,
) -> str:
    return _owner_identity_reason(
        info, owner_key, expected_identity
    ) or _tracker_identity_reason(info, expected_browser_tracker_id)


def _resource_validation_reason(info: dict[str, Any], min_ram_mb: float) -> str:
    if float(info["rss_mb"] or 0.0) < min_ram_mb and int(info["windows"] or 0) <= 0:
        return "low_ram"
    return ""


def _validation_reason(
    info: dict[str, Any],
    owner_key: str,
    expected_identity: str,
    launched_after: float | None,
    min_ram_mb: float,
    expected_browser_tracker_id: str,
) -> str:
    reasons = (
        _basic_validation_reason(info),
        _launch_time_reason(info, launched_after),
        _identity_validation_reason(
            info, owner_key, expected_identity, expected_browser_tracker_id
        ),
        _resource_validation_reason(info, min_ram_mb),
    )
    for reason in reasons:
        if reason:
            return reason
    return ""


def _validation_confidence(
    info: dict[str, Any],
    owner_key: str,
    expected_identity: str,
    expected_browser_tracker_id: str,
) -> float:
    confidence = 30.0
    if info["valid_exe"]:
        confidence += 15.0
    if owner_key and info["owner"] == owner_key:
        confidence += 25.0
    if info["valid_user"]:
        confidence += 8.0
    if expected_identity and info["identity"] == expected_identity:
        confidence += 35.0
    observed_tracker = str(info.get("browser_tracker_id") or "")
    if tracker_matches(expected_browser_tracker_id, observed_tracker):
        confidence += 40.0
    confidence += score_resource_signals(info["windows"], info["rss_mb"], info["cpu"])
    return confidence


def _validation_error_reason(error: Exception) -> str:
    error_name = error.__class__.__name__.lower()
    if "nosuchprocess" in error_name:
        return "no_such_process"
    if "accessdenied" in error_name:
        return "access_denied"
    return f"error:{error}"


def validate_game_process(
    cls,
    pid: int | None,
    owner_key: str = "",
    expected_identity: str = "",
    launched_after: float | None = None,
    min_ram_mb: float = 20.0,
    expected_browser_tracker_id: str = "",
) -> dict[str, Any]:
    result = _validation_result(pid)
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
        reason = _validation_reason(
            info,
            owner_key,
            expected_identity,
            launched_after,
            min_ram_mb,
            expected_browser_tracker_id,
        )
        if reason:
            result["reason"] = reason
            return result
        confidence = _validation_confidence(
            info, owner_key, expected_identity, expected_browser_tracker_id
        )
        result["ok"] = True
        result["reason"] = "ok"
        result["confidence"] = round(confidence, 1)
        result["confidence_level"] = cls.confidence_level(confidence)
        return result
    except ImportError:
        result["reason"] = "psutil_unavailable"
        return result
    except Exception as e:
        result["reason"] = _validation_error_reason(e)
        return result
def get_game_activity(cls, pid: int | None) -> dict[str, Any]:
    validation = cls.validate_game_process(pid, min_ram_mb=0.0)
    return {
        "alive": bool(validation.get("ok")),
        "windows": int(validation.get("windows") or 0),
        "cpu": float(validation.get("cpu") or 0.0),
        "ram_mb": float(validation.get("ram_mb") or 0.0),
        "reason": str(validation.get("reason") or ""),
    }
def get_pid_cmdline(cls, pid: int | None) -> str:
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

def _candidate_created_ok(info: dict[str, Any], launched_after: float | None) -> bool:
    created = float(info["created"] or 0.0)
    if not launched_after or not created:
        return True
    return created >= (launched_after - 3.0)


def _candidate_ram_ok(info: dict[str, Any], min_ram_mb: float) -> bool:
    rss_mb = float(info["rss_mb"] or 0.0)
    return rss_mb >= min_ram_mb or int(info["windows"] or 0) > 0


def _candidate_valid(info: dict[str, Any]) -> bool:
    return bool(
        info["valid_name"]
        and info["valid_exe"]
        and info["valid_cmdline"]
        and info["valid_user"]
    )


def _live_base_ok(
    info: dict[str, Any], launched_after: float | None, min_ram_mb: float
) -> bool:
    if str(info["status"]).lower() == "zombie":
        return False
    if not _candidate_created_ok(info, launched_after):
        return False
    return _candidate_ram_ok(info, min_ram_mb) and _candidate_valid(info)


def _candidate_identity_ok(
    info: dict[str, Any], owner_key: str, expected_browser_tracker_id: str
) -> bool:
    owner = str(info["owner"] or "")
    if owner and owner_key and owner != owner_key:
        return False
    observed_tracker = str(info.get("browser_tracker_id") or "")
    if (
        expected_browser_tracker_id
        and observed_tracker
        and observed_tracker != str(expected_browser_tracker_id)
    ):
        return False
    return True


def _tracker_bonus(
    expected_browser_tracker_id: str, info: dict[str, Any]
) -> float:
    observed_tracker = str(info.get("browser_tracker_id") or "")
    if tracker_matches(expected_browser_tracker_id, observed_tracker):
        return 650000.0
    return 0.0


def _candidate_bonus(
    info: dict[str, Any],
    owner_key: str,
    preferred_pid: int | None,
    expected_identity: str,
    expected_browser_tracker_id: str,
) -> float:
    bonus = 0.0
    owner = str(info["owner"] or "")
    if owner_key and owner == owner_key:
        bonus += 500000.0
    if preferred_pid and int(info["pid"]) == preferred_pid:
        bonus += 250000.0
    if expected_identity and info["identity"] == expected_identity:
        bonus += 400000.0
    return bonus + _tracker_bonus(expected_browser_tracker_id, info)


def _candidate_score(
    info: dict[str, Any],
    owner_key: str,
    preferred_pid: int | None,
    expected_identity: str,
    expected_browser_tracker_id: str,
) -> float:
    window_count = int(info["windows"] or 0)
    rss_mb = float(info["rss_mb"] or 0.0)
    created = float(info["created"] or 0.0)
    base = (window_count * 100000.0) + rss_mb + created
    return base + _candidate_bonus(
        info, owner_key, preferred_pid, expected_identity, expected_browser_tracker_id
    )


def _candidate_from_info(
    info: dict[str, Any],
    owner_key: str,
    preferred_pid: int | None,
    launched_after: float | None,
    expected_identity: str,
    expected_browser_tracker_id: str,
) -> tuple[float, float, int, str] | None:
    if not _live_base_ok(info, launched_after, 50.0):
        return None
    if not _candidate_identity_ok(info, owner_key, expected_browser_tracker_id):
        return None
    score = _candidate_score(
        info, owner_key, preferred_pid, expected_identity, expected_browser_tracker_id
    )
    return (
        score,
        float(info["created"] or 0.0),
        int(info["pid"]),
        str(info["name"] or ""),
    )


def _preferred_pid_rejected(
    cls,
    info: dict[str, Any],
    preferred_pid: int | None,
    owner_key: str,
    expected_identity: str,
    launched_after: float | None,
    expected_browser_tracker_id: str,
) -> bool:
    if (
        not preferred_pid
        or int(info["pid"]) != preferred_pid
        or not expected_identity
    ):
        return False
    validation = cls.validate_game_process(
        int(info["pid"]),
        owner_key=owner_key,
        expected_identity=expected_identity,
        launched_after=launched_after,
        min_ram_mb=20.0,
        expected_browser_tracker_id=expected_browser_tracker_id,
    )
    if validation.get("ok"):
        return False
    flog_kv(
        "PROC",
        "reject_preferred_pid",
        "warning",
        pid=int(info["pid"]),
        reason=validation.get("reason", ""),
    )
    return True


def _preferred_candidate(
    candidates: list[tuple[float, float, int, str]], preferred_pid: int | None
) -> tuple[int, str] | None:
    if not preferred_pid:
        return None
    for _score, _created, pid, name in candidates:
        if pid == preferred_pid:
            return pid, name
    return None


def find_bound_game_process(
    cls,
    preferred_pid: int | None = None,
    launched_after: float | None = None,
    owner_key: str = "",
    expected_identity: str = "",
    expected_browser_tracker_id: str = "",
) -> tuple[int | None, str]:
    try:
        import psutil
        candidates: list[tuple[float, float, int, str]] = []
        cls.cleanup_stale_pid_claims()
        for proc in cls._iter_roblox_processes(game_only=True):
            try:
                info = cls._inspect_roblox_process(proc)
                if _preferred_pid_rejected(
                    cls,
                    info,
                    preferred_pid,
                    owner_key,
                    expected_identity,
                    launched_after,
                    expected_browser_tracker_id,
                ):
                    continue
                candidate = _candidate_from_info(
                    info,
                    owner_key,
                    preferred_pid,
                    launched_after,
                    expected_identity,
                    expected_browser_tracker_id,
                )
                if candidate is not None:
                    candidates.append(candidate)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue

        preferred = _preferred_candidate(candidates, preferred_pid)
        if preferred is not None:
            return preferred
        if not candidates:
            return None, ""

        candidates.sort(reverse=True)
        _score, _created, pid, name = candidates[0]
        return pid, name
    except ImportError:
        return None, ""

def _live_game_info(
    cls, proc, launched_after: float | None, min_ram_mb: float
) -> dict[str, Any] | None:
    info = cls._inspect_roblox_process(proc)
    if not _live_base_ok(info, launched_after, min_ram_mb):
        return None
    return info


def _accumulate_presence(summary: dict[str, Any], info: dict[str, Any]) -> None:
    pid = int(info["pid"])
    windows = int(info["windows"] or 0)
    rss_mb = float(info["rss_mb"] or 0.0)
    created = float(info["created"] or 0.0)
    summary["pids"].append(pid)
    summary["visible_windows"] += windows
    summary["max_ram_mb"] = max(float(summary["max_ram_mb"]), rss_mb)
    summary["max_cpu"] = max(float(summary["max_cpu"]), float(info["cpu"] or 0.0))
    summary["newest_created"] = max(
        float(summary["newest_created"] or 0.0), created
    )


def summarize_game_presence(
    cls,
    launched_after: float | None = None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
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
                info = _live_game_info(cls, proc, launched_after, 20.0)
                if info is not None:
                    _accumulate_presence(summary, info)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue
    except ImportError:
        "intentional: ignore exception"
    return summary


def _process_text(value: Any) -> str:
    return str(value or "")


def _game_entry(info: dict[str, Any]) -> dict[str, Any]:
    return {
        "pid": int(info["pid"]),
        "name": _process_text(info["name"]),
        "created": float(info["created"] or 0.0),
        "rss_mb": float(info["rss_mb"] or 0.0),
        "windows": int(info["windows"] or 0),
        "hwnd": int(info["hwnd"] or 0),
        "cpu": float(info["cpu"] or 0.0),
        "identity": _process_text(info["identity"]),
        "owner": _process_text(info["owner"]),
        "browser_tracker_id": _process_text(info["browser_tracker_id"]),
        "exe": _process_text(info["exe"]),
        "cmdline": _process_text(info["cmdline"]),
        "username": _process_text(info["username"]),
    }


def _game_entry_sort_key(item: dict[str, Any]) -> tuple[int, float, float]:
    return (
        int(item.get("windows", 0)),
        float(item.get("rss_mb", 0.0)),
        float(item.get("created", 0.0)),
    )


def list_live_game_processes(
    cls,
    launched_after: float | None = None,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        import psutil
        for proc in cls._iter_roblox_processes(game_only=True):
            try:
                info = _live_game_info(cls, proc, launched_after, 50.0)
                if info is not None:
                    entries.append(_game_entry(info))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue
    except ImportError:
        return entries

    entries.sort(key=_game_entry_sort_key, reverse=True)
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

def _roblox_victims(psutil, excluded: set[int]) -> list[Any]:
    victims = []
    for p in psutil.process_iter(["pid", "name"]):
        if (p.info.get("name") or "").lower() in ROBLOX_NAMES:
            if p.pid in excluded:
                continue
            victims.append(p)
    return victims


def _terminate_victims(
    cls, psutil, victims: list[Any], wait_seconds: float
) -> int:
    killed = 0
    for proc in victims:
        try:
            cls.evict_pid_cache(proc.pid)
            proc.terminate()
            killed += 1
        except Exception:
            continue
    try:
        if victims:
            _gone, alive = psutil.wait_procs(
                victims, timeout=max(0.5, wait_seconds)
            )
            for proc in alive:
                try:
                    proc.kill()
                except Exception:
                    "intentional: ignore exception"
    except Exception:
        "intentional: ignore exception"
    return killed


def kill_all_roblox_clients(
    cls,
    wait_seconds: float = 4.0,
    exclude_pids: list[int] | None = None,
) -> int:
    try:
        import psutil
        excluded = {int(pid) for pid in (exclude_pids or []) if pid}
        victims = _roblox_victims(psutil, excluded)
        return _terminate_victims(cls, psutil, victims, wait_seconds)
    except ImportError:
        return 0
    except Exception as e:
        flog(f"[PROC] kill_all_roblox_clients error: {e}", "warning")
        return 0

def _leftover_victims(
    cls,
    psutil,
    before: set,
    keep: set[int],
    launched_after: float | None,
) -> list[Any]:
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
    return victims


def _cleanup_victims(
    cls, psutil, victims: list[Any], wait_seconds: float
) -> int:
    killed = 0
    for proc in victims:
        try:
            flog(f"[PROC] cleaning leftover launch PID {proc.pid}")
            cls.evict_pid_cache(proc.pid)
            proc.terminate()
            killed += 1
        except Exception:
            continue
    try:
        if victims:
            _gone, alive = psutil.wait_procs(
                victims, timeout=max(0.5, wait_seconds)
            )
            for proc in alive:
                try:
                    proc.kill()
                except Exception:
                    "intentional: ignore exception"
    except Exception:
        "intentional: ignore exception"
    return killed


def cleanup_extra_launch_processes(
    cls,
    before: set,
    keep_pids: list[int] | None = None,
    launched_after: float | None = None,
    wait_seconds: float = 2.0,
) -> int:
    keep = {int(pid) for pid in (keep_pids or []) if pid}
    try:
        import psutil
        victims = _leftover_victims(cls, psutil, before, keep, launched_after)
        return _cleanup_victims(cls, psutil, victims, wait_seconds)
    except ImportError:
        return 0
    except Exception as e:
        flog(f"[PROC] cleanup_extra_launch_processes error: {e}", "warning")
        return 0

def _created_threshold(
    launched_after: float | None, created_after_slack: float
) -> float | None:
    if not launched_after:
        return None
    return float(launched_after) - max(0.0, float(created_after_slack or 0.0))


def _new_live_pid(
    entry: dict[str, Any], before: set, created_threshold: float | None
) -> int | None:
    pid = int(entry.get("pid") or 0)
    if not pid or pid in before:
        return None
    created = float(entry.get("created") or 0.0)
    if created_threshold is not None and created and created < created_threshold:
        return None
    return pid


def _pid_settled(
    first_seen: dict[int, float], pid: int, now: float, settle_seconds: float
) -> bool:
    first_seen.setdefault(pid, now)
    return (now - first_seen[pid]) >= settle_seconds


def _validated_detected_pid(
    cls,
    pid: int,
    entry: dict[str, Any],
    expected_browser_tracker_id: str,
) -> int | None:
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
        return None
    flog_kv(
        "PROC",
        "stable_pid_detected",
        pid=pid,
        name=entry.get("name") or "unknown",
        confidence=validation.get("confidence", 0.0),
    )
    return pid


def detect_new_pid(
    cls,
    before: set,
    timeout: float = 20.0,
    launched_after: float | None = None,
    created_after_slack: float = 0.0,
    expected_browser_tracker_id: str = "",
) -> int | None:
    try:
        deadline = time.time() + timeout
        first_seen: dict[int, float] = {}
        settle_seconds = 2.0
        created_threshold = _created_threshold(
            launched_after, created_after_slack
        )
        while time.time() < deadline:
            now = time.time()
            live_new_pids = set()
            for entry in cls.list_live_game_processes(launched_after=None):
                pid = _new_live_pid(entry, before, created_threshold)
                if pid is None:
                    continue
                live_new_pids.add(pid)
                if not _pid_settled(first_seen, pid, now, settle_seconds):
                    continue
                detected = _validated_detected_pid(
                    cls, pid, entry, expected_browser_tracker_id
                )
                if detected is not None:
                    return detected
            first_seen = {
                pid: ts for pid, ts in first_seen.items() if pid in live_new_pids
            }
            time.sleep(0.5)
    except Exception:
        "intentional: ignore exception"
    return None

def is_pid_alive(cls, pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != "zombie"
    except Exception:
        return False

def is_bound_game_alive(
    cls,
    pid: int | None,
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

def evict_pid_cache(cls, pid: int | None):
    if pid is not None:
        with cls._cache_lock:
            cls._process_cache.pop(pid, None)
            cls._nr_cache.pop(pid, None)
        cls.release_pid_owner(pid)
        _rt_monitor.unregister(pid)

def kill_pid(cls, pid: int | None) -> bool:
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
