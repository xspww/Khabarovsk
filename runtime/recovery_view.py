from __future__ import annotations

from typing import Any, Tuple

from core import AccountState
from services.network_monitor import NET_ONLINE


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
    state_name = display_state.name
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
    if state_name == "VERIFY" or "verify" in reason_text:
        return "Rejoining", 6, float(acc.last_state_change_at or acc.last_launch_at or 0.0)
    if "session_conflict" in reason_text or "273" in reason_text:
        return "Rejoining", 5, float(acc.recovery_scheduled_at or acc.last_recovery_at or 0.0)
    if "popup" in reason_text or "disconnect_dialog" in reason_text:
        return "Disconnected", 4, float(acc.last_recovery_at or acc.last_state_change_at or 0.0)
    if "network_drop" in reason_text:
        return "Rejoining", 5, float(acc.recovery_scheduled_at or acc.last_recovery_at or 0.0)
    if "presence_limited" in reason_text:
        return "In Game", 8, float(acc.in_game_since or acc.last_state_change_at or 0.0)
    if "connection_error" in reason_text or "visual_disconnect" in reason_text or "rejoin" in reason_text or state_name == "JOINING":
        return "Rejoining", 5, float(acc.recovery_scheduled_at or acc.last_recovery_at or 0.0)
    if state_name in {"LAUNCHING", "STARTING"} or "launch" in reason_text:
        return "Launching", 3, float(acc.last_launch_at or acc.last_state_change_at or 0.0)
    if "kill" in reason_text or "process" in reason_text:
        return "Rejoining", 2, float(acc.last_pid_change_at or acc.last_recovery_at or 0.0)
    if (network_state and network_state != NET_ONLINE) or "network" in reason_text:
        return "Disconnected", 1, float(acc.last_network_lost_at or acc.last_recovery_at or 0.0)
    if "disconnect" in reason_text or "reconnect" in reason_text:
        return "Rejoining", 5, float(acc.recovery_scheduled_at or acc.last_recovery_at or 0.0)
    if state_name in {"CRASH", "NETWORK_LOST", "QUEUED"} or acc.recovery_inflight:
        return "Disconnected", 0, float(acc.last_recovery_at or acc.last_crash_at or acc.last_state_change_at or 0.0)
    return "Idle", -1, float(acc.last_state_change_at or 0.0)
