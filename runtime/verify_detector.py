"""Standalone verify detector — face/ID lock + passkey recovery.

Mirrors runtime/captcha_detector.py but owns two new reasons:
- identity_locked (face / ID scan)
- account_recovery (passkey / 2FA)

Two cheap sources, no screenshots:
1. Window text (local, fast): EnumWindows + GetWindowTextW for the bound
   PID, keyword match via services.verify_guard.
2. Cookie API (network): roblox_hybrid.validate_cookie_details detail
   contains lock / recovery wording.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from services.verify_guard import (
    LOCKED_REASON,
    RECOVERY_REASON,
    is_locked_text,
    is_locked_window_texts,
    is_recovery_text,
    is_recovery_window_texts,
)


def read_window_texts(pid: int | None) -> list[str]:
    if not pid:
        return []
    try:
        from runtime.captcha_detector import read_window_texts as _read

        return _read(pid)
    except Exception:
        return []


def inspect_locked_window(pid: int | None) -> dict[str, Any]:
    texts = read_window_texts(pid)
    if is_locked_window_texts(texts):
        detail = " | ".join(texts[:4]) if texts else "Roblox identity verification required"
        return {
            "matched": True,
            "reason_key": LOCKED_REASON,
            "detail": detail,
            "evidence_source": "verify_window_text",
            "confidence": 1.0,
            "popup_confidence": 1.0,
            "texts": texts[:8],
        }
    return {
        "matched": False,
        "reason_key": "",
        "detail": "",
        "evidence_source": "verify_window_text",
        "confidence": 0.0,
        "popup_confidence": 0.0,
        "texts": texts[:8],
    }


def inspect_recovery_window(pid: int | None) -> dict[str, Any]:
    texts = read_window_texts(pid)
    if is_recovery_window_texts(texts):
        detail = " | ".join(texts[:4]) if texts else "Roblox account recovery required"
        return {
            "matched": True,
            "reason_key": RECOVERY_REASON,
            "detail": detail,
            "evidence_source": "verify_window_text",
            "confidence": 1.0,
            "popup_confidence": 1.0,
            "texts": texts[:8],
        }
    return {
        "matched": False,
        "reason_key": "",
        "detail": "",
        "evidence_source": "verify_window_text",
        "confidence": 0.0,
        "popup_confidence": 0.0,
        "texts": texts[:8],
    }


def inspect_verify_window(pid: int | None) -> dict[str, Any]:
    """Check PID window texts for lock first, then recovery."""
    found = inspect_locked_window(pid)
    if found.get("matched"):
        return found
    return inspect_recovery_window(pid)


def probe_cookie_verify(
    cookie: str,
    validator: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    token = str(cookie or "").strip()
    if not token:
        return {"matched": False, "reason_key": "", "detail": "", "evidence_source": "verify_cookie_api"}
    try:
        if validator is None:
            from services.roblox_hybrid import validate_cookie_details as validator  # lazy: heavy deps
        _ok, _username, detail, _meta = validator(token)
    except Exception as exc:
        return {"matched": False, "reason_key": "", "detail": f"cookie probe failed: {exc}", "evidence_source": "verify_cookie_api"}
    detail_text = str(detail or "")
    if is_locked_text(detail_text):
        return {
            "matched": True,
            "reason_key": LOCKED_REASON,
            "detail": detail_text or "Roblox identity verification required",
            "evidence_source": "verify_cookie_api",
            "confidence": 1.0,
            "popup_confidence": 1.0,
        }
    if is_recovery_text(detail_text):
        return {
            "matched": True,
            "reason_key": RECOVERY_REASON,
            "detail": detail_text or "Roblox account recovery required",
            "evidence_source": "verify_cookie_api",
            "confidence": 1.0,
            "popup_confidence": 1.0,
        }
    return {"matched": False, "reason_key": "", "detail": detail_text, "evidence_source": "verify_cookie_api"}


def inspect_verify(
    pid: int | None,
    cookie: str = "",
    validator: Callable[..., Any] | None = None,
    *,
    window: bool = True,
    api: bool = True,
) -> dict[str, Any]:
    if window:
        found = inspect_verify_window(pid)
        if found.get("matched"):
            return found
    if api and str(cookie or "").strip():
        found = probe_cookie_verify(cookie, validator=validator)
        if found.get("matched"):
            return found
        return found
    return {"matched": False, "reason_key": "", "detail": "", "evidence_source": "verify_detector"}
