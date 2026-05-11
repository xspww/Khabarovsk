from __future__ import annotations

import json
import html as html_lib
import os
import re
import sys
import threading
import time
import asyncio
import secrets
import urllib.request
import urllib.error
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app_paths import APP_NAME

APP_USER_AGENT = "ArgusLauncher/RT"
APP_ICON_FILE = "ROBUGUARD Corners  .png"

if sys.platform != "win32":
    print(f"{APP_NAME} requires Windows.")
    sys.exit(1)

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
    import uvicorn
except ImportError:
    print("pip install fastapi uvicorn")
    sys.exit(1)

from core import (
    Account,
    ConfigManager,
    flog,
    flog_kv,
    APP_DATA_DIR,
    ACCOUNTS_TEXT_FILE,
    RUNTIME_TEXT_FILE,
    LOG_FILE,
    account_launch_block_reason,
    cookie_identity_block_reason,
)
from farm import FarmController
from services.process_service import ProcessManager
from services.resource_monitor import get_rt_monitor
from account_hybrid import ACCOUNT_STORE, audit_event
from performance_settings import (
    DEFAULT_ROBLOX_SETTINGS_PATH,
    apply_graphics_settings_file,
    apply_performance_settings_file,
    apply_process_priority_to_roblox,
    normalize_fps_limit,
    normalize_graphics_quality,
    normalize_process_priority,
    read_fps_settings,
)
from services.presence_service import PRESENCE_SERVICE
from services.network_fault_injector import NETWORK_FAULT_INJECTOR
from services.roblox_install_manager import RobloxInstallManager
from services.cpu_limiter import CPU_LIMITER, normalize_cpu_limiter_settings
from roblox_hybrid import (
    HybridLauncher,
    release_multi_roblox_guard,
    resolve_vip_access_code,
    validate_cookie_details,
    validate_cookie as hybrid_validate_cookie,
)

from ui_dashboard import HTML_UI
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#  BOOTSTRAP
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
cfg_mgr  = ConfigManager()
legacy_accounts = cfg_mgr.get_accounts()
try:
    ACCOUNT_STORE.ensure_from_legacy([account.to_dict() for account in legacy_accounts])
    accounts = [Account.from_dict(item) for item in ACCOUNT_STORE.to_roboguard_accounts()]
except Exception as e:
    flog_kv("ACCOUNT_DATA", "load_failed_fallback_legacy", "warning", error=e)
    accounts = legacy_accounts
farm     = FarmController(cfg_mgr)
farm.set_accounts(accounts)
ROBLOX_INSTALLER = RobloxInstallManager(
    guard_running=lambda: bool(getattr(farm, "running", False)),
    roblox_running=lambda: bool(ProcessManager.snapshot_pids()),
    logger=flog,
)

_COOKIE_RE = re.compile(r"(_\|WARNING:[^\s'\"<>]+|\.ROBLOSECURITY[^\s'\"<>]*)", re.IGNORECASE)
_KV_SECRET_RE = re.compile(r"(?i)\b(cookie|roblosecurity|ram_password|password)=([^\s]+)")
_AVATAR_CACHE: Dict[str, Tuple[float, str]] = {}
_AVATAR_CACHE_TTL = 300.0


def _redact_log_line(line: str) -> str:
    text = _COOKIE_RE.sub("[ROBLOX_COOKIE_REDACTED]", str(line or ""))
    return _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)


def _tail_log_lines(limit: int = 300) -> List[str]:
    limit = max(1, min(int(limit or 300), 1000))
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-limit:]
    except Exception as e:
        flog_kv("API", "log_tail_failed", "warning", error=e)
        return []
    return [_redact_log_line(line.rstrip("\r\n")) for line in lines]


def _int_setting(value, default: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(float(value))
    except Exception:
        parsed = int(default)
    return max(min_value, min(parsed, max_value))


WINDOW_SIZE_PRESETS: Dict[str, Tuple[int, int]] = {
    "320x240": (320, 240),
    "480x360": (480, 360),
    "640x480": (640, 480),
    "800x600": (800, 600),
    "1024x768": (1024, 768),
    "1280x720": (1280, 720),
    "1600x900": (1600, 900),
    "1920x1080": (1920, 1080),
}


def _normalize_window_size_settings(body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    body = body or {}
    enabled = bool(body.get("enabled", body.get("roblox_window_resize_enabled", cfg_mgr.get("roblox_window_resize_enabled", False))))
    preset = str(body.get("preset", body.get("roblox_window_size_preset", cfg_mgr.get("roblox_window_size_preset", "640x480"))) or "640x480")
    preset = preset.strip().lower().replace(" ", "")
    if preset != "custom" and preset not in WINDOW_SIZE_PRESETS:
        raise ValueError("Invalid window size preset")
    if preset in WINDOW_SIZE_PRESETS:
        width, height = WINDOW_SIZE_PRESETS[preset]
    else:
        width = _int_setting(body.get("width", body.get("roblox_window_width", cfg_mgr.get("roblox_window_width", 640))), 640, 320, 1920)
        height = _int_setting(body.get("height", body.get("roblox_window_height", cfg_mgr.get("roblox_window_height", 480))), 480, 240, 1080)
    interval = _int_setting(
        body.get("interval_seconds", body.get("roblox_window_resize_interval_seconds", cfg_mgr.get("roblox_window_resize_interval_seconds", 10))),
        10,
        1,
        3600,
    )
    arrange_enabled = bool(body.get("arrange_enabled", body.get("roblox_window_arrange_enabled", cfg_mgr.get("roblox_window_arrange_enabled", False))))
    arrange_columns = _int_setting(
        body.get("arrange_columns", body.get("roblox_window_arrange_columns", cfg_mgr.get("roblox_window_arrange_columns", 6))),
        6,
        1,
        32,
    )
    arrange_gap = _int_setting(
        body.get("arrange_gap", body.get("roblox_window_arrange_gap", cfg_mgr.get("roblox_window_arrange_gap", 2))),
        2,
        0,
        80,
    )
    arrange_margin = _int_setting(
        body.get("arrange_margin", body.get("roblox_window_arrange_margin", cfg_mgr.get("roblox_window_arrange_margin", 0))),
        0,
        0,
        300,
    )
    return {
        "enabled": enabled,
        "preset": preset,
        "width": int(width),
        "height": int(height),
        "interval_seconds": interval,
        "arrange_enabled": arrange_enabled,
        "arrange_columns": arrange_columns,
        "arrange_gap": arrange_gap,
        "arrange_margin": arrange_margin,
    }


def _window_size_status() -> Dict[str, Any]:
    settings = _normalize_window_size_settings({})
    return {
        "ok": True,
        "enabled": settings["enabled"],
        "preset": settings["preset"],
        "width": settings["width"],
        "height": settings["height"],
        "interval_seconds": settings["interval_seconds"],
        "arrange_enabled": settings["arrange_enabled"],
        "arrange_columns": settings["arrange_columns"],
        "arrange_gap": settings["arrange_gap"],
        "arrange_margin": settings["arrange_margin"],
        "presets": [{"value": key, "width": value[0], "height": value[1]} for key, value in WINDOW_SIZE_PRESETS.items()],
    }


def _roblox_runtime_restart_required() -> Dict[str, Any]:
    running = False
    count = 0
    try:
        live = ProcessManager.list_live_game_processes()
        count = len(live)
        running = count > 0
    except Exception:
        live = []
    rt_running = bool(getattr(farm, "running", False))
    requires_restart = bool(running or rt_running)
    warning = ""
    if requires_restart:
        warning = "Close Roblox or Stop guard, then re-game for performance settings to take effect."
    return {
        "roblox_running": running,
        "roblox_pid_count": count,
        "rt_running": rt_running,
        "requires_restart": requires_restart,
        "warning": warning,
    }


def _fps_limiter_status(path: str = DEFAULT_ROBLOX_SETTINGS_PATH) -> Dict[str, Any]:
    file_status = read_fps_settings(path)
    runtime_status = _roblox_runtime_restart_required()
    configured_limit = _int_setting(cfg_mgr.get("fps_limit", 240), 240, 15, 1000)
    graphics_enabled = bool(cfg_mgr.get("graphics_low_enabled", cfg_mgr.get("graphics_auto_enabled", False)))
    graphics_quality = _int_setting(cfg_mgr.get("graphics_quality_level", 1), 1, 1, 10)
    priority = str(cfg_mgr.get("process_priority", "low") or "low")
    payload = {
        "ok": bool(file_status.get("exists")),
        **file_status,
        **runtime_status,
        "enabled": bool(cfg_mgr.get("fps_limiter_enabled", False)),
        "fps_limit": configured_limit,
        "graphics_low_enabled": graphics_enabled,
        "graphics_auto_enabled": graphics_enabled,
        "graphics_quality_level": graphics_quality,
        "auto_process_priority_enabled": bool(cfg_mgr.get("auto_process_priority_enabled", False)),
        "process_priority": priority,
    }
    if file_status.get("framerate_cap") is not None:
        payload["fps_limit"] = int(file_status.get("framerate_cap") or configured_limit)
    return payload


def _graphics_status(path: str = DEFAULT_ROBLOX_SETTINGS_PATH) -> Dict[str, Any]:
    file_status = read_fps_settings(path)
    runtime_status = _roblox_runtime_restart_required()
    graphics_enabled = bool(cfg_mgr.get("graphics_low_enabled", cfg_mgr.get("graphics_auto_enabled", False)))
    graphics_quality = _int_setting(cfg_mgr.get("graphics_quality_level", 1), 1, 1, 10)
    priority = str(cfg_mgr.get("process_priority", "low") or "low")
    payload = {
        "ok": bool(file_status.get("exists")),
        **file_status,
        **runtime_status,
        "graphics_low_enabled": graphics_enabled,
        "graphics_auto_enabled": graphics_enabled,
        "graphics_quality_level": graphics_quality,
        "auto_process_priority_enabled": bool(cfg_mgr.get("auto_process_priority_enabled", False)),
        "process_priority": priority,
    }
    return payload


def _cpu_limiter_settings_from_config(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    source = {
        "cpu_limiter_enabled": cfg_mgr.get("cpu_limiter_enabled", False),
        "cpu_limiter_mode": cfg_mgr.get("cpu_limiter_mode", "hard"),
        "cpu_limiter_default_percent": cfg_mgr.get("cpu_limiter_default_percent", 20),
        "cpu_limiter_apply_all": cfg_mgr.get("cpu_limiter_apply_all", True),
        "cpu_limiter_accounts": cfg_mgr.get("cpu_limiter_accounts", {}),
    }
    if extra:
        for key, value in extra.items():
            if key == "enabled":
                source["cpu_limiter_enabled"] = value
            elif key == "mode":
                source["cpu_limiter_mode"] = value
            elif key == "default_limit_percent":
                source["cpu_limiter_default_percent"] = value
            elif key == "apply_all":
                source["cpu_limiter_apply_all"] = value
            elif key == "accounts":
                source["cpu_limiter_accounts"] = value
            elif key in {
                "cpu_limiter_enabled",
                "cpu_limiter_mode",
                "cpu_limiter_default_percent",
                "cpu_limiter_apply_all",
                "cpu_limiter_accounts",
            }:
                source[key] = value
    return normalize_cpu_limiter_settings(source)


def _cpu_limiter_status() -> Dict[str, Any]:
    settings = _cpu_limiter_settings_from_config()
    return CPU_LIMITER.snapshot(getattr(farm, "_accounts", []), settings)


def _apply_game_defaults(accounts_to_update: List[Account], persist: bool = False) -> int:
    cfg = cfg_mgr.snapshot()
    vip_url = str(cfg.get("game_private_server_url", "") or "").strip()
    place_id = str(cfg.get("game_place_id", "") or "").strip()
    if vip_url:
        parsed_place, _link_code = ProcessManager.parse_vip_link(vip_url)
        if parsed_place and not place_id:
            place_id = str(parsed_place)
    if not vip_url and not place_id:
        return 0

    changed = 0
    for acc in accounts_to_update:
        account_changed = False
        if place_id:
            if str(acc.place_id or "").strip() != place_id:
                acc.place_id = place_id
                account_changed = True
            filtered_links = [
                link for link in list(acc.vip_links or [])
                if not ProcessManager.parse_vip_link(str(link or "").strip())[0]
                or ProcessManager.parse_vip_link(str(link or "").strip())[0] == place_id
            ]
            active_place = ProcessManager.parse_vip_link(str(acc.active_vip or "").strip())[0]
            if active_place and active_place != place_id:
                acc.active_vip = ""
                account_changed = True
            if not vip_url and filtered_links != list(acc.vip_links or []):
                acc.vip_links = filtered_links
                account_changed = True
        if vip_url and list(acc.vip_links or []) != [vip_url]:
            acc.vip_links = [vip_url]
            account_changed = True
        if account_changed:
            changed += 1
    if changed and persist:
        cfg_mgr.save_accounts(farm._accounts)
        flog_kv("API", "game_defaults_applied", accounts=changed)
    return changed

app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)
from desktop_host import (
    INSTANCE_TOKEN,
    SHUTDOWN_REQUESTED,
    _cmdline_targets_this_app,
    _run_desktop_window,
    clear_instance_state,
    run_desktop,
    run_with_tray,
    run_without_tray,
)


def _account_data_records(include_cookies: bool = False) -> List[Dict[str, Any]]:
    try:
        return ACCOUNT_STORE.read_records(include_cookies=include_cookies)
    except Exception as e:
        flog_kv("ACCOUNT_DATA", "read_failed", "warning", error=e)
        return []


def _account_data_api_records() -> List[Dict[str, Any]]:
    records = _account_data_records(include_cookies=False)
    runtime_by_user = {
        str(account.username or "").strip().lower(): account
        for account in farm._accounts
        if str(account.username or "").strip()
    }
    result: List[Dict[str, Any]] = []
    for record in records:
        item = ACCOUNT_STORE.to_api_record(record)
        blocked_reason = cookie_identity_block_reason(
            str(item.get("username") or ""),
            str(item.get("cookie_username") or ""),
            bool(item.get("cookie_mismatch", False)),
        )
        runtime = runtime_by_user.get(str(item.get("username") or "").strip().lower())
        if runtime:
            runtime_snapshot = runtime.runtime_snapshot()
            runtime_blocked = account_launch_block_reason(runtime)
            if runtime_blocked:
                blocked_reason = runtime_blocked
            item["state"] = runtime.state.name
            item["pid"] = runtime.pid
            item["runtime_state"] = runtime_snapshot.get("runtime_state") or str(runtime.runtime.lifecycle_state)
            item["can_rejoin"] = bool(farm.running and runtime.state.name != "FAILED" and not blocked_reason)
            item["can_kill"] = bool(runtime.pid)
            item["cookie_username"] = runtime.cookie_username or item.get("cookie_username", "")
            item["cookie_user_id"] = runtime.cookie_user_id or item.get("cookie_user_id", "")
            item["cookie_mismatch"] = bool(runtime.cookie_mismatch or item.get("cookie_mismatch", False))
        item["blocked_reason"] = blocked_reason
        item["launchable"] = not bool(blocked_reason)
        result.append(item)
    return result


def _load_accounts_from_account_data() -> List[Account]:
    return [Account.from_dict(item) for item in ACCOUNT_STORE.to_roboguard_accounts()]


def _replace_farm_accounts_from_store() -> int:
    new_accounts = _load_accounts_from_account_data()
    _apply_game_defaults(new_accounts, persist=False)
    was_running = farm.running
    if was_running:
        farm.stop()
        time.sleep(0.5)
    farm.set_accounts(new_accounts)
    cfg_mgr.save_accounts(new_accounts)
    if was_running:
        farm.start()
    return len(new_accounts)


def _find_account_record(username: str, include_cookie: bool = True) -> Optional[Dict[str, Any]]:
    wanted = str(username or "").strip().lower()
    for record in _account_data_records(include_cookies=include_cookie):
        if str(record.get("username") or "").strip().lower() == wanted:
            return record
    return None


def _import_cookie_validator(cookie: str):
    ok, username, detail, meta = validate_cookie_details(cookie)
    return ok, username, detail, meta


def _global_launch_target(body: Dict[str, Any], record: Dict[str, Any]) -> Dict[str, Any]:
    target = dict(body or {})
    place_id = str(target.get("place_id") or cfg_mgr.get("game_place_id", "") or record.get("place_id") or "").strip()
    vip_links = list(record.get("vip_links") or [])
    if place_id:
        vip_links = [
            link for link in vip_links
            if not ProcessManager.parse_vip_link(str(link or "").strip())[0]
            or ProcessManager.parse_vip_link(str(link or "").strip())[0] == place_id
        ]
    target.setdefault("vip_links", vip_links)
    target["place_id"] = place_id
    global_vip = str(cfg_mgr.get("game_private_server_url", "") or "").strip()
    global_place = ProcessManager.parse_vip_link(global_vip)[0] if global_vip else ""
    target.setdefault("global_vip_link", global_vip if (not place_id or not global_place or global_place == place_id) else "")
    target.setdefault("auto_create_private_server_enabled", cfg_mgr.get("auto_create_private_server_enabled", False))
    target.setdefault("auto_create_private_server_free_only", cfg_mgr.get("auto_create_private_server_free_only", True))
    return target


def _lookup_roblox_place(place_id: str) -> Dict[str, Any]:
    place = str(place_id or "").strip()
    if not place.isdigit():
        raise HTTPException(400, "place_id must be numeric")

    details: Dict[str, Any] = {}
    universe_id = ""
    image_url = ""

    def fetch_json(url: str) -> Any:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": f"Mozilla/5.0 {APP_USER_AGENT}", "Accept": "application/json, text/plain, */*"},
        )
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    try:
        universe_payload = fetch_json(f"https://apis.roblox.com/universes/v1/places/{place}/universe")
        if isinstance(universe_payload, dict):
            universe_id = str(universe_payload.get("universeId") or "")
        if universe_id:
            games_payload = fetch_json(
                "https://games.roblox.com/v1/games?" + urllib.parse.urlencode({"universeIds": universe_id})
            )
            data = games_payload.get("data") if isinstance(games_payload, dict) else []
            if isinstance(data, list) and data:
                details = data[0] if isinstance(data[0], dict) else {}
    except Exception:
        details = {}

    try:
        thumb_target = universe_id or place
        thumb_path = "games/icons" if universe_id else "places/gameicons"
        thumb_key = "universeIds" if universe_id else "placeIds"
        thumb_url = "https://thumbnails.roblox.com/v1/" + thumb_path + "?" + urllib.parse.urlencode(
            {
                thumb_key: thumb_target,
                "size": "150x150",
                "format": "Png",
                "isCircular": "false",
            }
        )
        payload = fetch_json(thumb_url)
        items = payload.get("data") if isinstance(payload, dict) else []
        if isinstance(items, list) and items:
            image_url = str(items[0].get("imageUrl") or "")
    except Exception:
        image_url = ""

    name = str(details.get("name") or details.get("sourceName") or "").strip()
    creator = details.get("creator") if isinstance(details.get("creator"), dict) else {}
    builder = str(
        details.get("builder")
        or details.get("creatorName")
        or creator.get("name")
        or ""
    ).strip()

    if not name:
        try:
            req = urllib.request.Request(
                f"https://www.roblox.com/games/{place}",
                headers={"User-Agent": f"Mozilla/5.0 {APP_USER_AGENT}", "Accept": "text/html, */*"},
            )
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                page = resp.read().decode("utf-8", errors="replace")
            title_match = re.search(r"<title>(.*?)</title>", page, flags=re.I | re.S)
            if title_match:
                title = html_lib.unescape(re.sub(r"\s+", " ", title_match.group(1))).strip()
                name = re.sub(r"\s*\|\s*Roblox\s*$", "", title, flags=re.I).strip()
            image_match = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', page, flags=re.I)
            if image_match and not image_url:
                image_url = html_lib.unescape(image_match.group(1)).strip()
        except Exception as exc:
            raise HTTPException(502, f"Roblox place lookup failed: {exc}")

    return {
        "ok": True,
        "place_id": place,
        "name": name or f"Place {place}",
        "builder": builder,
        "universe_id": universe_id,
        "image_url": image_url,
        "url": f"https://www.roblox.com/games/{place}",
    }

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#  API ROUTES
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@app.get("/api/status")
def api_status():
    return farm.get_status()

@app.get("/api/runtime/health")
def api_runtime_health():
    return farm.get_runtime_health()

@app.get("/api/runtime/events")
def api_runtime_events(account_id: str = "", limit: int = 100):
    return farm.get_runtime_events(account_id=account_id, limit=limit)


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
        validation = NETWORK_FAULT_INJECTOR.validate_roblox_pid(pid)
        if not validation.get("ok"):
            raise HTTPException(400, validation)
        validation["account_id"] = account_id
        return validation
    live = NETWORK_FAULT_INJECTOR.find_live_roblox_processes()
    if not live:
        raise HTTPException(404, "No live RobloxPlayerBeta.exe process found")
    target = dict(live[0])
    target["account_id"] = account_id
    return target


@app.get("/api/test/network-fault/status")
def api_network_fault_status():
    return NETWORK_FAULT_INJECTOR.status()


@app.post("/api/test/network-fault/block-roblox")
async def api_network_fault_block_roblox(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected object")
    target = _network_fault_target(body)
    duration = _int_setting(body.get("duration_seconds", 90), 90, 1, 3600)
    result = NETWORK_FAULT_INJECTOR.block_roblox(
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
    result = NETWORK_FAULT_INJECTOR.restore()
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
        _apply_game_defaults(farm._accounts, persist=True)
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

@app.post("/api/account/{username}/save-cookie")
def api_save_cookie(username: str):
    acc = next((a for a in farm._accounts if a.username == username), None)
    if not acc:
        raise HTTPException(404, "Account not found")
    if not str(acc.cookie or "").strip():
        return {"ok": False, "msg": "No cookie loaded for this account"}
    ok, cookie_username, detail, meta = validate_cookie_details(acc.cookie)
    if not ok:
        return {"ok": False, "msg": detail}
    mismatch = bool(cookie_username and acc.username.lower() != cookie_username.lower())
    if mismatch:
        ACCOUNT_STORE.update_record(
            username,
            {
                "cookie_username": cookie_username,
                "cookie_user_id": str(meta.get("user_id") or ""),
                "cookie_mismatch": True,
                "import_status": "cookie_mismatch",
            },
        )
        return {"ok": False, "msg": f"Cookie belongs to {cookie_username}, not {username}. Reimport the correct cookie."}
    ACCOUNT_STORE.upsert_records([acc.to_dict()])
    cfg_mgr.save_accounts(farm._accounts)
    return {"ok": True, "msg": f"Saved encrypted cookie to AccountData.json for {username}"}

@app.get("/api/performance/fps-limiter")
def api_get_fps_limiter():
    return _fps_limiter_status()

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
    runtime_status = _roblox_runtime_restart_required()
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
    return _graphics_status()


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
    payload.update(_roblox_runtime_restart_required())
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
    return _cpu_limiter_status()


@app.post("/api/performance/cpu-limiter")
async def api_set_cpu_limiter(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected object")
    try:
        settings = _cpu_limiter_settings_from_config(body)
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
    return _window_size_status()


@app.post("/api/performance/window-size")
async def api_set_window_size(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected object")
    try:
        settings = _normalize_window_size_settings(body)
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
    payload = _window_size_status()
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

@app.get("/api/config")
def api_get_config():
    snap = cfg_mgr.snapshot()
    snap.pop("accounts", None)
    snap.pop("runtime_state", None)
    return snap

@app.post("/api/config")
async def api_set_config(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected object")
    allowed = {
        "auto_rejoin", "rejoin_delay", "max_retry", "max_fail_count",
        "crash_timeout", "heartbeat_timeout", "launch_verify_window", "login_warmup_delay",
        "anti_spam_window", "launch_rate_interval", "account_switch_cooldown",
        "queue_delay_seconds", "queue_duration_seconds", "max_concurrent_accounts",
        "game_private_server_url", "game_place_id",
        "auto_create_private_server_enabled", "auto_create_private_server_free_only",
        "auto_close_enabled", "auto_close_minutes",
        "auto_minimize_enabled", "auto_minimize_seconds",
        "not_responding_timeout",
        "network_check_interval", "network_debounce",
        "queue_timeout", "cooldown_after_crash", "relaunch_loop_limit",
        "connection_error_rejoin", "popup_disconnected_enabled", "connection_error_hold_time",
        "watchdog_enabled", "watchdog_cpu_low",
        "watchdog_ram_low", "watchdog_hold_time",
        "watchdog_activity_timeout", "watchdog_loading_grace",
        "recovery_restore_window", "event_bus_workers", "event_bus_max_pending",
        "fps_limiter_enabled", "fps_limit", "graphics_auto_enabled", "graphics_low_enabled", "graphics_quality_level",
        "auto_process_priority_enabled", "process_priority",
        "cpu_limiter_enabled", "cpu_limiter_mode", "cpu_limiter_default_percent",
        "cpu_limiter_apply_all", "cpu_limiter_accounts",
        "roblox_window_resize_enabled", "roblox_window_size_preset", "roblox_window_width",
        "roblox_window_height", "roblox_window_resize_interval_seconds",
        "roblox_window_arrange_enabled", "roblox_window_arrange_columns",
        "roblox_window_arrange_gap", "roblox_window_arrange_margin",
        "presence_api_enabled", "presence_poll_interval_seconds",
        "presence_cache_ttl_seconds", "presence_assist_rejoin_enabled",
        "multi_roblox_enabled", "rt_rotation_enabled",
    }
    updates = {k: v for k, v in body.items() if k in allowed}
    if "queue_delay_seconds" in updates:
        delay = _int_setting(updates["queue_delay_seconds"], 15, 0, 3600)
        updates["queue_delay_seconds"] = delay
        updates["launch_rate_interval"] = delay
        updates["account_switch_cooldown"] = delay
    if "queue_duration_seconds" in updates:
        updates["queue_duration_seconds"] = _int_setting(updates["queue_duration_seconds"], 15, 0, 86400)
    if "max_concurrent_accounts" in updates:
        updates["max_concurrent_accounts"] = _int_setting(updates["max_concurrent_accounts"], 40, 1, 500)
    if "auto_close_minutes" in updates:
        updates["auto_close_minutes"] = _int_setting(updates["auto_close_minutes"], 0, 0, 1440)
    if "auto_close_enabled" in updates:
        updates["auto_close_enabled"] = bool(updates["auto_close_enabled"])
    if "auto_minimize_enabled" in updates:
        updates["auto_minimize_enabled"] = bool(updates["auto_minimize_enabled"])
    if "auto_minimize_seconds" in updates:
        updates["auto_minimize_seconds"] = _int_setting(updates["auto_minimize_seconds"], 10, 1, 3600)
    if "fps_limiter_enabled" in updates:
        updates["fps_limiter_enabled"] = bool(updates["fps_limiter_enabled"])
    if "fps_limit" in updates:
        updates["fps_limit"] = _int_setting(updates["fps_limit"], 240, 15, 1000)
    if "graphics_auto_enabled" in updates:
        updates["graphics_auto_enabled"] = bool(updates["graphics_auto_enabled"])
    if "graphics_low_enabled" in updates:
        updates["graphics_low_enabled"] = bool(updates["graphics_low_enabled"])
        updates["graphics_auto_enabled"] = updates["graphics_low_enabled"]
    if "graphics_quality_level" in updates:
        try:
            updates["graphics_quality_level"] = normalize_graphics_quality(updates["graphics_quality_level"])
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    if "auto_process_priority_enabled" in updates:
        updates["auto_process_priority_enabled"] = bool(updates["auto_process_priority_enabled"])
    if "process_priority" in updates:
        try:
            updates["process_priority"] = normalize_process_priority(updates["process_priority"])
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    if any(k in updates for k in ("cpu_limiter_enabled", "cpu_limiter_mode", "cpu_limiter_default_percent", "cpu_limiter_apply_all", "cpu_limiter_accounts")):
        try:
            normalized_cpu = _cpu_limiter_settings_from_config({
                "cpu_limiter_enabled": updates.get("cpu_limiter_enabled", cfg_mgr.get("cpu_limiter_enabled", False)),
                "cpu_limiter_mode": updates.get("cpu_limiter_mode", cfg_mgr.get("cpu_limiter_mode", "hard")),
                "cpu_limiter_default_percent": updates.get("cpu_limiter_default_percent", cfg_mgr.get("cpu_limiter_default_percent", 20)),
                "cpu_limiter_apply_all": updates.get("cpu_limiter_apply_all", cfg_mgr.get("cpu_limiter_apply_all", True)),
                "cpu_limiter_accounts": updates.get("cpu_limiter_accounts", cfg_mgr.get("cpu_limiter_accounts", {})),
            })
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        updates["cpu_limiter_enabled"] = normalized_cpu["enabled"]
        updates["cpu_limiter_mode"] = normalized_cpu["mode"]
        updates["cpu_limiter_default_percent"] = normalized_cpu["default_limit_percent"]
        updates["cpu_limiter_apply_all"] = normalized_cpu["apply_all"]
        if normalized_cpu["apply_all"]:
            normalized_cpu["accounts"] = {}
        updates["cpu_limiter_accounts"] = normalized_cpu["accounts"]
    if any(k in updates for k in ("roblox_window_resize_enabled", "roblox_window_size_preset", "roblox_window_width", "roblox_window_height", "roblox_window_resize_interval_seconds", "roblox_window_arrange_enabled", "roblox_window_arrange_columns", "roblox_window_arrange_gap", "roblox_window_arrange_margin")):
        try:
            normalized_window = _normalize_window_size_settings({
                "enabled": updates.get("roblox_window_resize_enabled", cfg_mgr.get("roblox_window_resize_enabled", False)),
                "preset": updates.get("roblox_window_size_preset", cfg_mgr.get("roblox_window_size_preset", "640x480")),
                "width": updates.get("roblox_window_width", cfg_mgr.get("roblox_window_width", 640)),
                "height": updates.get("roblox_window_height", cfg_mgr.get("roblox_window_height", 480)),
                "interval_seconds": updates.get("roblox_window_resize_interval_seconds", cfg_mgr.get("roblox_window_resize_interval_seconds", 10)),
                "arrange_enabled": updates.get("roblox_window_arrange_enabled", cfg_mgr.get("roblox_window_arrange_enabled", False)),
                "arrange_columns": updates.get("roblox_window_arrange_columns", cfg_mgr.get("roblox_window_arrange_columns", 6)),
                "arrange_gap": updates.get("roblox_window_arrange_gap", cfg_mgr.get("roblox_window_arrange_gap", 2)),
                "arrange_margin": updates.get("roblox_window_arrange_margin", cfg_mgr.get("roblox_window_arrange_margin", 0)),
            })
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        updates["roblox_window_resize_enabled"] = normalized_window["enabled"]
        updates["roblox_window_size_preset"] = normalized_window["preset"]
        updates["roblox_window_width"] = normalized_window["width"]
        updates["roblox_window_height"] = normalized_window["height"]
        updates["roblox_window_resize_interval_seconds"] = normalized_window["interval_seconds"]
        updates["roblox_window_arrange_enabled"] = normalized_window["arrange_enabled"]
        updates["roblox_window_arrange_columns"] = normalized_window["arrange_columns"]
        updates["roblox_window_arrange_gap"] = normalized_window["arrange_gap"]
        updates["roblox_window_arrange_margin"] = normalized_window["arrange_margin"]
    if "presence_api_enabled" in updates:
        updates["presence_api_enabled"] = bool(updates["presence_api_enabled"])
    if "popup_disconnected_enabled" in updates:
        updates["popup_disconnected_enabled"] = bool(updates["popup_disconnected_enabled"])
    if "presence_assist_rejoin_enabled" in updates:
        updates["presence_assist_rejoin_enabled"] = bool(updates["presence_assist_rejoin_enabled"])
    if "presence_poll_interval_seconds" in updates:
        updates["presence_poll_interval_seconds"] = _int_setting(updates["presence_poll_interval_seconds"], 30, 10, 300)
    if "presence_cache_ttl_seconds" in updates:
        updates["presence_cache_ttl_seconds"] = _int_setting(updates["presence_cache_ttl_seconds"], 30, 10, 300)
    if "multi_roblox_enabled" in updates:
        updates["multi_roblox_enabled"] = bool(updates["multi_roblox_enabled"])
        if not updates["multi_roblox_enabled"]:
            release_multi_roblox_guard()
    if "rt_rotation_enabled" in updates:
        updates["rt_rotation_enabled"] = bool(updates["rt_rotation_enabled"])
    if "game_place_id" in updates:
        updates["game_place_id"] = str(updates["game_place_id"] or "").strip()
    if "game_private_server_url" in updates:
        updates["game_private_server_url"] = str(updates["game_private_server_url"] or "").strip()
    if "auto_create_private_server_enabled" in updates:
        updates["auto_create_private_server_enabled"] = bool(updates["auto_create_private_server_enabled"])
    if "auto_create_private_server_free_only" in updates:
        updates["auto_create_private_server_free_only"] = bool(updates["auto_create_private_server_free_only"])
    cfg_mgr.update(updates)
    cfg_mgr.save()
    applied_defaults = 0
    if "game_place_id" in updates or "game_private_server_url" in updates:
        applied_defaults = _apply_game_defaults(farm._accounts, persist=True)
    if hasattr(farm, "apply_config_snapshot"):
        farm.apply_config_snapshot()
    return {"ok": True, "updated": list(updates.keys()), "game_defaults_applied": applied_defaults}


@app.get("/api/logs")
def api_logs(limit: int = 300):
    return {
        "ok": True,
        "path": LOG_FILE,
        "lines": _tail_log_lines(limit),
    }


@app.post("/api/logs/clear")
def api_clear_logs():
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "w", encoding="utf-8"):
            pass
    except Exception as e:
        raise HTTPException(500, f"clear log failed: {e}")
    return {"ok": True, "path": LOG_FILE, "lines": []}


@app.get("/api/troubleshoot/roblox-install")
def api_roblox_install_status():
    return ROBLOX_INSTALLER.status()


@app.post("/api/troubleshoot/roblox-install/uninstall")
def api_roblox_install_uninstall():
    return ROBLOX_INSTALLER.start_uninstall()


@app.post("/api/troubleshoot/roblox-install/latest")
def api_roblox_install_latest():
    return ROBLOX_INSTALLER.start_latest()


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

@app.get("/api/accounts")
def api_get_accounts():
    return _account_data_api_records()


@app.get("/api/accounts/avatars")
def api_account_avatars(user_ids: str = ""):
    ids: List[str] = []
    seen = set()
    for raw in re.split(r"[,\s]+", str(user_ids or "")):
        uid = raw.strip()
        if not uid.isdigit() or uid in seen:
            continue
        seen.add(uid)
        ids.append(uid)
        if len(ids) >= 100:
            break

    now = time.time()
    avatars: Dict[str, str] = {}
    missing: List[str] = []
    to_fetch: List[str] = []
    for uid in ids:
        cached = _AVATAR_CACHE.get(uid)
        if cached and (now - cached[0]) < _AVATAR_CACHE_TTL:
            avatars[uid] = cached[1]
        else:
            to_fetch.append(uid)

    if to_fetch:
        try:
            url = "https://thumbnails.roblox.com/v1/users/avatar-headshot?" + urllib.parse.urlencode({
                "userIds": ",".join(to_fetch),
                "size": "48x48",
                "format": "Png",
                "isCircular": "false",
            })
            req = urllib.request.Request(
                url,
                headers={"User-Agent": APP_USER_AGENT, "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            for item in payload.get("data", []) if isinstance(payload, dict) else []:
                uid = str(item.get("targetId") or "")
                image_url = str(item.get("imageUrl") or "")
                if uid and image_url:
                    avatars[uid] = image_url
                    _AVATAR_CACHE[uid] = (now, image_url)
        except Exception as exc:
            return {"ok": False, "avatars": avatars, "missing": to_fetch, "msg": str(exc)}

    for uid in ids:
        if uid not in avatars:
            missing.append(uid)
    return {"ok": True, "avatars": avatars, "missing": missing}


@app.post("/api/accounts")
async def api_set_accounts(request: Request):
    body = await request.json()
    if not isinstance(body, list):
        raise HTTPException(400, "Expected array")
    try:
        ACCOUNT_STORE.replace_from_roboguard_payload([dict(item) for item in body])
        count = _replace_farm_accounts_from_store()
    except Exception as e:
        raise HTTPException(400, f"Bad account payload: {e}")
    return {"ok": True, "count": count, "store": "AccountData.json"}


@app.get("/api/accounts/export")
def api_export_accounts():
    return {"ok": True, "accounts": _account_data_api_records(), "path": ACCOUNT_STORE.path}


@app.post("/api/accounts/import")
async def api_import_accounts(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected object")
    kind = str(body.get("kind") or body.get("type") or "auto").strip().lower()
    lines = body.get("lines") or body.get("text") or ""
    if isinstance(lines, str):
        line_list = [line.strip() for line in lines.splitlines() if line.strip()]
    elif isinstance(lines, list):
        line_list = [str(line).strip() for line in lines if str(line).strip()]
    else:
        line_list = []
    try:
        if kind in {"cookie", "cookies", "roblosecurity"}:
            result = ACCOUNT_STORE.import_cookie_lines(line_list, validator=_import_cookie_validator)
        elif kind in {"userpass", "user:pass", "login"}:
            result = ACCOUNT_STORE.import_userpass_lines(line_list, open_browser=True)
        elif kind in {"accountdata", "ram", "file"}:
            path = str(body.get("path") or "").strip()
            if not path or not os.path.exists(path):
                raise HTTPException(400, "path not found")
            with open(path, "rb") as f:
                records = ACCOUNT_STORE.decode_account_file_bytes(f.read())
            imported, merged = ACCOUNT_STORE.upsert_records(records)
            result = {"ok": True, "imported": imported, "count": len(merged)}
        elif kind in {"json", "accounts"} and isinstance(body.get("accounts"), list):
            imported, merged = ACCOUNT_STORE.upsert_records(body.get("accounts") or [])
            result = {"ok": True, "imported": imported, "count": len(merged)}
        else:
            raise HTTPException(400, "Unsupported import kind")
        count = _replace_farm_accounts_from_store()
        result["count"] = count
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/account/{username}/launch")
async def api_launch_account(username: str, request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        body = {}
    record = _find_account_record(username, include_cookie=True)
    if not record:
        raise HTTPException(404, "Account not found")
    blocked_reason = cookie_identity_block_reason(
        str(record.get("username") or username),
        str(record.get("cookie_username") or ""),
        bool(record.get("cookie_mismatch", False)),
    )
    if blocked_reason:
        result = {"ok": False, "fatal": True, "msg": blocked_reason, "blocked_reason": blocked_reason, "cookie_mismatch": True}
        audit_event("launch", username=username, ok=False, detail=blocked_reason, mode="blocked")
        return result
    multi_roblox = bool(body.get("multi_roblox", cfg_mgr.get("multi_roblox_enabled", True)))
    result = HybridLauncher.launch_record(record, target=_global_launch_target(body, record), multi_roblox=multi_roblox)
    audit_event("launch", username=username, ok=bool(result.get("ok")), detail=result.get("msg", ""), mode=result.get("mode", ""))
    if result.get("ok"):
        try:
            window_settings = _normalize_window_size_settings({})
            if window_settings["enabled"]:
                resize_result = ProcessManager.resize_roblox_windows(window_settings["width"], window_settings["height"])
                result["window_resize"] = {
                    "ok": bool(resize_result.get("ok", True)),
                    "resized": int(resize_result.get("resized") or 0),
                    "count": int(resize_result.get("count") or 0),
                    "width": window_settings["width"],
                    "height": window_settings["height"],
                }
        except Exception as exc:
            flog_kv("WINDOW", "manual_launch_resize_failed", "warning", account=username, error=str(exc))
        _replace_farm_accounts_from_store()
    return result


@app.post("/api/account/{username}/kill-duplicate")
def api_kill_duplicate(username: str):
    record = _find_account_record(username, include_cookie=False)
    if not record:
        raise HTTPException(404, "Account not found")
    tracker = str(record.get("browser_tracker_id") or "")
    result = HybridLauncher.kill_duplicate_instances(tracker)
    audit_event("kill_duplicate", username=username, ok=bool(result.get("ok")), killed=result.get("killed", []))
    return result

@app.post("/api/account/{username}/test-vip")
async def api_test_vip(username: str, request: Request):
    body = await request.json()
    vip_url = body.get("vip_url", "")
    if not vip_url:
        raise HTTPException(400, "vip_url required")
    place_id, link_code = ProcessManager.parse_vip_link(vip_url)
    if not place_id:
        return {"ok": False, "msg": "Cannot parse place_id"}
    if not link_code:
        return {"ok": False,
                "msg": "âš  No linkCode found â€” this link will join a PUBLIC server, not VIP!"}
    resolved = {}
    record = _find_account_record(username, include_cookie=True)
    if record and record.get("cookie"):
        resolved = resolve_vip_access_code(str(record.get("cookie") or ""), vip_url)
    return {
        "ok":        True,
        "place_id":  place_id,
        "link_code": f"{link_code[:6]}...{link_code[-4:]}",
        "vip_resolved": bool(resolved.get("ok")) if resolved else False,
        "access_code_present": bool(resolved.get("access_code")) if resolved else False,
        "url":       f"roblox://experiences/start?placeId={place_id}&linkCode=***",
        "msg":       "âœ… VIP link valid" + (" and accessCode resolved" if resolved.get("ok") else ""),
    }


@app.get("/api/game/place/{place_id}")
def api_game_place(place_id: str):
    return _lookup_roblox_place(place_id)

@app.post("/api/test-cookie")
async def api_test_cookie(request: Request):
    body = await request.json()
    cookie = body.get("cookie", "")
    if not cookie:
        raise HTTPException(400, "cookie required")
    ok, username, detail, meta = validate_cookie_details(cookie)
    return {"ok": ok, "username": username if ok else "", "user_id": meta.get("user_id", "") if ok else "", "msg": detail if not ok else ""}

@app.get("/api/vip-tracker/{username}")
def api_vip_tracker(username: str):
    acc = next((a for a in farm._accounts if a.username == username), None)
    if not acc:
        raise HTTPException(404, "Account not found")
    if not acc._vip_tracker:
        return {"ok": False, "msg": "No VipTracker (no VIP links)"}
    return {"ok": True, "links": acc._vip_tracker.status()}


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#  WEB UI â€” Argus Launcher
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
















@app.get("/", response_class=HTMLResponse)
def serve_ui():
    return HTMLResponse(
        HTML_UI,
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
    if not token or not secrets.compare_digest(token, INSTANCE_TOKEN):
        raise HTTPException(403, "Invalid shutdown token")

    def _shutdown():
        SHUTDOWN_REQUESTED.set()
        try:
            if farm.running:
                farm.stop()
        except Exception as exc:
            flog_kv("MAIN", "shutdown_stop_farm_failed", "warning", error=str(exc))
        try:
            release_multi_roblox_guard()
        except Exception:
            pass
        clear_instance_state()
        time.sleep(0.3)
        os._exit(0)

    threading.Thread(target=_shutdown, daemon=True, name="RoboGuardShutdown").start()
    return {"ok": True, "msg": "shutdown requested"}

if __name__ == "__main__":
    if "--multi-roblox-guard" in sys.argv:
        import multi_roblox_guard

        idx = sys.argv.index("--multi-roblox-guard")
        sys.argv = [sys.argv[0], *sys.argv[idx + 1:]]
        raise SystemExit(multi_roblox_guard.main())
    run_desktop(app, farm)

