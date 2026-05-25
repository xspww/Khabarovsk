from __future__ import annotations

import json
import os
import secrets
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import PlainTextResponse

from app_paths import resource_path
from core import flog_kv
from runtime.lua_identity import resolve_lua_account
from services.lua_session_tokens import (
    DEFAULT_LUA_SESSION_TOKEN_TTL_SECONDS,
    LuaEventReplayCache,
    issue_lua_session_token,
    validate_lua_session_token,
)

from .auth import api_token_valid, require_api_token
from .context import ApiContext


_SCRIPT_PATH = resource_path("lua", "internal", "rejoin_monitor.lua")
_ACCOUNT_MODULE_PATH = resource_path("lua", "internal", "account_status_client.lua")
_EXECUTOR_LOADER_PATH = resource_path("lua", "run_in_executor.lua")
_TOKEN_HEADERS = ("X-Cronus-Token", "X-Argus-Token", "X-RoboGuard-Token")
_TOKEN_QUERY_KEYS = ("cronus_token", "argus_token")
_TOKEN_BODY_KEYS = ("token", "cronus_token", "argus_token", "api_token", "_cronus_token", "_argus_token")
_LOCAL_FALLBACK_EVENTS = {
    "heartbeat",
    "teleport_state",
}
_STATE_CHANGING_EVENTS = {
    "loaded",
    "in_game",
    "disconnect",
    "error_code",
    "teleport_error",
    "rejoin_requested",
    "finished",
    "mark_finished",
}
_LUA_EVENT_REPLAY_CACHE = LuaEventReplayCache()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lua_literal(value: Any) -> str:
    return json.dumps(str(value or ""), ensure_ascii=True)


def _load_script_template() -> str:
    with open(_SCRIPT_PATH, "r", encoding="utf-8") as handle:
        return handle.read()


def _load_account_module_template() -> str:
    with open(_ACCOUNT_MODULE_PATH, "r", encoding="utf-8") as handle:
        return handle.read()


def _load_executor_loader() -> str:
    with open(_EXECUTOR_LOADER_PATH, "r", encoding="utf-8") as handle:
        return handle.read()


def _render_lua_template(template: str, replacements: Dict[str, str]) -> str:
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


def _selected_lua_port(request: Request, port: int) -> int:
    return int(port or request.url.port or 7777)


def _render_requeue_source(account: str, port: int, shutdown_delay: float, token: str = "") -> str:
    account_qs = quote(str(account or ""), safe="")
    token_qs = quote(str(token or ""), safe="")
    helper_url = f"http://127.0.0.1:{int(port)}/api/lua/rejoin-helper?account={account_qs}&shutdown_delay={shutdown_delay:.2f}"
    if token_qs:
        helper_url = f"{helper_url}&cronus_token={token_qs}"
    token_header = f", [\"X-Cronus-Token\"] = {_lua_literal(token)}" if token else ""
    return "\n".join(
        [
            "local Request = (syn and syn.request) or (http and http.request) or http_request or request",
            "local Load = loadstring or load",
            "local function warnCronus(message)",
            "    local line = \"[Cronus] \" .. tostring(message or \"Rejoin helper failed to load\")",
            "    if rconsoleprint then pcall(rconsoleprint, line .. \"\\n\") end",
            "    if warn then pcall(warn, line) elseif print then pcall(print, line) end",
            "    return nil",
            "end",
            f"local url = {_lua_literal(helper_url)}",
            "local source = nil",
            "if Request then",
            f"    local response = Request({{ Method = \"GET\", Url = url, Headers = {{ [\"User-Agent\"] = \"CronusRejoinTeleport/1.0\"{token_header} }} }})",
            "    source = response and (response.Body or response.body or response.Data or response.data)",
            "elseif game.HttpGet then",
            "    source = game:HttpGet(url)",
            "end",
            "if type(source) ~= \"string\" or #source <= 0 then return warnCronus(\"Rejoin helper failed to load\") end",
            "if source:sub(1, 1) == \"{\" then return nil end",
            "local fn, err = Load(source)",
            "if not fn then return warnCronus(\"Rejoin helper failed to load\") end",
            "return fn()",
        ]
    )


def _account_session_fields(account: Any) -> Dict[str, str]:
    if not account:
        return {"session_id": "", "launch_nonce": "", "pid": ""}
    lock = getattr(account, "_lock", None)
    if lock:
        with lock:
            return {
                "session_id": _text(getattr(account, "session_id", "")),
                "launch_nonce": _text(getattr(account, "launch_nonce", "")),
                "pid": _text(getattr(account, "pid", "")),
            }
    return {
        "session_id": _text(getattr(account, "session_id", "")),
        "launch_nonce": _text(getattr(account, "launch_nonce", "")),
        "pid": _text(getattr(account, "pid", "")),
    }


def _lua_scope_for_account(farm: Any, account: str) -> Dict[str, str]:
    requested = _text(account)
    if requested:
        resolution = resolve_lua_account(
            getattr(farm, "_accounts", []) or [],
            {"account": requested, "username": requested, "configured_account": requested},
        )
        if resolution.account:
            fields = _account_session_fields(resolution.account)
            return {
                "account": resolution.account_key,
                "session_id": fields["session_id"],
                "launch_nonce": fields["launch_nonce"],
                "pid": fields["pid"],
            }
    return {"account": requested, "session_id": "", "launch_nonce": "", "pid": ""}


def _lua_scope_for_payload(farm: Any, body: Dict[str, Any]) -> Optional[Dict[str, str]]:
    resolution = resolve_lua_account(getattr(farm, "_accounts", []) or [], body)
    if not resolution.account:
        return None
    fields = _account_session_fields(resolution.account)
    return {
        "account": resolution.account_key,
        "session_id": fields["session_id"],
        "launch_nonce": fields["launch_nonce"],
        "pid": fields["pid"],
    }


def _issue_lua_token(ctx: ApiContext, account: str) -> Dict[str, str]:
    scope = _lua_scope_for_account(ctx.farm, account)
    return _issue_lua_token_for_scope(ctx, scope)


def _issue_lua_token_for_scope(ctx: ApiContext, scope: Dict[str, str]) -> Dict[str, str]:
    token = issue_lua_session_token(
        str(ctx.instance_token or ""),
        account=scope["account"],
        session_id=scope["session_id"],
        launch_nonce=scope["launch_nonce"],
        ttl_seconds=DEFAULT_LUA_SESSION_TOKEN_TTL_SECONDS,
    )
    return {**scope, "token": token}


def _render_token_scope(ctx: ApiContext, account: str, existing_scope: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    if existing_scope and existing_scope.get("token"):
        return {
            "account": _text(existing_scope.get("account") or account),
            "session_id": _text(existing_scope.get("session_id")),
            "launch_nonce": _text(existing_scope.get("launch_nonce")),
            "pid": _text(existing_scope.get("pid")),
            "token": _text(existing_scope.get("token")),
        }
    return _issue_lua_token(ctx, account)


def _validate_lua_helper_access(ctx: ApiContext, request: Request, account: str) -> Optional[Dict[str, str]]:
    if api_token_valid(request, str(ctx.instance_token or "")):
        return None

    bootstrap_scope = _validate_lua_bootstrap_access(ctx, request, account)
    if bootstrap_scope:
        return bootstrap_scope

    supplied_token = _lua_request_token(request, {})
    reject_reason = "missing_token"
    if supplied_token:
        scope = _lua_scope_for_account(ctx.farm, account)
        validation = validate_lua_session_token(
            str(ctx.instance_token or ""),
            supplied_token,
            account=scope["account"],
            session_id=scope["session_id"],
            launch_nonce=scope["launch_nonce"],
        )
        if validation.ok:
            return {**scope, "token": supplied_token}
        reject_reason = validation.reason or "invalid_lua_session_token"

    _reject_lua_helper_request(request, account, reject_reason)


def _render_rejoin_helper(
    ctx: ApiContext,
    request: Request,
    account: str,
    port: int,
    shutdown_delay: float,
    token_scope: Optional[Dict[str, str]] = None,
) -> str:
    selected_port = int(port or request.url.port or 7777)
    delay = max(0.5, min(float(shutdown_delay or 3.0), 60.0))
    template = _load_script_template()
    token_scope = _render_token_scope(ctx, account, token_scope)
    return _render_lua_template(template, {
        "__CRONUS_HOST__": _lua_literal("127.0.0.1"),
        "__CRONUS_PORT__": str(selected_port),
        "__CRONUS_TOKEN__": _lua_literal(token_scope["token"]),
        "__CRONUS_ACCOUNT__": _lua_literal(account),
        "__CRONUS_SESSION_ID__": _lua_literal(token_scope["session_id"]),
        "__CRONUS_LAUNCH_NONCE__": _lua_literal(token_scope["launch_nonce"]),
        "__CRONUS_PROCESS_ID__": _lua_literal(token_scope.get("pid", "")),
        "__CRONUS_SHUTDOWN_DELAY__": f"{delay:.2f}",
        "__CRONUS_REQUEUE_SOURCE__": _lua_literal(_render_requeue_source(account, selected_port, delay, token_scope["token"])),
    })


def _render_account_module(
    ctx: ApiContext,
    request: Request,
    account: str,
    port: int,
    token_scope: Optional[Dict[str, str]] = None,
) -> str:
    template = _load_account_module_template()
    token_scope = _render_token_scope(ctx, account, token_scope)
    return _render_lua_template(template, {
        "__CRONUS_HOST__": _lua_literal("127.0.0.1"),
        "__CRONUS_PORT__": str(_selected_lua_port(request, port)),
        "__CRONUS_TOKEN__": _lua_literal(token_scope["token"]),
        "__CRONUS_ACCOUNT__": _lua_literal(account),
        "__CRONUS_SESSION_ID__": _lua_literal(token_scope["session_id"]),
        "__CRONUS_LAUNCH_NONCE__": _lua_literal(token_scope["launch_nonce"]),
        "__CRONUS_PROCESS_ID__": _lua_literal(token_scope.get("pid", "")),
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
    for key in _TOKEN_QUERY_KEYS:
        supplied = str(request.query_params.get(key) or "")
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


def _query_enabled(request: Request, key: str) -> bool:
    return str(request.query_params.get(key) or "").strip().lower() in {"1", "true", "yes", "on"}


def _reject_lua_helper_request(
    request: Request,
    account: str,
    reason: str,
    *,
    status_code: int = 403,
    detail: str = "Invalid API token",
) -> None:
    flog_kv(
        "LUA",
        "helper_rejected",
        "warning",
        reason=reason,
        account=str(account or ""),
        client=str(getattr(getattr(request, "client", None), "host", "") or ""),
    )
    raise HTTPException(status_code, detail)


def _validate_lua_bootstrap_access(ctx: ApiContext, request: Request, account: str) -> Optional[Dict[str, str]]:
    if not _query_enabled(request, "bootstrap"):
        return None
    if not _is_loopback_request(request):
        _reject_lua_helper_request(request, account, "bootstrap_non_loopback")

    payload = {
        "account": str(account or request.query_params.get("account") or ""),
        "username": str(request.query_params.get("username") or account or ""),
        "configured_account": str(request.query_params.get("configured_account") or account or ""),
        "user_id": str(request.query_params.get("user_id") or ""),
        "pid": str(request.query_params.get("pid") or ""),
    }
    resolution = resolve_lua_account(getattr(ctx.farm, "_accounts", []) or [], payload)
    if resolution.ambiguous:
        _reject_lua_helper_request(request, account, "bootstrap_ambiguous_account")
    if not resolution.account:
        _reject_lua_helper_request(request, account, "bootstrap_account_not_found")

    scope = {
        "account": resolution.account_key,
        **_account_session_fields(resolution.account),
    }
    if not scope["session_id"] or not scope["launch_nonce"]:
        _reject_lua_helper_request(
            request,
            account,
            "bootstrap_inactive_session",
            status_code=409,
            detail="Lua bootstrap requires an active Cronus launch session",
        )

    if resolution.bound_pid and resolution.identity.pid is not None and not resolution.pid_match:
        _reject_lua_helper_request(request, account, "bootstrap_pid_mismatch")

    return _issue_lua_token_for_scope(ctx, scope)


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


def _state_changing_get_rejected(body: Dict[str, Any], transport: str) -> bool:
    event_name = str(body.get("event") or "").strip().lower()
    return transport == "get_fallback" and event_name in _STATE_CHANGING_EVENTS


def _validate_lua_session_auth(ctx: ApiContext, farm: Any, body: Dict[str, Any], supplied_token: str) -> str:
    scope = _lua_scope_for_payload(farm, body)
    if not scope:
        return "lua_account_not_found"
    validation = validate_lua_session_token(
        str(ctx.instance_token or ""),
        supplied_token,
        account=scope["account"],
        session_id=scope["session_id"],
        launch_nonce=scope["launch_nonce"],
    )
    if not validation.ok:
        return validation.reason or "invalid_lua_session_token"

    body_session_id = _text(body.get("session_id"))
    body_launch_nonce = _text(body.get("launch_nonce"))
    if body_session_id and body_session_id != scope["session_id"]:
        return "body_session_id_mismatch"
    if body_launch_nonce and body_launch_nonce != scope["launch_nonce"]:
        return "body_launch_nonce_mismatch"

    replay = _LUA_EVENT_REPLAY_CACHE.check_and_record(
        scope["account"],
        _text(body.get("event_id")),
        body.get("ts") or body.get("timestamp") or body.get("event_ts"),
    )
    if not replay.ok:
        return replay.reason or "lua_event_replay_rejected"
    return ""


def _query_payload(request: Request) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for key, value in request.query_params.multi_items():
        if key not in _TOKEN_QUERY_KEYS:
            payload[key] = value
    return payload


def _handle_lua_rejoin_event(ctx: ApiContext, farm: Any, request: Request, body: Dict[str, Any], transport: str) -> Dict[str, Any]:
    if _state_changing_get_rejected(body, transport):
        flog_kv(
            "LUA",
            "rejoin_event_rejected",
            "warning",
            reason="state_changing_get_requires_post",
            transport=transport,
            lua_event=str(body.get("event") or ""),
            account=str(body.get("username") or body.get("account") or ""),
        )
        raise HTTPException(403, "Lua state-changing events require POST")

    supplied_token = _lua_request_token(request, body)
    expected_token = str(ctx.instance_token or "")
    backend_token_valid = bool(expected_token and supplied_token and secrets.compare_digest(supplied_token, expected_token))
    if not backend_token_valid and supplied_token:
        lua_session_reject_reason = _validate_lua_session_auth(ctx, farm, body, supplied_token)
        if lua_session_reject_reason:
            if _local_fallback_allowed(request, body, transport):
                flog_kv(
                    "LUA",
                    "rejoin_event_local_fallback",
                    "warning",
                    reason=lua_session_reject_reason,
                    transport=transport,
                    lua_event=str(body.get("event") or ""),
                    account=str(body.get("username") or body.get("account") or ""),
                    helper_version=str(body.get("helper_version") or ""),
                    supplied_token_len=len(str(supplied_token or "")),
                )
            else:
                status_code = 409 if lua_session_reject_reason == "duplicate_event" else 403
                flog_kv(
                    "LUA",
                    "rejoin_event_rejected",
                    "warning",
                    reason=lua_session_reject_reason,
                    transport=transport,
                    lua_event=str(body.get("event") or ""),
                    account=str(body.get("username") or body.get("account") or ""),
                )
                raise HTTPException(status_code, "Invalid Lua session token")
    elif not backend_token_valid:
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

    @app.get("/api/lua/executor-loader")
    def api_lua_executor_loader(request: Request):
        require_api_token(request, ctx)
        if not os.path.exists(_EXECUTOR_LOADER_PATH):
            raise HTTPException(404, "Lua executor loader not found")
        return {"ok": True, "source": _load_executor_loader()}

    @app.get("/api/lua/rejoin-helper", response_class=PlainTextResponse)
    def api_lua_rejoin_helper(request: Request, account: str = "", port: int = 0, shutdown_delay: float = 3.0):
        if not os.path.exists(_SCRIPT_PATH):
            raise HTTPException(404, "Lua helper template not found")
        token_scope = _validate_lua_helper_access(ctx, request, account)
        script = _render_rejoin_helper(ctx, request, account, port, shutdown_delay, token_scope)
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
        token_scope = _validate_lua_helper_access(ctx, request, account)
        script = _render_account_module(ctx, request, account, port, token_scope)
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
