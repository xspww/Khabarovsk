from __future__ import annotations

import os
import re
import stat
from typing import Any, Dict, Optional


DEFAULT_ROBLOX_SETTINGS_PATH = os.path.join(
    os.path.expandvars(r"%LOCALAPPDATA%"),
    "Roblox",
    "GlobalBasicSettings_13.xml",
)

FPS_MIN = 1
FPS_UNLOCKED_CAP = 0
GRAPHICS_QUALITY_MIN = 1
GRAPHICS_QUALITY_MAX = 10
GRAPHICS_LOW_DEFAULT_LEVEL = 1
VOLUME_MIN = 0
VOLUME_MAX = 10
VOLUME_DEFAULT_LEVEL = 10
_FRAMERATE_RE = re.compile(r'(<int\s+name="FramerateCap"\s*>)(-?\d+)(</int>)', re.IGNORECASE)
_VOLUME_RE = re.compile(r'(<float\s+name="MasterVolume"\s*>)(.*?)(</float>)', re.IGNORECASE | re.DOTALL)
_SETTING_PATTERNS = {
    "graphics_optimization_mode": ("token", "GraphicsOptimizationMode"),
    "graphics_quality_level": ("int", "GraphicsQualityLevel"),
    "max_quality_enabled": ("bool", "MaxQualityEnabled"),
    "quality_reset_level": ("int", "QualityResetLevel"),
    "saved_quality_level": ("token", "SavedQualityLevel"),
}
GRAPHICS_AUTO_VALUES = {
    "graphics_optimization_mode": "1",
    "graphics_quality_level": "21",
    "max_quality_enabled": "false",
    "quality_reset_level": "0",
    "saved_quality_level": "10",
}
SUPPORTED_PROCESS_PRIORITIES = {"low", "below_normal", "normal", "above_normal", "high"}


def normalize_fps_limit(value: Any, default: int = 240) -> int:
    try:
        fps = int(float(value))
    except Exception:
        fps = int(default)
    if fps < FPS_MIN:
        raise ValueError(f"FPS limit must be at least {FPS_MIN}")
    return fps


def normalize_graphics_quality(value: Any, default: int = GRAPHICS_LOW_DEFAULT_LEVEL) -> int:
    try:
        quality = int(float(value))
    except Exception:
        quality = int(default)
    if quality < GRAPHICS_QUALITY_MIN or quality > GRAPHICS_QUALITY_MAX:
        raise ValueError(
            f"Graphics quality must be between {GRAPHICS_QUALITY_MIN} and {GRAPHICS_QUALITY_MAX}"
        )
    return quality


def normalize_process_priority(value: Any, default: str = "low") -> str:
    priority = str(value or default or "low").strip().lower().replace("-", "_").replace(" ", "_")
    if priority == "realtime":
        raise ValueError("Realtime priority is not supported")
    if priority not in SUPPORTED_PROCESS_PRIORITIES:
        raise ValueError("Process priority must be one of: low, below_normal, normal, above_normal, high")
    return priority


def normalize_roblox_volume(value: Any, default: int = VOLUME_DEFAULT_LEVEL) -> int:
    try:
        level = int(float(value))
    except Exception:
        level = int(default)
    if level < VOLUME_MIN or level > VOLUME_MAX:
        raise ValueError(
            f"Roblox volume must be between {VOLUME_MIN} and {VOLUME_MAX}"
        )
    return level


def volume_level_to_file_value(level: int, muted: bool = False) -> str:
    if bool(muted):
        return "0"
    level = normalize_roblox_volume(level)
    if level <= 0:
        return "0"
    if level >= 10:
        return "1"
    # Roblox stores MasterVolume as 0..1 float; UI scale is 0..10 like the in-game menu.
    text = f"{level / 10.0:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def volume_file_value_to_level(raw: Any) -> Optional[int]:
    try:
        value = float(str(raw or "").strip())
    except Exception:
        return None
    if value != value:  # NaN guard
        return None
    value = max(0.0, min(1.0, value))
    return int(round(value * 10))


def is_readonly(path: str) -> bool:
    try:
        info = os.stat(path)
        attrs = int(getattr(info, "st_file_attributes", 0) or 0)
        readonly_flag = int(getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1) or 0x1)
        if attrs:
            return bool(attrs & readonly_flag)
        return not bool(info.st_mode & stat.S_IWRITE)
    except OSError:
        return False


def _resolve_roblox_settings_path(path: Optional[str] = None) -> str:
    """Resolve and validate the Roblox settings file path.

    Only the official ``GlobalBasicSettings_13.xml`` inside
    ``%LOCALAPPDATA%\\Roblox`` is allowed, to prevent arbitrary file
    deletion through the API.
    """
    from pathlib import Path

    target = path or DEFAULT_ROBLOX_SETTINGS_PATH
    local = os.path.expandvars(r"%LOCALAPPDATA%")
    allowed_dir = os.path.normcase(os.path.abspath(os.path.join(local, "Roblox")))
    resolved = os.path.normcase(os.path.abspath(str(target)))
    if os.path.basename(resolved).lower() != "globalbasicsettings_13.xml":
        raise ValueError("Only GlobalBasicSettings_13.xml can be deleted")
    if os.path.dirname(resolved) != allowed_dir:
        raise ValueError("Only %LOCALAPPDATA%\\Roblox\\GlobalBasicSettings_13.xml can be deleted")
    # Keep a readable (non-normalized) absolute path for responses.
    try:
        return str(Path(str(target)).absolute())
    except Exception:
        return os.path.abspath(str(target))


def roblox_settings_status(path: Optional[str] = None) -> Dict[str, Any]:
    target = _resolve_roblox_settings_path(path)
    exists = os.path.exists(target)
    payload: Dict[str, Any] = {
        "ok": True,
        "path": target,
        "exists": exists,
        "read_only": is_readonly(target) if exists else False,
        "size": 0,
        "msg": "ok" if exists else "not found",
    }
    if exists:
        try:
            payload["size"] = int(os.path.getsize(target))
        except OSError:
            payload["size"] = 0
    return payload


def delete_roblox_settings_file(path: Optional[str] = None) -> Dict[str, Any]:
    """Delete GlobalBasicSettings_13.xml so Roblox recreates it on next launch."""
    import ctypes

    target = _resolve_roblox_settings_path(path)
    if not os.path.exists(target):
        return {"ok": True, "deleted": False, "path": target, "exists": False, "msg": "File not found (nothing to delete)"}
    if os.path.isdir(target):
        raise ValueError("Refusing to delete a directory")
    # Clear read-only / hidden / system flags so os.remove() succeeds.
    try:
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
    except OSError:
        pass
    if os.name == "nt":
        try:
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(target))
            if attrs != 0xFFFFFFFF:
                ctypes.windll.kernel32.SetFileAttributesW(str(target), 0x80)  # FILE_ATTRIBUTE_NORMAL
        except Exception:
            pass
    try:
        os.remove(target)
    except PermissionError as exc:
        raise PermissionError(f"Cannot delete settings file (close Roblox first): {exc}")
    except OSError as exc:
        raise OSError(f"Delete failed: {exc}")
    return {"ok": True, "deleted": True, "path": target, "exists": False, "msg": "Deleted GlobalBasicSettings_13.xml"}


def set_readonly(path: str, readonly: bool) -> None:
    mode = os.stat(path).st_mode
    if readonly:
        os.chmod(path, mode & ~stat.S_IWRITE)
    else:
        os.chmod(path, mode | stat.S_IWRITE)


def _read_named_setting(text: str, tag: str, name: str) -> Optional[str]:
    pattern = re.compile(rf'<{tag}\s+name="{re.escape(name)}"\s*>(.*?)</{tag}>', re.IGNORECASE | re.DOTALL)
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def _replace_named_setting(text: str, tag: str, name: str, value: Any) -> tuple[str, int]:
    pattern = re.compile(rf'(<{tag}\s+name="{re.escape(name)}"\s*>)(.*?)(</{tag}>)', re.IGNORECASE | re.DOTALL)
    return pattern.subn(rf"\g<1>{value}\g<3>", text, count=1)


def _graphics_low_values(quality_level: Any = GRAPHICS_LOW_DEFAULT_LEVEL) -> Dict[str, str]:
    quality = normalize_graphics_quality(quality_level)
    value = str(quality)
    return {
        "graphics_optimization_mode": "0",
        "graphics_quality_level": value,
        "max_quality_enabled": "false",
        "quality_reset_level": value,
        "saved_quality_level": value,
    }


def _apply_named_settings(text: str, values: Dict[str, str]) -> tuple[str, int]:
    changed_total = 0
    for key, expected in values.items():
        tag, name = _SETTING_PATTERNS[key]
        text, changed = _replace_named_setting(text, tag, name, expected)
        changed_total += changed
    return text, changed_total


def read_fps_settings(path: Optional[str] = None) -> Dict[str, Any]:
    target = path or DEFAULT_ROBLOX_SETTINGS_PATH
    payload: Dict[str, Any] = {
        "path": target,
        "exists": os.path.exists(target),
        "read_only": False,
        "framerate_cap": None,
        "msg": "",
    }
    if not payload["exists"]:
        payload["msg"] = "Roblox settings file not found. Open Roblox once so it can create GlobalBasicSettings_13.xml."
        return payload
    payload["read_only"] = is_readonly(target)
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as exc:
        payload["msg"] = str(exc)
        return payload
    match = _FRAMERATE_RE.search(text)
    if not match:
        payload["msg"] = "FramerateCap setting not found in GlobalBasicSettings_13.xml."
        return payload
    payload["framerate_cap"] = int(match.group(2))
    for key, (tag, name) in _SETTING_PATTERNS.items():
        payload[key] = _read_named_setting(text, tag, name)
    volume_match = _VOLUME_RE.search(text)
    payload["master_volume"] = volume_match.group(2).strip() if volume_match else None
    payload["volume_level_current"] = volume_file_value_to_level(payload.get("master_volume"))
    payload["volume_muted_current"] = (
        payload["volume_level_current"] is not None and int(payload["volume_level_current"]) <= 0
    )
    try:
        payload["graphics_quality_level_current"] = normalize_graphics_quality(payload.get("saved_quality_level"))
    except ValueError:
        payload["graphics_quality_level_current"] = None
    payload["graphics_auto_active"] = all(
        str(payload.get(key) or "").strip().lower() == expected
        for key, expected in GRAPHICS_AUTO_VALUES.items()
    )
    low_values = _graphics_low_values(payload.get("graphics_quality_level_current") or GRAPHICS_LOW_DEFAULT_LEVEL)
    payload["graphics_low_active"] = (
        str(payload.get("graphics_optimization_mode") or "").strip().lower() == "0"
        and str(payload.get("max_quality_enabled") or "").strip().lower() == "false"
        and str(payload.get("graphics_quality_level") or "").strip() == low_values["graphics_quality_level"]
        and str(payload.get("saved_quality_level") or "").strip() == low_values["saved_quality_level"]
        and int(payload.get("graphics_quality_level_current") or 99) <= 3
    )
    payload["msg"] = "ok"
    return payload


def apply_graphics_settings_file(
    graphics_low_enabled: bool,
    path: Optional[str] = None,
    readonly_after: Optional[bool] = None,
    quality_level: Any = GRAPHICS_LOW_DEFAULT_LEVEL,
    volume_muted: Optional[bool] = None,
    volume_level: Any = None,
) -> Dict[str, Any]:
    target = path or DEFAULT_ROBLOX_SETTINGS_PATH
    if not os.path.exists(target):
        raise FileNotFoundError(
            "Roblox settings file not found. Open Roblox once so it can create GlobalBasicSettings_13.xml."
    )
    original_readonly = is_readonly(target)
    success = False
    quality = normalize_graphics_quality(quality_level)
    volume_touched = volume_muted is not None or volume_level is not None
    resolved_volume_muted: Optional[bool] = None
    resolved_volume_level: Optional[int] = None
    if volume_touched:
        resolved_volume_muted = bool(volume_muted) if volume_muted is not None else False
        resolved_volume_level = normalize_roblox_volume(
            VOLUME_DEFAULT_LEVEL if volume_level is None else volume_level
        )
    if bool(graphics_low_enabled) or volume_touched:
        if original_readonly:
            set_readonly(target, False)
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            if bool(graphics_low_enabled):
                text, changed_total = _apply_named_settings(text, _graphics_low_values(quality))
                if changed_total < 1:
                    raise ValueError("Roblox graphics settings not found in GlobalBasicSettings_13.xml.")
            if volume_touched:
                next_text, changed = _VOLUME_RE.subn(
                    rf"\g<1>{volume_level_to_file_value(resolved_volume_level, resolved_volume_muted)}\g<3>",
                    text,
                    count=1,
                )
                if changed < 1:
                    raise ValueError("Roblox volume setting (MasterVolume) not found in GlobalBasicSettings_13.xml.")
                text = next_text
            with open(target, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            success = True
        finally:
            if readonly_after is not None:
                set_readonly(target, bool(readonly_after))
            else:
                # Lock the file when graphics is on, or when volume is
                # non-default (muted / lowered) so Roblox cannot revert it.
                lock_volume = bool(
                    volume_touched
                    and (bool(resolved_volume_muted) or int(resolved_volume_level or 10) < 10)
                )
                set_readonly(target, bool(graphics_low_enabled or lock_volume))
    elif readonly_after is not None:
        set_readonly(target, bool(readonly_after))
    payload = read_fps_settings(target)
    payload.update({
        "ok": True,
        "graphics_low_enabled": bool(graphics_low_enabled),
        "graphics_auto_enabled": bool(graphics_low_enabled),
        "graphics_quality_level": quality,
        "read_only": is_readonly(target),
    })
    if volume_touched:
        payload.update({
            "roblox_volume_muted": bool(resolved_volume_muted),
            "roblox_volume_level": int(resolved_volume_level or 0),
        })
    return payload


def apply_volume_settings_file(
    volume_muted: bool = False,
    volume_level: Any = VOLUME_DEFAULT_LEVEL,
    path: Optional[str] = None,
    readonly_after: Optional[bool] = None,
) -> Dict[str, Any]:
    target = path or DEFAULT_ROBLOX_SETTINGS_PATH
    if not os.path.exists(target):
        raise FileNotFoundError(
            "Roblox settings file not found. Open Roblox once so it can create GlobalBasicSettings_13.xml."
        )
    muted = bool(volume_muted)
    level = normalize_roblox_volume(volume_level)
    original_readonly = is_readonly(target)
    success = False
    if original_readonly:
        set_readonly(target, False)
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        next_text, changed = _VOLUME_RE.subn(
            rf"\g<1>{volume_level_to_file_value(level, muted)}\g<3>", text, count=1
        )
        if changed < 1:
            raise ValueError("Roblox volume setting (MasterVolume) not found in GlobalBasicSettings_13.xml.")
        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(next_text)
        success = True
    finally:
        if readonly_after is not None:
            set_readonly(target, bool(readonly_after))
        else:
            set_readonly(target, bool(muted or level < 10) if success else original_readonly)
    payload = read_fps_settings(target)
    payload.update({
        "ok": True,
        "roblox_volume_muted": muted,
        "roblox_volume_level": level,
        "read_only": is_readonly(target),
    })
    return payload


def apply_performance_settings_file(
    fps_enabled: bool,
    fps_limit: Any,
    graphics_auto_enabled: bool = False,
    path: Optional[str] = None,
    graphics_quality_level: Any = GRAPHICS_LOW_DEFAULT_LEVEL,
    volume_muted: Optional[bool] = None,
    volume_level: Any = None,
) -> Dict[str, Any]:
    target = path or DEFAULT_ROBLOX_SETTINGS_PATH
    fps = normalize_fps_limit(fps_limit)
    quality = normalize_graphics_quality(graphics_quality_level)
    volume_touched = volume_muted is not None or volume_level is not None
    resolved_volume_muted = bool(volume_muted) if volume_muted is not None else False
    resolved_volume_level: Optional[int] = None
    if volume_touched:
        resolved_volume_level = normalize_roblox_volume(
            VOLUME_DEFAULT_LEVEL if volume_level is None else volume_level
        )
    if not os.path.exists(target):
        raise FileNotFoundError(
            "Roblox settings file not found. Open Roblox once so it can create GlobalBasicSettings_13.xml."
        )

    original_readonly = is_readonly(target)
    success = False
    if original_readonly:
        set_readonly(target, False)
    try:
        # Always write: enabling sets the cap, disabling resets to uncap (0)
        # so OFF truly works instead of leaving a stale value like 15.
        # Readonly is re-applied below only if FPS or graphics is still on.
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        if fps_enabled:
            next_text, changed = _FRAMERATE_RE.subn(rf"\g<1>{fps}\g<3>", text, count=1)
            if changed < 1:
                raise ValueError("FramerateCap setting not found in GlobalBasicSettings_13.xml.")
            text = next_text
        else:
            next_text, changed = _FRAMERATE_RE.subn(rf"\g<1>{FPS_UNLOCKED_CAP}\g<3>", text, count=1)
            if changed < 1:
                raise ValueError("FramerateCap setting not found in GlobalBasicSettings_13.xml.")
            text = next_text
        if graphics_auto_enabled:
            text, changed_total = _apply_named_settings(text, _graphics_low_values(quality))
            if changed_total < 1:
                raise ValueError("Roblox graphics settings not found in GlobalBasicSettings_13.xml.")
        if volume_touched:
            next_text, changed = _VOLUME_RE.subn(
                rf"\g<1>{volume_level_to_file_value(resolved_volume_level, resolved_volume_muted)}\g<3>",
                text,
                count=1,
            )
            if changed < 1:
                raise ValueError("Roblox volume setting (MasterVolume) not found in GlobalBasicSettings_13.xml.")
            text = next_text
        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        success = True
    finally:
        lock_volume = bool(
            volume_touched
            and (bool(resolved_volume_muted) or int(resolved_volume_level or 10) < 10)
        )
        set_readonly(target, bool(fps_enabled or graphics_auto_enabled or lock_volume) if success else original_readonly)

    payload = read_fps_settings(target)
    payload.update({
        "ok": True,
        "enabled": bool(fps_enabled),
        "fps_limit": int(fps),
        "graphics_low_enabled": bool(graphics_auto_enabled),
        "graphics_auto_enabled": bool(graphics_auto_enabled),
        "graphics_quality_level": quality,
    })
    if volume_touched:
        payload.update({
            "roblox_volume_muted": bool(resolved_volume_muted),
            "roblox_volume_level": int(resolved_volume_level or 0),
        })
    return payload


def priority_to_psutil_value(priority: Any) -> int:
    normalized = normalize_process_priority(priority)
    import psutil

    mapping = {
        "low": psutil.IDLE_PRIORITY_CLASS,
        "below_normal": psutil.BELOW_NORMAL_PRIORITY_CLASS,
        "normal": psutil.NORMAL_PRIORITY_CLASS,
        "above_normal": psutil.ABOVE_NORMAL_PRIORITY_CLASS,
        "high": psutil.HIGH_PRIORITY_CLASS,
    }
    return int(mapping[normalized])


def apply_process_priority_to_roblox(priority: Any) -> Dict[str, Any]:
    normalized = normalize_process_priority(priority)
    value = priority_to_psutil_value(normalized)
    results = []
    try:
        import psutil
    except ImportError:
        return {"ok": False, "priority": normalized, "applied": 0, "count": 0, "results": [], "msg": "psutil unavailable"}

    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = str(proc.info.get("name") or "").lower()
            if name != "robloxplayerbeta.exe":
                continue
            proc.nice(value)
            results.append({"pid": int(proc.pid), "ok": True})
        except Exception as exc:
            results.append({"pid": int(getattr(proc, "pid", 0) or 0), "ok": False, "msg": str(exc)})
    return {
        "ok": True,
        "priority": normalized,
        "applied": sum(1 for item in results if item.get("ok")),
        "count": len(results),
        "results": results,
    }
