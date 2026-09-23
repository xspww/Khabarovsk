from __future__ import annotations

from typing import Tuple, Any
from services.network_monitor import NET_ONLINE
from core import AccountState


def recovery_step_for_account(acc: Any, display_state: AccountState, network_state: str = NET_ONLINE) -> Tuple[str, int, float]:
    reason_text = " ".join(
        str(value or "")
        for value in (
            acc.recovery_status,
            acc.last_recovery_reason,
            acc.last_crash_reason,
            acc.last_state_reason,
            acc.last_watchdog_classification,
            acc.liveness_state,
        )
    ).lower()
    recovery_status = str(acc.recovery_status or "").strip().lower()
    # Finished/Unfinished transitions leave stale kill/rejoin text behind.
    # Never label those as Rejoining — they are Idle until a fresh recovery starts.
    if "account_finished" in reason_text or "account_unfinished" in reason_text:
        # If a real new recovery started after the unfinished reset
        # (last_recovery_at newer than the reset), allow normal flow.
        # The revive path zeroes last_recovery_at, so any marker now is stale.
        try:
            _last_rec = max(
                float(getattr(acc, "last_recovery_at", 0.0) or 0.0),
                float(getattr(acc, "recovery_scheduled_at", 0.0) or 0.0),
            )
        except Exception:
            _last_rec = 0.0
        if not _last_rec:
            return "Idle", -1, float(acc.last_state_change_at or 0.0)
    state_name = display_state.name
    recovery_markers = (
        "rejoin",
        "disconnect",
        "session_conflict",
        "273",
        "network_drop",
        "connection_error",
        "visual_disconnect",
        "popup",
        "kill",
        "crash",
    )
    last_launch_at = float(getattr(acc, "last_launch_at", 0.0) or 0.0)
    last_recovery_at = max(
        float(getattr(acc, "last_recovery_at", 0.0) or 0.0),
        float(getattr(acc, "recovery_scheduled_at", 0.0) or 0.0),
    )
    has_current_recovery_marker = bool(
        any(marker in reason_text for marker in recovery_markers)
        and last_recovery_at > 0.0
        and last_recovery_at >= last_launch_at
    )
    # VERIFY/JOINING are also used by the first launch. Do not label the
    # initial Start flow as Rejoining unless a real recovery signal exists.
    if (
        state_name in {"LAUNCHING", "STARTING", "VERIFY", "JOINING"}
        and not has_current_recovery_marker
    ):
        return "Launching", 3, float(acc.last_launch_at or acc.last_state_change_at or 0.0)
    if state_name == "COOLDOWN":
        return "Cooldown", 7, float(acc.recovery_scheduled_at or acc.cooldown_until or acc.last_state_change_at or 0.0)
    if state_name == "IN_GAME" and not acc.recovery_inflight and str(acc.liveness_state or "").lower() in {"alive", "idle"}:
        return "Recovery Complete", 8, float(acc.in_game_since or acc.last_state_change_at or 0.0)
    if recovery_status == "checking_disconnect" or "checking_disconnect" in reason_text:
        return "Disconnected", 4, float(acc.last_recovery_at or acc.last_state_change_at or 0.0)
    if recovery_status == "disconnect_detected":
        return "Disconnected", 4, float(acc.last_recovery_at or acc.last_state_change_at or 0.0)
    if state_name == "IN_GAME" and recovery_status in {"", "in_game"}:
        return "Recovery Complete", 8, float(acc.in_game_since or acc.last_state_change_at or 0.0)
    if state_name == "VERIFY" and has_current_recovery_marker:
        return "Rejoining", 6, float(acc.last_state_change_at or acc.last_launch_at or 0.0)
    if has_current_recovery_marker and ("session_conflict" in reason_text or "273" in reason_text):
        return "Rejoining", 5, float(acc.recovery_scheduled_at or acc.last_recovery_at or 0.0)
    if has_current_recovery_marker and ("popup" in reason_text or "disconnect_dialog" in reason_text):
        return "Disconnected", 4, float(acc.last_recovery_at or acc.last_state_change_at or 0.0)
    if has_current_recovery_marker and "network_drop" in reason_text:
        return "Rejoining", 5, float(acc.recovery_scheduled_at or acc.last_recovery_at or 0.0)
    if has_current_recovery_marker and ("connection_error" in reason_text or "visual_disconnect" in reason_text or "rejoin" in reason_text or state_name == "JOINING"):
        return "Rejoining", 5, float(acc.recovery_scheduled_at or acc.last_recovery_at or 0.0)
    if state_name in {"LAUNCHING", "STARTING"} or "launch" in reason_text:
        return "Launching", 3, float(acc.last_launch_at or acc.last_state_change_at or 0.0)
    # kill/process text is stale unless a real recovery is active.
    # Without this, old "account_finished kill" markers show Rejoining forever.
    if "kill" in reason_text or "process" in reason_text:
        if has_current_recovery_marker or recovery_status or bool(getattr(acc, "recovery_inflight", False)):
            return "Rejoining", 2, float(acc.last_pid_change_at or acc.last_recovery_at or 0.0)
        return "Idle", -1, float(acc.last_state_change_at or 0.0)
    if (network_state and network_state != NET_ONLINE) or "network" in reason_text:
        return "Disconnected", 1, float(acc.last_network_lost_at or acc.last_recovery_at or 0.0)
    if "disconnect" in reason_text or "reconnect" in reason_text:
        return "Rejoining", 5, float(acc.recovery_scheduled_at or acc.last_recovery_at or 0.0)
    if state_name in {"CRASH", "NETWORK_LOST", "QUEUED"} or acc.recovery_inflight:
        return "Disconnected", 0, float(acc.last_recovery_at or acc.last_crash_at or acc.last_state_change_at or 0.0)
    return "Idle", -1, float(acc.last_state_change_at or 0.0)
