from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from runtime.popup_detector.popup_confidence import popup_confidence_score
from runtime.popup_detector.popup_text_detector import detect_text_features
from runtime.recovery_context import (
    NETWORK_DISCONNECT,
    SERVER_FULL,
    SESSION_CONFLICT,
    TELEPORT_FAILURE,
    VISUAL_DISCONNECT,
)


@dataclass(frozen=True)
class PopupClassification:
    matched: bool
    action: str = ""
    reason_key: str = ""
    disconnect_category: str = ""
    detail: str = ""
    error_code: str = ""
    confidence: float = 0.0
    confidence_breakdown: dict[str, float] = field(default_factory=dict)
    visual_disconnect: bool = False
    recovery_allowed: bool = False
    evidence_source: str = ""
    visual_strength: str = ""
    sampled_at: float = field(default_factory=time.time)
    visual_stage: str = ""
    button_pattern: str = ""
    overlay_score: float = 0.0
    modal_score: float = 0.0
    button_score: float = 0.0
    template_score: float = 0.0
    structural_score: float = 0.0
    text_code_confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "action": self.action,
            "reason_key": self.reason_key,
            "disconnect_category": self.disconnect_category,
            "detail": self.detail,
            "error_code": self.error_code,
            "confidence": self.confidence,
            "popup_confidence": self.confidence,
            "confidence_breakdown": dict(self.confidence_breakdown),
            "visual_disconnect": self.visual_disconnect,
            "recovery_allowed": self.recovery_allowed,
            "evidence_source": self.evidence_source,
            "visual_strength": self.visual_strength,
            "sampled_at": self.sampled_at,
            "visual_stage": self.visual_stage,
            "button_pattern": self.button_pattern,
            "overlay_score": self.overlay_score,
            "modal_score": self.modal_score,
            "button_score": self.button_score,
            "template_score": self.template_score,
            "structural_score": self.structural_score,
            "text_code_confirmed": self.text_code_confirmed,
        }


class _PopupContext(NamedTuple):
    text_features: dict[str, Any]
    visual_features: dict[str, Any]
    confidence: dict[str, Any]
    score: float
    code: str
    text_matched: bool
    visual_matched: bool
    visual_strength: str
    visual_strong: bool
    evidence_source: str
    visual_meta: dict[str, Any]


_CODE_RESULTS = {
    "277": ("rejoin", "network_drop", NETWORK_DISCONNECT),
    "278": ("rejoin", "idle_disconnect", NETWORK_DISCONNECT),
    "273": (
        "conditional_rejoin",
        "session_conflict",
        SESSION_CONFLICT,
    ),
    "267": ("rejoin", "security_kick", NETWORK_DISCONNECT),
    "268": (
        "rejoin",
        "unexpected_client_behavior",
        NETWORK_DISCONNECT,
    ),
}


def _visual_meta(
    visual_features: dict[str, Any],
    code: str,
) -> dict[str, Any]:
    return {
        "visual_stage": str(
            visual_features.get("visual_stage") or ""
        ),
        "button_pattern": str(
            visual_features.get("button_pattern") or ""
        ),
        "overlay_score": float(
            visual_features.get("overlay_score") or 0.0
        ),
        "modal_score": float(
            visual_features.get("modal_score") or 0.0
        ),
        "button_score": float(
            visual_features.get("button_score") or 0.0
        ),
        "template_score": float(
            visual_features.get("template_score") or 0.0
        ),
        "structural_score": float(
            visual_features.get("structural_score") or 0.0
        ),
        "text_code_confirmed": bool(code),
    }


def _evidence_source(
    code: str,
    text_matched: bool,
    visual_strong: bool,
    visual_matched: bool,
) -> str:
    if code:
        return "error_code"
    if text_matched:
        return "text"
    if visual_strong:
        return "visual_strong"
    if visual_matched:
        return "visual_weak"
    return ""


def _build_context(
    texts: Iterable[Any],
    visual_features: dict[str, Any] | None,
    process_idle: bool,
) -> _PopupContext:
    text_features = detect_text_features(texts)
    visual = dict(visual_features or {})
    confidence = popup_confidence_score(
        text_features,
        visual,
        process_idle=process_idle,
    )
    score = float(confidence.get("score") or 0.0)
    code = str(text_features.get("error_code") or "")
    visual_matched = bool(visual.get("matched") or False)
    text_matched = bool(text_features.get("matched") or False)
    visual_strength = str(
        visual.get("strength")
        or ("weak" if visual_matched else "none")
    )
    visual_strong = (
        visual_matched and visual_strength == "strong"
    )
    evidence_source = _evidence_source(
        code,
        text_matched,
        visual_strong,
        visual_matched,
    )
    return _PopupContext(
        text_features=text_features,
        visual_features=visual,
        confidence=confidence,
        score=score,
        code=code,
        text_matched=text_matched,
        visual_matched=visual_matched,
        visual_strength=visual_strength,
        visual_strong=visual_strong,
        evidence_source=evidence_source,
        visual_meta=_visual_meta(visual, code),
    )


def _soft_breakdown(ctx: _PopupContext) -> dict[str, float]:
    return dict(ctx.confidence.get("breakdown") or {})


def _direct_breakdown(ctx: _PopupContext) -> dict[str, float]:
    return dict(ctx.confidence["breakdown"])


def _make(
    ctx: _PopupContext,
    breakdown: dict[str, float],
    **overrides: Any,
) -> PopupClassification:
    values: dict[str, Any] = {
        "confidence": ctx.score,
        "confidence_breakdown": breakdown,
        "visual_strength": ctx.visual_strength,
        **ctx.visual_meta,
    }
    values.update(overrides)
    return PopupClassification(**values)


def _non_match(
    ctx: _PopupContext,
    breakdown: dict[str, float],
) -> PopupClassification:
    return _make(
        ctx,
        breakdown,
        matched=False,
        recovery_allowed=False,
        evidence_source=ctx.evidence_source,
    )


def _matched_make(
    ctx: _PopupContext,
    detail: str,
    breakdown: dict[str, float],
    *,
    reason_key: str,
    disconnect_category: str,
    evidence_source: str,
    action: str = "rejoin",
    visual_disconnect: bool = False,
    recovery_allowed: bool = True,
) -> PopupClassification:
    return _make(
        ctx,
        breakdown,
        matched=True,
        action=action,
        reason_key=reason_key,
        disconnect_category=disconnect_category,
        detail=detail,
        error_code=ctx.code,
        visual_disconnect=visual_disconnect,
        recovery_allowed=recovery_allowed,
        evidence_source=evidence_source,
    )


def _code_result(
    ctx: _PopupContext,
    detail: str,
    breakdown: dict[str, float],
) -> PopupClassification | None:
    spec = _CODE_RESULTS.get(ctx.code)
    if spec is not None:
        action, reason_key, category = spec
        return _matched_make(
            ctx,
            detail,
            breakdown,
            action=action,
            reason_key=reason_key,
            disconnect_category=category,
            evidence_source="error_code",
        )
    if ctx.code:
        return _matched_make(
            ctx,
            detail,
            breakdown,
            reason_key="connection_error",
            disconnect_category=NETWORK_DISCONNECT,
            evidence_source="error_code",
        )
    return None


def _text_result(
    ctx: _PopupContext,
    detail: str,
    breakdown: dict[str, float],
) -> PopupClassification | None:
    if ctx.text_features.get("has_server_full"):
        return _matched_make(
            ctx,
            detail,
            breakdown,
            reason_key="server_full",
            disconnect_category=SERVER_FULL,
            evidence_source="text",
        )
    if ctx.text_features.get("has_teleport"):
        return _matched_make(
            ctx,
            detail,
            breakdown,
            reason_key="teleport_timeout",
            disconnect_category=TELEPORT_FAILURE,
            evidence_source="text",
        )
    if ctx.text_matched:
        return _matched_make(
            ctx,
            detail or "Disconnected",
            breakdown,
            reason_key="connection_error",
            disconnect_category=NETWORK_DISCONNECT,
            evidence_source="text",
        )
    return None


def _visual_result(
    ctx: _PopupContext,
    breakdown: dict[str, float],
) -> PopupClassification | None:
    if not ctx.visual_matched:
        return None
    template_confirmed = bool(
        ctx.visual_strong
        and (
            str(ctx.visual_features.get("source") or "")
            == "template"
            or str(
                ctx.visual_features.get("visual_stage") or ""
            )
            == "template"
            or float(
                ctx.visual_features.get("template_score") or 0.0
            )
            >= 0.55
        )
    )
    visual_detail = "visual_disconnect"
    if ctx.visual_features:
        visual_detail += (
            f" source={ctx.visual_features.get('source', '')}"
            f" strength={ctx.visual_strength}"
            " stage="
            f"{ctx.visual_features.get('visual_stage', '')}"
            " button="
            f"{ctx.visual_features.get('button_pattern', '')}"
            " title_rms="
            f"{ctx.visual_features.get('title_rms', '')}"
            " reconnect_rms="
            f"{ctx.visual_features.get('reconnect_rms', '')}"
        )
    return _matched_make(
        ctx,
        visual_detail,
        breakdown,
        action="rejoin" if template_confirmed else "",
        reason_key="connection_error" if template_confirmed else "",
        disconnect_category=(
            VISUAL_DISCONNECT if template_confirmed else ""
        ),
        evidence_source=(
            "visual_strong"
            if ctx.visual_strong
            else "visual_weak"
        ),
        visual_disconnect=True,
        recovery_allowed=template_confirmed,
    )


def _final_result(
    ctx: _PopupContext,
    detail: str,
    breakdown: dict[str, float],
) -> PopupClassification:
    return _matched_make(
        ctx,
        detail or "Disconnected",
        breakdown,
        reason_key="connection_error",
        disconnect_category=NETWORK_DISCONNECT,
        evidence_source=ctx.evidence_source,
    )


def _classify_matched(
    ctx: _PopupContext,
    detail: str,
    breakdown: dict[str, float],
) -> PopupClassification:
    result = _code_result(ctx, detail, breakdown)
    if result is not None:
        return result
    result = _text_result(ctx, detail, breakdown)
    if result is not None:
        return result
    result = _visual_result(ctx, breakdown)
    if result is not None:
        return result
    return _final_result(ctx, detail, breakdown)


def _is_matched(ctx: _PopupContext, threshold: float) -> bool:
    return bool(
        ctx.text_matched
        or ctx.visual_matched
        or ctx.score >= threshold
    )


def classify_popup_observation(
    texts: Iterable[Any],
    visual_features: dict[str, Any] | None = None,
    *,
    process_idle: bool = False,
    threshold: float = 1.0,
) -> PopupClassification:
    ctx = _build_context(texts, visual_features, process_idle)
    matched = _is_matched(ctx, threshold)

    if not matched:
        return _non_match(ctx, _soft_breakdown(ctx))

    # Captcha is owned by runtime/captcha_detector.py — popup detector must
    # never claim it, otherwise a Security Verification page would trigger
    # a disconnect rejoin (or a captcha hold from the wrong pipeline).
    if ctx.text_features.get("has_captcha") and not ctx.code:
        return _non_match(ctx, _soft_breakdown(ctx))
    # Locked (face/ID) and Recovery (passkey/2FA) are owned by
    # runtime/verify_detector.py — same rule: never a disconnect rejoin.
    if (
        ctx.text_features.get("has_locked")
        or ctx.text_features.get("has_recovery")
    ) and not ctx.code:
        return _non_match(ctx, _soft_breakdown(ctx))
    if ctx.visual_features.get("captcha_challenge"):
        return _non_match(ctx, _soft_breakdown(ctx))

    detail = str(ctx.text_features.get("detail") or "")
    return _classify_matched(
        ctx,
        detail,
        _direct_breakdown(ctx),
    )


def classify_texts(texts: Iterable[Any]) -> dict[str, Any]:
    return classify_popup_observation(texts, threshold=0.75).to_dict()
