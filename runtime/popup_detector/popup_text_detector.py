from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from services.captcha_guard import is_captcha_window_texts
from services.verify_guard import is_locked_window_texts, is_recovery_window_texts

CONNECTION_KEYWORDS = (
    "connection error",
    "lost connection",
    "disconnected",
    "reconnect",
    "failed to connect",
    "teleport failed",
    "internet connection",
    "connection lost",
    "please check your internet connection",
    "lost connection to the game server",
    "same account launched",
)


def normalize_texts(texts: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen = set()
    for item in texts or []:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _has_connection(joined_lower: str) -> bool:
    return any(keyword in joined_lower for keyword in CONNECTION_KEYWORDS)


def _server_full(joined_lower: str) -> bool:
    return (
        "server is full" in joined_lower
        or "experience is full" in joined_lower
        or "requested game is full" in joined_lower
    )


def _teleport_failed(joined_lower: str) -> bool:
    return "teleport failed" in joined_lower or "teleport timeout" in joined_lower


def _matched(features: dict[str, Any]) -> bool:
    return bool(
        features["error_code"]
        or features["has_disconnected"]
        or features["has_reconnect"]
        or features["has_connection"]
        or features["has_server_full"]
        or features["has_teleport"]
        or features["has_captcha"]
        or features["has_locked"]
        or features["has_recovery"]
    )


def detect_text_features(texts: Iterable[Any]) -> dict[str, Any]:
    normalized = normalize_texts(texts)
    joined_lower = " | ".join(text.lower() for text in normalized)
    matched_code = re.search(r"error\s*code\s*[:#]?\s*(\d+)", joined_lower, flags=re.IGNORECASE)
    error_code = str(matched_code.group(1) or "").strip() if matched_code else ""
    features = {
        "texts": normalized,
        "joined": joined_lower,
        "error_code": error_code,
        "has_disconnected": "disconnected" in joined_lower,
        "has_leave": "leave" in joined_lower,
        "has_reconnect": "reconnect" in joined_lower,
        "has_connection": _has_connection(joined_lower),
        "has_server_full": _server_full(joined_lower),
        "has_teleport": _teleport_failed(joined_lower),
        "has_same_account": "same account launched" in joined_lower,
        "has_captcha": is_captcha_window_texts(normalized),
        "has_locked": is_locked_window_texts(normalized),
        "has_recovery": is_recovery_window_texts(normalized),
    }
    features["matched"] = _matched(features)
    features["detail"] = " | ".join(normalized[:4]) if normalized else ""
    return features
