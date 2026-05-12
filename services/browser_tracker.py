from __future__ import annotations

import re
from typing import Any


_TRACKER_PATTERNS = (
    re.compile(r"browsertrackerid:([A-Za-z0-9_-]+)", re.IGNORECASE),
    re.compile(r"browserTrackerId=([A-Za-z0-9_-]+)", re.IGNORECASE),
    re.compile(r"browsertrackerid=([A-Za-z0-9_-]+)", re.IGNORECASE),
    re.compile(r"browserTrackerID=([A-Za-z0-9_-]+)", re.IGNORECASE),
)


def extract_browser_tracker_id(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    for pattern in _TRACKER_PATTERNS:
        match = pattern.search(text)
        if match:
            return str(match.group(1) or "")
    return ""


def tracker_matches(expected: str = "", observed: str = "") -> bool:
    expected = str(expected or "").strip()
    observed = str(observed or "").strip()
    return bool(expected and observed and expected == observed)


def tracker_label(value: Any) -> str:
    tracker = str(value or "").strip()
    if not tracker:
        return ""
    return tracker if len(tracker) <= 8 else f"...{tracker[-8:]}"
