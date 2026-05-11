from __future__ import annotations

from typing import Any, Dict


def popup_confidence_score(
    text_features: Dict[str, Any],
    visual_features: Dict[str, Any] | None = None,
    *,
    process_idle: bool = False,
    presence_mismatch: bool = False,
) -> Dict[str, Any]:
    visual_features = dict(visual_features or {})
    score = 0.0
    breakdown: Dict[str, float] = {}

    if text_features.get("has_disconnected"):
        breakdown["disconnected_text"] = 0.5
    if text_features.get("error_code"):
        breakdown["error_code"] = 1.0
    if text_features.get("has_reconnect"):
        breakdown["reconnect_text"] = 0.4
    if text_features.get("has_connection"):
        breakdown["connection_text"] = 0.3
    if text_features.get("has_server_full") or text_features.get("has_teleport"):
        breakdown["roblox_error_text"] = 0.5

    visual_score = float(visual_features.get("score") or 0.0)
    if bool(visual_features.get("matched")) and visual_score > 0:
        breakdown["visual"] = min(1.0, visual_score)

    if process_idle:
        breakdown["process_idle"] = 0.3
    if presence_mismatch:
        breakdown["presence_mismatch"] = 0.2

    score = round(sum(breakdown.values()), 3)
    return {"score": score, "breakdown": breakdown}
