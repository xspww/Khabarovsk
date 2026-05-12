from __future__ import annotations

import time
import uuid
import hashlib
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from services.browser_tracker import tracker_label


def _new_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class RuntimeSessionIdentity:
    account_id: str
    runtime_generation: int
    account_runtime_id: str
    session_id: str
    launch_nonce: str
    reason: str = ""
    created_at: float = field(default_factory=time.time)
    launch_intent: Dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "runtime_generation": self.runtime_generation,
            "account_runtime_id": self.account_runtime_id,
            "session_id": self.session_id,
            "launch_nonce": self.launch_nonce,
            "reason": self.reason,
            "created_at": self.created_at,
            "launch_intent": dict(self.launch_intent or {}),
        }


@dataclass
class RejoinTransaction:
    transaction_id: str
    account_id: str
    runtime_generation: int
    recovery_generation: int
    command_generation: int
    account_runtime_id: str
    session_id: str
    launch_nonce: str
    status: str = "pending"
    step: str = "begin"
    reason: str = ""
    failure_reason: str = ""
    launch_intent: Dict[str, Any] = field(default_factory=dict)
    destination_evidence: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: float = 0.0

    def update(self, status: Optional[str] = None, step: Optional[str] = None, reason: str = "", failure_reason: str = "", destination_evidence: Optional[Dict[str, Any]] = None) -> None:
        if status:
            self.status = status
        if step:
            self.step = step
        if reason:
            self.reason = reason
        if failure_reason:
            self.failure_reason = failure_reason
        if destination_evidence:
            self.destination_evidence = dict(destination_evidence)
        self.updated_at = time.time()
        if self.status in {"committed", "rolled_back", "failed"}:
            self.completed_at = self.updated_at

    def snapshot(self) -> Dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "account_id": self.account_id,
            "runtime_generation": self.runtime_generation,
            "recovery_generation": self.recovery_generation,
            "command_generation": self.command_generation,
            "account_runtime_id": self.account_runtime_id,
            "session_id": self.session_id,
            "launch_nonce": self.launch_nonce,
            "status": self.status,
            "step": self.step,
            "reason": self.reason,
            "failure_reason": self.failure_reason,
            "launch_intent": dict(self.launch_intent or {}),
            "destination_evidence": dict(self.destination_evidence or {}),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }


def create_session_identity(account_id: str, runtime_generation: int, account_runtime_id: str = "", reason: str = "", launch_intent: Optional[Dict[str, Any]] = None) -> RuntimeSessionIdentity:
    runtime_id = account_runtime_id or _new_id("acctrt")
    return RuntimeSessionIdentity(
        account_id=str(account_id or ""),
        runtime_generation=int(runtime_generation or 0),
        account_runtime_id=runtime_id,
        session_id=_new_id("sess"),
        launch_nonce=_new_id("nonce"),
        reason=str(reason or ""),
        launch_intent=dict(launch_intent or {}),
    )


def create_rejoin_transaction(identity: RuntimeSessionIdentity, recovery_generation: int = 0, command_generation: int = 0, reason: str = "") -> RejoinTransaction:
    return RejoinTransaction(
        transaction_id=_new_id("rtx"),
        account_id=identity.account_id,
        runtime_generation=identity.runtime_generation,
        recovery_generation=int(recovery_generation or 0),
        command_generation=int(command_generation or 0),
        account_runtime_id=identity.account_runtime_id,
        session_id=identity.session_id,
        launch_nonce=identity.launch_nonce,
        reason=str(reason or identity.reason or ""),
        launch_intent=dict(identity.launch_intent or {}),
    )


def _safe_hash(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _parse_vip_link(vip_url: str) -> Dict[str, Any]:
    text = str(vip_url or "").strip()
    if not text:
        return {}
    try:
        parsed = urllib.parse.urlparse(text)
        qs = urllib.parse.parse_qs(parsed.query)
        match = re.search(r"/games/(\d+)", parsed.path or "")
        place_id = match.group(1) if match else qs.get("placeId", [""])[0]
        link_code = (
            qs.get("privateServerLinkCode", [""])[0]
            or qs.get("linkCode", [""])[0]
            or qs.get("code", [""])[0]
            or qs.get("accessCode", [""])[0]
        )
        job_id = (
            qs.get("gameInstanceId", [""])[0]
            or qs.get("jobId", [""])[0]
            or qs.get("serverId", [""])[0]
        )
        return {
            "place_id": str(place_id or ""),
            "link_code_hash": _safe_hash(link_code),
            "job_id_hash": _safe_hash(job_id),
            "url_hash": _safe_hash(text),
            "has_private_code": bool(link_code),
            "has_job_id": bool(job_id),
        }
    except Exception:
        return {"url_hash": _safe_hash(text), "parse_error": True}


def _unique(values: List[str], limit: int = 10) -> List[str]:
    seen = set()
    out: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def build_launch_intent(account: Any, reason: str = "") -> Dict[str, Any]:
    vip_links = list(getattr(account, "vip_links", []) or [])
    active_vip = str(getattr(account, "active_vip", "") or "")
    browser_tracker_id = str(getattr(account, "browser_tracker_id", "") or "")
    place_id = str(getattr(account, "place_id", "") or "")
    server_type = getattr(getattr(account, "server_type", None), "value", str(getattr(account, "server_type", "") or ""))
    launch_strategy = str(getattr(account, "launch_strategy", "") or "")
    parsed_active = _parse_vip_link(active_vip)
    parsed_links = [_parse_vip_link(link) for link in vip_links]
    configured_place_ids = _unique([str(item.get("place_id", "") or "") for item in parsed_links])
    configured_private_hashes = _unique([str(item.get("link_code_hash", "") or "") for item in parsed_links])
    active_private_hash = str(parsed_active.get("link_code_hash", "") or "")
    active_place_id = str(parsed_active.get("place_id", "") or "")
    effective_place_id = place_id or active_place_id or (configured_place_ids[0] if configured_place_ids else "")
    server_type_norm = (server_type or "UNKNOWN").upper()
    private_server_intent = bool(
        active_private_hash
        or configured_private_hashes
        or server_type_norm in {"VIP", "PRIVATE", "PRIVATE_SERVER"}
    )
    if private_server_intent and active_private_hash:
        vip_intent = "active_private"
    elif private_server_intent:
        vip_intent = "configured_private"
    elif vip_links or active_vip:
        vip_intent = "configured_public"
    else:
        vip_intent = "none"
    launch_intent_summary = {
        "place_id": effective_place_id,
        "server_type": server_type_norm or "UNKNOWN",
        "launch_strategy": launch_strategy,
        "vip_intent": vip_intent,
        "private_server_intent": private_server_intent,
        "active_vip_hash": str(parsed_active.get("url_hash", "") or ""),
        "active_private_link_code_hash": active_private_hash,
        "browser_tracker_id": tracker_label(browser_tracker_id),
    }
    return {
        "reason": str(reason or ""),
        "place_id": effective_place_id,
        "vip_configured": bool(vip_links),
        "vip_count": len(vip_links),
        "active_vip_present": bool(active_vip),
        "server_type": server_type_norm or "UNKNOWN",
        "launch_strategy": launch_strategy,
        "vip_intent": vip_intent,
        "private_server_intent": private_server_intent,
        "active_vip_place_id": active_place_id,
        "active_vip_hash": str(parsed_active.get("url_hash", "") or ""),
        "active_private_link_code_hash": active_private_hash,
        "active_job_id_hash": str(parsed_active.get("job_id_hash", "") or ""),
        "configured_vip_place_ids": configured_place_ids,
        "configured_private_link_code_hashes": configured_private_hashes,
        "browser_tracker_id": tracker_label(browser_tracker_id),
        "launch_intent_summary": launch_intent_summary,
    }
