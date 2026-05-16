from __future__ import annotations

import json
import os
import secrets
from typing import Any, Dict

from fastapi import HTTPException, Request
from fastapi.responses import PlainTextResponse

from app_paths import resource_path
from core import flog_kv

from .context import ApiContext


_SCRIPT_PATH = resource_path("lua", "argus_rejoin_helper.lua")
_ACCOUNT_MODULE_PATH = resource_path("lua", "argus_account_client.lua")
_TOKEN_HEADERS = ("X-Argus-Token", "X-RoboGuard-Token")
_TOKEN_BODY_KEYS = ("token", "argus_token", "api_token", "_argus_token")
_LOCAL_FALLBACK_EVENTS = {
    "loaded",
    "in_game",
    "disconnect",
    "error_code",
    "teleport_error",
    "rejoin_requested",
    "heartbeat",
    "teleport_state",
}


def _lua_literal(value: Any) -> str:
    return json.dumps(str(value or ""), ensure_ascii=True)


def _load_script_template() -> str:
    with open(_SCRIPT_PATH, "r", encoding="utf-8") as handle:
        return handle.read()


def _load_account_module_template() -> str:
    with open(_ACCOUNT_MODULE_PATH, "r", encoding="utf-8") as handle:
        return handle.read()


def _render_lua_template(template: str, replacements: Dict[str, str]) -> str:
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


def _selected_lua_port(request: Request, port: int) -> int:
    return int(port or request.url.port or 7777)


def _render_rejoin_helper(ctx: ApiContext, request: Request, account: str, port: int, shutdown_delay: float) -> str:
    selected_port = int(port or request.url.port or 7777)
    delay = max(0.5, min(float(shutdown_delay or 3.0), 60.0))
    template = _load_script_template()
    return _render_lua_template(template, {
        "__ARGUS_HOST__": _lua_literal("127.0.0.1"),
        "__ARGUS_PORT__": str(selected_port),
        "__ARGUS_TOKEN__": _lua_literal(ctx.instance_token),
        "__ARGUS_ACCOUNT__": _lua_literal(account),
        "__ARGUS_SHUTDOWN_DELAY__": f"{delay:.2f}",
    })


def _render_account_module(ctx: ApiContext, request: Request, account: str, port: int) -> str:
    template = _load_account_module_template()
    return _render_lua_template(template, {
        "__ARGUS_HOST__": _lua_literal("127.0.0.1"),
        "__ARGUS_PORT__": str(_selected_lua_port(request, port)),
        "__ARGUS_TOKEN__": _lua_literal(ctx.instance_token),
        "__ARGUS_ACCOUNT__": _lua_literal(account),
    })


def _lua_text_response(script: str) -> PlainTextResponse:
    return PlainTextResponse(
        script,
        media_type="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


def _lua_request_token(request: Request, body: Dict[str, Any]) -> str:
    for header in _TOKEN_HEADERS:
        supplied = str(request.headers.get(header) or "")
        if supplied:
            return supplied
    supplied = str(request.query_params.get("argus_token") or "")
    if supplied:
        return supplied
    for key in _TOKEN_BODY_KEYS:
        supplied = str(body.get(key) or "")
        if supplied:
            return supplied
    return ""


def _strip_token_fields(body: Dict[str, Any]) -> Dict[str, Any]:
    clean = dict(body)
    for key in _TOKEN_BODY_KEYS:
        clean.pop(key, None)
    return clean


def _is_loopback_request(request: Request) -> bool:
    client = getattr(request, "client", None)
    host = str(getattr(client, "host", "") or "").strip().lower()
    return host in {"127.0.0.1", "::1", "localhost", "testclient"} or host.startswith("::ffff:127.")


def _local_fallback_allowed(request: Request, body: Dict[str, Any], transport: str) -> bool:
    if transport != "get_fallback" or not _is_loopback_request(request):
        return False
    event_name = str(body.get("event") or "").strip().lower()
    if event_name not in _LOCAL_FALLBACK_EVENTS:
        return False
    helper_version = str(body.get("helper_version") or "").strip()
    if not helper_version.startswith("1.7"):
        return False
    identity = str(
        body.get("username")
        or body.get("account")
        or body.get("configured_account")
        or body.get("user_id")
        or ""
    ).strip()
    return bool(identity)


def _query_payload(request: Request) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for key, value in request.query_params.multi_items():
        if key != "argus_token":
            payload[key] = value
    return payload


def _handle_lua_rejoin_event(ctx: ApiContext, farm: Any, request: Request, body: Dict[str, Any], transport: str) -> Dict[str, Any]:
    supplied_token = _lua_request_token(request, body)
    expected_token = str(ctx.instance_token or "")
    if not expected_token or not supplied_token or not secrets.compare_digest(supplied_token, expected_token):
        if _local_fallback_allowed(request, body, transport):
            flog_kv(
                "LUA",
                "rejoin_event_local_fallback",
                "warning",
                reason="invalid_token_local_fallback",
                transport=transport,
                lua_event=str(body.get("event") or ""),
                account=str(body.get("username") or body.get("account") or ""),
                helper_version=str(body.get("helper_version") or ""),
                supplied_token_len=len(str(supplied_token or "")),
            )
        else:
            flog_kv("LUA", "rejoin_event_rejected", "warning", reason="invalid_token", transport=transport)
            raise HTTPException(403, "Invalid API token")

    result: Dict[str, Any] = farm.handle_lua_rejoin_event(_strip_token_fields(body))
    status = 200 if result.get("ok") else int(result.get("status_code") or 400)
    if status >= 400:
        raise HTTPException(status, result.get("msg") or "Lua rejoin event rejected")
    flog_kv(
        "LUA",
        "rejoin_event_api",
        account=result.get("account", ""),
        lua_event=result.get("event", ""),
        accepted=bool(result.get("accepted")),
        signal=result.get("signal", ""),
        transport=transport,
    )
    return result


def register(app, ctx: ApiContext) -> None:
    farm = ctx.farm

    @app.get("/api/lua/rejoin-helper", response_class=PlainTextResponse)
    def api_lua_rejoin_helper(request: Request, account: str = "", port: int = 0, shutdown_delay: float = 3.0):
        if not os.path.exists(_SCRIPT_PATH):
            raise HTTPException(404, "Lua helper template not found")
        script = _render_rejoin_helper(ctx, request, account, port, shutdown_delay)
        flog_kv(
            "LUA",
            "rejoin_helper_served",
            account=str(account or ""),
            port=_selected_lua_port(request, port),
            client=str(getattr(getattr(request, "client", None), "host", "") or ""),
        )
        return _lua_text_response(script)

    @app.get("/api/lua/account-module", response_class=PlainTextResponse)
    def api_lua_account_module(request: Request, account: str = "", port: int = 0):
        if not os.path.exists(_ACCOUNT_MODULE_PATH):
            raise HTTPException(404, "Lua account module template not found")
        script = _render_account_module(ctx, request, account, port)
        flog_kv(
            "LUA",
            "account_module_served",
            account=str(account or ""),
            port=_selected_lua_port(request, port),
            client=str(getattr(getattr(request, "client", None), "host", "") or ""),
        )
        return _lua_text_response(script)

    @app.post("/api/lua/rejoin-event")
    async def api_lua_rejoin_event(request: Request):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Expected JSON object")
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected JSON object")
        return _handle_lua_rejoin_event(ctx, farm, request, body, "post")

    @app.get("/api/lua/rejoin-event")
    async def api_lua_rejoin_event_get(request: Request):
        body = _query_payload(request)
        if not body:
            raise HTTPException(400, "Expected Lua event query")
        return _handle_lua_rejoin_event(ctx, farm, request, body, "get_fallback")
