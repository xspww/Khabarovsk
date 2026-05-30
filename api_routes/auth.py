from __future__ import annotations

import secrets
import time

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from core import flog_kv
from .context import ApiContext

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_TOKEN_HEADERS = ("X-Cronus-Token",)
_IDEMPOTENCY_HEADERS = ("X-Cronus-Idempotency-Key",)
_EXEMPT_PATHS = {"/api/lua/rejoin-event"}


def _first_header(request, names) -> str:
    for name in names:
        value = str(request.headers.get(name) or "")
        if value:
            return value
    return ""


def api_token_valid(request, expected: str) -> bool:
    supplied = _first_header(request, _TOKEN_HEADERS)
    expected = str(expected or "")
    return bool(expected and supplied and secrets.compare_digest(supplied, expected))


def require_api_token(request, ctx: ApiContext) -> None:
    if api_token_valid(request, str(ctx.instance_token or "")):
        return
    path = str(getattr(getattr(request, "url", None), "path", "") or "")
    method = str(getattr(request, "method", "") or "").upper()
    flog_kv("API", "read_detail_rejected", "warning", method=method, path=path, reason="invalid_token")
    raise HTTPException(status_code=403, detail="Invalid API token")


def install_api_token_middleware(app, ctx: ApiContext) -> None:
    @app.middleware("http")
    async def api_token_middleware(request, call_next):
        path = str(request.url.path or "")
        method = str(request.method or "").upper()
        mutating_api = path.startswith("/api/") and method in _MUTATING_METHODS
        started_at = time.time()
        if path.startswith("/api/") and method in _MUTATING_METHODS and path not in _EXEMPT_PATHS:
            if not api_token_valid(request, str(ctx.instance_token or "")):
                flog_kv("API", "mutation_rejected", "warning", method=method, path=path, reason="invalid_token")
                return JSONResponse({"detail": "Invalid API token"}, status_code=403)
        response = await call_next(request)
        if mutating_api:
            flog_kv(
                "API",
                "mutation_audit",
                method=method,
                path=path,
                status_code=getattr(response, "status_code", 0),
                duration_ms=round((time.time() - started_at) * 1000, 2),
                idempotency_key=_first_header(request, _IDEMPOTENCY_HEADERS),
                idempotency_body_hash=str(
                    getattr(request.state, "cronus_idempotency_body_hash", "")
                    or ""
                ),
                idempotency_action=str(
                    getattr(request.state, "cronus_idempotency_action", "")
                    or ""
                ),
                idempotency_account=str(
                    getattr(request.state, "cronus_idempotency_account", "")
                    or ""
                ),
            )
        return response
