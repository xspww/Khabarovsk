from __future__ import annotations

from typing import Any, Dict, Optional


def detect_home_rejoin_issue(acc: Any, cfg: Dict[str, Any], now: float, in_game_for: float) -> Optional[Dict[str, Any]]:
    if not cfg.get("home_rejoin_enabled", True):
        return None

    try:
        configured_grace = float(cfg.get("home_rejoin_grace_seconds", 60) or 60)
    except Exception:
        configured_grace = 60.0
    try:
        verify_window = float(cfg.get("launch_verify_window", 25) or 25)
    except Exception:
        verify_window = 25.0
    grace = max(15.0, configured_grace, verify_window + 10.0)

    with acc._lock:
        launch_started_at = float(getattr(acc, "last_launch_at", 0.0) or 0.0)
        in_game_since = float(getattr(acc, "in_game_since", 0.0) or 0.0)
        observed_at = float(getattr(acc, "observed_server_at", 0.0) or 0.0)
        observed_place = str(getattr(acc, "observed_place_id", "") or "").strip()
        observed_job = str(getattr(acc, "observed_job_id", "") or "").strip()
        observed_server_type = str(getattr(acc, "observed_server_type", "") or "").strip().upper()
        lua_in_game_at = float(getattr(acc, "lua_in_game_at", 0.0) or 0.0)
        lua_last_event_at = float(getattr(acc, "lua_last_event_at", 0.0) or 0.0)
        launch_intent = dict(getattr(acc, "launch_intent", {}) or {})
        configured_place = str(
            launch_intent.get("place_id")
            or getattr(acc, "place_id", "")
            or cfg.get("game_place_id", "")
            or ""
        ).strip()

    reference_at = launch_started_at or in_game_since
    launch_age = max(0.0, now - reference_at) if reference_at else max(0.0, in_game_for)
    if launch_age < grace:
        return None

    evidence_after_launch = bool(observed_at and (not reference_at or observed_at >= reference_at - 2.0))
    observed_place_valid = bool(observed_place and observed_place != "0")
    has_job = bool(observed_job)

    if configured_place and observed_place_valid and observed_place != configured_place:
        if evidence_after_launch and has_job:
            return None
        return {
            "reason_key": "home_screen_wrong_place",
            "detail": f"expected_place={configured_place} observed_place={observed_place}",
            "observed_place_id": observed_place,
            "observed_job_id": observed_job,
            "observed_server_type": observed_server_type,
            "launch_age": launch_age,
            "grace": grace,
        }

    if evidence_after_launch and observed_place_valid and not has_job:
        return {
            "reason_key": "home_screen_no_job",
            "detail": f"place={observed_place} has no Roblox JobId; likely Home/loading shell",
            "observed_place_id": observed_place,
            "observed_job_id": observed_job,
            "observed_server_type": observed_server_type,
            "launch_age": launch_age,
            "grace": grace,
        }

    lua_confirmed_after_launch = bool(
        lua_in_game_at
        and lua_last_event_at
        and (not reference_at or lua_in_game_at >= reference_at - 2.0)
    )
    if cfg.get("use_lua", False) and not evidence_after_launch and not lua_confirmed_after_launch:
        return {
            "reason_key": "home_screen_no_server_evidence",
            "detail": "No Lua in-game or server evidence after launch grace; likely Roblox Home/loading shell",
            "observed_place_id": observed_place,
            "observed_job_id": observed_job,
            "observed_server_type": observed_server_type,
            "launch_age": launch_age,
            "grace": grace,
        }

    if cfg.get("home_rejoin_require_server_evidence", True) and configured_place and not evidence_after_launch:
        return {
            "reason_key": "home_screen_no_server_evidence",
            "detail": f"No Roblox server evidence after launch grace for place={configured_place}; likely Home/loading shell",
            "observed_place_id": observed_place,
            "observed_job_id": observed_job,
            "observed_server_type": observed_server_type,
            "launch_age": launch_age,
            "grace": grace,
        }

    return None
