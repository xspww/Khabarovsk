from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from core import flog_kv
from services.account_hybrid import audit_event
from services.cpu_limiter import CPU_LIMITER
from services.performance_settings import (
    apply_volume_settings_file,
    delete_roblox_settings_file,
    is_readonly,
    normalize_fps_limit,
    normalize_graphics_quality,
    normalize_process_priority,
    normalize_roblox_volume,
    roblox_settings_status,
    set_readonly,
)
from services.process_service import ProcessService

from .context import ApiContext
from .settings_state import (
    _cpu_limiter_settings_from_config,
    _cpu_limiter_status,
    _fps_limiter_status,
    _graphics_status,
    _normalize_window_size_settings,
    _process_trim_status,
    _ram_cleanup_status,
    _roblox_runtime_restart_required,
    _virtual_memory_status,
    _window_size_status,
    persist_config,
)

_VOLUME_KEYS = {"roblox_volume_muted", "roblox_volume_level", "volume_muted", "volume_level"}


def _apply_graphics_file(ctx: ApiContext, *args, **kwargs):
    return ctx.get_apply_graphics_settings_file()(*args, **kwargs)


def _apply_performance_file(ctx: ApiContext, *args, **kwargs):
    return ctx.get_apply_performance_settings_file()(*args, **kwargs)


def _apply_priority(ctx: ApiContext, *args, **kwargs):
    return ctx.get_apply_process_priority_to_roblox()(*args, **kwargs)


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    if not isinstance(body, dict):
        return {}
    return body


def _read_volume(body: dict[str, Any], cfg_mgr) -> tuple[bool, int]:
    volume_muted = bool(
        body.get(
            "roblox_volume_muted",
            body.get("volume_muted", cfg_mgr.get("roblox_volume_muted", False)),
        )
    )
    volume_level = normalize_roblox_volume(
        body.get(
            "roblox_volume_level",
            body.get("volume_level", cfg_mgr.get("roblox_volume_level", 10)),
        )
    )
    return volume_muted, volume_level


def _read_graphics_payload(
    body: dict[str, Any], cfg_mgr, volume_touched: bool
) -> tuple[int, str, bool | None, int | None]:
    graphics_quality = normalize_graphics_quality(
        body.get("graphics_quality_level", cfg_mgr.get("graphics_quality_level", 1))
    )
    process_priority = normalize_process_priority(
        body.get("process_priority", cfg_mgr.get("process_priority", "low"))
    )
    if not volume_touched:
        return graphics_quality, process_priority, None, None
    volume_muted = bool(
        body.get(
            "roblox_volume_muted",
            body.get("volume_muted", cfg_mgr.get("roblox_volume_muted", False)),
        )
    )
    volume_level = normalize_roblox_volume(
        body.get(
            "roblox_volume_level",
            body.get("volume_level", cfg_mgr.get("roblox_volume_level", 10)),
        )
    )
    return graphics_quality, process_priority, volume_muted, volume_level


def _enforce_graphics_readonly(
    ctx: ApiContext,
    payload: dict[str, Any],
    enabled: bool,
    volume_muted: bool,
    volume_level: int,
) -> None:
    try:
        want_ro = bool(
            ctx.cfg_mgr.get("fps_limiter_enabled", False)
            or enabled
            or volume_muted
            or int(volume_level) < 10
        )
        path = str(payload.get("path") or "")
        set_readonly(path, want_ro)
        payload["read_only"] = is_readonly(path)
    except Exception:
        "intentional: ignore exception"


def _apply_graphics_payload(
    ctx: ApiContext,
    enabled: bool,
    graphics_quality: int,
    volume_touched: bool,
    volume_muted: bool | None,
    volume_level: int | None,
) -> dict[str, Any]:
    try:
        payload = _apply_graphics_file(
            ctx,
            enabled,
            readonly_after=None,
            quality_level=graphics_quality,
            **(
                {"volume_muted": volume_muted, "volume_level": volume_level}
                if volume_touched
                else {}
            ),
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        flog_kv("PERFORMANCE", "graphics_apply_failed", "error", error=str(exc))
        raise HTTPException(500, str(exc))
    # Enforce readonly when FPS is on, graphics is on, or volume is
    # non-default — otherwise Roblox reverts the file on exit.
    if volume_touched and volume_muted is not None and volume_level is not None:
        _enforce_graphics_readonly(ctx, payload, enabled, volume_muted, volume_level)
    return payload


def _persist_graphics_config(
    ctx: ApiContext,
    enabled: bool,
    graphics_quality: int,
    auto_priority_enabled: bool,
    process_priority: str,
    volume_touched: bool,
    volume_muted: bool | None,
    volume_level: int | None,
) -> None:
    cfg_update = {
        "graphics_low_enabled": enabled,
        "graphics_auto_enabled": enabled,
        "graphics_quality_level": graphics_quality,
        "auto_process_priority_enabled": auto_priority_enabled,
        "process_priority": process_priority,
    }
    if volume_touched:
        cfg_update["roblox_volume_muted"] = bool(volume_muted)
        cfg_update["roblox_volume_level"] = int(volume_level)
    persist_config(ctx, cfg_update)


def _update_graphics_payload(
    ctx: ApiContext,
    payload: dict[str, Any],
    enabled: bool,
    graphics_quality: int,
    auto_priority_enabled: bool,
    process_priority: str,
    priority_result: dict[str, Any],
    volume_touched: bool,
    volume_muted: bool | None,
    volume_level: int | None,
) -> None:
    payload.update(_roblox_runtime_restart_required(ctx))
    payload["graphics_low_enabled"] = enabled
    payload["graphics_auto_enabled"] = enabled
    payload["graphics_quality_level"] = graphics_quality
    payload["auto_process_priority_enabled"] = auto_priority_enabled
    payload["process_priority"] = process_priority
    payload["priority_result"] = priority_result
    if volume_touched:
        payload["roblox_volume_muted"] = bool(volume_muted)
        payload["roblox_volume_level"] = int(volume_level)


def _apply_volume_file(volume_muted: bool, volume_level: int) -> dict[str, Any]:
    try:
        return apply_volume_settings_file(volume_muted, volume_level)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        flog_kv("PERFORMANCE", "volume_apply_failed", "error", error=str(exc))
        raise HTTPException(500, str(exc))


def _apply_window_size(settings: dict, reason: str) -> dict[str, Any]:
    # Manual action: restore minimized windows first so arrange/resize
    # applies immediately instead of reporting 0. Auto cycles never call
    # this (they skip iconic to avoid flicker).
    try:
        ProcessService.unminimize_roblox_windows(reason=reason)
    except Exception:
        "intentional: ignore exception"
    if settings["arrange_enabled"]:
        return ProcessService.arrange_roblox_windows(
            settings["width"],
            settings["height"],
            settings["arrange_columns"],
            settings["arrange_gap"],
            settings["arrange_margin"],
            unlock_size=settings["unlock_size_enabled"],
            resize=settings["enabled"],
            rows=settings["arrange_rows"],
            reason=reason,
        )
    return ProcessService.resize_roblox_windows(
        settings["width"],
        settings["height"],
        unlock_size=settings["unlock_size_enabled"],
        reason=reason,
    )


def _show_hidden_before_apply(settings: dict) -> dict[str, Any]:
    if settings["hide_enabled"]:
        return {}
    # Turning hide off (or keeping it off): restore hidden windows
    # first so resize/arrange below applies to every client.
    try:
        return ProcessService.show_roblox_windows(
            reason="api_window_size_apply_show"
        )
    except Exception:
        return {}


def _resize_or_restore_windows(settings: dict) -> dict[str, Any]:
    if settings["enabled"] or settings["arrange_enabled"]:
        return _apply_window_size(settings, "api_window_size_apply")
    return ProcessService.restore_roblox_window_styles(
        reason="api_window_size_apply"
    )


def _hide_windows_after_apply(settings: dict) -> dict[str, Any]:
    if not settings["hide_enabled"]:
        return {}
    # Turning hide on: arrange first, then take windows off
    # screen + taskbar (they stay listed as Roblox in Task Manager).
    try:
        return ProcessService.hide_roblox_windows(
            reason="api_window_size_apply_hide"
        )
    except Exception:
        return {}


def _persist_window_settings(ctx: ApiContext, settings: dict) -> None:
    persist_config(
        ctx,
        {
            "roblox_window_unlock_size_enabled": settings["unlock_size_enabled"],
            "roblox_window_resize_enabled": settings["enabled"],
            "roblox_window_size_preset": settings["preset"],
            "roblox_window_width": settings["width"],
            "roblox_window_height": settings["height"],
            "roblox_window_resize_interval_seconds": settings["interval_seconds"],
            "roblox_window_arrange_enabled": settings["arrange_enabled"],
            "roblox_window_arrange_columns": settings["arrange_columns"],
            "roblox_window_arrange_rows": settings["arrange_rows"],
            "roblox_window_arrange_gap": settings["arrange_gap"],
            "roblox_window_arrange_margin": settings["arrange_margin"],
            "roblox_window_hide_enabled": settings["hide_enabled"],
            "auto_minimize_enabled": settings.get("auto_minimize_enabled", False),
            "auto_minimize_seconds": settings.get("auto_minimize_seconds", 10),
        },
        apply_snapshot=True,
        strict_snapshot=True,
    )
    try:
        flog_kv("CONFIG", "updated", updated=["window_size", "auto_minimize"])
    except Exception:
        "intentional: ignore exception"


def _window_size_message(settings: dict, resize_result: dict) -> str:
    if settings["arrange_enabled"]:
        return f"arranged {int(resize_result.get('arranged') or 0)} Roblox window(s)"
    if settings["enabled"]:
        return f"resized {int(resize_result.get('resized') or 0)} Roblox window(s)"
    return "window automation disabled; restored window style"


def _audit_window_size(settings: dict, resize_result: dict) -> None:
    audit_event(
        "window_size_apply",
        enabled=settings["enabled"],
        arrange_enabled=settings["arrange_enabled"],
        unlock_size_enabled=settings["unlock_size_enabled"],
        arrange_rows=settings["arrange_rows"],
        preset=settings["preset"],
        width=settings["width"],
        height=settings["height"],
        resized=resize_result.get("resized", 0),
        count=resize_result.get("count", 0),
    )


def _percent_text(raw: Any) -> str:
    try:
        if raw is not None and str(raw).strip() != "":
            return f"{float(raw):.1f}"
    except Exception:
        return ""
    return ""


def _account_pid_for_username(farm, username: str) -> int:
    for acc in getattr(farm, "_accounts", []):
        key = str(
            getattr(acc, "_config_username", "") or getattr(acc, "username", "")
        )
        if key == username or str(getattr(acc, "display_name", "")) == username:
            try:
                return int(getattr(acc, "pid", 0) or 0)
            except Exception:
                return 0
    return 0


def _process_trim_pid(body: dict[str, Any], farm) -> int:
    try:
        pid = int(float(body.get("pid", 0) or 0))
    except Exception:
        pid = 0
    username = str(body.get("username", body.get("account", "")) or "").strip()
    if pid or not username or not hasattr(farm, "_accounts"):
        return pid
    return _account_pid_for_username(farm, username)


def _trim_all_processes(ctx: ApiContext) -> dict[str, Any]:
    from services.process_trim import PROCESS_TRIM

    farm = ctx.farm
    try:
        result = PROCESS_TRIM.apply(
            getattr(farm, "_accounts", []),
            ctx.cfg_mgr.snapshot(),
            bypass_throttle=True,
        )
    except Exception as exc:
        flog_kv(
            "PERFORMANCE", "process_trim_failed", "error", error=str(exc), source="manual"
        )
        raise HTTPException(500, str(exc))
    flog_kv(
        "PERFORMANCE", "process_trim_manual", trimmed=result.get("trimmed", 0), source="manual"
    )
    audit_event("process_trim_manual", ok=True, trimmed=result.get("trimmed", 0))
    payload = _process_trim_status(ctx)
    payload.update(result)
    return payload


def _trim_single_process(ctx: ApiContext, pid: int) -> dict[str, Any]:
    from services.process_trim import PROCESS_TRIM

    try:
        result = PROCESS_TRIM.trim_now(pid)
    except Exception as exc:
        flog_kv(
            "PERFORMANCE", "process_trim_failed", "error", error=str(exc), source="manual"
        )
        raise HTTPException(500, str(exc))
    if not result.get("ok"):
        audit_event("process_trim_manual", ok=False, msg=result.get("reason", ""))
        return result
    flog_kv(
        "PERFORMANCE",
        "process_trim_manual",
        freed_mb=result.get("freed_mb", 0.0),
        pid=pid,
        source="manual",
    )
    audit_event(
        "process_trim_manual", ok=True, freed_mb=result.get("freed_mb", 0.0), pid=pid
    )
    payload = _process_trim_status(ctx)
    payload.update(result)
    return payload


def _api_get_fps_limiter(ctx: ApiContext) -> dict[str, Any]:
    return _fps_limiter_status(ctx)


async def _api_set_fps_limiter(ctx: ApiContext, request: Request) -> dict[str, Any]:
    cfg_mgr = ctx.cfg_mgr
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected object")
    enabled = bool(body.get("enabled", False))
    graphics_enabled = bool(
        body.get(
            "graphics_low_enabled",
            body.get(
                "graphics_auto_enabled",
                cfg_mgr.get(
                    "graphics_low_enabled",
                    cfg_mgr.get("graphics_auto_enabled", False),
                ),
            ),
        )
    )
    auto_priority_enabled = bool(
        body.get(
            "auto_process_priority_enabled",
            cfg_mgr.get("auto_process_priority_enabled", False),
        )
    )
    volume_touched = any(k in body for k in _VOLUME_KEYS)
    try:
        fps_limit = normalize_fps_limit(
            body.get("fps_limit", cfg_mgr.get("fps_limit", 240))
        )
        graphics_quality = normalize_graphics_quality(
            body.get(
                "graphics_quality_level", cfg_mgr.get("graphics_quality_level", 1)
            )
        )
        process_priority = normalize_process_priority(
            body.get("process_priority", cfg_mgr.get("process_priority", "low"))
        )
        volume_muted = None
        volume_level = None
        if volume_touched:
            volume_muted = bool(
                body.get(
                    "roblox_volume_muted",
                    body.get(
                        "volume_muted",
                        cfg_mgr.get("roblox_volume_muted", False),
                    ),
                )
            )
            volume_level = normalize_roblox_volume(
                body.get(
                    "roblox_volume_level",
                    body.get(
                        "volume_level", cfg_mgr.get("roblox_volume_level", 10)
                    ),
                )
            )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    try:
        payload = _apply_performance_file(
            ctx,
            enabled,
            fps_limit,
            graphics_enabled,
            graphics_quality_level=graphics_quality,
            **(
                {"volume_muted": volume_muted, "volume_level": volume_level}
                if volume_touched
                else {}
            ),
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        flog_kv("PERFORMANCE", "fps_limiter_apply_failed", "error", error=str(exc))
        raise HTTPException(500, str(exc))
    stored_limit = int(payload.get("fps_limit") or fps_limit)
    priority_result = {
        "ok": True,
        "priority": process_priority,
        "applied": 0,
        "count": 0,
        "results": [],
    }
    if auto_priority_enabled:
        priority_result = _apply_priority(ctx, process_priority)
    cfg_update = {
        "fps_limiter_enabled": enabled,
        "fps_limit": stored_limit,
        "graphics_low_enabled": graphics_enabled,
        "graphics_auto_enabled": graphics_enabled,
        "graphics_quality_level": graphics_quality,
        "auto_process_priority_enabled": auto_priority_enabled,
        "process_priority": process_priority,
    }
    if volume_touched:
        cfg_update["roblox_volume_muted"] = bool(volume_muted)
        cfg_update["roblox_volume_level"] = int(volume_level)
        payload["roblox_volume_muted"] = bool(volume_muted)
        payload["roblox_volume_level"] = int(volume_level)
    persist_config(ctx, cfg_update)
    runtime_status = _roblox_runtime_restart_required(ctx)
    payload.update(runtime_status)
    payload.update(
        {
            "auto_process_priority_enabled": auto_priority_enabled,
            "process_priority": process_priority,
            "priority_result": priority_result,
        }
    )
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


def _api_get_graphics(ctx: ApiContext) -> dict[str, Any]:
    return _graphics_status(ctx)


async def _api_set_graphics(ctx: ApiContext, request: Request) -> dict[str, Any]:
    cfg_mgr = ctx.cfg_mgr
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected object")
    enabled = bool(
        body.get(
            "graphics_low_enabled",
            body.get("graphics_auto_enabled", body.get("enabled", False)),
        )
    )
    auto_priority_enabled = bool(
        body.get(
            "auto_process_priority_enabled",
            cfg_mgr.get("auto_process_priority_enabled", False),
        )
    )
    volume_touched = any(k in body for k in _VOLUME_KEYS)
    try:
        graphics_quality, process_priority, volume_muted, volume_level = (
            _read_graphics_payload(body, cfg_mgr, volume_touched)
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    payload = _apply_graphics_payload(
        ctx,
        enabled,
        graphics_quality,
        volume_touched,
        volume_muted,
        volume_level,
    )
    priority_result = {
        "ok": True,
        "priority": process_priority,
        "applied": 0,
        "count": 0,
        "results": [],
    }
    if auto_priority_enabled:
        priority_result = _apply_priority(ctx, process_priority)
    _persist_graphics_config(
        ctx,
        enabled,
        graphics_quality,
        auto_priority_enabled,
        process_priority,
        volume_touched,
        volume_muted,
        volume_level,
    )
    _update_graphics_payload(
        ctx,
        payload,
        enabled,
        graphics_quality,
        auto_priority_enabled,
        process_priority,
        priority_result,
        volume_touched,
        volume_muted,
        volume_level,
    )
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


def _api_get_volume(ctx: ApiContext) -> dict[str, Any]:
    return _graphics_status(ctx)


async def _api_set_volume(ctx: ApiContext, request: Request) -> dict[str, Any]:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected object")
    try:
        volume_muted, volume_level = _read_volume(body, ctx.cfg_mgr)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    payload = _apply_volume_file(volume_muted, volume_level)
    persist_config(
        ctx,
        {
            "roblox_volume_muted": volume_muted,
            "roblox_volume_level": int(volume_level),
        },
    )
    payload.update(_roblox_runtime_restart_required(ctx))
    audit_event(
        "volume_apply",
        roblox_volume_muted=volume_muted,
        roblox_volume_level=int(volume_level),
        path=payload.get("path", ""),
        read_only=payload.get("read_only", False),
    )
    return payload


def _api_get_roblox_settings() -> dict[str, Any]:
    try:
        return roblox_settings_status()
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        flog_kv("PERFORMANCE", "roblox_settings_status_failed", "error", error=str(exc))
        raise HTTPException(500, str(exc))


async def _api_delete_roblox_settings(ctx: ApiContext, request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    # Optional explicit confirmation from the Card UI.
    # Deleting without confirm is still allowed for API callers.
    try:
        payload = delete_roblox_settings_file()
    except PermissionError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        flog_kv(
            "PERFORMANCE", "roblox_settings_delete_failed", "error", error=str(exc)
        )
        raise HTTPException(500, str(exc))
    audit_event(
        "roblox_settings_delete",
        deleted=payload.get("deleted", False),
        path=payload.get("path", ""),
    )
    return payload


def _api_get_cpu_limiter(ctx: ApiContext) -> dict[str, Any]:
    return _cpu_limiter_status(ctx)


async def _api_set_cpu_limiter(ctx: ApiContext, request: Request) -> dict[str, Any]:
    farm = ctx.farm
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected object")
    try:
        settings = _cpu_limiter_settings_from_config(ctx, body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if settings["apply_all"]:
        settings["accounts"] = {}
    persist_config(
        ctx,
        {
            "cpu_limiter_enabled": settings["enabled"],
            "cpu_limiter_mode": settings["mode"],
            "cpu_limiter_default_percent": settings["default_limit_percent"],
            "cpu_limiter_apply_all": settings["apply_all"],
            "cpu_limiter_accounts": settings["accounts"],
        },
        apply_snapshot=True,
        strict_snapshot=True,
    )
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


def _api_get_window_size(ctx: ApiContext) -> dict[str, Any]:
    return _window_size_status(ctx)


async def _api_set_window_size(ctx: ApiContext, request: Request) -> dict[str, Any]:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected object")
    try:
        settings = _normalize_window_size_settings(ctx, body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    show_result = _show_hidden_before_apply(settings)
    resize_result = _resize_or_restore_windows(settings)
    hide_result = _hide_windows_after_apply(settings)
    _persist_window_settings(ctx, settings)
    payload = _window_size_status(ctx)
    payload["resize_result"] = resize_result
    payload["show_result"] = show_result
    payload["hide_result"] = hide_result
    payload["msg"] = _window_size_message(settings, resize_result)
    _audit_window_size(settings, resize_result)
    return payload


async def _api_rearrange_window_size(
    ctx: ApiContext, request: Request
) -> dict[str, Any]:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected object")
    try:
        settings = _normalize_window_size_settings(ctx, body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    one_shot_settings = dict(settings)
    one_shot_settings["enabled"] = True
    one_shot_settings["arrange_enabled"] = True
    resize_result = _apply_window_size(
        one_shot_settings, "api_window_rearrange_existing"
    )
    payload = _window_size_status(ctx)
    payload.update(settings)
    payload.update(
        {
            "resize_result": resize_result,
            "msg": f"arranged {int(resize_result.get('arranged') or 0)} Roblox window(s)",
        }
    )
    audit_event(
        "window_rearrange_existing",
        arrange_enabled=settings["arrange_enabled"],
        unlock_size_enabled=settings["unlock_size_enabled"],
        arrange_rows=settings["arrange_rows"],
        width=settings["width"],
        height=settings["height"],
        arranged=resize_result.get("arranged", 0),
        resized=resize_result.get("resized", 0),
        count=resize_result.get("count", 0),
    )
    return payload


async def _api_hide_roblox_windows(
    ctx: ApiContext, request: Request
) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    persist_config(ctx, {"roblox_window_hide_enabled": True}, apply_snapshot=True)
    result = ProcessService.hide_roblox_windows(reason="api_window_hide")
    try:
        hidden = int(result.get("hidden") or 0)
        count = int(result.get("count") or 0)
    except Exception:
        hidden, count = 0, 0
    audit_event("window_hide", hidden=hidden, count=count)
    return {
        "ok": True,
        "hidden": hidden,
        "count": count,
        "hide_enabled": True,
        "msg": f"hidden {hidden} Roblox window(s)",
    }


async def _api_show_roblox_windows(
    ctx: ApiContext, request: Request
) -> dict[str, Any]:
    body = await _json_object(request)
    persist_config(ctx, {"roblox_window_hide_enabled": False}, apply_snapshot=True)
    try:
        settings = _normalize_window_size_settings(ctx, body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    show_result = ProcessService.show_roblox_windows(reason="api_window_show")
    try:
        shown = int(show_result.get("shown") or 0)
    except Exception:
        shown = 0
    arrange_result: dict = {}
    if settings["enabled"] or settings["arrange_enabled"]:
        arrange_result = _apply_window_size(settings, "api_window_show_arrange")
    audit_event("window_show", shown=shown, arranged=arrange_result.get("arranged", 0))
    payload = _window_size_status(ctx)
    payload.update(
        {
            "shown": shown,
            "arrange_result": arrange_result,
            "msg": f"shown {shown} Roblox window(s)",
        }
    )
    return payload


def _api_get_ram_cleanup(ctx: ApiContext) -> dict[str, Any]:
    return _ram_cleanup_status(ctx)


async def _api_set_ram_cleanup(ctx: ApiContext, request: Request) -> dict[str, Any]:
    from services.ram_cleanup import normalize_ram_cleanup_settings

    cfg_mgr = ctx.cfg_mgr
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected object")
    settings = normalize_ram_cleanup_settings(
        {
            "ram_cleanup_enabled": body.get(
                "enabled",
                body.get(
                    "ram_cleanup_enabled",
                    cfg_mgr.get("ram_cleanup_enabled", False),
                ),
            ),
            "ram_cleanup_threshold_pct": body.get(
                "threshold_pct",
                body.get(
                    "ram_cleanup_threshold_pct",
                    cfg_mgr.get("ram_cleanup_threshold_pct", 85.0),
                ),
            ),
            "ram_cleanup_interval_min": body.get(
                "interval_min",
                body.get(
                    "ram_cleanup_interval_min",
                    cfg_mgr.get("ram_cleanup_interval_min", 15),
                ),
            ),
        }
    )
    persist_config(
        ctx,
        {
            "ram_cleanup_enabled": settings["enabled"],
            "ram_cleanup_threshold_pct": settings["threshold_pct"],
            "ram_cleanup_interval_min": settings["interval_min"],
        },
        apply_snapshot=True,
    )
    payload = _ram_cleanup_status(ctx)
    audit_event(
        "ram_cleanup_apply",
        enabled=settings["enabled"],
        threshold_pct=settings["threshold_pct"],
        interval_min=settings["interval_min"],
    )
    return payload


async def _api_ram_cleanup_now(ctx: ApiContext, request: Request) -> dict[str, Any]:
    from services.ram_cleanup import RAM_CLEANUP

    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_source = str((body or {}).get("source") or "manual").strip().lower()
    source = "auto" if raw_source == "auto" else "manual"
    try:
        result = RAM_CLEANUP.clean(source=source)
    except Exception as exc:
        flog_kv(
            "PERFORMANCE", "ram_cleanup_failed", "error", error=str(exc), source=source
        )
        raise HTTPException(500, str(exc))
    if not result.get("ok"):
        flog_kv(
            "PERFORMANCE",
            "ram_cleanup_manual_failed",
            "warning",
            error=str(result.get("msg", ""))[:200],
            source=source,
        )
        audit_event("ram_cleanup_manual", ok=False, msg=result.get("msg", ""))
        return result
    before = result.get("before")
    if not isinstance(before, dict):
        before = {}
    pct_text = _percent_text(before.get("percent"))
    flog_kv(
        "PERFORMANCE",
        "ram_cleanup_manual",
        freed_mb=result.get("freed_mb", 0.0),
        source=source,
        percent=pct_text,
    )
    audit_event(
        "ram_cleanup_manual", ok=True, freed_mb=result.get("freed_mb", 0.0), source=source
    )
    payload = _ram_cleanup_status(ctx)
    payload.update(result)  # live result wins over cached status
    return payload


def _api_get_process_trim(ctx: ApiContext) -> dict[str, Any]:
    return _process_trim_status(ctx)


async def _api_set_process_trim(ctx: ApiContext, request: Request) -> dict[str, Any]:
    from services.process_trim import normalize_process_trim_settings

    cfg_mgr = ctx.cfg_mgr
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected object")
    settings = normalize_process_trim_settings(
        {
            "process_trim_enabled": body.get(
                "enabled",
                body.get(
                    "process_trim_enabled",
                    cfg_mgr.get("process_trim_enabled", False),
                ),
            ),
            "process_trim_threshold_mb": body.get(
                "threshold_mb",
                body.get(
                    "process_trim_threshold_mb",
                    cfg_mgr.get("process_trim_threshold_mb", 1200),
                ),
            ),
            "process_trim_cooldown_sec": body.get(
                "cooldown_sec",
                body.get(
                    "process_trim_cooldown_sec",
                    cfg_mgr.get("process_trim_cooldown_sec", 300),
                ),
            ),
            "process_trim_max_per_cycle": body.get(
                "max_per_cycle",
                body.get(
                    "process_trim_max_per_cycle",
                    cfg_mgr.get("process_trim_max_per_cycle", 1),
                ),
            ),
        }
    )
    persist_config(
        ctx,
        {
            "process_trim_enabled": settings["enabled"],
            "process_trim_threshold_mb": settings["threshold_mb"],
            "process_trim_cooldown_sec": settings["cooldown_sec"],
            "process_trim_max_per_cycle": settings["max_per_cycle"],
        },
        apply_snapshot=True,
    )
    payload = _process_trim_status(ctx)
    audit_event(
        "process_trim_apply",
        enabled=settings["enabled"],
        threshold_mb=settings["threshold_mb"],
        cooldown_sec=settings["cooldown_sec"],
        max_per_cycle=settings["max_per_cycle"],
    )
    return payload


async def _api_process_trim_now(ctx: ApiContext, request: Request) -> dict[str, Any]:
    body = await _json_object(request)
    pid = _process_trim_pid(body, ctx.farm)
    # ไม่ระบุ pid/username = trim อัตโนมัติ 1 รอบ (เฉพาะจอที่เข้าเกณฑ์)
    # manual: ข้าม throttle 30s (แต่ยังเคารพ cooldown ราย PID + gap หลัง global clean)
    if not pid:
        return _trim_all_processes(ctx)
    return _trim_single_process(ctx, pid)


def _api_get_virtual_memory(ctx: ApiContext) -> dict[str, Any]:
    return _virtual_memory_status(ctx)


async def _api_set_virtual_memory(
    ctx: ApiContext, request: Request
) -> dict[str, Any]:
    from services import virtual_memory as _vm

    cfg_mgr = ctx.cfg_mgr
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected object")
    try:
        settings = _vm.normalize_virtual_memory_settings(
            {
                "virtual_memory_mode": body.get(
                    "mode",
                    body.get(
                        "virtual_memory_mode",
                        cfg_mgr.get("virtual_memory_mode", "system_managed"),
                    ),
                ),
                "virtual_memory_size_gb": body.get(
                    "size_gb",
                    body.get(
                        "virtual_memory_size_gb",
                        cfg_mgr.get("virtual_memory_size_gb", 16),
                    ),
                ),
            }
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    persist_config(
        ctx,
        {
            "virtual_memory_mode": settings["mode"],
            "virtual_memory_size_gb": settings["size_gb"],
        },
    )
    payload = _virtual_memory_status(ctx)
    payload["staged"] = settings
    payload["msg"] = "Saved — press Apply Virtual Memory to change Windows (requires reboot)"
    audit_event(
        "virtual_memory_stage", mode=settings["mode"], size_gb=settings["size_gb"]
    )
    return payload


async def _api_apply_virtual_memory(
    ctx: ApiContext, request: Request
) -> dict[str, Any]:
    from services import virtual_memory as _vm

    cfg_mgr = ctx.cfg_mgr
    body = await _json_object(request)
    mode = str(
        body.get(
            "mode",
            body.get(
                "virtual_memory_mode",
                cfg_mgr.get("virtual_memory_mode", "system_managed"),
            ),
        )
        or "system_managed"
    )
    try:
        size_gb = int(
            float(
                body.get(
                    "size_gb",
                    body.get(
                        "virtual_memory_size_gb",
                        cfg_mgr.get("virtual_memory_size_gb", 16),
                    ),
                )
                or 16
            )
        )
    except Exception:
        size_gb = 16
    try:
        result = _vm.apply(mode, size_gb)
    except Exception as exc:
        flog_kv("PERFORMANCE", "virtual_memory_apply_failed", "error", error=str(exc))
        raise HTTPException(500, str(exc))
    if result.get("ok"):
        persist_config(
            ctx,
            {
                "virtual_memory_mode": result.get("mode", "system_managed"),
                "virtual_memory_size_gb": result.get("size_gb")
                or max(1, min(64, size_gb)),
            },
        )
        audit_event(
            "virtual_memory_apply",
            mode=result.get("mode"),
            size_gb=result.get("size_gb"),
        )
    payload = _virtual_memory_status(ctx)
    payload.update(result)
    return payload


def _register_fps_graphics(app, ctx: ApiContext) -> None:
    @app.get("/api/performance/fps-limiter")
    def api_get_fps_limiter():
        return _api_get_fps_limiter(ctx)

    @app.post("/api/performance/fps-limiter")
    async def api_set_fps_limiter(request: Request):
        return await _api_set_fps_limiter(ctx, request)

    @app.get("/api/performance/graphics")
    def api_get_graphics():
        return _api_get_graphics(ctx)

    @app.post("/api/performance/graphics")
    async def api_set_graphics(request: Request):
        return await _api_set_graphics(ctx, request)

    @app.get("/api/performance/volume")
    def api_get_volume():
        return _api_get_volume(ctx)

    @app.post("/api/performance/volume")
    async def api_set_volume(request: Request):
        return await _api_set_volume(ctx, request)


def _register_settings(app, ctx: ApiContext) -> None:
    @app.get("/api/performance/roblox-settings")
    def api_get_roblox_settings():
        return _api_get_roblox_settings()

    @app.post("/api/performance/roblox-settings/delete")
    async def api_delete_roblox_settings(request: Request):
        return await _api_delete_roblox_settings(ctx, request)

    @app.get("/api/performance/cpu-limiter")
    def api_get_cpu_limiter():
        return _api_get_cpu_limiter(ctx)

    @app.post("/api/performance/cpu-limiter")
    async def api_set_cpu_limiter(request: Request):
        return await _api_set_cpu_limiter(ctx, request)


def _register_windows(app, ctx: ApiContext) -> None:
    @app.get("/api/performance/window-size")
    def api_get_window_size():
        return _api_get_window_size(ctx)

    @app.post("/api/performance/window-size")
    async def api_set_window_size(request: Request):
        return await _api_set_window_size(ctx, request)

    @app.post("/api/performance/window-size/rearrange")
    async def api_rearrange_window_size(request: Request):
        return await _api_rearrange_window_size(ctx, request)

    @app.post("/api/performance/window-hide")
    async def api_hide_roblox_windows(request: Request):
        return await _api_hide_roblox_windows(ctx, request)

    @app.post("/api/performance/window-show")
    async def api_show_roblox_windows(request: Request):
        return await _api_show_roblox_windows(ctx, request)


def _register_cleanup(app, ctx: ApiContext) -> None:
    @app.get("/api/performance/ram-cleanup")
    def api_get_ram_cleanup():
        return _api_get_ram_cleanup(ctx)

    @app.post("/api/performance/ram-cleanup")
    async def api_set_ram_cleanup(request: Request):
        return await _api_set_ram_cleanup(ctx, request)

    @app.post("/api/performance/ram-cleanup/clean-now")
    async def api_ram_cleanup_now(request: Request):
        return await _api_ram_cleanup_now(ctx, request)

    @app.get("/api/performance/process-trim")
    def api_get_process_trim():
        return _api_get_process_trim(ctx)

    @app.post("/api/performance/process-trim")
    async def api_set_process_trim(request: Request):
        return await _api_set_process_trim(ctx, request)

    @app.post("/api/performance/process-trim/trim-now")
    async def api_process_trim_now(request: Request):
        return await _api_process_trim_now(ctx, request)


def _register_virtual_memory(app, ctx: ApiContext) -> None:
    @app.get("/api/performance/virtual-memory")
    def api_get_virtual_memory():
        return _api_get_virtual_memory(ctx)

    @app.post("/api/performance/virtual-memory")
    async def api_set_virtual_memory(request: Request):
        return await _api_set_virtual_memory(ctx, request)

    @app.post("/api/performance/virtual-memory/apply")
    async def api_apply_virtual_memory(request: Request):
        return await _api_apply_virtual_memory(ctx, request)


def register(app, ctx: ApiContext) -> None:
    _register_fps_graphics(app, ctx)
    _register_settings(app, ctx)
    _register_windows(app, ctx)
    _register_cleanup(app, ctx)
    _register_virtual_memory(app, ctx)
