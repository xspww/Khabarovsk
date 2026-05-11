from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List


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


def normalize_texts(texts: Iterable[Any]) -> List[str]:
    out: List[str] = []
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


def detect_text_features(texts: Iterable[Any]) -> Dict[str, Any]:
    normalized = normalize_texts(texts)
    joined_lower = " | ".join(text.lower() for text in normalized)
    matched_code = re.search(r"error\s*code\s*[:#]?\s*(\d+)", joined_lower, flags=re.IGNORECASE)
    error_code = str(matched_code.group(1) or "").strip() if matched_code else ""
    has_disconnected = "disconnected" in joined_lower
    has_leave = "leave" in joined_lower
    has_reconnect = "reconnect" in joined_lower
    has_connection = any(keyword in joined_lower for keyword in CONNECTION_KEYWORDS)
    has_server_full = (
        "server is full" in joined_lower
        or "experience is full" in joined_lower
        or "requested game is full" in joined_lower
    )
    has_teleport = "teleport failed" in joined_lower or "teleport timeout" in joined_lower
    has_same_account = "same account launched" in joined_lower
    return {
        "texts": normalized,
        "joined": joined_lower,
        "error_code": error_code,
        "has_disconnected": has_disconnected,
        "has_leave": has_leave,
        "has_reconnect": has_reconnect,
        "has_connection": has_connection,
        "has_server_full": has_server_full,
        "has_teleport": has_teleport,
        "has_same_account": has_same_account,
        "matched": bool(error_code or has_disconnected or has_reconnect or has_connection or has_server_full or has_teleport),
        "detail": " | ".join(normalized[:4]) if normalized else "",
    }
