"""Standalone captcha detector — independent of the popup/disconnect detector.

Two cheap sources, no screenshots, no PIL, no confidence scoring:

1. Window text (local, fast): EnumWindows + GetWindowTextW for the bound
   PID, keyword match via ``services.captcha_guard.is_captcha_window_texts``.
2. Cookie API (network): ``roblox_hybrid.validate_cookie_details`` detail
   contains a Roblox challenge (``services.captcha_guard.is_captcha_text``).
   Used for stuck accounts / launch / reload paths, never per-tick.

Popup detector (``runtime/popup_detector``) owns disconnects only and must
never report captcha — see ``popup_classifier`` guard.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from services.captcha_guard import (
    CAPTCHA_REASON,
    is_captcha_text,
    is_captcha_window_texts,
)


def read_window_texts(pid: Optional[int]) -> List[str]:
    """Read visible window + child control texts for a PID (text only)."""
    if not pid:
        return []
    try:
        import ctypes

        user32 = ctypes.windll.user32  # Windows only; AttributeError elsewhere
    except Exception:
        return []
    try:
        texts: List[str] = []
        seen = set()
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

        def _read_text(hwnd) -> str:
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return ""
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            return str(buf.value or "").strip()

        def _collect(hwnd) -> None:
            text = _read_text(hwnd)
            if not text:
                return
            key = text.lower()
            if key not in seen:
                seen.add(key)
                texts.append(text)

        def _child_callback(child_hwnd, lparam):
            _collect(child_hwnd)
            return True

        def _enum_callback(hwnd, lparam):
            win_pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
            if int(win_pid.value or 0) != int(pid):
                return True
            _collect(hwnd)
            user32.EnumChildWindows(
                ctypes.c_void_p(hwnd), WNDENUMPROC(_child_callback), 0
            )
            return True

        user32.EnumWindows(WNDENUMPROC(_enum_callback), 0)
        return texts
    except Exception:
        return []


def inspect_captcha_window(pid: Optional[int]) -> Dict[str, Any]:
    """Check a bound PID's window texts for a captcha challenge."""
    texts = read_window_texts(pid)
    if is_captcha_window_texts(texts):
        detail = " | ".join(texts[:4]) if texts else "Roblox captcha challenge visible"
        return {
            "matched": True,
            "reason_key": CAPTCHA_REASON,
            "detail": detail,
            "evidence_source": "captcha_window_text",
            "confidence": 1.0,
            "popup_confidence": 1.0,
            "texts": texts[:8],
        }
    return {
        "matched": False,
        "reason_key": "",
        "detail": "",
        "evidence_source": "captcha_window_text",
        "confidence": 0.0,
        "popup_confidence": 0.0,
        "texts": texts[:8],
    }


def probe_cookie_captcha(
    cookie: str,
    validator: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Check a .ROBLOSECURITY cookie for a Roblox challenge response."""
    token = str(cookie or "").strip()
    if not token:
        return {
            "matched": False,
            "reason_key": "",
            "detail": "",
            "evidence_source": "captcha_cookie_api",
        }
    try:
        if validator is None:
            from roblox_hybrid import validate_cookie_details as validator  # lazy: heavy deps
        ok, _username, detail, _meta = validator(token)
    except Exception as exc:
        return {
            "matched": False,
            "reason_key": "",
            "detail": f"cookie probe failed: {exc}",
            "evidence_source": "captcha_cookie_api",
        }
    if is_captcha_text(detail):
        return {
            "matched": True,
            "reason_key": CAPTCHA_REASON,
            "detail": str(detail or "Roblox CAPTCHA challenge detected"),
            "evidence_source": "captcha_cookie_api",
            "confidence": 1.0,
            "popup_confidence": 1.0,
        }
    return {
        "matched": False,
        "reason_key": "",
        "detail": str(detail or ""),
        "evidence_source": "captcha_cookie_api",
    }


def inspect_captcha(
    pid: Optional[int],
    cookie: str = "",
    validator: Optional[Callable[..., Any]] = None,
    *,
    window: bool = True,
    api: bool = True,
) -> Dict[str, Any]:
    """Combined captcha check: window text first (cheap), cookie API second."""
    if window:
        found = inspect_captcha_window(pid)
        if found.get("matched"):
            return found
    if api and str(cookie or "").strip():
        found = probe_cookie_captcha(cookie, validator=validator)
        if found.get("matched"):
            return found
        return found
    return {
        "matched": False,
        "reason_key": "",
        "detail": "",
        "evidence_source": "captcha_detector",
    }
