from __future__ import annotations

from typing import Any, Optional


def retry_bucket_exceeded(cfg: dict, acc: Any) -> Optional[str]:
    max_retry = max(1, int(cfg.get("max_retry", 10) or 10))
    buckets = {
        "crash_retry": acc.crash_retry_count,
        "launch_retry": acc.launch_fail_count,
        "network_retry": acc.network_retry_count,
        "session_retry": acc.session_retry_count,
    }
    for label, count in buckets.items():
        if count >= max_retry:
            return f"{label} reached max retry ({max_retry})"
    return None
