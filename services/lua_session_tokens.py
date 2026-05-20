from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, Optional


TOKEN_VERSION = "lua1"
DEFAULT_LUA_SESSION_TOKEN_TTL_SECONDS = 15 * 60


@dataclass(frozen=True)
class LuaSessionTokenValidation:
    ok: bool
    reason: str = ""
    account: str = ""
    session_id: str = ""
    launch_nonce: str = ""
    expires_at: int = 0


@dataclass(frozen=True)
class LuaEventReplayResult:
    ok: bool
    reason: str = ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64url(value: str) -> bytes:
    padded = value + ("=" * ((4 - len(value) % 4) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _sign(secret: str, payload_b64: str) -> str:
    digest = hmac.new(_text(secret).encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64url(digest)


def issue_lua_session_token(
    secret: str,
    *,
    account: str,
    session_id: str,
    launch_nonce: str,
    ttl_seconds: int = DEFAULT_LUA_SESSION_TOKEN_TTL_SECONDS,
    now: Optional[float] = None,
) -> str:
    issued_at = int(now if now is not None else time.time())
    ttl = max(30, int(ttl_seconds or DEFAULT_LUA_SESSION_TOKEN_TTL_SECONDS))
    payload = {
        "account": _text(account),
        "session_id": _text(session_id),
        "launch_nonce": _text(launch_nonce),
        "iat": issued_at,
        "exp": issued_at + ttl,
        "jti": secrets.token_urlsafe(12),
    }
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64url(payload_json)
    return f"{TOKEN_VERSION}.{payload_b64}.{_sign(secret, payload_b64)}"


def validate_lua_session_token(
    secret: str,
    token: str,
    *,
    account: str,
    session_id: str,
    launch_nonce: str,
    now: Optional[float] = None,
) -> LuaSessionTokenValidation:
    def reject(reason: str, expires_at: int = 0) -> LuaSessionTokenValidation:
        return LuaSessionTokenValidation(False, reason, expires_at=expires_at)

    raw = _text(token)
    parts = raw.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_VERSION:
        return reject("invalid_format")

    _, payload_b64, supplied_sig = parts
    expected_sig = _sign(secret, payload_b64)
    if not hmac.compare_digest(supplied_sig, expected_sig):
        return reject("bad_signature")

    try:
        payload = json.loads(_unb64url(payload_b64).decode("utf-8"))
    except Exception:
        return reject("invalid_payload")

    expected_account = _text(account)
    expected_session_id = _text(session_id)
    expected_launch_nonce = _text(launch_nonce)
    actual_account = _text(payload.get("account"))
    actual_session_id = _text(payload.get("session_id"))
    actual_launch_nonce = _text(payload.get("launch_nonce"))
    expires_at = int(payload.get("exp") or 0)

    current = int(now if now is not None else time.time())
    if not expires_at or expires_at < current:
        return reject("expired", expires_at)
    if actual_account != expected_account:
        return reject("account_mismatch", expires_at)
    if actual_session_id != expected_session_id:
        return reject("session_id_mismatch", expires_at)
    if actual_launch_nonce != expected_launch_nonce:
        return reject("launch_nonce_mismatch", expires_at)

    return LuaSessionTokenValidation(True, "", actual_account, actual_session_id, actual_launch_nonce, expires_at)


class LuaEventReplayCache:
    def __init__(
        self,
        *,
        ttl_seconds: int = 5 * 60,
        max_clock_skew_seconds: int = 120,
        max_events_per_account: int = 256,
    ) -> None:
        self._ttl_seconds = max(10, int(ttl_seconds or 300))
        self._max_clock_skew_seconds = max(1, int(max_clock_skew_seconds or 120))
        self._max_events_per_account = max(8, int(max_events_per_account or 256))
        self._lock = threading.Lock()
        self._events: Dict[str, OrderedDict[str, float]] = {}

    def check_and_record(
        self,
        account: str,
        event_id: str,
        timestamp: Any,
        *,
        now: Optional[float] = None,
    ) -> LuaEventReplayResult:
        account_key = _text(account)
        event_key = _text(event_id)
        if not account_key:
            return LuaEventReplayResult(False, "missing_account")
        if not event_key:
            return LuaEventReplayResult(False, "missing_event_id")
        try:
            event_ts = float(str(timestamp or "").strip())
        except (TypeError, ValueError):
            return LuaEventReplayResult(False, "invalid_timestamp")

        current = float(now if now is not None else time.time())
        if event_ts < current - self._ttl_seconds:
            return LuaEventReplayResult(False, "stale_event")
        if event_ts > current + self._max_clock_skew_seconds:
            return LuaEventReplayResult(False, "future_event")

        with self._lock:
            bucket = self._events.setdefault(account_key, OrderedDict())
            cutoff = current - self._ttl_seconds
            for key, recorded_at in list(bucket.items()):
                if recorded_at < cutoff:
                    bucket.pop(key, None)
                else:
                    break
            if event_key in bucket:
                return LuaEventReplayResult(False, "duplicate_event")
            bucket[event_key] = event_ts
            while len(bucket) > self._max_events_per_account:
                bucket.popitem(last=False)
        return LuaEventReplayResult(True)
