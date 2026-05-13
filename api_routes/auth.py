from __future__ import annotations

import secrets
import time

from fastapi.responses import JSONResponse

from core import flog_kv
from .context import ApiContext

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_TOKEN_HEADERS = ("X-Argus-Token", "X-RoboGuard-Token")
_EXEMPT_PATHS = {"/api/app/shutdown", "/api/lua/rejoin-event"}


def install_api_token_middleware(app, ctx: ApiContext) -> None:
    @app.middleware("http")
    async def api_token_middleware(request, call_next):
        path = str(request.url.path or "")
        method = str(request.method or "").upper()
        mutating_api = path.startswith("/api/") and method in _MUTATING_METHODS
        started_at = time.time()
        if path.startswith("/api/") and method in _MUTATING_METHODS and path not in _EXEMPT_PATHS:
            expected = str(ctx.instance_token or "")
            supplied = ""
            for header in _TOKEN_HEADERS:
                supplied = str(request.headers.get(header) or "")
                if supplied:
                    break
            if not expected or not supplied or not secrets.compare_digest(supplied, expected):
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
                idempotency_key=str(request.headers.get("X-Argus-Idempotency-Key") or ""),
                idempotency_body_hash=str(getattr(request.state, "argus_idempotency_body_hash", "") or ""),
                idempotency_action=str(getattr(request.state, "argus_idempotency_action", "") or ""),
                idempotency_account=str(getattr(request.state, "argus_idempotency_account", "") or ""),
            )
        return response
