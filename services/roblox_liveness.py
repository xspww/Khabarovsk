from __future__ import annotations

import time
from typing import Any, NamedTuple

from core import Account, flog_kv
from runtime.runtime_state_manager import RuntimeStateManager
from services.process_proof_policy import score_resource_signals
from services.roblox_log_evidence import CachedLogEvidenceCollector, collect_recent_log_evidence

_RUNTIME_STATE = RuntimeStateManager(logger=flog_kv)
_POPUP_LOG_EVIDENCE_WINDOW_SECONDS = 120.0
_POPUP_LOG_EVIDENCE_CACHE_SECONDS = 2.0
_LOG_EVIDENCE_CACHE = CachedLogEvidenceCollector(ttl_seconds=_POPUP_LOG_EVIDENCE_CACHE_SECONDS)


def _text_hint_from_log_evidence(evidence: dict[str, Any]) -> list[str]:
    code = str(evidence.get("error_code") or "").strip()
    keyword = str(evidence.get("keyword") or "").strip()
    line = str(evidence.get("line") or "").strip()
    texts = ["Disconnected"]
    if keyword:
        texts.append(keyword)
    if line:
        texts.append(line)
    if code:
        texts.append(f"(Error Code: {code})")
    return texts


def _first_text(*values: Any) -> str:
    for value in values:
        if value:
            return str(value)
    return ""


def _merge_dialog_detail(
    dialog: dict[str, Any],
    evidence: dict[str, Any],
    classified: dict[str, Any],
) -> str:
    old_detail = _first_text(dialog.get("detail")).strip()
    log_detail = _first_text(classified.get("detail"), evidence.get("line")).strip()
    if not log_detail or log_detail in old_detail:
        return old_detail
    return f"{old_detail}; roblox_log={log_detail}" if old_detail else f"roblox_log={log_detail}"


def _merged_dialog_fields(
    dialog: dict[str, Any],
    evidence: dict[str, Any],
    classified: dict[str, Any],
    detail: str,
) -> dict[str, Any]:
    return {
        "action": _first_text(classified.get("action"), dialog.get("action")),
        "reason_key": _first_text(classified.get("reason_key"), dialog.get("reason_key")),
        "disconnect_category": _first_text(classified.get("disconnect_category"), dialog.get("disconnect_category")),
        "detail": detail,
        "error_code": _first_text(classified.get("error_code"), evidence.get("error_code")),
        "recovery_allowed": True,
        "evidence_source": "error_code",
        "log_evidence": dict(evidence),
        "visual_disconnect": bool(dialog.get("visual_disconnect", False)),
        "visual_evidence_source": _first_text(dialog.get("evidence_source")),
    }


def _merged_popup_confidence(dialog: dict[str, Any], classified: dict[str, Any]) -> float:
    return max(
        float(dialog.get("popup_confidence", dialog.get("confidence", 0.0)) or 0.0),
        float(classified.get("popup_confidence", classified.get("confidence", 0.0)) or 0.0),
    )


def _merge_log_evidence_into_dialog(cls, dialog: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    if not dialog or not dialog.get("matched"):
        return dialog
    if dialog.get("error_code") or not evidence.get("matched") or not evidence.get("error_code"):
        return dialog

    classified = cls.classify_disconnect_dialog_texts(_text_hint_from_log_evidence(evidence))
    if not classified.get("matched") or not classified.get("recovery_allowed"):
        return dialog

    detail = _merge_dialog_detail(dialog, evidence, classified)
    merged = dict(dialog)
    merged.update(_merged_dialog_fields(dialog, evidence, classified, detail))
    merged["popup_confidence"] = _merged_popup_confidence(dialog, classified)
    merged["confidence"] = merged["popup_confidence"]
    return merged


def _collect_popup_log_evidence(now: float | None = None, force_refresh: bool = False) -> dict[str, Any]:
    return _LOG_EVIDENCE_CACHE.collect(
        collector=collect_recent_log_evidence,
        since_seconds=_POPUP_LOG_EVIDENCE_WINDOW_SECONDS,
        max_files=8,
        max_lines=1200,
        now=now,
        force_refresh=force_refresh,
    )

class _ProcessCandidate(NamedTuple):
    pid: int
    owner: str
    windows: int
    cpu: float
    ram_mb: float
    identity: str
    browser_tracker_id: str
    hwnd: int
    name: str
    cmdline: str
    exe: str
    created: float


class _CandidateMatches(NamedTuple):
    pid_match: bool
    identity_match: bool
    tracker_match: bool
    owner_match: bool
    created_after_launch: bool


def _entry_int(entry: dict[str, Any], key: str) -> int:
    return int(entry.get(key) or 0)


def _entry_float(entry: dict[str, Any], key: str) -> float:
    return float(entry.get(key) or 0.0)


def _entry_text(entry: dict[str, Any], key: str) -> str:
    return str(entry.get(key) or "")


def _candidate_from_entry(entry: dict[str, Any]) -> _ProcessCandidate:
    return _ProcessCandidate(
        pid=_entry_int(entry, "pid"),
        owner=_entry_text(entry, "owner"),
        windows=_entry_int(entry, "windows"),
        cpu=_entry_float(entry, "cpu"),
        ram_mb=_entry_float(entry, "rss_mb"),
        identity=_entry_text(entry, "identity"),
        browser_tracker_id=_entry_text(entry, "browser_tracker_id"),
        hwnd=_entry_int(entry, "hwnd"),
        name=_entry_text(entry, "name"),
        cmdline=_entry_text(entry, "cmdline"),
        exe=_entry_text(entry, "exe"),
        created=_entry_float(entry, "created"),
    )


def _candidate_matches(
    candidate: _ProcessCandidate,
    preferred_pid: int | None,
    launched_after: float | None,
    owner_key: str,
    expected_identity: str,
    expected_browser_tracker_id: str,
) -> _CandidateMatches:
    pid_match = bool(preferred_pid and candidate.pid == preferred_pid)
    identity_match = bool(expected_identity and candidate.identity == expected_identity)
    tracker_match = bool(expected_browser_tracker_id and candidate.browser_tracker_id == expected_browser_tracker_id)
    owner_match = bool(owner_key and candidate.owner == owner_key)
    created_after_launch = bool(
        launched_after and candidate.created >= (float(launched_after) - 3.0)
    )
    return _CandidateMatches(pid_match, identity_match, tracker_match, owner_match, created_after_launch)


def _rejection_reason(
    candidate: _ProcessCandidate,
    match: _CandidateMatches,
    expected_browser_tracker_id: str,
    expected_identity: str,
) -> str:
    if expected_browser_tracker_id and candidate.browser_tracker_id and not match.tracker_match:
        return "browser_tracker_mismatch"
    if match.pid_match and expected_identity and not match.identity_match:
        return "identity_mismatch"
    return ""


def _rejected_candidate(
    candidate: _ProcessCandidate,
    match: _CandidateMatches,
    rejected: str,
) -> dict[str, Any]:
    if rejected == "browser_tracker_mismatch":
        fields = {"browser_tracker_match": False, "pid_match": match.pid_match}
    else:
        fields = {"identity_match": False, "pid_match": True}
    return {
        "pid": candidate.pid,
        "owner": candidate.owner,
        **fields,
        "created_after_launch": match.created_after_launch,
        "windows": candidate.windows,
        "hwnd": candidate.hwnd,
        "cpu": round(candidate.cpu, 2),
        "ram_mb": round(candidate.ram_mb, 1),
        "score": 0.0,
        "rejected": rejected,
    }


def _candidate_score(candidate: _ProcessCandidate, match: _CandidateMatches) -> float:
    score = 0.0
    if match.pid_match:
        score += 35.0
    if match.identity_match:
        score += 35.0
    if match.owner_match:
        score += 20.0
    if match.tracker_match:
        score += 40.0
    if match.created_after_launch:
        score += 12.0
    score += score_resource_signals(candidate.windows, candidate.ram_mb, candidate.cpu)
    if candidate.exe:
        score += 5.0
    if "roblox" in candidate.cmdline.lower():
        score += 3.0
    return score


def _candidate_record(candidate: _ProcessCandidate, match: _CandidateMatches, score: float) -> dict[str, Any]:
    return {
        "pid": candidate.pid,
        "owner": candidate.owner,
        "identity_match": match.identity_match,
        "browser_tracker_match": match.tracker_match,
        "pid_match": match.pid_match,
        "created_after_launch": match.created_after_launch,
        "windows": candidate.windows,
        "hwnd": candidate.hwnd,
        "cpu": round(candidate.cpu, 2),
        "ram_mb": round(candidate.ram_mb, 1),
        "score": round(score, 1),
    }


def _update_best_result(
    result: dict[str, Any],
    candidate: _ProcessCandidate,
    match: _CandidateMatches,
    score: float,
    confidence_level: str,
) -> None:
    result.update({
        "pid": candidate.pid,
        "name": candidate.name,
        "identity": candidate.identity,
        "confidence": round(score, 1),
        "confidence_level": confidence_level,
    })
    result["signals"].update({
        "pid_match": match.pid_match,
        "identity_match": match.identity_match,
        "created_after_launch": match.created_after_launch,
        "windows": candidate.windows,
        "hwnd": candidate.hwnd,
        "cpu": round(candidate.cpu, 2),
        "ram_mb": round(candidate.ram_mb, 1),
        "owner_match": match.owner_match,
        "browser_tracker_match": match.tracker_match,
    })


def multi_signal_validate(
    cls,
    preferred_pid: int | None = None,
    launched_after: float | None = None,
    owner_key: str = "",
    expected_identity: str = "",
    expected_browser_tracker_id: str = "",
) -> dict[str, Any]:
    result: dict[str, Any] = {
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
            "browser_tracker_match": False,
            "candidates": [],
        },
    }
    best_score = -1.0
    for entry in cls.list_live_game_processes(launched_after=launched_after):
        candidate = _candidate_from_entry(entry)
        if owner_key and candidate.owner and candidate.owner != owner_key:
            continue
        match = _candidate_matches(
            candidate,
            preferred_pid,
            launched_after,
            owner_key,
            expected_identity,
            expected_browser_tracker_id,
        )
        rejected = _rejection_reason(candidate, match, expected_browser_tracker_id, expected_identity)
        if rejected:
            result["signals"]["candidates"].append(_rejected_candidate(candidate, match, rejected))
            continue
        score = _candidate_score(candidate, match)
        result["signals"]["candidates"].append(_candidate_record(candidate, match, score))
        if score > best_score:
            best_score = score
            _update_best_result(result, candidate, match, score, cls.confidence_level(score))
    return result

class _OrphanCandidate(NamedTuple):
    pid: int
    confidence: float
    level: str
    signals: dict[str, Any]


def _orphan_candidate(validation: dict[str, Any], cls: Any) -> _OrphanCandidate:
    confidence = float(validation.get("confidence") or 0.0)
    return _OrphanCandidate(
        pid=int(validation.get("pid") or 0),
        confidence=confidence,
        level=str(validation.get("confidence_level") or cls.confidence_level(confidence)),
        signals=validation.get("signals") or {},
    )


def _orphan_result(validation: dict[str, Any], candidate: _OrphanCandidate) -> dict[str, Any]:
    return {
        "action": "ignore",
        "pid": candidate.pid or None,
        "name": str(validation.get("name") or ""),
        "identity": str(validation.get("identity") or ""),
        "confidence": candidate.confidence,
        "confidence_level": candidate.level,
        "validation": validation,
        "reason": "",
    }


def _mark_orphan_unbound(acc: Account, result: dict[str, Any]) -> None:
    with acc._lock:
        _RUNTIME_STATE.set_binding_status(acc, "unbound", reason="orphan_reconcile_no_candidate")
        acc.orphan_confidence = 0.0
    result["reason"] = "no_candidate"


def _restore_trusted_orphan(acc: Account, result: dict[str, Any], candidate: _OrphanCandidate) -> None:
    with acc._lock:
        _RUNTIME_STATE.set_binding_status(acc, "verified", reason="orphan_reconcile_trusted_restore")
        acc.orphan_confidence = candidate.confidence
        acc.orphan_pid = None
        acc.orphan_identity = ""
        acc.orphan_observed_at = 0.0
        acc.orphan_verify_after = 0.0
    result["action"] = "auto_bind"
    result["reason"] = "trusted_restore"


def _quarantine_orphan(
    acc: Account,
    result: dict[str, Any],
    candidate: _OrphanCandidate,
    validation: dict[str, Any],
    quarantine_seconds: float,
    now: float,
) -> None:
    identity = str(validation.get("identity") or "")
    with acc._lock:
        same_orphan = acc.orphan_pid == candidate.pid and acc.orphan_identity == identity
        if not same_orphan:
            acc.orphan_pid = candidate.pid
            acc.orphan_identity = identity
            acc.orphan_observed_at = now
            acc.orphan_verify_after = now + max(5.0, float(quarantine_seconds or 20.0))
        acc.orphan_confidence = candidate.confidence
        _RUNTIME_STATE.set_binding_status(acc, "orphan_pending_verification", reason="orphan_reconcile_pending")
        verify_after = acc.orphan_verify_after
    result["action"] = "quarantine" if now < verify_after else "monitor_only"
    result["reason"] = "medium_confidence_pending" if now < verify_after else "medium_confidence_unowned"


def _mark_untrusted_orphan(acc: Account, result: dict[str, Any], candidate: _OrphanCandidate) -> None:
    with acc._lock:
        acc.orphan_confidence = candidate.confidence
        _RUNTIME_STATE.set_binding_status(
            acc,
            "untrusted_orphan" if candidate.confidence > 0 else "unbound",
            reason="orphan_reconcile_low_confidence",
        )
    result["action"] = "monitor_only"
    result["reason"] = "low_confidence"


def staged_orphan_reconcile(
    cls,
    acc: Account,
    launched_after: float | None = None,
    quarantine_seconds: float = 20.0,
) -> dict[str, Any]:
    validation = cls.multi_signal_validate(
        preferred_pid=acc.pid,
        launched_after=launched_after,
        owner_key=acc._config_username,
        expected_identity=acc.bound_process_identity,
        expected_browser_tracker_id=acc.browser_tracker_id,
    )
    candidate = _orphan_candidate(validation, cls)
    now = time.time()
    result = _orphan_result(validation, candidate)
    if not candidate.pid:
        _mark_orphan_unbound(acc, result)
        return result

    signals = candidate.signals
    trusted_owner = bool(signals.get("owner_match"))
    trusted_identity = bool(signals.get("identity_match"))
    trusted_tracker = bool(signals.get("browser_tracker_match"))
    trusted_restore = trusted_owner or trusted_identity or trusted_tracker
    if candidate.level == "HIGH_CONFIDENCE" and trusted_restore:
        _restore_trusted_orphan(acc, result, candidate)
        return result

    if candidate.level == "MEDIUM_CONFIDENCE":
        _quarantine_orphan(acc, result, candidate, validation, quarantine_seconds, now)
        return result

    _mark_untrusted_orphan(acc, result, candidate)
    return result

class _LivenessSample(NamedTuple):
    cpu: float
    ram: float
    windows: int
    cpu_delta: float
    ram_delta: float
    responsive: bool


def _liveness_sample(
    cls: Any,
    validation: dict[str, Any],
    previous_cpu: float,
    previous_ram_mb: float,
    pid: int | None,
) -> _LivenessSample:
    cpu = float(validation.get("cpu") or 0.0)
    ram = float(validation.get("ram_mb") or 0.0)
    windows = int(validation.get("windows") or 0)
    cpu_delta = abs(cpu - float(previous_cpu or 0.0))
    ram_delta = abs(ram - float(previous_ram_mb or 0.0))
    responsive = windows > 0 and not cls.is_not_responding(pid)
    return _LivenessSample(cpu, ram, windows, cpu_delta, ram_delta, responsive)


def _cpu_active(sample: _LivenessSample, cpu_threshold: float) -> bool:
    threshold = float(cpu_threshold or 0.9)
    return sample.cpu >= threshold or sample.cpu_delta >= max(0.2, threshold / 2.0)


def _liveness_score(
    sample: _LivenessSample,
    net_online: bool,
    recovery_inflight: bool,
    cpu_threshold: float,
    ram_delta_threshold: float,
) -> float:
    score = 1.0
    if sample.responsive:
        score += 3.0
    if _cpu_active(sample, cpu_threshold):
        score += 2.0
    if sample.ram_delta >= max(1.0, float(ram_delta_threshold or 8.0)):
        score += 1.0
    if sample.ram >= 90.0:
        score += 1.0
    if net_online:
        score += 1.0
    if recovery_inflight:
        score -= 1.0
    return score


def _dialog_state(reason_key: str) -> str:
    return "teleporting" if reason_key == "teleport_timeout" else "reconnecting"


def _popup_dialog(
    cls: Any,
    pid: int | None,
    score: float,
    inspect_ui: bool,
    ui_sample_count: int | None,
) -> dict[str, Any]:
    return cls.inspect_disconnect_dialog(
        pid,
        prepare=bool(inspect_ui),
        process_idle=score <= 4.0,
        sample_count=int(ui_sample_count or (6 if inspect_ui else 2)),
    )


def _inspect_liveness_dialog(
    cls: Any,
    pid: int | None,
    score: float,
    inspect_ui: bool,
    ui_sample_count: int | None,
) -> tuple[dict[str, Any], str, str, dict[str, Any]]:
    dialog = _popup_dialog(cls, pid, score, inspect_ui, ui_sample_count)
    log_evidence: dict[str, Any] = {}
    state = "alive"
    reason_key = ""
    if inspect_ui and dialog.get("matched") and not dialog.get("error_code"):
        log_evidence = _collect_popup_log_evidence()
        dialog = _merge_log_evidence_into_dialog(cls, dialog, log_evidence)
    # Captcha is owned by runtime/captcha_detector.py — this dialog carries
    # disconnects only, never a captcha verdict.
    if dialog.get("matched") and dialog.get("recovery_allowed"):
        reason_key = str(dialog.get("reason_key") or "connection_error")
        state = _dialog_state(reason_key)
    elif inspect_ui and score <= 4.0:
        log_evidence = collect_recent_log_evidence(since_seconds=180.0)
    return dialog, state, reason_key, log_evidence


def _fallback_liveness_state(
    state: str,
    sample: _LivenessSample,
    score: float,
    in_game_for: float,
    loading_grace: float,
) -> tuple[str, str]:
    if state and state != "alive":
        return state, ""
    if in_game_for < max(30.0, float(loading_grace or 90.0)) and not sample.responsive and score <= 4.0:
        return "loading", ""
    if score >= 5.0:
        return "alive", ""
    if score >= 3.0:
        return "idle", ""
    return "suspect_frozen", ("watchdog_timeout" if sample.windows > 0 else "loading_freeze")


def assess_liveness(
    cls,
    pid: int | None,
    previous_cpu: float = 0.0,
    previous_ram_mb: float = 0.0,
    net_online: bool = True,
    recovery_inflight: bool = False,
    in_game_for: float = 0.0,
    loading_grace: float = 90.0,
    cpu_threshold: float = 0.9,
    ram_delta_threshold: float = 8.0,
    inspect_ui: bool = False,
    ui_sample_count: int | None = None,
) -> dict[str, Any]:
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

    sample = _liveness_sample(cls, validation, previous_cpu, previous_ram_mb, pid)
    score = _liveness_score(sample, net_online, recovery_inflight, cpu_threshold, ram_delta_threshold)

    dialog: dict[str, Any] = {}
    log_evidence: dict[str, Any] = {}
    state = "alive"
    reason_key = ""
    if inspect_ui or (sample.windows > 0 and score <= 4.0):
        dialog, state, reason_key, log_evidence = _inspect_liveness_dialog(
            cls, pid, score, inspect_ui, ui_sample_count,
        )

    state, fallback_reason = _fallback_liveness_state(state, sample, score, in_game_for, loading_grace)
    if not reason_key:
        reason_key = fallback_reason

    return {
        "state": state,
        "score": round(max(0.0, score), 1),
        "reason_key": reason_key,
        "validation": validation,
        "cpu_delta": round(sample.cpu_delta, 2),
        "ram_delta": round(sample.ram_delta, 1),
        "dialog": dialog,
        "log_evidence": log_evidence,
    }
