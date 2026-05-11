from __future__ import annotations

import time
from typing import List, Optional, Tuple

from core import Account, flog_kv
from services.process_service import ProcessManager


def _window_resize_target_from_config(cfg: dict) -> Optional[Tuple[int, int]]:
    if not bool(cfg.get("roblox_window_resize_enabled", False)):
        return None
    try:
        width = int(float(cfg.get("roblox_window_width", 640) or 640))
    except Exception:
        width = 640
    try:
        height = int(float(cfg.get("roblox_window_height", 480) or 480))
    except Exception:
        height = 480
    width = max(320, min(width, 1920))
    height = max(240, min(height, 1080))
    return width, height

def _window_arrange_settings_from_config(cfg: dict) -> Optional[Tuple[int, int, int, int, int]]:
    target = _window_resize_target_from_config(cfg)
    if not target or not bool(cfg.get("roblox_window_arrange_enabled", False)):
        return None
    width, height = target
    try:
        columns = int(float(cfg.get("roblox_window_arrange_columns", 6) or 6))
    except Exception:
        columns = 6
    try:
        gap = int(float(cfg.get("roblox_window_arrange_gap", 2) or 2))
    except Exception:
        gap = 2
    try:
        margin = int(float(cfg.get("roblox_window_arrange_margin", 0) or 0))
    except Exception:
        margin = 0
    columns = max(1, min(columns, 32))
    gap = max(0, min(gap, 80))
    margin = max(0, min(margin, 300))
    return width, height, columns, gap, margin

def _apply_cpu_limiter_for_bound_process(
    accounts: List[Account],
    cfg: dict,
    reason: str,
    account: Optional[Account] = None,
) -> None:
    try:
        from services.cpu_limiter import CPU_LIMITER, normalize_cpu_limiter_settings

        settings = normalize_cpu_limiter_settings(cfg)
        if not bool(settings.get("enabled")):
            return
        result = CPU_LIMITER.apply(accounts, settings)
        row = None
        if account:
            account_key = getattr(account, "_config_username", "") or getattr(account, "username", "")
            row = next((item for item in result.get("rows", []) if item.get("username") == account_key), None)
        flog_kv(
            "PERFORMANCE",
            "cpu_limiter_bound_apply",
            account=getattr(account, "display_name", "") if account else "",
            reason=reason,
            mode=result.get("mode", ""),
            status=(row or {}).get("status", ""),
            pid=(row or {}).get("pid", ""),
            limit_percent=(row or {}).get("limit_percent", ""),
            applied=result.get("applied", 0),
            fallback=result.get("fallback", 0),
            failed=result.get("failed", 0),
        )
    except Exception as exc:
        flog_kv(
            "PERFORMANCE",
            "cpu_limiter_bound_apply_failed",
            "warning",
            account=getattr(account, "display_name", "") if account else "",
            reason=reason,
            error=str(exc),
        )


class MaintenancePerformanceMixin:
    def _apply_auto_process_priority(self):
        if not bool(self._cfg.get("auto_process_priority_enabled", False)):
            return
        now = time.time()
        if (now - self._last_priority_apply_at) < 10.0:
            return
        self._last_priority_apply_at = now
        try:
            from performance_settings import apply_process_priority_to_roblox

            result = apply_process_priority_to_roblox(self._cfg.get("process_priority", "low"))
            if int(result.get("applied") or 0) > 0:
                flog_kv(
                    "PERFORMANCE",
                    "auto_process_priority_applied",
                    priority=result.get("priority", ""),
                    applied=result.get("applied", 0),
                    count=result.get("count", 0),
                )
        except Exception as exc:
            flog_kv("PERFORMANCE", "auto_process_priority_failed", "warning", error=str(exc))

    def _apply_cpu_limiter(self):
        now = time.time()
        try:
            from services.cpu_limiter import CPU_LIMITER, normalize_cpu_limiter_settings

            settings = normalize_cpu_limiter_settings(self._cfg)
            if not bool(settings.get("enabled")):
                CPU_LIMITER.release_all()
                self._last_cpu_limiter_apply_at = now
                return
            if (now - self._last_cpu_limiter_apply_at) < 10.0:
                return
            self._last_cpu_limiter_apply_at = now
            result = CPU_LIMITER.apply(self._accounts, settings)
            if any(int(result.get(key) or 0) > 0 for key in ("applied", "fallback", "failed")):
                flog_kv(
                    "PERFORMANCE",
                    "cpu_limiter_applied",
                    mode=result.get("mode", ""),
                    applied=result.get("applied", 0),
                    fallback=result.get("fallback", 0),
                    failed=result.get("failed", 0),
                )
        except Exception as exc:
            flog_kv("PERFORMANCE", "cpu_limiter_failed", "warning", error=str(exc))

    def _enforce_window_resize(self):
        target = _window_resize_target_from_config(self._cfg)
        if not target:
            self._last_window_resize_at = time.time()
            return
        try:
            seconds = max(1.0, float(self._cfg.get("roblox_window_resize_interval_seconds", 10) or 10))
        except Exception:
            seconds = 10.0
        now = time.time()
        if (now - self._last_window_resize_at) < seconds:
            return
        self._last_window_resize_at = now
        width, height = target
        arrange = _window_arrange_settings_from_config(self._cfg)
        if arrange:
            width, height, columns, gap, margin = arrange
            result = ProcessManager.arrange_roblox_windows(width, height, columns, gap, margin)
            changed = int(result.get("arranged") or 0)
            event = "auto_window_arrange_cycle"
        else:
            result = ProcessManager.resize_roblox_windows(width, height)
            changed = int(result.get("resized") or 0)
            event = "auto_window_resize_cycle"
        if changed > 0:
            flog_kv(
                "WINDOW",
                event,
                arranged=result.get("arranged", 0),
                resized=result.get("resized", 0),
                count=result.get("count", 0),
                width=width,
                height=height,
                columns=result.get("columns", ""),
                seconds=f"{seconds:.1f}",
            )
