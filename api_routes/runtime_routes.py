from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from core import flog_kv
from runtime.popup_detector.popup_sampler import PopupWindowSampler
from runtime.start_readiness import decide_start
from services.process_service import ProcessManager

from .auth import require_api_token
from .context import ApiContext
from .request_names import bulk_usernames as _bulk_names
from .settings_state import _apply_game_defaults


def _begin_runtime_command(
    farm: Any, request: Request, key: str, action: str, account: str = "", ttl: float = 15.0
):
    fingerprint = f"{request.method}:{request.url.path}:{action}:{account}"
    return farm.begin_command(
        key,
        action,
        account=account,
        ttl=ttl,
        idempotency_key=str(request.headers.get("X-Cronus-Idempotency-Key") or ""),
        request_fingerprint=fingerprint,
    )


def _command_rejected_payload(command: dict, fallback_msg: str):
    if command.get("idempotent_replay") and isinstance(command.get("response"), dict):
        return command["response"]
    duplicate = bool(command.get("duplicate"))
    return {
        "ok": duplicate,
        "accepted": False,
        "duplicate": duplicate,
        "command_id": command.get("command_id", ""),
        "msg": command.get("msg") or fallback_msg,
    }


def _run_runtime_command(
    farm: Any,
    request: Request,
    key: str,
    command_name: str,
    *,
    ttl: float,
    reject_message: str,
    log_event: str,
    action: Callable[[dict[str, Any]], dict[str, Any]],
    account: str = "",
    log_account: str = "",
    not_found_as_404: bool = False,
) -> dict[str, Any]:
    """Begin, run, log and finish one runtime command."""
    accepted, command = _begin_runtime_command(
        farm, request, key, command_name, account=account, ttl=ttl
    )
    if not accepted:
        if not_found_as_404 and command.get("msg") == "Account not found":
            raise HTTPException(404, "Account not found")
        return _command_rejected_payload(command, reject_message)
    ok = False
    error = ""
    result = None
    try:
        result = action(command)
        ok = bool(result.get("ok"))
        return result
    except Exception as e:
        error = str(e)
        extra = {"account": log_account} if log_account else {}
        flog_kv("API", log_event, "error", command_id=command["command_id"], error=e, **extra)
        raise
    finally:
        farm.finish_command(key, command["command_id"], ok=ok, error=error, response=result)


def _api_status(ctx: ApiContext) -> dict[str, Any]:
    return ctx.farm.get_status()


def _lite_account(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    return {
        "username": str(item.get("username") or ""),
        "account_id": str(item.get("account_id") or item.get("username") or ""),
        "state": str(item.get("state") or ""),
        "public_state": str(item.get("public_state") or ""),
        "blocked_reason": str(item.get("blocked_reason") or ""),
        "pid": item.get("pid"),
        "process_alive": bool(item.get("process_alive", False)),
    }


def _lite_accounts(accounts: Any) -> list[dict[str, Any]]:
    lite: list[dict[str, Any]] = []
    try:
        for item in accounts or []:
            entry = _lite_account(item)
            if entry is not None:
                lite.append(entry)
    except Exception:
        return []
    return lite


def _lite_payload(full: Any, lite_accounts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ok": True,
        "running": bool(full.get("running", False)),
        "status_revision": int(full.get("status_revision", -1) or -1),
        "status_updated_at": float(full.get("status_updated_at") or 0.0),
        "total_accounts": int(
            full.get("total_accounts", len(lite_accounts)) or len(lite_accounts)
        ),
        "in_game": int(full.get("in_game", 0) or 0),
        "failed": int(full.get("failed", 0) or 0),
        "queued": int(full.get("queued", 0) or 0),
        "launching": int(full.get("launching", 0) or 0),
        "accounts": lite_accounts,
    }


def _api_status_lite(ctx: ApiContext) -> dict[str, Any]:
    try:
        full = ctx.farm.get_status()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    try:
        accounts = full.get("accounts") if isinstance(full, dict) else []
    except Exception:
        accounts = []
    lite_accounts = _lite_accounts(accounts)
    return _lite_payload(full, lite_accounts)


def _api_runtime_health(ctx: ApiContext) -> dict[str, Any]:
    return ctx.farm.get_runtime_health()


def _api_farm_health(ctx: ApiContext) -> dict[str, Any]:
    return ctx.farm.get_public_farm_health()


def _api_farm_health_detail(ctx: ApiContext, request: Request) -> dict[str, Any]:
    require_api_token(request, ctx)
    return ctx.farm.get_detailed_farm_health()


def _api_runtime_telemetry(ctx: ApiContext) -> dict[str, Any]:
    return ctx.farm.get_runtime_telemetry()


def _api_runtime_events(
    ctx: ApiContext,
    request: Request,
    account_id: str = "",
    limit: int = 100,
    event_type: str = "",
    severity: str = "",
) -> dict[str, Any]:
    require_api_token(request, ctx)
    return ctx.farm.get_runtime_events(
        account_id=account_id,
        limit=limit,
        event_type=event_type,
        severity=severity,
    )


def _api_runtime_diagnostics(
    ctx: ApiContext,
    request: Request,
    account_id: str = "",
    limit: int = 200,
    event_type: str = "",
    severity: str = "",
) -> dict[str, Any]:
    require_api_token(request, ctx)
    return ctx.farm.get_runtime_diagnostics(
        account_id=account_id,
        limit=limit,
        event_type=event_type,
        severity=severity,
    )


async def _api_stream(ctx: ApiContext, request: Request) -> StreamingResponse:
    farm = ctx.farm

    async def stream():
        last_revision = None
        last_snapshot_sent = 0.0
        if hasattr(farm, "open_status_stream"):
            farm.open_status_stream()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    now = time.time()
                    revision = (
                        farm.get_status_revision()
                        if hasattr(farm, "get_status_revision")
                        else None
                    )
                    if (
                        last_revision is None
                        or revision != last_revision
                        or now - last_snapshot_sent >= 10.0
                    ):
                        snapshot = farm.get_status()
                        revision = snapshot.get("status_revision")
                        payload = json.dumps(
                            snapshot,
                            ensure_ascii=False,
                            default=str,
                            separators=(",", ":"),
                        )
                        yield f"event: snapshot\ndata: {payload}\n\n"
                        last_revision = revision
                        last_snapshot_sent = now
                    else:
                        yield f": keepalive {now:.0f}\n\n"
                except Exception as e:
                    payload = json.dumps(
                        {"ok": False, "error": str(e), "ts": time.time()},
                        ensure_ascii=False,
                    )
                    yield f"event: stream_error\ndata: {payload}\n\n"
                await asyncio.sleep(1.0)
        finally:
            if hasattr(farm, "close_status_stream"):
                farm.close_status_stream()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _api_account(ctx: ApiContext, username: str) -> dict[str, Any]:
    data = ctx.farm.get_account(username)
    if not data:
        raise HTTPException(404, "Account not found")
    return data


async def _api_start(ctx: ApiContext, request: Request) -> dict[str, Any]:
    farm = ctx.farm
    body = {}
    try:
        raw = await request.json()
        if isinstance(raw, dict):
            body = raw
    except Exception:
        body = {}
    try:
        from services.roblox_http import set_selected_roblox_version

        set_selected_roblox_version(str(body.get("roblox_version") or "").strip())
    except Exception:
        "intentional: ignore exception"
    accepted, command = _begin_runtime_command(farm, request, "global", "start", ttl=60.0)
    if not accepted:
        return _command_rejected_payload(command, "Start unavailable")
    ok = False
    error = ""
    result = None
    try:
        if farm.running:
            result = {
                "ok": False,
                "accepted": False,
                "command_id": command["command_id"],
                "msg": "Already running",
            }
            return result
        _apply_game_defaults(ctx, farm._accounts, persist=True)
        cfg = ctx.cfg_mgr.snapshot()
        result = decide_start(
            farm._accounts,
            cfg,
            command["command_id"],
            starter=farm.start,
            cfg_mgr=ctx.cfg_mgr,
        )
        ok = bool(result.get("ok") and result.get("accepted"))
        return result
    except Exception as e:
        error = str(e)
        flog_kv("API", "start_failed", "error", command_id=command["command_id"], error=e)
        if "Multi Roblox guard failed" in error:
            result = {
                "ok": False,
                "accepted": False,
                "command_id": command["command_id"],
                "msg": error,
                "multi_roblox_guard_state": "failed",
            }
            return result
        raise
    finally:
        farm.finish_command("global", command["command_id"], ok=ok, error=error, response=result)


def _api_stop(ctx: ApiContext, request: Request) -> dict[str, Any]:
    farm = ctx.farm

    def _stop(command: dict[str, Any]) -> dict[str, Any]:
        if not farm.running:
            return {
                "ok": False,
                "accepted": False,
                "command_id": command["command_id"],
                "msg": "Not running",
            }
        farm.stop()
        return {
            "ok": True,
            "accepted": True,
            "command_id": command["command_id"],
            "msg": "Farm stopped",
        }

    return _run_runtime_command(
        farm,
        request,
        "global",
        "stop",
        ttl=60.0,
        reject_message="Stop unavailable",
        log_event="stop_failed",
        action=_stop,
    )


def _api_close_all_roblox(ctx: ApiContext, request: Request) -> dict[str, Any]:
    farm = ctx.farm

    def _close_all(command: dict[str, Any]) -> dict[str, Any]:
        farm_was_running = bool(farm.running)
        closed = farm.close_all_roblox(
            wait_seconds=4.0,
            reason="api_close_all_roblox",
            idempotency_key=str(request.headers.get("X-Cronus-Idempotency-Key") or ""),
            command_id=command["command_id"],
        )
        flog_kv(
            "API",
            "close_all_roblox",
            account="*",
            closed=closed,
            farm_was_running=farm_was_running,
        )
        return {
            "ok": True,
            "accepted": True,
            "command_id": command["command_id"],
            "closed": closed,
            "farm_was_running": farm_was_running,
            "msg": f"Closed Roblox clients: {closed}",
        }

    return _run_runtime_command(
        farm,
        request,
        "global",
        "close_all_roblox",
        ttl=60.0,
        reject_message="Close all Roblox unavailable",
        log_event="close_all_roblox_failed",
        action=_close_all,
    )


def _live_game_processes() -> list[dict[str, Any]]:
    try:
        live = ProcessManager.list_live_game_processes()
    except Exception:
        return []
    return live if isinstance(live, list) else []


def _account_identity(acc: Any) -> dict[str, str]:
    return {
        "username": str(getattr(acc, "_config_username", "") or ""),
        "display": str(getattr(acc, "display_name", "") or ""),
        "state": str(getattr(getattr(acc, "state", ""), "name", "") or ""),
    }


def _pid_account_map(farm: Any) -> dict[int, dict[str, str]]:
    pid_to_account: dict[int, dict[str, str]] = {}
    try:
        for acc in list(getattr(farm, "_accounts", []) or []):
            try:
                with acc._lock:
                    pid = int(getattr(acc, "pid", 0) or 0)
                    if not pid:
                        continue
                    pid_to_account[pid] = _account_identity(acc)
            except Exception:
                continue
    except Exception:
        "intentional: ignore exception"
    return pid_to_account


def _owner_info(pid_to_account: dict[int, dict[str, str]], pid: int) -> dict[str, str]:
    return pid_to_account.get(pid) or {}


def _client_metrics(entry: Any) -> dict[str, Any]:
    return {
        "rss_mb": float((entry or {}).get("rss_mb") or 0.0),
        "windows": int((entry or {}).get("windows") or 0),
        "exe": str((entry or {}).get("name") or "RobloxPlayerBeta.exe"),
    }


def _running_client_item(
    entry: Any, pid_to_account: dict[int, dict[str, str]]
) -> dict[str, Any] | None:
    try:
        pid = int((entry or {}).get("pid") or 0)
    except Exception:
        return None
    if not pid:
        return None
    owner = _owner_info(pid_to_account, pid)
    return {
        "pid": pid,
        "account": str(owner.get("username") or ""),
        "display": str(owner.get("display") or ""),
        "state": str(owner.get("state") or ""),
        **_client_metrics(entry),
    }


def _running_client_items(
    live: Any, pid_to_account: dict[int, dict[str, str]]
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for entry in live if isinstance(live, list) else []:
        item = _running_client_item(entry, pid_to_account)
        if item is not None:
            items.append(item)
    return items


def _api_roblox_running_clients(ctx: ApiContext) -> dict[str, Any]:
    live = _live_game_processes()
    pid_to_account = _pid_account_map(ctx.farm)
    items = _running_client_items(live, pid_to_account)
    return {"ok": True, "clients": items, "count": len(items)}


def _parse_pids(body: Any) -> list[int]:
    raw_pids = (body or {}).get("pids", []) if isinstance(body, dict) else []
    if not isinstance(raw_pids, list):
        raise HTTPException(400, "pids must be a list")
    wanted: list[int] = []
    for raw in raw_pids:
        try:
            pid = int(raw)
        except Exception:
            continue
        if pid > 0 and pid not in wanted:
            wanted.append(pid)
    return wanted


def _live_pid_set() -> set[int]:
    try:
        live = {
            int((entry or {}).get("pid") or 0)
            for entry in (ProcessManager.list_live_game_processes() or [])
        }
    except Exception:
        live = set()
    live.discard(0)
    return live


def _pid_bound_accounts(farm: Any) -> dict[int, str]:
    bound: dict[int, str] = {}
    try:
        for acc in list(getattr(farm, "_accounts", []) or []):
            try:
                with acc._lock:
                    pid = int(getattr(acc, "pid", 0) or 0)
                    if pid:
                        bound[pid] = str(getattr(acc, "_config_username", "") or "")
            except Exception:
                continue
    except Exception:
        return {}
    return bound


def _close_wanted_pids(
    farm: Any, wanted: list[int], live_pids: set[int], bound: dict[int, str]
) -> tuple[list[int], list[int]]:
    closed: list[int] = []
    skipped: list[int] = []
    for pid in wanted:
        if pid not in live_pids:
            skipped.append(pid)
            continue
        username = bound.get(pid, "")
        if username:
            ok, _message = farm.kill_account_pid(
                username, reason="api_close_pids_roblox"
            )
            if ok:
                closed.append(pid)
            else:
                skipped.append(pid)
        else:
            try:
                if ProcessManager.kill_pid(pid):
                    closed.append(pid)
                else:
                    skipped.append(pid)
            except Exception:
                skipped.append(pid)
    return closed, skipped


def _closed_pids_message(closed_count: int) -> str:
    if closed_count == 0:
        return "No running Roblox clients to close"
    if closed_count == 1:
        return "Closed 1 Roblox client"
    return f"Closed {closed_count} Roblox clients"


async def _api_close_pids_roblox(
    ctx: ApiContext, request: Request
) -> dict[str, Any]:
    body = await request.json()
    wanted = _parse_pids(body)
    # Re-verify every PID against the live Roblox game list at kill
    # time so a forged PID can never kill an arbitrary process.
    live_pids = _live_pid_set()
    bound = _pid_bound_accounts(ctx.farm)
    closed, skipped = _close_wanted_pids(ctx.farm, wanted, live_pids, bound)
    flog_kv(
        "API",
        "close_pids_roblox",
        account="*",
        closed=len(closed),
        requested=len(wanted),
    )
    closed_count = len(closed)
    return {
        "ok": True,
        "closed_pids": closed,
        "closed": closed,
        "closed_count": closed_count,
        "skipped_pids": skipped,
        "msg": _closed_pids_message(closed_count),
    }


async def _api_close_selected_roblox(
    ctx: ApiContext, request: Request
) -> dict[str, Any]:
    body = await request.json()
    usernames = body.get("usernames", []) if isinstance(body, dict) else []
    if not isinstance(usernames, list):
        raise HTTPException(400, "usernames must be a list")
    closed = []
    missing = []
    for raw_username in usernames:
        username = str(raw_username or "").strip()
        if not username:
            continue
        ok, message = ctx.farm.kill_account_pid(
            username, reason="api_close_selected_roblox"
        )
        if ok:
            closed.append(username)
        elif message == "No active PID":
            missing.append(username)
    flog_kv(
        "API",
        "close_selected_roblox",
        account=",".join(closed) or "*",
        closed=len(closed),
        requested=len(usernames),
    )
    closed_count = len(closed)
    if closed_count == 0:
        close_msg = "No running Roblox clients to close"
    elif closed_count == 1:
        close_msg = "Closed Roblox for 1 account"
    else:
        close_msg = f"Closed Roblox for {closed_count} accounts"
    return {
        "ok": True,
        "closed": closed,
        "closed_count": closed_count,
        "no_active_roblox": missing,
        "msg": close_msg,
    }


def _api_rejoin(ctx: ApiContext, username: str, request: Request) -> dict[str, Any]:
    farm = ctx.farm

    def _rejoin(command: dict[str, Any]) -> dict[str, Any]:
        ok, msg = farm.force_rejoin(username)
        return {
            "ok": ok,
            "accepted": ok,
            "command_id": command["command_id"],
            "msg": msg,
        }

    return _run_runtime_command(
        farm,
        request,
        f"account:{username}",
        "force_rejoin",
        ttl=20.0,
        reject_message=f"Rejoin unavailable: {username}",
        log_event="rejoin_failed",
        action=_rejoin,
        account=username,
        log_account=username,
    )


def _api_resume_captcha(
    ctx: ApiContext, username: str, request: Request
) -> dict[str, Any]:
    farm = ctx.farm

    def _resume(command: dict[str, Any]) -> dict[str, Any]:
        ok, msg = farm.resume_captcha_account(username)
        if msg == "Account not found":
            raise HTTPException(404, "Account not found")
        return {
            "ok": ok,
            "accepted": ok,
            "command_id": command["command_id"],
            "msg": msg,
        }

    return _run_runtime_command(
        farm,
        request,
        f"account:{username}",
        "captcha_resume",
        ttl=20.0,
        reject_message=f"Resume unavailable: {username}",
        log_event="captcha_resume_failed",
        action=_resume,
        account=username,
        log_account=username,
        not_found_as_404=True,
    )


def _api_resume_locked(
    ctx: ApiContext, username: str, request: Request
) -> dict[str, Any]:
    farm = ctx.farm

    def _resume(command: dict[str, Any]) -> dict[str, Any]:
        ok, msg = farm.resume_locked_account(username)
        if msg == "Account not found":
            raise HTTPException(404, "Account not found")
        return {
            "ok": ok,
            "accepted": ok,
            "command_id": command["command_id"],
            "msg": msg,
        }

    return _run_runtime_command(
        farm,
        request,
        f"account:{username}",
        "locked_resume",
        ttl=20.0,
        reject_message=f"Resume unavailable: {username}",
        log_event="locked_resume_failed",
        action=_resume,
        account=username,
        log_account=username,
        not_found_as_404=True,
    )


def _api_resume_recovery(
    ctx: ApiContext, username: str, request: Request
) -> dict[str, Any]:
    farm = ctx.farm

    def _resume(command: dict[str, Any]) -> dict[str, Any]:
        ok, msg = farm.resume_recovery_account(username)
        if msg == "Account not found":
            raise HTTPException(404, "Account not found")
        return {
            "ok": ok,
            "accepted": ok,
            "command_id": command["command_id"],
            "msg": msg,
        }

    return _run_runtime_command(
        farm,
        request,
        f"account:{username}",
        "recovery_resume",
        ttl=20.0,
        reject_message=f"Resume unavailable: {username}",
        log_event="recovery_resume_failed",
        action=_resume,
        account=username,
        log_account=username,
        not_found_as_404=True,
    )


def _api_resume_verify(
    ctx: ApiContext, username: str, request: Request
) -> dict[str, Any]:
    farm = ctx.farm

    def _resume(command: dict[str, Any]) -> dict[str, Any]:
        ok, msg = farm.resume_verify_account(username)
        if msg == "Account not found":
            raise HTTPException(404, "Account not found")
        return {
            "ok": ok,
            "accepted": ok,
            "command_id": command["command_id"],
            "msg": msg,
        }

    return _run_runtime_command(
        farm,
        request,
        f"account:{username}",
        "verify_resume",
        ttl=20.0,
        reject_message=f"Resume unavailable: {username}",
        log_event="verify_resume_failed",
        action=_resume,
        account=username,
        log_account=username,
        not_found_as_404=True,
    )


async def _bulk_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected object")
    return body


def _bulk_message(
    prefix: str, suffix: str, resumed: list, skipped: list, missing: list
) -> str:
    msg = f"{prefix}: {len(resumed)}"
    if skipped:
        msg += f", skipped {len(skipped)} ({suffix})"
    if missing:
        msg += f", missing {len(missing)}"
    return msg


def _captcha_guard():
    try:
        from services.captcha_guard import is_account_captcha_required
    except Exception:
        return lambda _a: False
    return is_account_captcha_required


def _resume_captcha_names(
    farm: Any, names: list[str], is_required
) -> tuple[list, list, list]:
    resumed: list = []
    skipped: list = []
    missing: list = []
    for username in names:
        acc = farm._find_account(username) if hasattr(farm, "_find_account") else None
        if acc is None:
            missing.append(username)
            continue
        try:
            needs = bool(is_required(acc))
        except Exception:
            needs = False
        if not needs:
            skipped.append(username)
            continue
        try:
            ok, _msg = farm.resume_captcha_account(username)
        except Exception:
            ok = False
        if ok:
            resumed.append(username)
        else:
            skipped.append(username)
    return resumed, skipped, missing


async def _api_resume_captcha_bulk(
    ctx: ApiContext, request: Request
) -> dict[str, Any]:
    body = await _bulk_body(request)
    names = _bulk_names(body)
    is_required = _captcha_guard()
    resumed, skipped, missing = _resume_captcha_names(ctx.farm, names, is_required)
    msg = _bulk_message("Resumed CAPTCHA", "not captcha", resumed, skipped, missing)
    flog_kv(
        "API",
        "captcha_resume_bulk",
        resumed=len(resumed),
        skipped=len(skipped),
        missing=len(missing),
    )
    return {
        "ok": True,
        "resumed": resumed,
        "skipped": skipped,
        "missing": missing,
        "msg": msg,
    }


def _verify_guards():
    try:
        from services.captcha_guard import is_account_captcha_required as _is_cap
        from services.verify_guard import is_account_locked, is_account_recovery
    except Exception:
        return (lambda _a: False, lambda _a: False, lambda _a: False)
    return is_account_locked, is_account_recovery, _is_cap


def _resume_verify_names(
    farm: Any, names: list[str], locked, recovery, cap
) -> tuple[list, list, list]:
    resumed: list = []
    skipped: list = []
    missing: list = []
    for username in names:
        acc = farm._find_account(username) if hasattr(farm, "_find_account") else None
        if acc is None:
            missing.append(username)
            continue
        try:
            needs = bool(locked(acc) or recovery(acc) or cap(acc))
        except Exception:
            needs = False
        if not needs:
            skipped.append(username)
            continue
        try:
            ok, _msg = farm.resume_verify_account(username)
        except Exception:
            ok = False
        if ok:
            resumed.append(username)
        else:
            skipped.append(username)
    return resumed, skipped, missing


async def _api_resume_verify_bulk(
    ctx: ApiContext, request: Request
) -> dict[str, Any]:
    body = await _bulk_body(request)
    names = _bulk_names(body)
    locked, recovery, cap = _verify_guards()
    resumed, skipped, missing = _resume_verify_names(
        ctx.farm, names, locked, recovery, cap
    )
    msg = _bulk_message(
        "Resumed verify", "not locked/recovery/captcha", resumed, skipped, missing
    )
    flog_kv(
        "API",
        "verify_resume_bulk",
        resumed=len(resumed),
        skipped=len(skipped),
        missing=len(missing),
    )
    return {
        "ok": True,
        "resumed": resumed,
        "skipped": skipped,
        "missing": missing,
        "msg": msg,
    }


def _api_focus_captcha(
    ctx: ApiContext, username: str, request: Request
) -> dict[str, Any]:
    farm = ctx.farm
    key = f"account:{username}"
    accepted, command = _begin_runtime_command(
        farm, request, key, "captcha_focus", account=username, ttl=8.0
    )
    if not accepted:
        if command.get("msg") == "Account not found":
            raise HTTPException(404, "Account not found")
        return _command_rejected_payload(command, f"Focus unavailable: {username}")
    ok = False
    error = ""
    result = None
    try:
        acc = getattr(farm, "_find_account", lambda _username: None)(username)
        if not acc:
            raise HTTPException(404, "Account not found")
        pid = int(getattr(acc, "pid", 0) or 0)
        if not pid:
            result = {
                "ok": False,
                "accepted": False,
                "command_id": command["command_id"],
                "msg": "No Roblox window bound to this account.",
                "pid": 0,
            }
            return result
        focused = PopupWindowSampler().focus_pid_window(pid)
        ok = bool(focused.get("ok"))
        result = {
            "ok": ok,
            "accepted": ok,
            "command_id": command["command_id"],
            "pid": pid,
            "focused": bool(focused.get("focused", False)),
            "msg": "Roblox CAPTCHA window focused."
            if ok
            else str(focused.get("reason") or "Unable to focus Roblox window."),
        }
        flog_kv(
            "API",
            "captcha_focus",
            "warning" if ok else "error",
            command_id=command["command_id"],
            account=username,
            pid=pid,
            focused=result["focused"],
            ok=ok,
        )
        return result
    except Exception as e:
        error = str(e)
        flog_kv(
            "API",
            "captcha_focus_failed",
            "error",
            command_id=command["command_id"],
            account=username,
            error=e,
        )
        raise
    finally:
        farm.finish_command(key, command["command_id"], ok=ok, error=error, response=result)


def _api_kill(ctx: ApiContext, username: str, request: Request) -> dict[str, Any]:
    farm = ctx.farm
    key = f"account:{username}"
    accepted, command = _begin_runtime_command(
        farm, request, key, "kill_pid", account=username, ttl=20.0
    )
    if not accepted:
        if command.get("msg") == "Account not found":
            raise HTTPException(404, "Account not found")
        return _command_rejected_payload(command, f"Kill unavailable: {username}")
    ok = False
    error = ""
    result = None
    try:
        ok, msg = farm.kill_account_pid(username)
        if msg == "Account not found":
            raise HTTPException(404, "Account not found")
        result = {
            "ok": ok,
            "accepted": ok,
            "command_id": command["command_id"],
            "msg": msg,
        }
        return result
    except Exception as e:
        error = str(e)
        flog_kv(
            "API",
            "kill_failed",
            "error",
            command_id=command["command_id"],
            account=username,
            error=e,
        )
        raise
    finally:
        farm.finish_command(key, command["command_id"], ok=ok, error=error, response=result)


def _register_status(app, ctx: ApiContext) -> None:
    @app.get("/api/status")
    def api_status():
        return _api_status(ctx)

    @app.get("/api/status/lite")
    def api_status_lite():
        """Lightweight poll for background refresh: revision + counts only.

        Full /api/status carries ~150 fields per account and re-renders the
        whole table. Background timers (5s fallback, 60s keepalive, update
        notice) must use this instead to avoid Chromium heap growth.
        """
        return _api_status_lite(ctx)

    @app.get("/api/runtime/health")
    def api_runtime_health():
        return _api_runtime_health(ctx)

    @app.get("/api/farm/health")
    def api_farm_health():
        return _api_farm_health(ctx)

    @app.get("/api/farm/health/detail")
    def api_farm_health_detail(request: Request):
        return _api_farm_health_detail(ctx, request)

    @app.get("/api/runtime/telemetry")
    def api_runtime_telemetry():
        return _api_runtime_telemetry(ctx)

    @app.get("/api/runtime/events")
    def api_runtime_events(
        request: Request,
        account_id: str = "",
        limit: int = 100,
        event_type: str = "",
        severity: str = "",
    ):
        return _api_runtime_events(
            ctx, request, account_id, limit, event_type, severity
        )

    @app.get("/api/runtime/diagnostics")
    def api_runtime_diagnostics(
        request: Request,
        account_id: str = "",
        limit: int = 200,
        event_type: str = "",
        severity: str = "",
    ):
        return _api_runtime_diagnostics(
            ctx, request, account_id, limit, event_type, severity
        )

    @app.get("/api/stream")
    async def api_stream(request: Request):
        return await _api_stream(ctx, request)


def _register_runtime_commands(app, ctx: ApiContext) -> None:
    @app.get("/api/account/{username}")
    def api_account(username: str):
        return _api_account(ctx, username)

    @app.post("/api/start")
    async def api_start(request: Request):
        return await _api_start(ctx, request)

    @app.post("/api/stop")
    def api_stop(request: Request):
        return _api_stop(ctx, request)

    @app.post("/api/roblox/close-all")
    def api_close_all_roblox(request: Request):
        return _api_close_all_roblox(ctx, request)


def _register_roblox(app, ctx: ApiContext) -> None:
    @app.get("/api/roblox/running-clients")
    def api_roblox_running_clients():
        return _api_roblox_running_clients(ctx)

    @app.post("/api/roblox/close-pids")
    async def api_close_pids_roblox(request: Request):
        return await _api_close_pids_roblox(ctx, request)

    @app.post("/api/roblox/close-selected")
    async def api_close_selected_roblox(request: Request):
        return await _api_close_selected_roblox(ctx, request)


def _register_resume(app, ctx: ApiContext) -> None:
    @app.post("/api/account/{username}/rejoin")
    def api_rejoin(username: str, request: Request):
        return _api_rejoin(ctx, username, request)

    @app.post("/api/account/{username}/captcha/resume")
    def api_resume_captcha(username: str, request: Request):
        return _api_resume_captcha(ctx, username, request)

    @app.post("/api/account/{username}/locked/resume")
    def api_resume_locked(username: str, request: Request):
        """Resume an account held by an identity lock."""

        return _api_resume_locked(ctx, username, request)

    @app.post("/api/account/{username}/recovery/resume")
    def api_resume_recovery(username: str, request: Request):
        """Resume an account held by an account-recovery challenge."""

        return _api_resume_recovery(ctx, username, request)

    @app.post("/api/account/{username}/verify/resume")
    def api_resume_verify(username: str, request: Request):
        """Resume an account held by a verification challenge."""

        return _api_resume_verify(ctx, username, request)

    @app.post("/api/accounts/captcha/resume")
    async def api_resume_captcha_bulk(request: Request):
        return await _api_resume_captcha_bulk(ctx, request)

    @app.post("/api/accounts/verify/resume")
    async def api_resume_verify_bulk(request: Request):
        return await _api_resume_verify_bulk(ctx, request)

    @app.post("/api/account/{username}/captcha/focus")
    def api_focus_captcha(username: str, request: Request):
        return _api_focus_captcha(ctx, username, request)

    @app.post("/api/account/{username}/kill")
    def api_kill(username: str, request: Request):
        return _api_kill(ctx, username, request)


def register(app, ctx: ApiContext) -> None:
    _register_status(app, ctx)
    _register_runtime_commands(app, ctx)
    _register_roblox(app, ctx)
    _register_resume(app, ctx)
