from __future__ import annotations

import os
import re
import secrets
import subprocess
import threading
import time
from typing import Any, Dict, List

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from account_hybrid import audit_event
from core import flog_kv
from roblox_hybrid import release_multi_roblox_guard

from .auth import require_api_token
from .idempotency import begin_idempotent_request, begin_idempotent_request_sync, finish_idempotent_request
from .settings_state import _int_setting
from .context import ApiContext

_COOKIE_RE = re.compile(r'(_\|WARNING:[^\s\'"<>]+|\.ROBLOSECURITY[^\s\'"<>]*)', re.IGNORECASE)
_KV_SECRET_RE = re.compile(r"(?i)\b(cookie|roblosecurity|password)=([^\s]+)")


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
        idem = await begin_idempotent_request(request, "network_fault_block")
        if idem.replay:
            return idem.response
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
        finish_idempotent_request(idem, result)
        return result


    @app.post("/api/test/network-fault/restore")
    async def api_network_fault_restore(request: Request):
        idem = await begin_idempotent_request(request, "network_fault_restore")
        if idem.replay:
            return idem.response
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
        finish_idempotent_request(idem, result)
        return result

    @app.get("/api/logs")
    def api_logs(request: Request, limit: int = 300):
        require_api_token(request, ctx)
        return {
            "ok": True,
            "path": _log_file(),
            "lines": _tail_log_lines(limit),
        }


    @app.post("/api/logs/clear")
    def api_clear_logs(request: Request):
        idem = begin_idempotent_request_sync(request, "logs_clear")
        if idem.replay:
            return idem.response
        try:
            log_file = _log_file()
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            with open(log_file, "w", encoding="utf-8"):
                pass
        except Exception as e:
            raise HTTPException(500, f"clear log failed: {e}")
        result = {"ok": True, "path": _log_file(), "lines": []}
        finish_idempotent_request(idem, result)
        return result


    @app.get("/api/troubleshoot/roblox-install")
    def api_roblox_install_status():
        return roblox_installer.status()

    @app.get("/api/troubleshoot/executor")
    def api_executor_status():
        return ctx.executor_tracker.status()

    @app.post("/api/troubleshoot/executor/check")
    def api_executor_check():
        return ctx.executor_tracker.refresh()

    @app.get("/api/troubleshoot/executor/relaunch")
    def api_executor_relaunch_status():
        return ctx.executor_relauncher.status()

    @app.post("/api/troubleshoot/executor/relaunch")
    def api_executor_relaunch():
        return {"ok": ctx.executor_relauncher.retry(), **ctx.executor_relauncher.status()}

    @app.post("/api/troubleshoot/executor/browse")
    def api_executor_browse(request: Request):
        if getattr(ctx.farm, "running", False):
            raise HTTPException(409, "Stop Auto Rejoin before changing Executor path")
        # FastAPI executes sync endpoints in a worker thread. tkinter requires
        # its Tcl interpreter to run on the main thread, so use the native
        # Windows common dialog instead.
        selected = ""
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class OpenFileName(ctypes.Structure):
                _fields_ = [
                    ("lStructSize", wintypes.DWORD),
                    ("hwndOwner", wintypes.HWND),
                    ("hInstance", wintypes.HINSTANCE),
                    ("lpstrFilter", wintypes.LPCWSTR),
                    ("lpstrCustomFilter", wintypes.LPWSTR),
                    ("nMaxCustFilter", wintypes.DWORD),
                    ("nFilterIndex", wintypes.DWORD),
                    ("lpstrFile", wintypes.LPWSTR),
                    ("nMaxFile", wintypes.DWORD),
                    ("lpstrFileTitle", wintypes.LPWSTR),
                    ("nMaxFileTitle", wintypes.DWORD),
                    ("lpstrInitialDir", wintypes.LPCWSTR),
                    ("lpstrTitle", wintypes.LPCWSTR),
                    ("Flags", wintypes.DWORD),
                    ("nFileOffset", wintypes.WORD),
                    ("nFileExtension", wintypes.WORD),
                    ("lpstrDefExt", wintypes.LPCWSTR),
                    ("lCustData", wintypes.LPARAM),
                    ("lpfnHook", wintypes.LPVOID),
                    ("lpTemplateName", wintypes.LPCWSTR),
                    ("pvReserved", wintypes.LPVOID),
                    ("dwReserved", wintypes.DWORD),
                    ("FlagsEx", wintypes.DWORD),
                ]

            buffer = ctypes.create_unicode_buffer(32768)
            dialog = OpenFileName()
            dialog.lStructSize = ctypes.sizeof(OpenFileName)
            dialog.lpstrFilter = "Executable (*.exe)\0*.exe\0All files\0*.*\0\0"
            dialog.nFilterIndex = 1
            # OPENFILENAMEW expects an LPWSTR pointer.  Assigning the ctypes
            # array directly raises TypeError on Python 3.14 before the dialog
            # can be shown.
            dialog.lpstrFile = ctypes.cast(buffer, wintypes.LPWSTR)
            dialog.nMaxFile = len(buffer)
            dialog.lpstrTitle = "Select Roblox Executor"
            dialog.Flags = 0x00001000 | 0x00000800 | 0x00000004  # FILEMUSTEXIST | PATHMUSTEXIST | EXPLORER
            if ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(dialog)):
                selected = buffer.value
        else:
            raise HTTPException(500, "Native Executor file picker is only supported on Windows")
        if not selected:
            return {"ok": False, "cancelled": True}
        return {"ok": True, "path": selected}


    @app.get("/api/troubleshoot/roblox-install/versions")
    def api_roblox_install_versions():
        versions = roblox_installer.list_installed()
        exploitstrap_versions = roblox_installer.list_exploitstrap_installed()
        latest_status = roblox_installer._latest_version_status()
        latest_version = str(latest_status.get("version") or "").strip().lower()
        items = []
        for index, item in enumerate(versions):
            version = str(item.get("version") or "").strip()
            is_latest = bool(
                (latest_version and version.lower() == latest_version)
                or (not latest_version and index == 0)
            )
            items.append({
                "version": version,
                "path": str(item.get("path") or ""),
                "root": str(item.get("root") or ""),
                "modified": float(item.get("modified") or 0),
                "is_latest": is_latest,
                "source": str(item.get("source") or "roblox"),
                "launcher": str(item.get("launcher") or "Roblox"),
                "launcher_path": str(item.get("launcher_path") or ""),
                "launcher_version": str(item.get("launcher_version") or ""),
            })
        for item in exploitstrap_versions:
            items.append({
                "version": str(item.get("version") or ""),
                "path": str(item.get("path") or ""),
                "root": str(item.get("root") or ""),
                "modified": float(item.get("modified") or 0),
                "is_latest": False,
                "source": "exploitstrap",
                "launcher": "ExploitStrap",
                "launcher_path": str(item.get("launcher_path") or ""),
                "launcher_version": str(item.get("launcher_version") or ""),
            })
        return {
            "ok": True,
            "latest_version": str(latest_status.get("version") or ""),
            "latest_version_error": str(latest_status.get("error") or ""),
            "versions": items,
        }

    @app.post("/api/troubleshoot/roblox-install/open-location")
    async def api_roblox_install_open_location(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected object")
        requested_version = str(body.get("version") or "").strip().lower()
        requested_source = str(body.get("source") or "roblox").strip().lower()
        if not requested_version or requested_source not in {"roblox", "exploitstrap"}:
            raise HTTPException(400, "Invalid version selection")
        candidates = (
            roblox_installer.list_exploitstrap_installed()
            if requested_source == "exploitstrap"
            else roblox_installer.list_installed()
        )
        match = next(
            (item for item in candidates if str(item.get("version") or "").strip().lower() == requested_version),
            None,
        )
        if not match:
            raise HTTPException(404, "Version not found")
        location = str(match.get("root") or "").strip()
        if not location or not os.path.isdir(location):
            raise HTTPException(404, "Version folder not found")
        try:
            # Passing the folder as a plain Explorer argument is reliable on Windows.
            # The /select form can fall back to This PC when the path is not quoted
            # exactly as Explorer expects.
            subprocess.Popen(["explorer.exe", location], close_fds=True)
        except OSError as exc:
            raise HTTPException(500, f"Could not open location: {exc}")
        return {"ok": True, "location": location, "version": match.get("version"), "source": requested_source}


    @app.post("/api/troubleshoot/roblox-install/uninstall")
    def api_roblox_install_uninstall(request: Request):
        idem = begin_idempotent_request_sync(request, "roblox_install_uninstall")
        if idem.replay:
            return idem.response
        result = roblox_installer.start_uninstall()
        finish_idempotent_request(idem, result)
        return result


    @app.post("/api/troubleshoot/roblox-install/latest")
    def api_roblox_install_latest(request: Request):
        idem = begin_idempotent_request_sync(request, "roblox_install_latest")
        if idem.replay:
            return idem.response
        result = roblox_installer.start_latest()
        finish_idempotent_request(idem, result)
        return result


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
        html_ui = str(ctx.html_ui() or "").replace("__CRONUS_API_TOKEN__", str(ctx.instance_token or ""))
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
        token = token or str(request.headers.get("X-Cronus-Token") or "")
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

        threading.Thread(target=_shutdown, daemon=True, name="CronusShutdown").start()
        return {"ok": True, "msg": "shutdown requested"}
