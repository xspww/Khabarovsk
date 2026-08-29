from __future__ import annotations

import os
import threading
import time

from fastapi import HTTPException, Request
from core import flog_kv
from services import updater

from .auth import require_api_token
from .context import ApiContext
def register(app, ctx: ApiContext) -> None:
    farm = ctx.farm

    @app.get("/api/update/status")
    def api_update_status(request: Request):
        require_api_token(request, ctx)
        return updater.status()

    @app.post("/api/update/check")
    def api_update_check(request: Request):
        require_api_token(request, ctx)
        return updater.check_for_update(force=True)

    @app.post("/api/update/download")
    def api_update_download(request: Request):
        require_api_token(request, ctx)
        if not updater.status().get("update_available"):
            return updater.status()
        return updater.download_update()

    @app.post("/api/update/apply")
    def api_update_apply(request: Request):
        require_api_token(request, ctx)
        result = updater.apply_update()
        if not result.get("download_pending"):
            raise HTTPException(400, result.get("last_error") or "No update is ready to apply")

        def _restart() -> None:
            try:
                updater.apply_update()
                ctx.shutdown_requested.set()
                try:
                    if farm.running:
                        farm.stop()
                except Exception as exc:
                    flog_kv("MAIN", "update_shutdown_stop_farm_failed", "warning", error=str(exc))
                try:
                    from roblox_hybrid import release_multi_roblox_guard

                    release_multi_roblox_guard()
                except Exception:
                    pass
                ctx.clear_instance_state()
                time.sleep(0.4)
                os._exit(0)
            except Exception as exc:
                flog_kv("MAIN", "update_restart_failed", "error", error=str(exc))
                os._exit(1)

        threading.Thread(target=_restart, daemon=True, name="CronusUpdateRestart").start()
        return {"ok": True, "msg": "Update scheduled; Cronus is restarting."}
