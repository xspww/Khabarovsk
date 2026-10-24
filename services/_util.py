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
