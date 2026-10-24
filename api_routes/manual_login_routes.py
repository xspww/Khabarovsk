"""Manual Login API (Add Accounts -> Manual Login -> Single/Multi)."""

from __future__ import annotations

from fastapi import HTTPException, Request

from services.account_hybrid import ACCOUNT_STORE
from services.manual_login_service import (
    MANUAL_LOGIN_MANAGER,
    parse_userpass_entries,
)

from . import account_records
from .context import ApiContext


def _refresh_farm_accounts(ctx: ApiContext) -> None:
    try:
        account_records.replace_farm_accounts_from_store(
            ctx, ctx.cfg_mgr, ctx.farm, ACCOUNT_STORE
        )
    except Exception:
        "intentional: ignore exception"


def _wire_manual_login_manager(ctx: ApiContext) -> None:
    try:
        MANUAL_LOGIN_MANAGER._on_imported = lambda: _refresh_farm_accounts(ctx)
    except Exception:
        "intentional: ignore exception"


async def _request_object(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return body


def _multi_login_text(body: dict) -> str:
    raw = body.get("lines")
    if raw is None:
        raw = body.get("text", "")
    if isinstance(raw, list):
        return "\n".join(str(line) for line in raw)
    return str(raw or "")


async def _api_manual_login_single(
    ctx: ApiContext, request: Request
) -> dict:
    body = await _request_object(request)
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    try:
        session = MANUAL_LOGIN_MANAGER.start_single(
            username=username, password=password
        )
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    try:
        _refresh_farm_accounts(ctx)
    except Exception:
        "intentional: ignore exception"
    return {
        "ok": True,
        "session_id": session.session_id,
        "mode": "single",
        "total": 1,
    }


async def _api_manual_login_multi(
    ctx: ApiContext, request: Request
) -> dict:
    body = await _request_object(request)
    entries = parse_userpass_entries(_multi_login_text(body))
    if not entries:
        raise HTTPException(
            400,
            "No valid user:pass lines (need username:password per line)",
        )
    try:
        session = MANUAL_LOGIN_MANAGER.start_multi(entries)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "ok": True,
        "session_id": session.session_id,
        "mode": "multi",
        "total": len(session.entries),
        "truncated": len(entries) > len(session.entries),
    }


def _api_manual_login_status(session_id: str) -> dict:
    status = MANUAL_LOGIN_MANAGER.public_status(session_id)
    if status is None:
        raise HTTPException(404, "Manual login session not found")
    return {"ok": True, **status}


def _api_manual_login_cancel(session_id: str) -> dict:
    ok = MANUAL_LOGIN_MANAGER.cancel(session_id)
    if not ok:
        raise HTTPException(404, "Manual login session not found")
    return {"ok": True, "msg": "Manual login cancelled"}


def _api_manual_login_skip(session_id: str) -> dict:
    ok = MANUAL_LOGIN_MANAGER.skip_current(session_id)
    if not ok:
        raise HTTPException(404, "Manual login session not found")
    return {"ok": True, "msg": "Skipped to next account"}


def _register_manual_login_start(app, ctx: ApiContext) -> None:
    @app.post("/api/manual-login/single")
    async def api_manual_login_single(request: Request):
        return await _api_manual_login_single(ctx, request)

    @app.post("/api/manual-login/multi")
    async def api_manual_login_multi(request: Request):
        return await _api_manual_login_multi(ctx, request)


def _register_manual_login_session(app, ctx: ApiContext) -> None:
    @app.get("/api/manual-login/{session_id}")
    def api_manual_login_status(session_id: str):
        return _api_manual_login_status(session_id)

    @app.post("/api/manual-login/{session_id}/cancel")
    def api_manual_login_cancel(session_id: str):
        return _api_manual_login_cancel(session_id)

    @app.post("/api/manual-login/{session_id}/skip")
    def api_manual_login_skip(session_id: str):
        return _api_manual_login_skip(session_id)


def register(app, ctx: ApiContext) -> None:
    _wire_manual_login_manager(ctx)
    _register_manual_login_start(app, ctx)
    _register_manual_login_session(app, ctx)
