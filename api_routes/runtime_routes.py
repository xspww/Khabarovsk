from __future__ import annotations

import asyncio
import json
import time

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from core import account_launch_block_reason, flog_kv
from runtime.popup_detector.popup_sampler import PopupWindowSampler
from .context import ApiContext
from .settings_state import _apply_game_defaults


def register(app, ctx: ApiContext) -> None:
    farm = ctx.farm

    def _begin_runtime_command(request: Request, key: str, action: str, account: str = "", ttl: float = 15.0):
        fingerprint = f"{request.method}:{request.url.path}:{action}:{account}"
        return farm.begin_command(
            key,
            action,
            account=account,
            ttl=ttl,
            idempotency_key=str(request.headers.get("X-Argus-Idempotency-Key") or ""),
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

    def _blocked_summary(blocked: list[dict]) -> str:
        if not blocked:
            return ""
        reasons = [str(item.get("reason") or "").lower() for item in blocked]
        if reasons and all("captcha" in reason for reason in reasons):
            return f"{len(blocked)} blocked by CAPTCHA"
        if reasons and all("cookie" in reason for reason in reasons):
            return f"{len(blocked)} blocked by cookie"
        return f"{len(blocked)} blocked"

    @app.get("/api/status")
    def api_status():
        return farm.get_status()

    @app.get("/api/runtime/health")
    def api_runtime_health():
        return farm.get_runtime_health()

    @app.get("/api/runtime/telemetry")
    def api_runtime_telemetry():
        return farm.get_runtime_telemetry()

    @app.get("/api/runtime/events")
    def api_runtime_events(account_id: str = "", limit: int = 100):
        return farm.get_runtime_events(account_id=account_id, limit=limit)

    @app.get("/api/stream")
    async def api_stream(request: Request):
        async def stream():
            last_revision = None
            last_snapshot_sent = 0.0
            while True:
                if await request.is_disconnected():
                    break
                try:
                    snapshot = farm.get_status()
                    revision = snapshot.get("status_revision")
                    now = time.time()
                    if revision != last_revision or now - last_snapshot_sent >= 2.5:
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
    def api_start(request: Request):
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
            blocked = [
                {"username": a.username, "reason": account_launch_block_reason(a)}
                for a in farm._accounts
                if account_launch_block_reason(a)
            ]
            blocked_names = {str(item["username"]).strip().lower() for item in blocked}
            launchable_accounts = [
                a for a in farm._accounts
                if str(a.username or "").strip().lower() not in blocked_names
            ]
            if not launchable_accounts:
                result = {
                    "ok": False,
                    "accepted": False,
                    "command_id": command["command_id"],
                    "msg": "No launchable accounts. Reimport the correct cookie for blocked accounts.",
                    "launchable_count": 0,
                    "blocked_count": len(blocked),
                    "blocked": blocked,
                }
                return result
            missing_targets = [
                a.username for a in launchable_accounts
                if not str(a.place_id or "").strip() and not list(a.vip_links or [])
            ]
            if missing_targets:
                shown = ", ".join(missing_targets[:3])
                suffix = "" if len(missing_targets) <= 3 else " ..."
                result = {"ok": False, "accepted": False, "command_id": command["command_id"], "msg": f"Missing Place ID or VIP link for: {shown}{suffix}"}
                return result
            farm.start()
            ok = True
            msg = f"Farm started: {len(launchable_accounts)}/{len(farm._accounts)} accounts launchable"
            if blocked:
                msg += f"; {_blocked_summary(blocked)}"
            result = {
                "ok": True,
                "accepted": True,
                "command_id": command["command_id"],
                "msg": msg,
                "launchable_count": len(launchable_accounts),
                "blocked_count": len(blocked),
                "blocked": blocked,
            }
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
                idempotency_key=str(request.headers.get("X-Argus-Idempotency-Key") or ""),
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

    @app.post("/api/account/{username}/verify")
    def api_verify(username: str, request: Request):
        key = f"account:{username}"
        accepted, command = _begin_runtime_command(request, key, "verify_finished", account=username, ttl=20.0)
        if not accepted:
            if command.get("msg") == "Account not found":
                raise HTTPException(404, "Account not found")
            return _command_rejected_payload(command, f"Verify unavailable: {username}")
        ok = False
        error = ""
        result = None
        try:
            ok, msg = farm.verify_account(username)
            if msg == "Account not found":
                raise HTTPException(404, "Account not found")
            result = {"ok": ok, "accepted": ok, "command_id": command["command_id"], "msg": msg}
            return result
        except Exception as e:
            error = str(e)
            flog_kv("API", "verify_failed", "error", command_id=command["command_id"], account=username, error=e)
            raise
        finally:
            farm.finish_command(key, command["command_id"], ok=ok, error=error, response=result)
