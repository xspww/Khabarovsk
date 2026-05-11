from __future__ import annotations

import time
from typing import Any, Dict, Optional

from core import Account
from runtime.runtime_state_manager import RuntimeStateManager
from core import flog_kv

_RUNTIME_STATE = RuntimeStateManager(logger=flog_kv)

def multi_signal_validate(
    cls,
    preferred_pid: Optional[int] = None,
    launched_after: Optional[float] = None,
    owner_key: str = "",
    expected_identity: str = "",
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "pid": None,
        "name": "",
        "identity": "",
        "confidence": 0.0,
        "confidence_level": "UNTRUSTED",
        "signals": {
            "pid_match": False,
            "identity_match": False,
            "created_after_launch": False,
            "windows": 0,
            "hwnd": 0,
            "cpu": 0.0,
            "ram_mb": 0.0,
            "owner_match": False,
            "candidates": [],
        },
    }
    best_score = -1.0
    for entry in cls.list_live_game_processes(launched_after=launched_after):
        owner = str(entry.get("owner") or "")
        if owner_key and owner and owner != owner_key:
            continue
        pid = int(entry.get("pid") or 0)
        windows = int(entry.get("windows") or 0)
        cpu = float(entry.get("cpu") or 0.0)
        ram_mb = float(entry.get("rss_mb") or 0.0)
        identity = str(entry.get("identity") or "")
        pid_match = bool(preferred_pid and pid == preferred_pid)
        identity_match = bool(expected_identity and identity == expected_identity)
        created_after_launch = bool(
            launched_after and float(entry.get("created") or 0.0) >= (float(launched_after) - 3.0)
        )
        if pid_match and expected_identity and not identity_match:
            result["signals"]["candidates"].append({
                "pid": pid,
                "owner": owner,
                "identity_match": False,
                "pid_match": True,
                "created_after_launch": created_after_launch,
                "windows": windows,
                "hwnd": int(entry.get("hwnd") or 0),
                "cpu": round(cpu, 2),
                "ram_mb": round(ram_mb, 1),
                "score": 0.0,
                "rejected": "identity_mismatch",
            })
            continue
        owner_match = bool(owner_key and owner == owner_key)
        score = 0.0
        if pid_match:
            score += 35.0
        if identity_match:
            score += 35.0
        if owner_match:
            score += 20.0
        if created_after_launch:
            score += 12.0
        score += min(15.0, float(windows) * 7.0)
        score += min(12.0, ram_mb / 120.0)
        score += min(10.0, cpu * 2.0)
        if entry.get("exe"):
            score += 5.0
        if "roblox" in str(entry.get("cmdline") or "").lower():
            score += 3.0
        result["signals"]["candidates"].append({
            "pid": pid,
            "owner": owner,
            "identity_match": identity_match,
            "pid_match": pid_match,
            "created_after_launch": created_after_launch,
            "windows": windows,
            "hwnd": int(entry.get("hwnd") or 0),
            "cpu": round(cpu, 2),
            "ram_mb": round(ram_mb, 1),
            "score": round(score, 1),
        })
        if score > best_score:
            best_score = score
            result.update({
                "pid": pid,
                "name": str(entry.get("name") or ""),
                "identity": identity,
                "confidence": round(score, 1),
                "confidence_level": cls.confidence_level(score),
            })
            result["signals"].update({
                "pid_match": pid_match,
                "identity_match": identity_match,
                "created_after_launch": created_after_launch,
                "windows": windows,
                "hwnd": int(entry.get("hwnd") or 0),
                "cpu": round(cpu, 2),
                "ram_mb": round(ram_mb, 1),
                "owner_match": owner_match,
            })
    return result

def staged_orphan_reconcile(
    cls,
    acc: Account,
    launched_after: Optional[float] = None,
    quarantine_seconds: float = 20.0,
) -> Dict[str, Any]:
    validation = cls.multi_signal_validate(
        preferred_pid=acc.pid,
        launched_after=launched_after,
        owner_key=acc._config_username,
        expected_identity=acc.bound_process_identity,
    )
    pid = int(validation.get("pid") or 0)
    confidence = float(validation.get("confidence") or 0.0)
    level = str(validation.get("confidence_level") or cls.confidence_level(confidence))
    signals = validation.get("signals") or {}
    now = time.time()
    result = {
        "action": "ignore",
        "pid": pid or None,
        "name": str(validation.get("name") or ""),
        "identity": str(validation.get("identity") or ""),
        "confidence": confidence,
        "confidence_level": level,
        "validation": validation,
        "reason": "",
    }
    if not pid:
        with acc._lock:
            _RUNTIME_STATE.set_binding_status(acc, "unbound", reason="orphan_reconcile_no_candidate")
            acc.orphan_confidence = 0.0
        result["reason"] = "no_candidate"
        return result

    trusted_owner = bool(signals.get("owner_match"))
    trusted_identity = bool(signals.get("identity_match"))
    trusted_restore = trusted_owner or trusted_identity
    if level == "HIGH_CONFIDENCE" and trusted_restore:
        with acc._lock:
            _RUNTIME_STATE.set_binding_status(acc, "verified", reason="orphan_reconcile_trusted_restore")
            acc.orphan_confidence = confidence
            acc.orphan_pid = None
            acc.orphan_identity = ""
            acc.orphan_observed_at = 0.0
            acc.orphan_verify_after = 0.0
        result["action"] = "auto_bind"
        result["reason"] = "trusted_restore"
        return result

    if level == "MEDIUM_CONFIDENCE":
        identity = str(validation.get("identity") or "")
        with acc._lock:
            same_orphan = acc.orphan_pid == pid and acc.orphan_identity == identity
            if not same_orphan:
                acc.orphan_pid = pid
                acc.orphan_identity = identity
                acc.orphan_observed_at = now
                acc.orphan_verify_after = now + max(5.0, float(quarantine_seconds or 20.0))
            acc.orphan_confidence = confidence
            _RUNTIME_STATE.set_binding_status(acc, "orphan_pending_verification", reason="orphan_reconcile_pending")
            verify_after = acc.orphan_verify_after
        result["action"] = "quarantine" if now < verify_after else "monitor_only"
        result["reason"] = "medium_confidence_pending" if now < verify_after else "medium_confidence_unowned"
        return result

    with acc._lock:
        acc.orphan_confidence = confidence
        _RUNTIME_STATE.set_binding_status(
            acc,
            "untrusted_orphan" if confidence > 0 else "unbound",
            reason="orphan_reconcile_low_confidence",
        )
    result["action"] = "monitor_only"
    result["reason"] = "low_confidence"
    return result

def assess_liveness(
    cls,
    pid: Optional[int],
    previous_cpu: float = 0.0,
    previous_ram_mb: float = 0.0,
    net_online: bool = True,
    recovery_inflight: bool = False,
    in_game_for: float = 0.0,
    loading_grace: float = 90.0,
    cpu_threshold: float = 0.9,
    ram_delta_threshold: float = 8.0,
    inspect_ui: bool = False,
) -> Dict[str, Any]:
    validation = cls.validate_game_process(pid, min_ram_mb=0.0)
    if not validation.get("ok"):
        return {
            "state": "missing",
            "score": 0.0,
            "reason_key": "process_crash",
            "validation": validation,
            "cpu_delta": 0.0,
            "ram_delta": 0.0,
            "dialog": {},
        }

    cpu = float(validation.get("cpu") or 0.0)
    ram = float(validation.get("ram_mb") or 0.0)
    windows = int(validation.get("windows") or 0)
    cpu_delta = abs(cpu - float(previous_cpu or 0.0))
    ram_delta = abs(ram - float(previous_ram_mb or 0.0))
    responsive = windows > 0 and not cls.is_not_responding(pid)

    score = 1.0
    if responsive:
        score += 3.0
    if cpu >= float(cpu_threshold or 0.9) or cpu_delta >= max(0.2, float(cpu_threshold or 0.9) / 2.0):
        score += 2.0
    if ram_delta >= max(1.0, float(ram_delta_threshold or 8.0)):
        score += 1.0
    if ram >= 90.0:
        score += 1.0
    if net_online:
        score += 1.0
    if recovery_inflight:
        score -= 1.0

    dialog: Dict[str, Any] = {}
    state = "alive"
    reason_key = ""
    if inspect_ui or (windows > 0 and score <= 4.0):
        dialog = cls.inspect_disconnect_dialog(
            pid,
            prepare=bool(inspect_ui),
            presence_mismatch=bool(inspect_ui),
            process_idle=score <= 4.0,
            sample_count=6 if inspect_ui else 2,
        )
        if dialog.get("matched") and dialog.get("recovery_allowed"):
            reason_key = str(dialog.get("reason_key") or "connection_error")
            if reason_key == "teleport_timeout":
                state = "teleporting"
            elif reason_key in {"network_drop", "connection_error", "server_full"}:
                state = "reconnecting"
            else:
                state = "reconnecting"

    if not state or state == "alive":
        if in_game_for < max(30.0, float(loading_grace or 90.0)) and not responsive and score <= 4.0:
            state = "loading"
        elif score >= 5.0:
            state = "alive"
        elif score >= 3.0:
            state = "idle"
        else:
            state = "suspect_frozen"
            reason_key = "watchdog_timeout" if windows > 0 else "loading_freeze"

    return {
        "state": state,
        "score": round(max(0.0, score), 1),
        "reason_key": reason_key,
        "validation": validation,
        "cpu_delta": round(cpu_delta, 2),
        "ram_delta": round(ram_delta, 1),
        "dialog": dialog,
    }
