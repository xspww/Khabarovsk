from __future__ import annotations

import os
import threading
import time

from fastapi import HTTPException, Request

from core import flog_kv
from roblox_hybrid import release_multi_roblox_guard

from .context import ApiContext
from .idempotency import begin_idempotent_request_sync, finish_idempotent_request


def register(app, ctx: ApiContext) -> None:
    farm = ctx.farm
    updater = ctx.app_updater

    @app.get("/api/update/status")
    def api_update_status():
        snap = updater.status_snapshot()
        return {"ok": True, **snap}

    @app.post("/api/update/check")
    def api_update_check(request: Request):
        idem = begin_idempotent_request_sync(request, "update_check")
        if idem.replay:
            return idem.response
        result = updater.check_for_updates(manual=True)
        finish_idempotent_request(idem, result)
        return result

    @app.post("/api/update/download")
    def api_update_download(request: Request):
        idem = begin_idempotent_request_sync(request, "update_download")
        if idem.replay:
            return idem.response
        result = updater.download_release()
        finish_idempotent_request(idem, result)
        return result

    @app.post("/api/update/install")
    def api_update_install(request: Request):
        idem = begin_idempotent_request_sync(request, "update_install")
        if idem.replay:
            return idem.response
        result = updater.prepare_install()
        if not result.get("ok"):
            # Product: install auto-stops the farm itself, so no 409 for
            # farm_running anymore. 409 only when target is not writable and
            # the user must install manually.
            reason = str(result.get("install_blocked_reason") or "")
            status = 409 if reason == "target_not_writable" else 400
            detail = str(result.get("msg") or "Install unavailable")
            manual = str(result.get("manual_url") or result.get("latest_url") or "")
            if manual:
                detail = f"{detail} — {manual}"
            raise HTTPException(status, detail)
        updater_path = str(result.get("updater") or "")
        if not updater_path or not os.path.isfile(updater_path):
            raise HTTPException(500, "Updater script missing")
        # Start the updater first: it waits for this process to exit, so
        # launching early is safe. If it fails to start, abort before
        # touching the running farm.
        if not updater.launch_updater_detached(updater_path):
            raise HTTPException(500, "Could not start updater")

        def _shutdown_for_update():
            ctx.shutdown_requested.set()
            try:
                if getattr(farm, "running", False):
                    farm.stop()
            except Exception as exc:
                flog_kv("UPDATE", "install_stop_farm_failed", "warning", error=str(exc))
            try:
                release_multi_roblox_guard()
            except Exception:
                pass
            try:
                ctx.clear_instance_state()
            except Exception:
                pass
            time.sleep(0.5)
            os._exit(0)

        threading.Thread(target=_shutdown_for_update, daemon=True, name="CronusUpdateInstall").start()
        finish_idempotent_request(idem, result)
        return result
