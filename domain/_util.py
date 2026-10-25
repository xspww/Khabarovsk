from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any


def _safe_hash(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def account_key(account: Any) -> str:
    """Return the stable config username for an account object."""
    return str(getattr(account, "_config_username", "") or getattr(account, "username", "") or "")


def account_display(account: Any) -> str:
    """Return the best human-readable label for an account object."""
    return str(
        getattr(account, "display_name", "")
        or getattr(account, "display", "")
        or account_key(account)
    )


def as_int(mapping: Mapping[str, Any], key: str, default: int = 0) -> int:
    return int(mapping.get(key) or default)


def as_float(mapping: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    return float(mapping.get(key) or default)


def mapping_text(mapping: Mapping[str, Any], key: str, default: str = "") -> str:
    return str(mapping.get(key) or default)


def attr_text(obj: Any, name: str, default: str = "") -> str:
    return str(getattr(obj, name, default) or default)


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def record_text(mapping: Mapping[str, Any], key: str) -> str:
    return str(mapping.get(key) or "").strip()


def first_text(*values: Any) -> str:
    for value in values:
        if value:
            return str(value)
    return ""


def contains_any(text: str, tokens: Iterable[str]) -> bool:
    return any(token in text for token in tokens)


def coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return bool(default)


def identity_matches(expected: Any, actual: Any) -> bool:
    return bool(expected and actual and actual == str(expected))


def int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def account_pid(account: Any) -> int:
    return int_or_zero(getattr(account, "pid", 0))


def state_name(value: Any) -> str:
    return str(getattr(value, "name", value) or "").upper()


def event_type(event: Mapping[str, Any]) -> str:
    return str(event.get("event_type") or event.get("kind") or "").lower()


def config_bool(mapping: Mapping[str, Any], key: str, default: bool) -> bool:
    value = mapping.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def config_float(mapping: Mapping[str, Any], key: str, default: float) -> float:
    try:
        return float(mapping.get(key, default))
    except Exception:
        return float(default)


def config_int(mapping: Mapping[str, Any], key: str, default: int) -> int:
    try:
        return int(float(mapping.get(key, default)))
    except Exception:
        return int(default)


def clamped_float(
    mapping: Mapping[str, Any], key: str, default: float, minimum: float, maximum: float
) -> float:
    try:
        value = float(mapping.get(key, default) or default)
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def start_thread_once(thread: Any) -> None:
    if not thread.is_alive():
        thread.start()
