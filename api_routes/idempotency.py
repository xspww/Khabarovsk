from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict

from fastapi import Request

from core import flog_kv


_LOCK = threading.RLock()
_CACHE: Dict[str, Dict[str, Any]] = {}
_TTL_SECONDS = 300.0


@dataclass
class IdempotencyContext:
    key: str = ""
    scope: str = ""
    action: str = ""
    account: str = ""
    body_hash: str = ""
    replay: bool = False
    response: Dict[str, Any] | None = None


def _cleanup_locked(now: float) -> None:
    expired = [key for key, item in _CACHE.items() if float(item.get("expires_at") or 0.0) <= now]
    for key in expired:
        _CACHE.pop(key, None)


def _hash_body(raw: bytes) -> str:
    if not raw:
        return hashlib.sha256(b"").hexdigest()[:16]
    try:
        parsed = json.loads(raw.decode("utf-8"))
        canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except Exception:
        canonical = raw
    return hashlib.sha256(canonical).hexdigest()[:16]


def _begin(request: Request, action: str, account: str, body_hash: str) -> IdempotencyContext:
    key = str(request.headers.get("X-Cronus-Idempotency-Key") or "").strip()
    try:
        request.state.cronus_idempotency_body_hash = body_hash
        request.state.cronus_idempotency_action = action
        request.state.cronus_idempotency_account = account
    except Exception:
        pass
    if not key:
        return IdempotencyContext(action=action, account=account, body_hash=body_hash)
    scope = f"{request.method}:{request.url.path}:{action}:{account}:{body_hash}:{key}"
    now = time.time()
    with _LOCK:
        _cleanup_locked(now)
        cached = _CACHE.get(scope)
        if cached and cached.get("status") == "finished" and isinstance(cached.get("response"), dict):
            flog_kv("API", "idempotency_replay", method=request.method, path=request.url.path, action=action, account=account, idempotency_key=key, body_hash=body_hash)
            return IdempotencyContext(key=key, scope=scope, action=action, account=account, body_hash=body_hash, replay=True, response=dict(cached["response"]))
        if cached and cached.get("status") == "running":
            response = {"ok": False, "msg": "Idempotent request already in progress"}
            return IdempotencyContext(key=key, scope=scope, action=action, account=account, body_hash=body_hash, replay=True, response=response)
        _CACHE[scope] = {"status": "running", "expires_at": now + _TTL_SECONDS}
    flog_kv("API", "idempotency_begin", method=request.method, path=request.url.path, action=action, account=account, idempotency_key=key, body_hash=body_hash)
    return IdempotencyContext(key=key, scope=scope, action=action, account=account, body_hash=body_hash)


async def begin_idempotent_request(request: Request, action: str, account: str = "") -> IdempotencyContext:
    raw = await request.body()
    return _begin(request, action, account, _hash_body(raw))


def begin_idempotent_request_sync(request: Request, action: str, account: str = "") -> IdempotencyContext:
    return _begin(request, action, account, _hash_body(b""))


def finish_idempotent_request(ctx: IdempotencyContext, response: Dict[str, Any]) -> None:
    if not ctx.scope or ctx.replay:
        return
    with _LOCK:
        _CACHE[ctx.scope] = {
            "status": "finished",
            "response": dict(response or {}),
            "expires_at": time.time() + _TTL_SECONDS,
            "body_hash": ctx.body_hash,
            "action": ctx.action,
            "account": ctx.account,
        }
