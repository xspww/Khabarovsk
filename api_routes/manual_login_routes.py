"""Manual Login API (Add Accounts -> Manual Login -> Single/Multi)."""

from __future__ import annotations

from fastapi import HTTPException, Request

from account_hybrid import ACCOUNT_STORE
from services.manual_login_service import (
    MANUAL_LOGIN_MANAGER,
    parse_userpass_entries,
)

from . import account_records
from .context import ApiContext


def register(app, ctx: ApiContext) -> None:
    cfg_mgr = ctx.cfg_mgr
    farm = ctx.farm

    def _refresh_farm_accounts() -> None:
        try:
            account_records.replace_farm_accounts_from_store(ctx, cfg_mgr, farm, ACCOUNT_STORE)
        except Exception:
            pass

    # Wire the manager once: every captured cookie refreshes the farm view.
    try:
        MANUAL_LOGIN_MANAGER._on_imported = _refresh_farm_accounts  # noqa: SLF001
    except Exception:
        pass

    @app.post("/api/manual-login/single")
    async def api_manual_login_single(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        try:
            session = MANUAL_LOGIN_MANAGER.start_single(username=username, password=password)
        except RuntimeError as exc:
            raise HTTPException(400, str(exc))
        try:
            _refresh_farm_accounts()
        except Exception:
            pass
        return {"ok": True, "session_id": session.session_id, "mode": "single", "total": 1}

    @app.post("/api/manual-login/multi")
    async def api_manual_login_multi(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        raw = body.get("lines")
        if raw is None:
            raw = body.get("text", "")
        if isinstance(raw, list):
            text = "\n".join(str(line) for line in raw)
        else:
            text = str(raw or "")
        entries = parse_userpass_entries(text)
        if not entries:
            raise HTTPException(400, "No valid user:pass lines (need username:password per line)")
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

    @app.get("/api/manual-login/{session_id}")
    def api_manual_login_status(session_id: str):
        status = MANUAL_LOGIN_MANAGER.public_status(session_id)
        if status is None:
            raise HTTPException(404, "Manual login session not found")
        return {"ok": True, **status}

    @app.post("/api/manual-login/{session_id}/cancel")
    def api_manual_login_cancel(session_id: str):
        ok = MANUAL_LOGIN_MANAGER.cancel(session_id)
        if not ok:
            raise HTTPException(404, "Manual login session not found")
        return {"ok": True, "msg": "Manual login cancelled"}

    @app.post("/api/manual-login/{session_id}/skip")
    def api_manual_login_skip(session_id: str):
        ok = MANUAL_LOGIN_MANAGER.skip_current(session_id)
        if not ok:
            raise HTTPException(404, "Manual login session not found")
        return {"ok": True, "msg": "Skipped to next account"}
