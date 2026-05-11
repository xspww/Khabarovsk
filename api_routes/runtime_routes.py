from __future__ import annotations

import asyncio
import json
import time

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from core import account_launch_block_reason, flog_kv
from services.process_service import ProcessManager

from .context import ApiContext
from .settings_state import _apply_game_defaults


def register(app, ctx: ApiContext) -> None:
    farm = ctx.farm
    @app.get("/api/status")
    def api_status():
        return farm.get_status()

    @app.get("/api/runtime/health")
    def api_runtime_health():
        return farm.get_runtime_health()

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
    def api_start():
        accepted, command = farm.begin_command("global", "start", ttl=60.0)
        if not accepted:
            duplicate = bool(command.get("duplicate"))
            return {"ok": duplicate, "accepted": False, "duplicate": duplicate, "command_id": command.get("command_id", ""), "msg": command.get("msg") or "Start unavailable"}
        ok = False
        error = ""
        try:
            if farm.running:
                return {"ok": False, "accepted": False, "command_id": command["command_id"], "msg": "Already running"}
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
                return {
                    "ok": False,
                    "accepted": False,
                    "command_id": command["command_id"],
                    "msg": "No launchable accounts. Reimport the correct cookie for blocked accounts.",
                    "launchable_count": 0,
                    "blocked_count": len(blocked),
                    "blocked": blocked,
                }
            missing_targets = [
                a.username for a in launchable_accounts
                if not str(a.place_id or "").strip() and not list(a.vip_links or [])
            ]
            if missing_targets:
                shown = ", ".join(missing_targets[:3])
                suffix = "" if len(missing_targets) <= 3 else " ..."
                return {"ok": False, "accepted": False, "command_id": command["command_id"], "msg": f"Missing Place ID or VIP link for: {shown}{suffix}"}
            farm.start()
            ok = True
            msg = f"Farm started: {len(launchable_accounts)}/{len(farm._accounts)} accounts launchable"
            if blocked:
                msg += f"; {len(blocked)} blocked by cookie mismatch"
            return {
                "ok": True,
                "accepted": True,
                "command_id": command["command_id"],
                "msg": msg,
                "launchable_count": len(launchable_accounts),
                "blocked_count": len(blocked),
                "blocked": blocked,
            }
        except Exception as e:
            error = str(e)
            flog_kv("API", "start_failed", "error", command_id=command["command_id"], error=e)
            if "Multi Roblox guard failed" in error:
                return {
                    "ok": False,
                    "accepted": False,
                    "command_id": command["command_id"],
                    "msg": error,
                    "multi_roblox_guard_state": "failed",
                }
            raise
        finally:
            farm.finish_command("global", command["command_id"], ok=ok, error=error)

    @app.post("/api/stop")
    def api_stop():
        accepted, command = farm.begin_command("global", "stop", ttl=60.0)
        if not accepted:
            duplicate = bool(command.get("duplicate"))
            return {"ok": duplicate, "accepted": False, "duplicate": duplicate, "command_id": command.get("command_id", ""), "msg": command.get("msg") or "Stop unavailable"}
        ok = False
        error = ""
        try:
            if not farm.running:
                return {"ok": False, "accepted": False, "command_id": command["command_id"], "msg": "Not running"}
            farm.stop()
            ok = True
            return {"ok": True, "accepted": True, "command_id": command["command_id"], "msg": "Farm stopped"}
        except Exception as e:
            error = str(e)
            flog_kv("API", "stop_failed", "error", command_id=command["command_id"], error=e)
            raise
        finally:
            farm.finish_command("global", command["command_id"], ok=ok, error=error)

    @app.post("/api/roblox/close-all")
    def api_close_all_roblox():
        accepted, command = farm.begin_command("global", "close_all_roblox", ttl=60.0)
        if not accepted:
            duplicate = bool(command.get("duplicate"))
            return {"ok": duplicate, "accepted": False, "duplicate": duplicate, "command_id": command.get("command_id", ""), "msg": command.get("msg") or "Close all Roblox unavailable"}
        ok = False
        error = ""
        try:
            farm_was_running = bool(farm.running)
            if farm_was_running:
                farm.stop()
            closed = ProcessManager.kill_all_roblox_clients(wait_seconds=4.0)
            ok = True
            flog_kv("API", "close_all_roblox", account="*", closed=closed, farm_was_running=farm_was_running)
            return {
                "ok": True,
                "accepted": True,
                "command_id": command["command_id"],
                "closed": closed,
                "farm_was_running": farm_was_running,
                "msg": f"Closed Roblox clients: {closed}",
            }
        except Exception as e:
            error = str(e)
            flog_kv("API", "close_all_roblox_failed", "error", command_id=command["command_id"], error=e)
            raise
        finally:
            farm.finish_command("global", command["command_id"], ok=ok, error=error)

    @app.post("/api/account/{username}/rejoin")
    def api_rejoin(username: str):
        key = f"account:{username}"
        accepted, command = farm.begin_command(key, "force_rejoin", account=username, ttl=20.0)
        if not accepted:
            duplicate = bool(command.get("duplicate"))
            return {"ok": duplicate, "accepted": False, "duplicate": duplicate, "command_id": command.get("command_id", ""), "msg": command.get("msg") or f"Rejoin unavailable: {username}"}
        ok = False
        error = ""
        try:
            ok, msg = farm.force_rejoin(username)
            return {"ok": ok, "accepted": ok, "command_id": command["command_id"], "msg": msg}
        except Exception as e:
            error = str(e)
            flog_kv("API", "rejoin_failed", "error", command_id=command["command_id"], account=username, error=e)
            raise
        finally:
            farm.finish_command(key, command["command_id"], ok=ok, error=error)

    @app.post("/api/account/{username}/kill")
    def api_kill(username: str):
        key = f"account:{username}"
        accepted, command = farm.begin_command(key, "kill_pid", account=username, ttl=20.0)
        if not accepted:
            if command.get("msg") == "Account not found":
                raise HTTPException(404, "Account not found")
            duplicate = bool(command.get("duplicate"))
            return {"ok": duplicate, "accepted": False, "duplicate": duplicate, "command_id": command.get("command_id", ""), "msg": command.get("msg") or f"Kill unavailable: {username}"}
        ok = False
        error = ""
        try:
            ok, msg = farm.kill_account_pid(username)
            if msg == "Account not found":
                raise HTTPException(404, "Account not found")
            return {"ok": ok, "accepted": ok, "command_id": command["command_id"], "msg": msg}
        except Exception as e:
            error = str(e)
            flog_kv("API", "kill_failed", "error", command_id=command["command_id"], account=username, error=e)
            raise
        finally:
            farm.finish_command(key, command["command_id"], ok=ok, error=error)

    @app.post("/api/account/{username}/verify")
    def api_verify(username: str):
        key = f"account:{username}"
        accepted, command = farm.begin_command(key, "verify_finished", account=username, ttl=20.0)
        if not accepted:
            if command.get("msg") == "Account not found":
                raise HTTPException(404, "Account not found")
            duplicate = bool(command.get("duplicate"))
            return {"ok": duplicate, "accepted": False, "duplicate": duplicate, "command_id": command.get("command_id", ""), "msg": command.get("msg") or f"Verify unavailable: {username}"}
        ok = False
        error = ""
        try:
            ok, msg = farm.verify_account(username)
            if msg == "Account not found":
                raise HTTPException(404, "Account not found")
            return {"ok": ok, "accepted": ok, "command_id": command["command_id"], "msg": msg}
        except Exception as e:
            error = str(e)
            flog_kv("API", "verify_failed", "error", command_id=command["command_id"], account=username, error=e)
            raise
        finally:
            farm.finish_command(key, command["command_id"], ok=ok, error=error)
