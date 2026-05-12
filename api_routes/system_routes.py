from __future__ import annotations

import os
import re
import secrets
import threading
import time
from typing import Any, Dict, List

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from account_hybrid import audit_event
from core import flog_kv
from roblox_hybrid import release_multi_roblox_guard

from .context import ApiContext
from .settings_state import _int_setting

_COOKIE_RE = re.compile(r'(_\|WARNING:[^\s\'"<>]+|\.ROBLOSECURITY[^\s\'"<>]*)', re.IGNORECASE)
_KV_SECRET_RE = re.compile(r"(?i)\b(cookie|roblosecurity|ram_password|password)=([^\s]+)")


def register(app, ctx: ApiContext) -> None:
    farm = ctx.farm
    roblox_installer = ctx.roblox_installer

    def _network_fault_injector():
        return ctx.get_network_fault_injector()

    def _log_file() -> str:
        return ctx.get_log_file()
    def _redact_log_line(line: str) -> str:
        text = _COOKIE_RE.sub("[ROBLOX_COOKIE_REDACTED]", str(line or ""))
        return _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)


    def _tail_log_lines(limit: int = 300) -> List[str]:
        limit = max(1, min(int(limit or 300), 1000))
        log_file = _log_file()
        if not os.path.exists(log_file):
            return []
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-limit:]
        except Exception as e:
            flog_kv("API", "log_tail_failed", "warning", error=e)
            return []
        return [_redact_log_line(line.rstrip("\r\n")) for line in lines]

    def _record_network_fault_event(event_type: str, account_id: str = "", severity: str = "warning", **payload):
        try:
            acc = getattr(farm, "_find_account", lambda _account: None)(account_id) if account_id else None
            if hasattr(farm, "_push_event"):
                farm._push_event(
                    event_type,
                    event_type,
                    account=acc,
                    severity=severity,
                    reason=event_type,
                )
        except Exception as exc:
            flog_kv("NETWORK_FAULT", "runtime_event_failed", "warning", event_type=event_type, account=account_id, error=str(exc))
        try:
            flog_kv("NETWORK_FAULT", event_type, severity, account=account_id, **payload)
        except Exception:
            pass


    def _network_fault_target(body: Dict[str, Any]) -> Dict[str, Any]:
        account_id = str(body.get("account_id") or body.get("username") or "").strip()
        pid = body.get("pid")
        if pid in ("", None) and account_id:
            status = farm.get_status()
            for account in status.get("accounts", []):
                if str(account.get("username") or "") == account_id or str(account.get("account_id") or "") == account_id:
                    pid = account.get("pid")
                    break
        if pid not in ("", None):
            validation = _network_fault_injector().validate_roblox_pid(pid)
            if not validation.get("ok"):
                raise HTTPException(400, validation)
            validation["account_id"] = account_id
            return validation
        live = _network_fault_injector().find_live_roblox_processes()
        if not live:
            raise HTTPException(404, "No live RobloxPlayerBeta.exe process found")
        target = dict(live[0])
        target["account_id"] = account_id
        return target


    @app.get("/api/test/network-fault/status")
    def api_network_fault_status():
        return _network_fault_injector().status()


    @app.post("/api/test/network-fault/block-roblox")
    async def api_network_fault_block_roblox(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected object")
        target = _network_fault_target(body)
        duration = _int_setting(body.get("duration_seconds", 90), 90, 1, 3600)
        result = _network_fault_injector().block_roblox(
            str(target.get("exe") or ""),
            duration_seconds=duration,
            account_id=str(target.get("account_id") or ""),
            pid=int(target.get("pid") or 0),
        )
        severity = "warning" if result.get("ok") else "error"
        _record_network_fault_event(
            "network_fault_blocked" if result.get("ok") else "network_fault_block_failed",
            account_id=str(target.get("account_id") or ""),
            severity=severity,
            pid=target.get("pid"),
            duration_seconds=duration,
            program=result.get("program", ""),
            error=result.get("stderr", ""),
        )
        audit_event(
            "network_fault_blocked",
            ok=bool(result.get("ok")),
            account_id=str(target.get("account_id") or ""),
            pid=target.get("pid"),
            duration_seconds=duration,
            program=result.get("program", ""),
        )
        if not result.get("ok"):
            raise HTTPException(500, result.get("stderr") or result.get("msg") or "Failed to block Roblox outbound")
        result["target"] = {k: target.get(k) for k in ("account_id", "pid", "name", "exe", "create_time")}
        return result


    @app.post("/api/test/network-fault/restore")
    async def api_network_fault_restore(request: Request):
        body: Dict[str, Any] = {}
        try:
            parsed = await request.json()
            if isinstance(parsed, dict):
                body = parsed
        except Exception:
            body = {}
        result = _network_fault_injector().restore()
        account_id = str(body.get("account_id") or body.get("username") or "").strip()
        severity = "info" if result.get("ok") else "error"
        _record_network_fault_event(
            "network_fault_restored" if result.get("ok") else "network_fault_restore_failed",
            account_id=account_id,
            severity=severity,
            error=result.get("stderr", ""),
        )
        audit_event("network_fault_restored", ok=bool(result.get("ok")), account_id=account_id)
        if not result.get("ok"):
            raise HTTPException(500, result.get("stderr") or result.get("msg") or "Failed to restore Roblox outbound")
        return result

    @app.get("/api/logs")
    def api_logs(limit: int = 300):
        return {
            "ok": True,
            "path": _log_file(),
            "lines": _tail_log_lines(limit),
        }


    @app.post("/api/logs/clear")
    def api_clear_logs():
        try:
            log_file = _log_file()
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            with open(log_file, "w", encoding="utf-8"):
                pass
        except Exception as e:
            raise HTTPException(500, f"clear log failed: {e}")
        return {"ok": True, "path": _log_file(), "lines": []}


    @app.get("/api/troubleshoot/roblox-install")
    def api_roblox_install_status():
        return roblox_installer.status()


    @app.post("/api/troubleshoot/roblox-install/uninstall")
    def api_roblox_install_uninstall():
        return roblox_installer.start_uninstall()


    @app.post("/api/troubleshoot/roblox-install/latest")
    def api_roblox_install_latest():
        return roblox_installer.start_latest()


    @app.get("/api/ram/status")
    def api_ram_status():
        return {
            "ok": False,
            "msg": "Roblox Account Manager is disabled in RT 1.4",
            "enabled": False,
        }


    @app.post("/api/ram/import")
    def api_ram_import():
        return {"ok": False, "msg": "Roblox Account Manager is disabled in RT 1.4"}

    @app.get("/", response_class=HTMLResponse)
    def serve_ui():
        html_ui = str(ctx.html_ui or "").replace("__ARGUS_API_TOKEN__", str(ctx.instance_token or ""))
        return HTMLResponse(
            html_ui,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )


    @app.post("/api/app/shutdown")
    async def api_app_shutdown(request: Request):
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        token = ""
        if isinstance(body, dict):
            token = str(body.get("token") or "")
        token = token or str(request.headers.get("X-RoboGuard-Token") or "")
        if not token or not secrets.compare_digest(token, ctx.instance_token):
            raise HTTPException(403, "Invalid shutdown token")

        def _shutdown():
            ctx.shutdown_requested.set()
            try:
                if farm.running:
                    farm.stop()
            except Exception as exc:
                flog_kv("MAIN", "shutdown_stop_farm_failed", "warning", error=str(exc))
            try:
                release_multi_roblox_guard()
            except Exception:
                pass
            ctx.clear_instance_state()
            time.sleep(0.3)
            os._exit(0)

        threading.Thread(target=_shutdown, daemon=True, name="RoboGuardShutdown").start()
        return {"ok": True, "msg": "shutdown requested"}
