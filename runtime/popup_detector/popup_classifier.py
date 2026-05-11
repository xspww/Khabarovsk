from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Dict, Iterable

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
    confidence_breakdown: Dict[str, float] = field(default_factory=dict)
    visual_disconnect: bool = False
    recovery_allowed: bool = False
    evidence_source: str = ""
    visual_strength: str = ""
    sampled_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
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
        }


def classify_popup_observation(
    texts: Iterable[Any],
    visual_features: Dict[str, Any] | None = None,
    *,
    process_idle: bool = False,
    presence_mismatch: bool = False,
    threshold: float = 1.0,
) -> PopupClassification:
    text_features = detect_text_features(texts)
    visual_features = dict(visual_features or {})
    confidence = popup_confidence_score(
        text_features,
        visual_features,
        process_idle=process_idle,
        presence_mismatch=presence_mismatch,
    )
    score = float(confidence.get("score") or 0.0)
    code = str(text_features.get("error_code") or "")
    visual_matched = bool(visual_features.get("matched") or False)
    text_matched = bool(text_features.get("matched") or False)
    visual_strength = str(visual_features.get("strength") or ("weak" if visual_matched else "none"))
    visual_strong = visual_matched and visual_strength == "strong"
    evidence_source = "error_code" if code else ("text" if text_matched else ("visual_strong" if visual_strong else ("visual_weak" if visual_matched else "")))
    matched = bool(text_matched or visual_matched or score >= threshold)

    if not matched:
        return PopupClassification(
            matched=False,
            confidence=score,
            confidence_breakdown=dict(confidence.get("breakdown") or {}),
            recovery_allowed=False,
            evidence_source=evidence_source,
            visual_strength=visual_strength,
        )

    detail = str(text_features.get("detail") or "")
    if code == "277":
        return PopupClassification(True, "rejoin", "network_drop", NETWORK_DISCONNECT, detail, code, score, dict(confidence["breakdown"]), False, True, "error_code", visual_strength)
    if code == "273":
        return PopupClassification(True, "conditional_rejoin", "session_conflict", SESSION_CONFLICT, detail, code, score, dict(confidence["breakdown"]), False, True, "error_code", visual_strength)
    if code == "267":
        return PopupClassification(True, "rejoin", "security_kick", NETWORK_DISCONNECT, detail, code, score, dict(confidence["breakdown"]), False, True, "error_code", visual_strength)
    if code == "268":
        return PopupClassification(True, "rejoin", "unexpected_client_behavior", NETWORK_DISCONNECT, detail, code, score, dict(confidence["breakdown"]), False, True, "error_code", visual_strength)
    if code:
        return PopupClassification(True, "rejoin", "connection_error", NETWORK_DISCONNECT, detail, code, score, dict(confidence["breakdown"]), False, True, "error_code", visual_strength)
    if text_features.get("has_server_full"):
        return PopupClassification(True, "rejoin", "server_full", SERVER_FULL, detail, code, score, dict(confidence["breakdown"]), False, True, "text", visual_strength)
    if text_features.get("has_teleport"):
        return PopupClassification(True, "rejoin", "teleport_timeout", TELEPORT_FAILURE, detail, code, score, dict(confidence["breakdown"]), False, True, "text", visual_strength)
    if text_matched:
        return PopupClassification(True, "rejoin", "connection_error", NETWORK_DISCONNECT, detail or "Disconnected", code, score, dict(confidence["breakdown"]), False, True, "text", visual_strength)
    if visual_matched:
        visual_detail = "visual_disconnect"
        if visual_features:
            visual_detail += (
                f" source={visual_features.get('source', '')}"
                f" strength={visual_strength}"
                f" title_rms={visual_features.get('title_rms', '')}"
                f" reconnect_rms={visual_features.get('reconnect_rms', '')}"
            )
        return PopupClassification(
            True,
            "rejoin" if visual_strong else "",
            "connection_error" if visual_strong else "",
            VISUAL_DISCONNECT if visual_strong else "",
            visual_detail,
            code,
            score,
            dict(confidence["breakdown"]),
            True,
            visual_strong,
            "visual_strong" if visual_strong else "visual_weak",
            visual_strength,
        )
    return PopupClassification(True, "rejoin", "connection_error", NETWORK_DISCONNECT, detail or "Disconnected", code, score, dict(confidence["breakdown"]), False, True, evidence_source, visual_strength)


def classify_texts(texts: Iterable[Any]) -> Dict[str, Any]:
    return classify_popup_observation(texts, threshold=0.75).to_dict()
