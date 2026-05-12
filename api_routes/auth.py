from __future__ import annotations

import secrets

from fastapi.responses import JSONResponse

from .context import ApiContext

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_TOKEN_HEADERS = ("X-Argus-Token", "X-RoboGuard-Token")
_EXEMPT_PATHS = {"/api/app/shutdown"}


def install_api_token_middleware(app, ctx: ApiContext) -> None:
    @app.middleware("http")
    async def api_token_middleware(request, call_next):
        path = str(request.url.path or "")
        method = str(request.method or "").upper()
        if path.startswith("/api/") and method in _MUTATING_METHODS and path not in _EXEMPT_PATHS:
            expected = str(ctx.instance_token or "")
            supplied = ""
            for header in _TOKEN_HEADERS:
                supplied = str(request.headers.get(header) or "")
                if supplied:
                    break
            if not expected or not supplied or not secrets.compare_digest(supplied, expected):
                return JSONResponse({"detail": "Invalid API token"}, status_code=403)
        return await call_next(request)
