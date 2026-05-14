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
    if text_features.get("has_captcha"):
        breakdown["captcha_text"] = 1.0

    if bool(visual_features.get("matched")):
        overlay_score = float(visual_features.get("overlay_score") or 0.0)
        modal_score = float(visual_features.get("modal_score") or 0.0)
        button_score = float(visual_features.get("button_score") or 0.0)
        template_score = float(visual_features.get("template_score") or 0.0)
        structural_score = float(visual_features.get("structural_score") or 0.0)
        if overlay_score > 0:
            breakdown["visual_overlay"] = min(0.25, overlay_score)
        if modal_score > 0:
            breakdown["visual_modal"] = min(0.45, modal_score)
        if button_score > 0:
            breakdown["visual_button"] = min(0.40, button_score)
        if template_score > 0:
            breakdown["visual_template"] = min(0.70, template_score)
        if not any(key.startswith("visual_") for key in breakdown):
            visual_score = float(visual_features.get("score") or 0.0)
            if visual_score > 0:
                breakdown["visual_structural"] = min(0.45, max(visual_score, structural_score))

    if process_idle:
        breakdown["process_idle"] = 0.3
    score = round(sum(breakdown.values()), 3)
    return {"score": score, "breakdown": breakdown}
