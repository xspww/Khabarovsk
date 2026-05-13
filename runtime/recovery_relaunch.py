from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from runtime.recovery_policy import canonical_reason


def detect_relaunch_loop(
    acc: Any,
    reason_key: str,
    cfg: Dict[str, Any],
    net: Any,
    logger: Callable[[str, str], None],
) -> Optional[str]:
    canonical = canonical_reason(reason_key)
    fast_crash_reasons = {"process_crash", "watchdog_timeout", "loading_freeze"}
    if canonical not in fast_crash_reasons:
        with acc._lock:
            acc.rapid_relaunch_count = 0
        return None

    window = max(10.0, float(cfg.get("relaunch_loop_window", 45) or 45))
    limit = max(1, int(cfg.get("relaunch_loop_limit", 3) or 3))
    now = time.time()
    with acc._lock:
        runtime = (now - acc.in_game_since) if acc.in_game_since else None
        recent_network_loss = (
            acc.last_network_lost_at is not None and
            (now - acc.last_network_lost_at) <= max(window, 30.0)
        )
        if runtime is None or runtime > window:
            acc.rapid_relaunch_count = 0
            return None
        if recent_network_loss or not net.is_online():
            acc.rapid_relaunch_count = 0
            logger(
                f"[RECOVERY] {acc.display_name} rapid crash ignored "
                f"(reason={canonical}, network_context=true)",
                "warning",
            )
            return None
        acc.rapid_relaunch_count += 1
        rapid_count = acc.rapid_relaunch_count

    logger(
        f"[RECOVERY] {acc.display_name} rapid crash #{rapid_count}/{limit} "
        f"(reason={canonical}, runtime={runtime:.1f}s)",
        "warning",
    )
    if rapid_count >= limit:
        return (
            f"Stopped auto rejoin after {rapid_count} rapid crashes "
            f"within {window:.0f}s"
        )
    return None
