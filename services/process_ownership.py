from __future__ import annotations

import os
from typing import Any, Dict, Optional


def _lower_path(value: Any) -> str:
    return os.path.normcase(os.path.abspath(str(value or ""))) if value else ""


def validate_process_ownership(
    validation: Dict[str, Any],
    *,
    pid: Optional[int],
    owner_key: str,
    expected_identity: str = "",
    launched_after: Optional[float] = None,
    expected_runtime_generation: Optional[int] = None,
    current_runtime_generation: Optional[int] = None,
    expected_executable_hint: str = "RobloxPlayerBeta.exe",
) -> Dict[str, Any]:
    result = dict(validation or {})
    reasons = []
    if not pid:
        reasons.append("missing_pid")
    if expected_runtime_generation is not None and current_runtime_generation is not None:
        if int(expected_runtime_generation) != int(current_runtime_generation):
            reasons.append("runtime_generation_mismatch")

    identity = str(result.get("identity") or "")
    if expected_identity and identity and identity != str(expected_identity):
        reasons.append("identity_mismatch")

    created = float(result.get("created") or result.get("create_time") or 0.0)
    if not created:
        reasons.append("missing_create_time")
    elif launched_after is not None and created < (float(launched_after) - 5.0):
        reasons.append("stale_pid_reuse")

    name = str(result.get("name") or result.get("exe") or "")
    exe = _lower_path(result.get("exe") or result.get("path") or "")
    if expected_executable_hint:
        hint = str(expected_executable_hint).lower()
        if hint not in name.lower() and hint not in exe.lower():
            reasons.append("wrong_executable")

    owner = str(result.get("owner") or "")
    if owner and owner_key and owner != owner_key:
        reasons.append("owner_mismatch")

    if reasons:
        result["ok"] = False
        result["reason"] = reasons[0]
        result["ownership_reasons"] = reasons
    else:
        result.setdefault("ownership_reasons", [])
    return result

