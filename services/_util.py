from __future__ import annotations

import ctypes
from typing import Any


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _cfg(cfg_mgr: Any, key: str, default: Any = None) -> Any:
    try:
        return cfg_mgr.get(key, default)
    except Exception:
        return default


def as_number(value: Any, default: float) -> float:
    """float() that only falls back on None/''/invalid — never on 0."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        return float(value)
    except Exception:
        return default


def is_cookie_auth_status_text(text: str) -> bool:
    return (
        "cookie identity mismatch" in text
        or "cookie belongs to" in text
        or ".roblosecurity" in text
        or "cookie invalid" in text
    )
