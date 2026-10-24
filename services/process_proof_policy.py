from __future__ import annotations

from typing import Any, NamedTuple

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


def score_resource_signals(windows: Any = 0, rss_mb: Any = 0.0, cpu: Any = 0.0) -> float:
    """Single owner for the shared resource-signal weights.

    Both the single-process validator and the multi-signal ranker leverage
    this so the windows/RAM/CPU math cannot drift between the two seams.
    """
    try:
        return (
            min(15.0, float(windows or 0) * 7.0)
            + min(12.0, float(rss_mb or 0.0) / 120.0)
            + min(10.0, float(cpu or 0.0) * 2.0)
        )
    except Exception:
        return 0.0


class _ValidationText(NamedTuple):
    owner_key: str
    owner: str
    identity: str
    expected_identity: str
    observed_tracker: str
    expected_tracker: str


class _ValidationMetrics(NamedTuple):
    confidence: float
    created: float
    windows: int
    ram_mb: float


def _validation_text(
    validation: dict[str, Any],
    owner_key: str,
    expected_identity: str,
    expected_browser_tracker_id: str,
) -> _ValidationText:
    return _ValidationText(
        owner_key=str(owner_key or ""),
        owner=str(validation.get("owner") or ""),
        identity=str(validation.get("identity") or ""),
        expected_identity=str(expected_identity or ""),
        observed_tracker=str(validation.get("browser_tracker_id") or ""),
        expected_tracker=str(expected_browser_tracker_id or ""),
    )


def _identity_proof(
    text: _ValidationText,
    current_process_proof_level: str,
) -> dict[str, Any] | None:
    if tracker_matches(text.expected_tracker, text.observed_tracker):
        return {
            "process_proof_level": PROOF_STRONG,
            "process_proof_reason": "browser_tracker_match",
        }
    current_proof_is_strong = (
        normalize_process_proof_level(current_process_proof_level) == PROOF_STRONG
    )
    if (
        text.expected_identity
        and text.identity
        and text.identity == text.expected_identity
        and current_proof_is_strong
    ):
        return {
            "process_proof_level": PROOF_STRONG,
            "process_proof_reason": "identity_match",
        }
    if text.owner_key and text.owner and text.owner == text.owner_key:
        return {
            "process_proof_level": PROOF_STRONG,
            "process_proof_reason": "owner_match",
        }
    return None


def _validation_metrics(validation: dict[str, Any]) -> _ValidationMetrics:
    return _ValidationMetrics(
        confidence=float(validation.get("confidence") or 0.0),
        created=float(
            validation.get("created") or validation.get("create_time") or 0.0
        ),
        windows=int(validation.get("windows") or 0),
        ram_mb=float(
            validation.get("ram_mb") or validation.get("rss_mb") or 0.0
        ),
    )


def _runtime_proof(
    metrics: _ValidationMetrics,
    launched_after: float | None,
) -> dict[str, Any] | None:
    if (
        launched_after is not None
        and metrics.created
        and metrics.created >= (float(launched_after) - 5.0)
    ):
        return {
            "process_proof_level": PROOF_MEDIUM,
            "process_proof_reason": "launched_after",
        }
    if metrics.windows > 0 or metrics.ram_mb >= 100.0:
        return {
            "process_proof_level": PROOF_MEDIUM,
            "process_proof_reason": "visible_or_stable_runtime",
        }
    if metrics.confidence > 0:
        return {
            "process_proof_level": PROOF_WEAK,
            "process_proof_reason": "process_shape_only",
        }
    return None


def classify_process_proof(
    validation: dict[str, Any],
    *,
    owner_key: str = "",
    expected_identity: str = "",
    expected_browser_tracker_id: str = "",
    launched_after: float | None = None,
    current_process_proof_level: str = "",
) -> dict[str, Any]:
    if not validation or not validation.get("ok"):
        return {
            "process_proof_level": PROOF_UNTRUSTED,
            "process_proof_reason": str((validation or {}).get("reason") or "validation_failed"),
        }

    text = _validation_text(
        validation,
        owner_key,
        expected_identity,
        expected_browser_tracker_id,
    )
    strong = _identity_proof(text, current_process_proof_level)
    if strong is not None:
        return strong
    metrics = _validation_metrics(validation)
    runtime = _runtime_proof(metrics, launched_after)
    if runtime is not None:
        return runtime
    return {
        "process_proof_level": PROOF_UNTRUSTED,
        "process_proof_reason": "no_process_evidence",
    }


__all__ = [
    "PROOF_MEDIUM",
    "PROOF_STRONG",
    "PROOF_UNTRUSTED",
    "PROOF_WEAK",
    "allows_destructive_process_action",
    "classify_process_proof",
    "is_at_least_process_proof",
    "normalize_process_proof_level",
    "process_proof_allowed_for_state",
    "required_process_proof_for_state",
    "score_resource_signals",
]
