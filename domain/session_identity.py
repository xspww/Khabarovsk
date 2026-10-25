from __future__ import annotations

import re
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from domain._util import _safe_hash
from domain._util import attr_text as _account_text
from domain._util import first_text as _first_text
from domain._util import mapping_text as _mapping_text
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
    launch_intent: dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
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
    launch_intent: dict[str, Any] = field(default_factory=dict)
    destination_evidence: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: float = 0.0

    def update(self, status: str | None = None, step: str | None = None, reason: str = "", failure_reason: str = "", destination_evidence: dict[str, Any] | None = None) -> None:
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

    def snapshot(self) -> dict[str, Any]:
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


def create_session_identity(account_id: str, runtime_generation: int, account_runtime_id: str = "", reason: str = "", launch_intent: dict[str, Any] | None = None) -> RuntimeSessionIdentity:
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


def _first_query_value(qs: dict[str, list[str]], *keys: str) -> str:
    for key in keys:
        values = qs.get(key) or []
        if values:
            return str(values[0] or "")
    return ""


def _vip_link_fields(text: str) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(text)
    qs = urllib.parse.parse_qs(parsed.query)
    match = re.search(r"/games/(\d+)", parsed.path or "")
    place_id = match.group(1) if match else _first_query_value(qs, "placeId")
    link_code = _first_query_value(qs, "privateServerLinkCode", "linkCode", "code", "accessCode")
    job_id = _first_query_value(qs, "gameInstanceId", "jobId", "serverId")
    return {
        "place_id": str(place_id or ""),
        "link_code_hash": _safe_hash(link_code),
        "job_id_hash": _safe_hash(job_id),
        "url_hash": _safe_hash(text),
        "has_private_code": bool(link_code),
        "has_job_id": bool(job_id),
    }


def _parse_vip_link(vip_url: str) -> dict[str, Any]:
    text = str(vip_url or "").strip()
    if not text:
        return {}
    try:
        return _vip_link_fields(text)
    except Exception:
        return {"url_hash": _safe_hash(text), "parse_error": True}


def _unique(values: list[str], limit: int = 10) -> list[str]:
    seen = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


class _LaunchIntentParts(NamedTuple):
    vip_links: list[Any]
    active_vip: str
    browser_tracker_id: str
    parsed_active: dict[str, Any]
    configured_place_ids: list[str]
    configured_private_hashes: list[str]
    active_private_hash: str
    active_place_id: str
    effective_place_id: str
    server_type_norm: str
    launch_strategy: str
    private_server_intent: bool
    vip_intent: str


def _account_server_type(account: Any) -> str:
    raw = getattr(account, "server_type", None)
    fallback = str(getattr(account, "server_type", "") or "")
    return str(getattr(raw, "value", fallback) or "")


def _parsed_link_hashes(parsed_links: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    place_ids = _unique([_mapping_text(item, "place_id") for item in parsed_links])
    link_hashes = _unique([_mapping_text(item, "link_code_hash") for item in parsed_links])
    return place_ids, link_hashes


def _active_vip_intent(private_server_intent: bool, active_private_hash: str, has_vip: bool) -> str:
    if private_server_intent and active_private_hash:
        return "active_private"
    if private_server_intent:
        return "configured_private"
    if has_vip:
        return "configured_public"
    return "none"


def _launch_intent_parts(account: Any) -> _LaunchIntentParts:
    vip_links = list(getattr(account, "vip_links", []) or [])
    active_vip = _account_text(account, "active_vip")
    browser_tracker_id = _account_text(account, "browser_tracker_id")
    place_id = _account_text(account, "place_id")
    server_type = _account_server_type(account)
    launch_strategy = _account_text(account, "launch_strategy")
    parsed_active = _parse_vip_link(active_vip)
    parsed_links = [_parse_vip_link(link) for link in vip_links]
    configured_place_ids, configured_private_hashes = _parsed_link_hashes(parsed_links)
    active_private_hash = _mapping_text(parsed_active, "link_code_hash")
    active_place_id = _mapping_text(parsed_active, "place_id")
    effective_place_id = _first_text(
        place_id,
        active_place_id,
        configured_place_ids[0] if configured_place_ids else "",
    )
    server_type_norm = (server_type or "UNKNOWN").upper()
    private_server_intent = bool(
        active_private_hash
        or configured_private_hashes
        or server_type_norm in {"VIP", "PRIVATE", "PRIVATE_SERVER"}
    )
    vip_intent = _active_vip_intent(
        private_server_intent,
        active_private_hash,
        bool(vip_links or active_vip),
    )
    return _LaunchIntentParts(
        vip_links=vip_links,
        active_vip=active_vip,
        browser_tracker_id=browser_tracker_id,
        parsed_active=parsed_active,
        configured_place_ids=configured_place_ids,
        configured_private_hashes=configured_private_hashes,
        active_private_hash=active_private_hash,
        active_place_id=active_place_id,
        effective_place_id=effective_place_id,
        server_type_norm=server_type_norm,
        launch_strategy=launch_strategy,
        private_server_intent=private_server_intent,
        vip_intent=vip_intent,
    )


def _launch_intent_summary(parts: _LaunchIntentParts) -> dict[str, Any]:
    return {
        "place_id": parts.effective_place_id,
        "server_type": parts.server_type_norm or "UNKNOWN",
        "launch_strategy": parts.launch_strategy,
        "vip_intent": parts.vip_intent,
        "private_server_intent": parts.private_server_intent,
        "active_vip_hash": _mapping_text(parts.parsed_active, "url_hash"),
        "active_private_link_code_hash": parts.active_private_hash,
        "browser_tracker_id": tracker_label(parts.browser_tracker_id),
    }


def _launch_intent_output(parts: _LaunchIntentParts, reason: str, summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "reason": str(reason or ""),
        "place_id": parts.effective_place_id,
        "vip_configured": bool(parts.vip_links),
        "vip_count": len(parts.vip_links),
        "active_vip_present": bool(parts.active_vip),
        "server_type": parts.server_type_norm or "UNKNOWN",
        "launch_strategy": parts.launch_strategy,
        "vip_intent": parts.vip_intent,
        "private_server_intent": parts.private_server_intent,
        "active_vip_place_id": parts.active_place_id,
        "active_vip_hash": _mapping_text(parts.parsed_active, "url_hash"),
        "active_private_link_code_hash": parts.active_private_hash,
        "active_job_id_hash": _mapping_text(parts.parsed_active, "job_id_hash"),
        "configured_vip_place_ids": parts.configured_place_ids,
        "configured_private_link_code_hashes": parts.configured_private_hashes,
        "browser_tracker_id": tracker_label(parts.browser_tracker_id),
        "launch_intent_summary": summary,
    }


def build_launch_intent(account: Any, reason: str = "") -> dict[str, Any]:
    parts = _launch_intent_parts(account)
    return _launch_intent_output(parts, reason, _launch_intent_summary(parts))
