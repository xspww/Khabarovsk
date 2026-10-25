from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def bulk_usernames(body: dict[str, Any]) -> list[str]:
    """Parse and validate a bulk ``usernames`` request body."""
    raw_names = body.get("usernames", [])
    if isinstance(raw_names, str):
        raw_names = [raw_names]
    if not isinstance(raw_names, list):
        raise HTTPException(400, "usernames must be a list")
    names = [str(n or "").strip() for n in raw_names if str(n or "").strip()]
    if not names:
        raise HTTPException(400, "usernames required")
    return names
