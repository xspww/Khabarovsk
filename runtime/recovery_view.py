from __future__ import annotations

from functools import partial
from typing import Any, NamedTuple

from core import AccountState
from services.network_monitor import NET_ONLINE

RECOVERY_MARKERS = (
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


class _RecoveryContext(NamedTuple):
    acc: Any
    reason_text: str
    recovery_status: str
    state_name: str
    has_current_recovery_marker: bool
    network_state: str


def _contains_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def _build_context(
    acc: Any,
    display_state: AccountState,
    network_state: str,
) -> _RecoveryContext:
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
    return _RecoveryContext(
        acc=acc,
        reason_text=reason_text,
        recovery_status=recovery_status,
        state_name=display_state.name,
        has_current_recovery_marker=False,
        network_state=network_state,
    )


def _last_recovery_signal(acc: Any) -> float:
    try:
        return max(
            float(getattr(acc, "last_recovery_at", 0.0) or 0.0),
            float(getattr(acc, "recovery_scheduled_at", 0.0) or 0.0),
        )
    except Exception:
        return 0.0


def _has_current_recovery_marker(acc: Any, reason_text: str) -> bool:
    last_launch_at = float(
        getattr(acc, "last_launch_at", 0.0) or 0.0
    )
    last_recovery_at = max(
        float(getattr(acc, "last_recovery_at", 0.0) or 0.0),
        float(
            getattr(acc, "recovery_scheduled_at", 0.0) or 0.0
        ),
    )
    return bool(
        _contains_any(reason_text, RECOVERY_MARKERS)
        and last_recovery_at > 0.0
        and last_recovery_at >= last_launch_at
    )


def _step_finished(
    ctx: _RecoveryContext,
) -> tuple[str, int, float] | None:
    if not _contains_any(
        ctx.reason_text,
        ("account_finished", "account_unfinished"),
    ):
        return None
    if _last_recovery_signal(ctx.acc):
        return None
    return (
        "Idle",
        -1,
        float(ctx.acc.last_state_change_at or 0.0),
    )


def _step_launching_initial(
    ctx: _RecoveryContext,
) -> tuple[str, int, float] | None:
    if (
        ctx.state_name
        in {"LAUNCHING", "STARTING", "VERIFY", "JOINING"}
        and not ctx.has_current_recovery_marker
    ):
        return (
            "Launching",
            3,
            float(
                ctx.acc.last_launch_at
                or ctx.acc.last_state_change_at
                or 0.0
            ),
        )
    return None


def _step_cooldown(
    ctx: _RecoveryContext,
) -> tuple[str, int, float] | None:
    if ctx.state_name == "COOLDOWN":
        return (
            "Cooldown",
            7,
            float(
                ctx.acc.recovery_scheduled_at
                or ctx.acc.cooldown_until
                or ctx.acc.last_state_change_at
                or 0.0
            ),
        )
    return None


def _step_in_game_alive(
    ctx: _RecoveryContext,
) -> tuple[str, int, float] | None:
    if (
        ctx.state_name == "IN_GAME"
        and not ctx.acc.recovery_inflight
        and str(ctx.acc.liveness_state or "").lower()
        in {"alive", "idle"}
    ):
        return (
            "Recovery Complete",
            8,
            float(
                ctx.acc.in_game_since
                or ctx.acc.last_state_change_at
                or 0.0
            ),
        )
    return None


def _step_disconnect_status(
    ctx: _RecoveryContext,
) -> tuple[str, int, float] | None:
    if ctx.recovery_status == "checking_disconnect" or (
        _contains_any(ctx.reason_text, ("checking_disconnect",))
    ):
        return (
            "Disconnected",
            4,
            float(
                ctx.acc.last_recovery_at
                or ctx.acc.last_state_change_at
                or 0.0
            ),
        )
    if ctx.recovery_status == "disconnect_detected":
        return (
            "Disconnected",
            4,
            float(
                ctx.acc.last_recovery_at
                or ctx.acc.last_state_change_at
                or 0.0
            ),
        )
    return None


def _step_in_game_complete(
    ctx: _RecoveryContext,
) -> tuple[str, int, float] | None:
    if (
        ctx.state_name == "IN_GAME"
        and ctx.recovery_status in {"", "in_game"}
    ):
        return (
            "Recovery Complete",
            8,
            float(
                ctx.acc.in_game_since
                or ctx.acc.last_state_change_at
                or 0.0
            ),
        )
    return None


def _step_verify_rejoin(
    ctx: _RecoveryContext,
) -> tuple[str, int, float] | None:
    if (
        ctx.state_name == "VERIFY"
        and ctx.has_current_recovery_marker
    ):
        return (
            "Rejoining",
            6,
            float(
                ctx.acc.last_state_change_at
                or ctx.acc.last_launch_at
                or 0.0
            ),
        )
    return None


def _marker_step(
    ctx: _RecoveryContext,
    *,
    tokens: tuple[str, ...],
    label: str,
    priority: int,
    first_attr: str,
    second_attr: str,
) -> tuple[str, int, float] | None:
    if not ctx.has_current_recovery_marker or not _contains_any(
        ctx.reason_text,
        tokens,
    ):
        return None
    first = getattr(ctx.acc, first_attr)
    timestamp = (
        float(first)
        if first
        else float(getattr(ctx.acc, second_attr) or 0.0)
    )
    return label, priority, timestamp


def _step_rejoin_text(
    ctx: _RecoveryContext,
) -> tuple[str, int, float] | None:
    if not ctx.has_current_recovery_marker:
        return None
    if (
        not _contains_any(
            ctx.reason_text,
            (
                "connection_error",
                "visual_disconnect",
                "rejoin",
            ),
        )
        and ctx.state_name != "JOINING"
    ):
        return None
    return (
        "Rejoining",
        5,
        float(
            ctx.acc.recovery_scheduled_at
            or ctx.acc.last_recovery_at
            or 0.0
        ),
    )


def _step_launching_late(
    ctx: _RecoveryContext,
) -> tuple[str, int, float] | None:
    if (
        ctx.state_name in {"LAUNCHING", "STARTING"}
        or "launch" in ctx.reason_text
    ):
        return (
            "Launching",
            3,
            float(
                ctx.acc.last_launch_at
                or ctx.acc.last_state_change_at
                or 0.0
            ),
        )
    return None


def _step_kill_process(
    ctx: _RecoveryContext,
) -> tuple[str, int, float] | None:
    if not _contains_any(ctx.reason_text, ("kill", "process")):
        return None
    if (
        ctx.has_current_recovery_marker
        or ctx.recovery_status
        or bool(getattr(ctx.acc, "recovery_inflight", False))
    ):
        return (
            "Rejoining",
            2,
            float(
                ctx.acc.last_pid_change_at
                or ctx.acc.last_recovery_at
                or 0.0
            ),
        )
    return (
        "Idle",
        -1,
        float(ctx.acc.last_state_change_at or 0.0),
    )


def _step_network(
    ctx: _RecoveryContext,
) -> tuple[str, int, float] | None:
    if (
        ctx.network_state and ctx.network_state != NET_ONLINE
    ) or "network" in ctx.reason_text:
        return (
            "Disconnected",
            1,
            float(
                ctx.acc.last_network_lost_at
                or ctx.acc.last_recovery_at
                or 0.0
            ),
        )
    return None


def _step_disconnect_text(
    ctx: _RecoveryContext,
) -> tuple[str, int, float] | None:
    if _contains_any(
        ctx.reason_text,
        ("disconnect", "reconnect"),
    ):
        return (
            "Rejoining",
            5,
            float(
                ctx.acc.recovery_scheduled_at
                or ctx.acc.last_recovery_at
                or 0.0
            ),
        )
    return None


def _step_crash(
    ctx: _RecoveryContext,
) -> tuple[str, int, float] | None:
    if (
        ctx.state_name in {"CRASH", "NETWORK_LOST", "QUEUED"}
        or ctx.acc.recovery_inflight
    ):
        return (
            "Disconnected",
            0,
            float(
                ctx.acc.last_recovery_at
                or ctx.acc.last_crash_at
                or ctx.acc.last_state_change_at
                or 0.0
            ),
        )
    return None


_VIEW_STEPS = (
    _step_launching_initial,
    _step_cooldown,
    _step_in_game_alive,
    _step_disconnect_status,
    _step_in_game_complete,
    _step_verify_rejoin,
    partial(
        _marker_step,
        tokens=("session_conflict", "273"),
        label="Rejoining",
        priority=5,
        first_attr="recovery_scheduled_at",
        second_attr="last_recovery_at",
    ),
    partial(
        _marker_step,
        tokens=("popup", "disconnect_dialog"),
        label="Disconnected",
        priority=4,
        first_attr="last_recovery_at",
        second_attr="last_state_change_at",
    ),
    partial(
        _marker_step,
        tokens=("network_drop",),
        label="Rejoining",
        priority=5,
        first_attr="recovery_scheduled_at",
        second_attr="last_recovery_at",
    ),
    _step_rejoin_text,
    _step_launching_late,
    _step_kill_process,
    _step_network,
    _step_disconnect_text,
    _step_crash,
)


def recovery_step_for_account(
    acc: Any,
    display_state: AccountState,
    network_state: str = NET_ONLINE,
) -> tuple[str, int, float]:
    ctx = _build_context(acc, display_state, network_state)
    finished = _step_finished(ctx)
    if finished is not None:
        return finished
    ctx = ctx._replace(
        has_current_recovery_marker=_has_current_recovery_marker(
            ctx.acc,
            ctx.reason_text,
        ),
    )
    for resolver in _VIEW_STEPS:
        step = resolver(ctx)
        if step is not None:
            return step
    return (
        "Idle",
        -1,
        float(ctx.acc.last_state_change_at or 0.0),
    )
