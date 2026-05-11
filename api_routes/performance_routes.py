from __future__ import annotations

from fastapi import HTTPException, Request

from account_hybrid import audit_event
from core import flog_kv
from performance_settings import (
    normalize_fps_limit,
    normalize_graphics_quality,
    normalize_process_priority,
)
from services.cpu_limiter import CPU_LIMITER
from services.process_service import ProcessManager

from .context import ApiContext
from .settings_state import (
    _cpu_limiter_settings_from_config,
    _cpu_limiter_status,
    _fps_limiter_status,
    _graphics_status,
    _normalize_window_size_settings,
    _roblox_runtime_restart_required,
    _window_size_status,
)


def register(app, ctx: ApiContext) -> None:
    cfg_mgr = ctx.cfg_mgr
    farm = ctx.farm

    def apply_graphics_settings_file(*args, **kwargs):
        return ctx.get_apply_graphics_settings_file()(*args, **kwargs)

    def apply_performance_settings_file(*args, **kwargs):
        return ctx.get_apply_performance_settings_file()(*args, **kwargs)

    def apply_process_priority_to_roblox(*args, **kwargs):
        return ctx.get_apply_process_priority_to_roblox()(*args, **kwargs)
    @app.get("/api/performance/fps-limiter")
    def api_get_fps_limiter():
        return _fps_limiter_status(ctx)

    @app.post("/api/performance/fps-limiter")
    async def api_set_fps_limiter(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected object")
        enabled = bool(body.get("enabled", False))
        graphics_enabled = bool(body.get(
            "graphics_low_enabled",
            body.get("graphics_auto_enabled", cfg_mgr.get("graphics_low_enabled", cfg_mgr.get("graphics_auto_enabled", False))),
        ))
        auto_priority_enabled = bool(body.get("auto_process_priority_enabled", cfg_mgr.get("auto_process_priority_enabled", False)))
        try:
            fps_limit = normalize_fps_limit(body.get("fps_limit", cfg_mgr.get("fps_limit", 240)))
            graphics_quality = normalize_graphics_quality(body.get("graphics_quality_level", cfg_mgr.get("graphics_quality_level", 1)))
            process_priority = normalize_process_priority(body.get("process_priority", cfg_mgr.get("process_priority", "low")))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        try:
            payload = apply_performance_settings_file(
                enabled,
                fps_limit,
                graphics_enabled,
                graphics_quality_level=graphics_quality,
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            flog_kv("PERFORMANCE", "fps_limiter_apply_failed", "error", error=str(exc))
            raise HTTPException(500, str(exc))
        stored_limit = int(payload.get("fps_limit") or fps_limit)
        priority_result = {"ok": True, "priority": process_priority, "applied": 0, "count": 0, "results": []}
        if auto_priority_enabled:
            priority_result = apply_process_priority_to_roblox(process_priority)
        cfg_mgr.update({
            "fps_limiter_enabled": enabled,
            "fps_limit": stored_limit,
            "graphics_low_enabled": graphics_enabled,
            "graphics_auto_enabled": graphics_enabled,
            "graphics_quality_level": graphics_quality,
            "auto_process_priority_enabled": auto_priority_enabled,
            "process_priority": process_priority,
        })
        cfg_mgr.save()
        runtime_status = _roblox_runtime_restart_required(ctx)
        payload.update(runtime_status)
        payload.update({
            "auto_process_priority_enabled": auto_priority_enabled,
            "process_priority": process_priority,
            "priority_result": priority_result,
        })
        audit_event(
            "performance_apply",
            enabled=enabled,
            fps_limit=fps_limit,
            graphics_low_enabled=graphics_enabled,
            graphics_quality_level=graphics_quality,
            auto_process_priority_enabled=auto_priority_enabled,
            process_priority=process_priority,
            path=payload.get("path", ""),
            read_only=payload.get("read_only", False),
            requires_restart=payload.get("requires_restart", False),
        )
        return payload


    @app.get("/api/performance/graphics")
    def api_get_graphics():
        return _graphics_status(ctx)


    @app.post("/api/performance/graphics")
    async def api_set_graphics(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected object")
        enabled = bool(body.get("graphics_low_enabled", body.get("graphics_auto_enabled", body.get("enabled", False))))
        auto_priority_enabled = bool(body.get("auto_process_priority_enabled", cfg_mgr.get("auto_process_priority_enabled", False)))
        try:
            graphics_quality = normalize_graphics_quality(body.get("graphics_quality_level", cfg_mgr.get("graphics_quality_level", 1)))
            process_priority = normalize_process_priority(body.get("process_priority", cfg_mgr.get("process_priority", "low")))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        try:
            payload = apply_graphics_settings_file(
                enabled,
                readonly_after=bool(cfg_mgr.get("fps_limiter_enabled", False) or enabled),
                quality_level=graphics_quality,
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            flog_kv("PERFORMANCE", "graphics_apply_failed", "error", error=str(exc))
            raise HTTPException(500, str(exc))
        priority_result = {"ok": True, "priority": process_priority, "applied": 0, "count": 0, "results": []}
        if auto_priority_enabled:
            priority_result = apply_process_priority_to_roblox(process_priority)
        cfg_mgr.update({
            "graphics_low_enabled": enabled,
            "graphics_auto_enabled": enabled,
            "graphics_quality_level": graphics_quality,
            "auto_process_priority_enabled": auto_priority_enabled,
            "process_priority": process_priority,
        })
        cfg_mgr.save()
        payload.update(_roblox_runtime_restart_required(ctx))
        payload["graphics_low_enabled"] = enabled
        payload["graphics_auto_enabled"] = enabled
        payload["graphics_quality_level"] = graphics_quality
        payload["auto_process_priority_enabled"] = auto_priority_enabled
        payload["process_priority"] = process_priority
        payload["priority_result"] = priority_result
        audit_event(
            "graphics_apply",
            graphics_low_enabled=enabled,
            graphics_quality_level=graphics_quality,
            auto_process_priority_enabled=auto_priority_enabled,
            process_priority=process_priority,
            path=payload.get("path", ""),
            read_only=payload.get("read_only", False),
            requires_restart=payload.get("requires_restart", False),
        )
        return payload


    @app.get("/api/performance/cpu-limiter")
    def api_get_cpu_limiter():
        return _cpu_limiter_status(ctx)


    @app.post("/api/performance/cpu-limiter")
    async def api_set_cpu_limiter(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected object")
        try:
            settings = _cpu_limiter_settings_from_config(ctx, body)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if settings["apply_all"]:
            settings["accounts"] = {}
        cfg_mgr.update({
            "cpu_limiter_enabled": settings["enabled"],
            "cpu_limiter_mode": settings["mode"],
            "cpu_limiter_default_percent": settings["default_limit_percent"],
            "cpu_limiter_apply_all": settings["apply_all"],
            "cpu_limiter_accounts": settings["accounts"],
        })
        cfg_mgr.save()
        if hasattr(farm, "apply_config_snapshot"):
            farm.apply_config_snapshot()
        result = CPU_LIMITER.apply(getattr(farm, "_accounts", []), settings)
        audit_event(
            "cpu_limiter_apply",
            enabled=settings["enabled"],
            mode=settings["mode"],
            default_limit_percent=settings["default_limit_percent"],
            apply_all=settings["apply_all"],
            applied=result.get("applied", 0),
            fallback=result.get("fallback", 0),
            failed=result.get("failed", 0),
        )
        return result


    @app.get("/api/performance/window-size")
    def api_get_window_size():
        return _window_size_status(ctx)


    @app.post("/api/performance/window-size")
    async def api_set_window_size(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected object")
        try:
            settings = _normalize_window_size_settings(ctx, body)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        resize_result = {"ok": True, "count": 0, "resized": 0, "skipped": 0}
        if settings["enabled"]:
            if settings["arrange_enabled"]:
                resize_result = ProcessManager.arrange_roblox_windows(
                    settings["width"],
                    settings["height"],
                    settings["arrange_columns"],
                    settings["arrange_gap"],
                    settings["arrange_margin"],
                )
            else:
                resize_result = ProcessManager.resize_roblox_windows(settings["width"], settings["height"])
        else:
            resize_result = ProcessManager.restore_roblox_window_styles()
        cfg_mgr.update({
            "roblox_window_resize_enabled": settings["enabled"],
            "roblox_window_size_preset": settings["preset"],
            "roblox_window_width": settings["width"],
            "roblox_window_height": settings["height"],
            "roblox_window_resize_interval_seconds": settings["interval_seconds"],
            "roblox_window_arrange_enabled": settings["arrange_enabled"],
            "roblox_window_arrange_columns": settings["arrange_columns"],
            "roblox_window_arrange_gap": settings["arrange_gap"],
            "roblox_window_arrange_margin": settings["arrange_margin"],
        })
        cfg_mgr.save()
        if hasattr(farm, "apply_config_snapshot"):
            farm.apply_config_snapshot()
        payload = _window_size_status(ctx)
        payload["resize_result"] = resize_result
        payload["msg"] = (
            (
                f"arranged {int(resize_result.get('arranged') or 0)} Roblox window(s)"
                if settings["arrange_enabled"]
                else f"resized {int(resize_result.get('resized') or 0)} Roblox window(s)"
            ) if settings["enabled"] else "window resize disabled; restored window style"
        )
        audit_event(
            "window_size_apply",
            enabled=settings["enabled"],
            preset=settings["preset"],
            width=settings["width"],
            height=settings["height"],
            resized=resize_result.get("resized", 0),
            count=resize_result.get("count", 0),
        )
        return payload
