from __future__ import annotations

import asyncio
import json
import time

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from core import flog_kv
from desktop import console_output
from runtime.start_readiness import decide_start
from runtime.popup_detector.popup_sampler import PopupWindowSampler
from .auth import require_api_token
from .settings_state import _apply_game_defaults
from .context import ApiContext


def register(app, ctx: ApiContext) -> None:
    farm = ctx.farm

    def _begin_runtime_command(request: Request, key: str, action: str, account: str = "", ttl: float = 15.0):
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

    @app.get("/api/status")
    def api_status():
        return farm.get_status()

    @app.get("/api/status/lite")
    def api_status_lite():
        """Lightweight poll for background refresh: revision + counts only.

        Full /api/status carries ~150 fields per account and re-renders the
        whole table. Background timers (5s fallback, 60s keepalive, update
        notice) must use this instead to avoid Chromium heap growth.
        """
        try:
            full = farm.get_status()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        try:
            accounts = full.get("accounts") if isinstance(full, dict) else []
        except Exception:
            accounts = []
        lite_accounts = []
        try:
            for item in (accounts or []):
                if not isinstance(item, dict):
                    continue
                lite_accounts.append(
                    {
                        "username": str(item.get("username") or ""),
                        "account_id": str(item.get("account_id") or item.get("username") or ""),
                        "state": str(item.get("state") or ""),
                        "public_state": str(item.get("public_state") or ""),
                        "blocked_reason": str(item.get("blocked_reason") or ""),
                        "pid": item.get("pid"),
                        "process_alive": bool(item.get("process_alive", False)),
                    }
                )
        except Exception:
            lite_accounts = []
        return {
            "ok": True,
            "running": bool(full.get("running", False)),
            "status_revision": int(full.get("status_revision", -1) or -1),
            "status_updated_at": float(full.get("status_updated_at") or 0.0),
            "total_accounts": int(full.get("total_accounts", len(lite_accounts)) or len(lite_accounts)),
            "in_game": int(full.get("in_game", 0) or 0),
            "failed": int(full.get("failed", 0) or 0),
            "queued": int(full.get("queued", 0) or 0),
            "launching": int(full.get("launching", 0) or 0),
            "accounts": lite_accounts,
        }

    @app.get("/api/runtime/health")
    def api_runtime_health():
        return farm.get_runtime_health()

    @app.get("/api/farm/health")
    def api_farm_health():
        return farm.get_public_farm_health()

    @app.get("/api/farm/health/detail")
    def api_farm_health_detail(request: Request):
        require_api_token(request, ctx)
        return farm.get_detailed_farm_health()

    @app.get("/api/runtime/telemetry")
    def api_runtime_telemetry():
        return farm.get_runtime_telemetry()

    @app.get("/api/runtime/events")
    def api_runtime_events(request: Request, account_id: str = "", limit: int = 100, event_type: str = "", severity: str = ""):
        require_api_token(request, ctx)
        return farm.get_runtime_events(
            account_id=account_id,
            limit=limit,
            event_type=event_type,
            severity=severity,
        )

    @app.get("/api/runtime/diagnostics")
    def api_runtime_diagnostics(request: Request, account_id: str = "", limit: int = 200, event_type: str = "", severity: str = ""):
        require_api_token(request, ctx)
        return farm.get_runtime_diagnostics(
            account_id=account_id,
            limit=limit,
            event_type=event_type,
            severity=severity,
        )

    @app.get("/api/stream")
    async def api_stream(request: Request):
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
                        revision = farm.get_status_revision() if hasattr(farm, "get_status_revision") else None
                        if last_revision is None or revision != last_revision or now - last_snapshot_sent >= 10.0:
                            snapshot = farm.get_status()
                            revision = snapshot.get("status_revision")
                            payload = json.dumps(snapshot, ensure_ascii=False, default=str, separators=(",", ":"))
                            yield f"event: snapshot\ndata: {payload}\n\n"
                            last_revision = revision
                            last_snapshot_sent = now
                        else:
                            yield f": keepalive {now:.0f}\n\n"
                    except Exception as e:
                        payload = json.dumps({"ok": False, "error": str(e), "ts": time.time()}, ensure_ascii=False)
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

    @app.get("/api/account/{username}")
    def api_account(username: str):
        data = farm.get_account(username)
        if not data:
            raise HTTPException(404, "Account not found")
        return data

    @app.post("/api/start")
    async def api_start(request: Request):
        body = {}
        try:
            raw = await request.json()
            if isinstance(raw, dict):
                body = raw
        except Exception:
            body = {}
        try:
            from roblox_hybrid import set_selected_roblox_version
            set_selected_roblox_version(str(body.get("roblox_version") or "").strip())
        except Exception:
            pass
        accepted, command = _begin_runtime_command(request, "global", "start", ttl=60.0)
        if not accepted:
            return _command_rejected_payload(command, "Start unavailable")
        ok = False
        error = ""
        result = None
        try:
            if farm.running:
                result = {"ok": False, "accepted": False, "command_id": command["command_id"], "msg": "Already running"}
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

    @app.post("/api/stop")
    def api_stop(request: Request):
        accepted, command = _begin_runtime_command(request, "global", "stop", ttl=60.0)
        if not accepted:
            return _command_rejected_payload(command, "Stop unavailable")
        ok = False
        error = ""
        result = None
        try:
            if not farm.running:
                result = {"ok": False, "accepted": False, "command_id": command["command_id"], "msg": "Not running"}
                return result
            farm.stop()
            console_output.clear_screen()
            ok = True
            result = {"ok": True, "accepted": True, "command_id": command["command_id"], "msg": "Farm stopped"}
            return result
        except Exception as e:
            error = str(e)
            flog_kv("API", "stop_failed", "error", command_id=command["command_id"], error=e)
            raise
        finally:
            farm.finish_command("global", command["command_id"], ok=ok, error=error, response=result)

    @app.post("/api/roblox/close-all")
    def api_close_all_roblox(request: Request):
        accepted, command = _begin_runtime_command(request, "global", "close_all_roblox", ttl=60.0)
        if not accepted:
            return _command_rejected_payload(command, "Close all Roblox unavailable")
        ok = False
        error = ""
        result = None
        try:
            farm_was_running = bool(farm.running)
            closed = farm.close_all_roblox(
                wait_seconds=4.0,
                reason="api_close_all_roblox",
                idempotency_key=str(request.headers.get("X-Cronus-Idempotency-Key") or ""),
                command_id=command["command_id"],
            )
            ok = True
            flog_kv("API", "close_all_roblox", account="*", closed=closed, farm_was_running=farm_was_running)
            result = {
                "ok": True,
                "accepted": True,
                "command_id": command["command_id"],
                "closed": closed,
                "farm_was_running": farm_was_running,
                "msg": f"Closed Roblox clients: {closed}",
            }
            return result
        except Exception as e:
            error = str(e)
            flog_kv("API", "close_all_roblox_failed", "error", command_id=command["command_id"], error=e)
            raise
        finally:
            farm.finish_command("global", command["command_id"], ok=ok, error=error, response=result)

    @app.get("/api/roblox/running-clients")
    def api_roblox_running_clients():
        from services.process_service import ProcessManager

        try:
            live = ProcessManager.list_live_game_processes()
        except Exception:
            live = []
        pid_to_account: dict[int, dict] = {}
        try:
            for acc in list(getattr(farm, "_accounts", []) or []):
                try:
                    with acc._lock:
                        pid = int(getattr(acc, "pid", 0) or 0)
                        if not pid:
                            continue
                        pid_to_account[pid] = {
                            "username": str(getattr(acc, "_config_username", "") or ""),
                            "display": str(getattr(acc, "display_name", "") or ""),
                            "state": str(getattr(getattr(acc, "state", ""), "name", "") or ""),
                        }
                except Exception:
                    continue
        except Exception:
            pass
        items = []
        for entry in live if isinstance(live, list) else []:
            try:
                pid = int((entry or {}).get("pid") or 0)
            except Exception:
                continue
            if not pid:
                continue
            owner = pid_to_account.get(pid) or {}
            items.append({
                "pid": pid,
                "account": str(owner.get("username") or ""),
                "display": str(owner.get("display") or ""),
                "state": str(owner.get("state") or ""),
                "rss_mb": float((entry or {}).get("rss_mb") or 0.0),
                "windows": int((entry or {}).get("windows") or 0),
                "exe": str((entry or {}).get("name") or "RobloxPlayerBeta.exe"),
            })
        return {"ok": True, "clients": items, "count": len(items)}

    @app.post("/api/roblox/close-pids")
    async def api_close_pids_roblox(request: Request):
        from services.process_service import ProcessManager

        body = await request.json()
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
        # Re-verify every PID against the live Roblox game list at kill
        # time so a forged PID can never kill an arbitrary process.
        try:
            live_pids = {int((e or {}).get("pid") or 0) for e in (ProcessManager.list_live_game_processes() or [])}
        except Exception:
            live_pids = set()
        live_pids.discard(0)
        try:
            bound: dict[int, str] = {}
            for acc in list(getattr(farm, "_accounts", []) or []):
                try:
                    with acc._lock:
                        pid = int(getattr(acc, "pid", 0) or 0)
                        if pid:
                            bound[pid] = str(getattr(acc, "_config_username", "") or "")
                except Exception:
                    continue
        except Exception:
            bound = {}
        closed: list[int] = []
        skipped: list[int] = []
        for pid in wanted:
            if pid not in live_pids:
                skipped.append(pid)
                continue
            username = bound.get(pid, "")
            if username:
                ok, _message = farm.kill_account_pid(username, reason="api_close_pids_roblox")
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
        flog_kv(
            "API",
            "close_pids_roblox",
            account="*",
            closed=len(closed),
            requested=len(wanted),
        )
        closed_count = len(closed)
        if closed_count == 0:
            close_msg = "No running Roblox clients to close"
        elif closed_count == 1:
            close_msg = "Closed 1 Roblox client"
        else:
            close_msg = f"Closed {closed_count} Roblox clients"
        return {
            "ok": True,
            "closed_pids": closed,
            "closed": closed,
            "closed_count": closed_count,
            "skipped_pids": skipped,
            "msg": close_msg,
        }

    @app.post("/api/roblox/close-selected")
    async def api_close_selected_roblox(request: Request):
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
            ok, message = farm.kill_account_pid(username, reason="api_close_selected_roblox")
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

    @app.post("/api/account/{username}/rejoin")
    def api_rejoin(username: str, request: Request):
        key = f"account:{username}"
        accepted, command = _begin_runtime_command(request, key, "force_rejoin", account=username, ttl=20.0)
        if not accepted:
            return _command_rejected_payload(command, f"Rejoin unavailable: {username}")
        ok = False
        error = ""
        result = None
        try:
            ok, msg = farm.force_rejoin(username)
            result = {"ok": ok, "accepted": ok, "command_id": command["command_id"], "msg": msg}
            return result
        except Exception as e:
            error = str(e)
            flog_kv("API", "rejoin_failed", "error", command_id=command["command_id"], account=username, error=e)
            raise
        finally:
            farm.finish_command(key, command["command_id"], ok=ok, error=error, response=result)

    @app.post("/api/account/{username}/captcha/resume")
    def api_resume_captcha(username: str, request: Request):
        key = f"account:{username}"
        accepted, command = _begin_runtime_command(request, key, "captcha_resume", account=username, ttl=20.0)
        if not accepted:
            if command.get("msg") == "Account not found":
                raise HTTPException(404, "Account not found")
            return _command_rejected_payload(command, f"Resume unavailable: {username}")
        ok = False
        error = ""
        result = None
        try:
            ok, msg = farm.resume_captcha_account(username)
            if msg == "Account not found":
                raise HTTPException(404, "Account not found")
            result = {"ok": ok, "accepted": ok, "command_id": command["command_id"], "msg": msg}
            return result
        except Exception as e:
            error = str(e)
            flog_kv("API", "captcha_resume_failed", "error", command_id=command["command_id"], account=username, error=e)
            raise
        finally:
            farm.finish_command(key, command["command_id"], ok=ok, error=error, response=result)

    @app.post("/api/accounts/captcha/resume")
    async def api_resume_captcha_bulk(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected object")
        raw_names = body.get("usernames", [])
        if isinstance(raw_names, str):
            raw_names = [raw_names]
        if not isinstance(raw_names, list):
            raise HTTPException(400, "usernames must be a list")
        names = [str(n or "").strip() for n in raw_names if str(n or "").strip()]
        if not names:
            raise HTTPException(400, "usernames required")
        try:
            from services.captcha_guard import is_account_captcha_required
        except Exception:
            is_account_captcha_required = lambda _a: False  # type: ignore
        resumed: list = []
        skipped: list = []
        missing: list = []
        for username in names:
            acc = farm._find_account(username) if hasattr(farm, "_find_account") else None
            if acc is None:
                missing.append(username)
                continue
            try:
                needs = bool(is_account_captcha_required(acc))
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
        msg = f"Resumed CAPTCHA: {len(resumed)}"
        if skipped:
            msg += f", skipped {len(skipped)} (not captcha)"
        if missing:
            msg += f", missing {len(missing)}"
        flog_kv("API", "captcha_resume_bulk", resumed=len(resumed), skipped=len(skipped), missing=len(missing))
        return {"ok": True, "resumed": resumed, "skipped": skipped, "missing": missing, "msg": msg}

    @app.post("/api/account/{username}/captcha/focus")
    def api_focus_captcha(username: str, request: Request):
        key = f"account:{username}"
        accepted, command = _begin_runtime_command(request, key, "captcha_focus", account=username, ttl=8.0)
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
                result = {"ok": False, "accepted": False, "command_id": command["command_id"], "msg": "No Roblox window bound to this account.", "pid": 0}
                return result
            focused = PopupWindowSampler().focus_pid_window(pid)
            ok = bool(focused.get("ok"))
            result = {
                "ok": ok,
                "accepted": ok,
                "command_id": command["command_id"],
                "pid": pid,
                "focused": bool(focused.get("focused", False)),
                "msg": "Roblox CAPTCHA window focused." if ok else str(focused.get("reason") or "Unable to focus Roblox window."),
            }
            flog_kv("API", "captcha_focus", "warning" if ok else "error", command_id=command["command_id"], account=username, pid=pid, focused=result["focused"], ok=ok)
            return result
        except Exception as e:
            error = str(e)
            flog_kv("API", "captcha_focus_failed", "error", command_id=command["command_id"], account=username, error=e)
            raise
        finally:
            farm.finish_command(key, command["command_id"], ok=ok, error=error, response=result)

    @app.post("/api/account/{username}/kill")
    def api_kill(username: str, request: Request):
        key = f"account:{username}"
        accepted, command = _begin_runtime_command(request, key, "kill_pid", account=username, ttl=20.0)
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
            result = {"ok": ok, "accepted": ok, "command_id": command["command_id"], "msg": msg}
            return result
        except Exception as e:
            error = str(e)
            flog_kv("API", "kill_failed", "error", command_id=command["command_id"], account=username, error=e)
            raise
        finally:
            farm.finish_command(key, command["command_id"], ok=ok, error=error, response=result)
