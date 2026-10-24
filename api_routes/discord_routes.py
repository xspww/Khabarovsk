from __future__ import annotations

from fastapi import Request

from .context import ApiContext


def register(app, ctx: ApiContext) -> None:
    @app.get("/api/discord/status")
    def api_discord_status():
        try:
            from services.discord_presence import DISCORD_PRESENCE

            snap = DISCORD_PRESENCE.snapshot()
        except Exception as exc:
            snap = {"enabled": False, "connected": False, "last_error": str(exc)[:200]}
        try:
            snap["configured"] = {
                "enabled": bool(ctx.cfg_mgr.get("discord_presence_enabled", False)),
                "show_accounts": bool(ctx.cfg_mgr.get("discord_show_accounts", True)),
                "show_game": bool(ctx.cfg_mgr.get("discord_show_game", True)),
            }
        except Exception:
            "intentional: ignore exception"
        snap["ok"] = True
        return snap

    @app.post("/api/discord/test")
    async def api_discord_test(request: Request):
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        client_id = str(body.get("client_id") or "").strip()
        # Empty = use built-in one-click app ID inside the service.
        try:
            from services.discord_presence import DISCORD_PRESENCE

            return DISCORD_PRESENCE.test(client_id)
        except Exception as exc:
            return {"ok": False, "msg": str(exc)[:300]}
