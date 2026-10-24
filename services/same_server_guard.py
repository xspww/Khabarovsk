from __future__ import annotations

import time
from typing import Any

from domain.games import _text

_SAME_SERVER_HOP_COOLDOWN = 60.0


def _account_key(account: Any) -> str:
    return _text(getattr(account, "_config_username", "") or getattr(account, "username", "")).lower()


def _observed_ids(account: Any) -> tuple[str, str]:
    return _text(getattr(account, "observed_job_id", "")), _text(getattr(account, "observed_place_id", ""))


def _is_other_conflict(other: Any, current: Any, cur_job: str, cur_place: str, cur_key: str) -> bool:
    if other is current:
        return False
    other_job, other_place = _observed_ids(other)
    if not other_job or other_job != cur_job:
        return False
    # If both have place ids and they differ, same JobId across different
    # places is not a conflict (JobIds are per-place but be strict only when
    # places match or either is empty).
    if cur_place and other_place and cur_place != other_place:
        return False
    other_key = _account_key(other)
    if other_key and other_key == cur_key:
        return False
    return True


def _conflict_record(other: Any, other_job: str, other_place: str, cur_place: str) -> dict[str, str]:
    return {
        "username": _text(getattr(other, "display_name", "") or getattr(other, "username", "")),
        "config_username": _text(getattr(other, "_config_username", "") or getattr(other, "username", "")),
        "job_id": other_job,
        "place_id": other_place or cur_place,
    }


def find_same_server_conflicts(accounts: list[Any], current: Any) -> list[dict[str, str]]:
    """Return other accounts sharing the same JobId + PlaceId as current."""
    try:
        cur_job, cur_place = _observed_ids(current)
    except Exception:
        return []
    if not cur_job:
        return []
    cur_key = _account_key(current)
    conflicts: list[dict[str, str]] = []
    for other in accounts or []:
        try:
            if not _is_other_conflict(other, current, cur_job, cur_place, cur_key):
                continue
            other_job, other_place = _observed_ids(other)
            conflicts.append(_conflict_record(other, other_job, other_place, cur_place))
        except Exception:
            continue
    return conflicts


def should_hop_for_same_server(acc: Any, cooldown: float = _SAME_SERVER_HOP_COOLDOWN) -> bool:
    try:
        last = float(getattr(acc, "_last_same_server_hop_at", 0.0) or 0.0)
    except Exception:
        last = 0.0
    if (time.time() - last) < max(10.0, float(cooldown or 60.0)):
        return False
    # Storm guard: max 3 hops per 10 minutes per account.
    try:
        hist = list(getattr(acc, "_same_server_hop_history", []) or [])
        now = time.time()
        hist = [t for t in hist if (now - float(t)) < 600.0]
        if len(hist) >= 3:
            return False
    except Exception:
        "intentional: ignore exception"
    return True


def mark_same_server_hop(acc: Any) -> None:
    try:
        acc._last_same_server_hop_at = time.time()
    except Exception:
        "intentional: ignore exception"
    try:
        hist = list(getattr(acc, "_same_server_hop_history", []) or [])
        hist.append(time.time())
        # Keep last 10 entries.
        acc._same_server_hop_history = hist[-10:]
    except Exception:
        "intentional: ignore exception"


def _same_server_config(farm: Any) -> dict[str, Any]:
    try:
        return farm.cfg_mgr.snapshot() if hasattr(farm, "cfg_mgr") else {}
    except Exception:
        return {}


def _newest_other_server_at(farm: Any, acc: Any, job: str) -> float:
    newest = 0.0
    for other in list(getattr(farm, "_accounts", []) or []):
        try:
            if other is acc:
                continue
            if _text(getattr(other, "observed_job_id", "")) != job:
                continue
            observed_at = float(getattr(other, "observed_server_at", 0.0) or 0.0)
            newest = max(newest, observed_at)
        except Exception:
            continue
    return newest


def _should_defer_for_newer(farm: Any, acc: Any, job: str) -> bool:
    try:
        my_at = float(getattr(acc, "observed_server_at", 0.0) or 0.0)
        if not my_at:
            return False
        newest_other_at = _newest_other_server_at(farm, acc, job)
        return bool(newest_other_at and my_at + 5.0 < newest_other_at)
    except Exception:
        return False


def _log_same_server_blocked(acc: Any, other: dict[str, str], job: str, detection_place_id: str) -> None:
    try:
        from core import flog_kv as _flog

        _flog(
            "SERVER",
            "same_server_blocked",
            account=getattr(acc, "display_name", ""),
            conflict_with=other.get("username", ""),
            job_id=job,
            job_id_short=job[:8] if len(job) > 8 else job,
            place_id=_text(detection_place_id or getattr(acc, "observed_place_id", "")),
            reason="same_server_blocked",
        )
    except Exception:
        "intentional: ignore exception"


def _request_same_server_hop(farm: Any, acc: Any) -> bool:
    try:
        orchestrator = getattr(farm, "_runtime_orchestrator", None)
        if orchestrator is not None and hasattr(orchestrator, "request_rejoin"):
            return bool(orchestrator.request_rejoin(acc, reason="same_server_blocked"))
        ok, _msg = farm.force_rejoin(acc._config_username)
        return bool(ok)
    except Exception:
        return False


def _emit_same_server_event(farm: Any, acc: Any, other: dict[str, str], job: str) -> None:
    try:
        if hasattr(farm, "_push_event"):
            farm._push_event(
                "server",
                f"Same server blocked: {getattr(acc, 'display_name', '')} shares Job {job[:8]} with {other.get('username','')} — hopping",
                account=acc,
                severity="warn",
                reason="same_server_blocked",
                conflict_with=other.get("username", ""),
                job_id=job,
            )
    except Exception:
        "intentional: ignore exception"


def _wake_same_server_worker(farm: Any, acc: Any) -> None:
    try:
        worker = (getattr(farm, "_workers", None) or {}).get(getattr(acc, "_config_username", ""))
        if worker is not None:
            worker.wake()
    except Exception:
        "intentional: ignore exception"


def _same_server_precondition(farm: Any, acc: Any, detection_job_id: str) -> str:
    cfg = _same_server_config(farm)
    if not bool((cfg or {}).get("block_same_server_enabled", False)):
        return ""
    if not bool(getattr(farm, "running", False)):
        return ""
    return _text(detection_job_id or getattr(acc, "observed_job_id", ""))


def check_and_hop_same_server(farm: Any, acc: Any, detection_job_id: str = "", detection_place_id: str = "") -> dict[str, Any] | None:
    """Enforce block_same_server_enabled. Returns conflict info if hopped."""
    job = _same_server_precondition(farm, acc, detection_job_id)
    if not job:
        return None
    conflicts = find_same_server_conflicts(list(getattr(farm, "_accounts", []) or []), acc)
    if not conflicts:
        return None
    # Newest-only: if this account was already here and the conflict is
    # newer, let the newer one hop instead to avoid both hopping at once.
    if _should_defer_for_newer(farm, acc, job):
        return None
    if not should_hop_for_same_server(acc):
        return None
    other = conflicts[0]
    _log_same_server_blocked(acc, other, job, detection_place_id)
    mark_same_server_hop(acc)
    hopped = _request_same_server_hop(farm, acc)
    _emit_same_server_event(farm, acc, other, job)
    _wake_same_server_worker(farm, acc)
    return {"conflict_with": other.get("username", ""), "job_id": job, "hopped": hopped}
