"""Window-management operations for Roblox processes."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core import flog_kv
from services.process_backend import ProcessManager as _ProcessBackend


def _account_name(acc: Any) -> str:
    account_key = str(getattr(acc, "_config_username", "") or getattr(acc, "username", "") or "")
    return str(getattr(acc, "display_name", "") or getattr(acc, "username", "") or account_key)


def resize_roblox_windows(
    width: int,
    height: int,
    exclude_pids: Optional[List[int]] = None,
    reason: str = "",
    account: Any = None,
    idempotency_key: str = "",
) -> Dict[str, Any]:
    result = _ProcessBackend.resize_roblox_windows(width, height, exclude_pids=exclude_pids)
    flog_kv(
        "WINDOW",
        "process_window_resize",
        account=_account_name(account) if account is not None else "",
        width=width,
        height=height,
        resized=result.get("resized", 0),
        count=result.get("count", 0),
        reason=reason,
        process_action="resize_roblox_windows",
        idempotency_key=idempotency_key,
    )
    return result


def arrange_roblox_windows(
    width: int,
    height: int,
    columns: int = 6,
    gap: int = 2,
    margin: int = 0,
    exclude_pids: Optional[List[int]] = None,
    reason: str = "",
    account: Any = None,
    idempotency_key: str = "",
) -> Dict[str, Any]:
    result = _ProcessBackend.arrange_roblox_windows(
        width,
        height,
        columns=columns,
        gap=gap,
        margin=margin,
        exclude_pids=exclude_pids,
    )
    flog_kv(
        "WINDOW",
        "process_window_arrange",
        account=_account_name(account) if account is not None else "",
        width=width,
        height=height,
        columns=columns,
        gap=gap,
        margin=margin,
        arranged=result.get("arranged", 0),
        count=result.get("count", 0),
        reason=reason,
        process_action="arrange_roblox_windows",
        idempotency_key=idempotency_key,
    )
    return result


def restore_roblox_window_styles(
    reason: str = "",
    account: Any = None,
    idempotency_key: str = "",
) -> Dict[str, Any]:
    result = _ProcessBackend.restore_roblox_window_styles()
    flog_kv(
        "WINDOW",
        "process_window_restore",
        account=_account_name(account) if account is not None else "",
        restored=result.get("restored", 0),
        count=result.get("count", 0),
        reason=reason,
        process_action="restore_roblox_window_styles",
        idempotency_key=idempotency_key,
    )
    return result
