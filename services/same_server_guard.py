from __future__ import annotations

import time
from typing import Any, Dict, List, Optional


_SAME_SERVER_HOP_COOLDOWN = 60.0


def _text(value: Any) -> str:
    return str(value or "").strip()


def find_same_server_conflicts(accounts: List[Any], current: Any) -> List[Dict[str, str]]:
    """Return other accounts sharing the same JobId + PlaceId as current."""
    try:
        cur_job = _text(getattr(current, "observed_job_id", ""))
        cur_place = _text(getattr(current, "observed_place_id", ""))
    except Exception:
        return []
    if not cur_job:
        return []
    cur_key = _text(getattr(current, "_config_username", "") or getattr(current, "username", "")).lower()
    conflicts: List[Dict[str, str]] = []
    for other in accounts or []:
        try:
            if other is current:
                continue
            other_job = _text(getattr(other, "observed_job_id", ""))
            if not other_job or other_job != cur_job:
                continue
            other_place = _text(getattr(other, "observed_place_id", ""))
            # If both have place ids and they differ, same JobId across
            # different places is not a conflict (JobIds are per-place but
            # be strict only when places match or either is empty).
            if cur_place and other_place and cur_place != other_place:
                continue
            other_key = _text(getattr(other, "_config_username", "") or getattr(other, "username", "")).lower()
            if other_key and other_key == cur_key:
                continue
            conflicts.append(
                {
                    "username": _text(getattr(other, "display_name", "") or getattr(other, "username", "")),
                    "config_username": _text(getattr(other, "_config_username", "") or getattr(other, "username", "")),
                    "job_id": other_job,
                    "place_id": other_place or cur_place,
                }
            )
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
        pass
    return True


def mark_same_server_hop(acc: Any) -> None:
    try:
        acc._last_same_server_hop_at = time.time()  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        hist = list(getattr(acc, "_same_server_hop_history", []) or [])
        hist.append(time.time())
        # Keep last 10 entries.
        acc._same_server_hop_history = hist[-10:]  # type: ignore[attr-defined]
    except Exception:
        pass


def check_and_hop_same_server(farm: Any, acc: Any, detection_job_id: str = "", detection_place_id: str = "") -> Optional[Dict[str, Any]]:
    """Enforce block_same_server_enabled. Returns conflict info if hopped."""
    try:
        cfg = farm.cfg_mgr.snapshot() if hasattr(farm, "cfg_mgr") else {}
    except Exception:
        cfg = {}
    if not bool((cfg or {}).get("block_same_server_enabled", False)):
        return None
    if not bool(getattr(farm, "running", False)):
        return None
    job = _text(detection_job_id or getattr(acc, "observed_job_id", ""))
    if not job:
        return None
    conflicts = find_same_server_conflicts(list(getattr(farm, "_accounts", []) or []), acc)
    if not conflicts:
        return None
    # Newest-only: if this account was already here and the conflict is
    # newer, let the newer one hop instead to avoid both hopping at once.
    try:
        my_at = float(getattr(acc, "observed_server_at", 0.0) or 0.0)
        if my_at:
            others = list(getattr(farm, "_accounts", []) or [])
            newest_other_at = 0.0
            for _o in others:
                try:
                    if _o is acc:
                        continue
                    _oj = _text(getattr(_o, "observed_job_id", ""))
                    if _oj != job:
                        continue
                    _oa = float(getattr(_o, "observed_server_at", 0.0) or 0.0)
                    newest_other_at = max(newest_other_at, _oa)
                except Exception:
                    continue
            # If I am clearly older (>5s) and the other just arrived,
            # stay put — the newcomer will hop on its own detection.
            if newest_other_at and my_at + 5.0 < newest_other_at:
                return None
    except Exception:
        pass
    if not should_hop_for_same_server(acc):
        return None
    other = conflicts[0]
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
        pass
    mark_same_server_hop(acc)
    hopped = False
    try:
        orchestrator = getattr(farm, "_runtime_orchestrator", None)
        if orchestrator is not None and hasattr(orchestrator, "request_rejoin"):
            hopped = bool(orchestrator.request_rejoin(acc, reason="same_server_blocked"))
        else:
            ok, _msg = farm.force_rejoin(acc._config_username)
            hopped = bool(ok)
    except Exception:
        hopped = False
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
        pass
    try:
        worker = (getattr(farm, "_workers", None) or {}).get(getattr(acc, "_config_username", ""))
        if worker is not None:
            worker.wake()
    except Exception:
        pass
    return {"conflict_with": other.get("username", ""), "job_id": job, "hopped": hopped}
