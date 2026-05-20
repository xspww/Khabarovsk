from __future__ import annotations

from typing import Any, Dict, Optional

from services.browser_tracker import tracker_matches


PROOF_UNTRUSTED = "untrusted"
PROOF_WEAK = "weak"
PROOF_MEDIUM = "medium"
PROOF_STRONG = "strong"

PROOF_ORDER = {
    PROOF_UNTRUSTED: 0,
    PROOF_WEAK: 1,
    PROOF_MEDIUM: 2,
    PROOF_STRONG: 3,
}

MEDIUM_STATES = {"LAUNCHING", "VERIFY"}


def _state_name(state: Any) -> str:
    return str(getattr(state, "name", state) or "").strip().upper()


def normalize_process_proof_level(level: Any) -> str:
    text = str(level or "").strip().lower()
    return text if text in PROOF_ORDER else PROOF_UNTRUSTED


def process_proof_rank(level: Any) -> int:
    return PROOF_ORDER.get(normalize_process_proof_level(level), 0)


def is_at_least_process_proof(level: Any, required: Any) -> bool:
    return process_proof_rank(level) >= process_proof_rank(required)


def required_process_proof_for_state(state: Any, destructive: bool = False) -> str:
    if destructive:
        return PROOF_STRONG
    return PROOF_MEDIUM if _state_name(state) in MEDIUM_STATES else PROOF_STRONG


def process_proof_allowed_for_state(level: Any, state: Any, destructive: bool = False) -> bool:
    return is_at_least_process_proof(level, required_process_proof_for_state(state, destructive=destructive))


def allows_destructive_process_action(level: Any) -> bool:
    return normalize_process_proof_level(level) == PROOF_STRONG


def classify_process_proof(
    validation: Dict[str, Any],
    *,
    owner_key: str = "",
    expected_identity: str = "",
    expected_browser_tracker_id: str = "",
    launched_after: Optional[float] = None,
    current_process_proof_level: str = "",
) -> Dict[str, Any]:
    if not validation or not validation.get("ok"):
        return {
            "process_proof_level": PROOF_UNTRUSTED,
            "process_proof_reason": str((validation or {}).get("reason") or "validation_failed"),
        }

    owner_key = str(owner_key or "")
    owner = str(validation.get("owner") or "")
    identity = str(validation.get("identity") or "")
    expected_identity = str(expected_identity or "")
    observed_tracker = str(validation.get("browser_tracker_id") or "")
    expected_browser_tracker_id = str(expected_browser_tracker_id or "")

    if tracker_matches(expected_browser_tracker_id, observed_tracker):
        return {"process_proof_level": PROOF_STRONG, "process_proof_reason": "browser_tracker_match"}
    current_proof_is_strong = normalize_process_proof_level(current_process_proof_level) == PROOF_STRONG
    if expected_identity and identity and identity == expected_identity and current_proof_is_strong:
        return {"process_proof_level": PROOF_STRONG, "process_proof_reason": "identity_match"}
    if owner_key and owner and owner == owner_key:
        return {"process_proof_level": PROOF_STRONG, "process_proof_reason": "owner_match"}

    confidence = float(validation.get("confidence") or 0.0)
    created = float(validation.get("created") or validation.get("create_time") or 0.0)
    windows = int(validation.get("windows") or 0)
    ram_mb = float(validation.get("ram_mb") or validation.get("rss_mb") or 0.0)
    if launched_after is not None and created and created >= (float(launched_after) - 5.0):
        return {"process_proof_level": PROOF_MEDIUM, "process_proof_reason": "launched_after"}
    if windows > 0 or ram_mb >= 100.0:
        return {"process_proof_level": PROOF_MEDIUM, "process_proof_reason": "visible_or_stable_runtime"}
    if confidence > 0:
        return {"process_proof_level": PROOF_WEAK, "process_proof_reason": "process_shape_only"}
    return {"process_proof_level": PROOF_UNTRUSTED, "process_proof_reason": "no_process_evidence"}


__all__ = [
    "PROOF_UNTRUSTED",
    "PROOF_WEAK",
    "PROOF_MEDIUM",
    "PROOF_STRONG",
    "allows_destructive_process_action",
    "classify_process_proof",
    "normalize_process_proof_level",
    "process_proof_allowed_for_state",
    "required_process_proof_for_state",
    "is_at_least_process_proof",
]
