from __future__ import annotations

from typing import Any, NamedTuple


class _VisualScores(NamedTuple):
    overlay: float
    modal: float
    button: float
    template: float
    structural: float
    captcha: float


def _text_breakdown(text_features: dict[str, Any]) -> dict[str, float]:
    breakdown: dict[str, float] = {}
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
    return breakdown


def _visual_scores(visual_features: dict[str, Any]) -> _VisualScores:
    return _VisualScores(
        float(visual_features.get("overlay_score") or 0.0),
        float(visual_features.get("modal_score") or 0.0),
        float(visual_features.get("button_score") or 0.0),
        float(visual_features.get("template_score") or 0.0),
        float(visual_features.get("structural_score") or 0.0),
        float(visual_features.get("captcha_score") or 0.0),
    )


def _visual_breakdown(visual_features: dict[str, Any]) -> dict[str, float]:
    breakdown: dict[str, float] = {}
    if not bool(visual_features.get("matched")):
        return breakdown
    scores = _visual_scores(visual_features)
    if bool(visual_features.get("captcha_challenge")):
        breakdown["visual_captcha"] = min(1.0, max(0.75, scores.captcha))
    if scores.overlay > 0:
        breakdown["visual_overlay"] = min(0.25, scores.overlay)
    if scores.modal > 0:
        breakdown["visual_modal"] = min(0.45, scores.modal)
    if scores.button > 0:
        breakdown["visual_button"] = min(0.40, scores.button)
    if scores.template > 0:
        breakdown["visual_template"] = min(0.70, scores.template)
    if not any(key.startswith("visual_") for key in breakdown):
        visual_score = float(visual_features.get("score") or 0.0)
        if visual_score > 0:
            breakdown["visual_structural"] = min(
                0.45,
                max(visual_score, scores.structural),
            )
    return breakdown


def popup_confidence_score(
    text_features: dict[str, Any],
    visual_features: dict[str, Any] | None = None,
    *,
    process_idle: bool = False,
) -> dict[str, Any]:
    visual_features = dict(visual_features or {})
    breakdown = _text_breakdown(text_features)
    breakdown.update(_visual_breakdown(visual_features))
    if process_idle:
        breakdown["process_idle"] = 0.3
    score = round(sum(breakdown.values()), 3)
    return {"score": score, "breakdown": breakdown}
