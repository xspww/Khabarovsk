from __future__ import annotations

import ctypes
import os
import re
import sys
import threading
import time
import urllib.request
import webbrowser
from typing import Any, Optional, Tuple

from app_paths import APP_NAME, APP_ROOT_DIR
from desktop import console_output
from desktop.constants import DEFAULT_PORT
from console_activity import format_console_line
from core import LOG_FILE, flog, flog_kv
from desktop.backend_host import BackendHost
from desktop.webview_window import DesktopWindow
from desktop.console_icon import set_console_window_icon as _set_console_window_icon
from desktop.instance_guard import (
    INSTANCE_TOKEN as _INSTANCE_TOKEN,
    _acquire_instance_socket,
    _acquire_single_instance_mutex,
    _clear_instance_state,
    _find_free_port,
    _stop_previous_instance,
    _stop_same_app_processes,
    _write_instance_state,
    clear_instance_state,
)

BASE_DIR = APP_ROOT_DIR
HOST = "127.0.0.1"
PORT = DEFAULT_PORT
_SHUTDOWN_REQUESTED = threading.Event()
_app = None
_farm = None
_backend_host: Optional[BackendHost] = None
_STARTUP_PROGRESS_WIDTH = 44
_STARTUP_PROGRESS_TOTAL_STEPS = 6
_STARTUP_PROGRESS_FRAME_DELAY = 0.012
_STARTUP_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_STARTUP_PROGRESS_ACTIVE = False
_STARTUP_PROGRESS_LAST_LEN = 0
_STARTUP_PROGRESS_PERCENT = 0
_STARTUP_SPINNER_INDEX = 0
_STARTUP_CLEAR_AFTER_WINDOW_SHOW = False
_STARTUP_COLOR_SUPPORT: Optional[bool] = None
_COLOR_DIM = "\x1b[90m"
_COLOR_NAVY_BLUE = "\x1b[38;2;30;64;175m"
_COLOR_NAVY_TEXT = "\x1b[38;2;96;165;250m"


def _console_write(message: str = "") -> None:
    console_output.write_line(message, {"→": "->"})


def _console_write_inline(message: str = "") -> None:
    console_output.write_inline(message, {"→": "->", "█": "#", "░": "."}, _STARTUP_SPINNER_FRAMES)


def _startup_enable_virtual_terminal() -> bool:
    return console_output.enable_virtual_terminal(sys.stdout)


def _startup_colors_enabled() -> bool:
    global _STARTUP_COLOR_SUPPORT
    if not console_output.color_requested():
        return False
    if _STARTUP_COLOR_SUPPORT is None:
        _STARTUP_COLOR_SUPPORT = _startup_enable_virtual_terminal()
    return bool(_STARTUP_COLOR_SUPPORT)


def _startup_paint(text: str, color: str) -> str:
    return console_output.paint(text, color, enabled=_startup_colors_enabled())


def _startup_visible_len(text: str) -> int:
    return console_output.visible_len(text)


def _startup_progress_step(percent: int) -> int:
    pct = max(0, min(100, int(percent)))
    if pct >= 96:
        return 6
    if pct >= 88:
        return 5
    if pct >= 78:
        return 4
    if pct >= 55:
        return 3
    if pct >= 35:
        return 2
    return 1


def _startup_progress_line(percent: int, detail: str) -> str:
    global _STARTUP_SPINNER_INDEX
    try:
        pct = int(percent)
    except (TypeError, ValueError):
        pct = 0
    pct = max(0, min(100, pct))
    filled = int(round(_STARTUP_PROGRESS_WIDTH * (pct / 100.0)))
    bar = _startup_paint("█" * filled, _COLOR_NAVY_BLUE) + _startup_paint("░" * (_STARTUP_PROGRESS_WIDTH - filled), _COLOR_DIM)
    spinner = _STARTUP_SPINNER_FRAMES[_STARTUP_SPINNER_INDEX % len(_STARTUP_SPINNER_FRAMES)]
    _STARTUP_SPINNER_INDEX += 1
    detail_text = str(detail or "").strip() or "Starting"
    label = _startup_paint(detail_text, _COLOR_NAVY_TEXT)
    step = f"{_startup_progress_step(pct)}/{_STARTUP_PROGRESS_TOTAL_STEPS}"
    return f"{spinner} {label}  [{bar}] {step} {pct:3d}%"


def _render_startup_progress(percent: int, detail: str) -> None:
    global _STARTUP_PROGRESS_LAST_LEN
    line = _startup_progress_line(percent, detail)
    visible_len = _startup_visible_len(line)
    padding = " " * max(0, _STARTUP_PROGRESS_LAST_LEN - visible_len)
    _console_write_inline(f"\r{line}{padding}")
    _STARTUP_PROGRESS_LAST_LEN = visible_len


def _console_startup_progress(percent: int, detail: str) -> None:
    global _STARTUP_PROGRESS_ACTIVE, _STARTUP_PROGRESS_PERCENT
    try:
        target = int(percent)
    except (TypeError, ValueError):
        target = 0
    target = max(0, min(100, target))
    start = _STARTUP_PROGRESS_PERCENT if _STARTUP_PROGRESS_ACTIVE else target
    if _STARTUP_PROGRESS_ACTIVE and target > start:
        frame_count = min(10, max(3, target - start))
        for index in range(1, frame_count + 1):
            frame_percent = start + round((target - start) * (index / frame_count))
            _render_startup_progress(frame_percent, detail)
            if index < frame_count:
                time.sleep(_STARTUP_PROGRESS_FRAME_DELAY)
    else:
        _render_startup_progress(target, detail)
    _STARTUP_PROGRESS_ACTIVE = True
    _STARTUP_PROGRESS_PERCENT = target


def _console_clear_startup_screen() -> None:
    console_output.clear_screen()


def _console_finish_startup(*, clear: bool) -> None:
    global _STARTUP_PROGRESS_ACTIVE, _STARTUP_PROGRESS_LAST_LEN, _STARTUP_PROGRESS_PERCENT, _STARTUP_SPINNER_INDEX
    if _STARTUP_PROGRESS_ACTIVE:
        _console_write_inline("\r" + (" " * _STARTUP_PROGRESS_LAST_LEN) + "\r")
    _STARTUP_PROGRESS_ACTIVE = False
    _STARTUP_PROGRESS_LAST_LEN = 0
    _STARTUP_PROGRESS_PERCENT = 0
    _STARTUP_SPINNER_INDEX = 0
    if clear:
        _console_clear_startup_screen()


def _console_seal_inline_line() -> None:
    """Finish the current inline progress line cleanly.

    The progress line is written inline (no trailing newline), so a plain
    write after it would concatenate onto the same line ("100%Restarting").
    Sealing keeps the completed line and moves the cursor to the next line
    for whoever writes next (usually the swap script's own header).
    """
    global _STARTUP_PROGRESS_LAST_LEN
    if _STARTUP_PROGRESS_LAST_LEN > 0:
        _console_write_inline("\n")
        _STARTUP_PROGRESS_LAST_LEN = 0


def _console_clear_after_window_show(enabled: bool = True) -> None:
    global _STARTUP_CLEAR_AFTER_WINDOW_SHOW
    _STARTUP_CLEAR_AFTER_WINDOW_SHOW = bool(enabled)


def _console_finish_after_window_show() -> None:
    global _STARTUP_CLEAR_AFTER_WINDOW_SHOW
    clear = bool(_STARTUP_CLEAR_AFTER_WINDOW_SHOW)
    _STARTUP_CLEAR_AFTER_WINDOW_SHOW = False
    _console_finish_startup(clear=clear)


def _console_event(icon: str, message: str, *, indent: bool = False) -> None:
    _console_write(format_console_line(icon, message, indent=indent))


_UPDATE_BAR_WIDTH = 30
_UPDATE_PHASE_LABELS = {
    "starting": "Preparing update",
    "downloading": "Downloading",
    "verifying": "Verifying checksum",
    "restarting": "Restarting into the new version",
}


def _parse_update_percent(job: dict) -> Optional[int]:
    try:
        match = re.search(r"(\d{1,3})\s*%", str((job or {}).get("progress") or ""))
        if match:
            return max(0, min(100, int(match.group(1))))
    except Exception:
        pass
    return None


def _render_update_progress(percent: int, label: str) -> None:
    """Single-line updater progress reusing the boot bar style (no step x/6)."""
    # Mark the progress as active so _console_finish_startup() actually
    # erases this line later; without this the clear was skipped and the
    # next message concatenated onto it ("100%Restarting into...").
    global _STARTUP_PROGRESS_LAST_LEN, _STARTUP_SPINNER_INDEX, _STARTUP_PROGRESS_ACTIVE
    try:
        pct = max(0, min(100, int(percent)))
    except (TypeError, ValueError):
        pct = 0
    filled = int(round(_UPDATE_BAR_WIDTH * (pct / 100.0)))
    bar = _startup_paint("█" * filled, _COLOR_NAVY_BLUE) + _startup_paint("░" * (_UPDATE_BAR_WIDTH - filled), _COLOR_DIM)
    spinner = _STARTUP_SPINNER_FRAMES[_STARTUP_SPINNER_INDEX % len(_STARTUP_SPINNER_FRAMES)]
    _STARTUP_SPINNER_INDEX += 1
    text = _startup_paint(str(label or "Updating"), _COLOR_NAVY_TEXT)
    line = f"{spinner} {text}  [{bar}] {pct:3d}%"
    visible_len = _startup_visible_len(line)
    padding = " " * max(0, _STARTUP_PROGRESS_LAST_LEN - visible_len)
    _console_write_inline(f"\r{line}{padding}")
    _STARTUP_PROGRESS_ACTIVE = True
    _STARTUP_PROGRESS_LAST_LEN = visible_len


def _console_status(label: str, detail: str) -> None:
    label_key = str(label or "").strip().lower()
    detail_text = str(detail or "").strip()
    if label_key == "startup":
        if "existing" in detail_text.lower():
            _console_startup_progress(18, detail_text)
        else:
            _console_startup_progress(8, detail_text or "Preparing startup")
        return
    if label_key == "port":
        _console_startup_progress(35, detail_text or "Selecting local port")
        return
    if label_key == "backend":
        if detail_text.lower().startswith("not ready"):
            _console_finish_startup(clear=False)
            _console_event("XX", f"Cronus backend not ready: {detail_text.replace('Not ready:', '', 1).strip()}")
        elif detail_text.lower().startswith("ready"):
            _console_startup_progress(78, detail_text)
        else:
            _console_startup_progress(55, detail_text or "Starting FastAPI server")
        return
    if label_key == "dashboard":
        _console_startup_progress(88, detail_text or "Dashboard ready")
        return
    if label_key == "desktop":
        _console_startup_progress(96, detail_text or "Opening desktop window")
        return
    if label_key == "shutdown":
        return
    if label_key == "log":
        return
    return


def _console_header() -> None:
    os.environ.setdefault("CRONUS_CONSOLE_ACTIVITY", "1")
    os.environ.setdefault("CRONUS_CONSOLE_COLOR", "1")
    try:
        if os.name == "nt":
            ctypes.windll.kernel32.SetConsoleTitleW(f"{APP_NAME} Console")
            _set_console_window_icon()
    except Exception:
        pass
    return


def configure(fastapi_app: Any, farm_controller: Any) -> None:
    global _app, _farm
    _app = fastapi_app
    _farm = farm_controller


def _require_configured() -> Tuple[Any, Any]:
    if _app is None or _farm is None:
        raise RuntimeError("desktop_host is not configured")
    return _app, _farm


BOOT_FARM_DELAY_SECONDS = 30.0


def _autostart_api(path: str, method: str = "GET", body: Any = None) -> Any:
    import json as _json

    data = _json.dumps(body or {}).encode("utf-8") if body is not None else None
    # Same-process loopback calls must carry the API token: the middleware
    # rejects every mutating POST /api/* without X-Cronus-Token (403), which
    # used to make the startup update prompt and --autostart updates fail
    # silently and fall back to a normal boot with the update button shown.
    req = urllib.request.Request(
        f"http://{HOST}:{PORT}{path}",
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Cronus-Token": str(_INSTANCE_TOKEN or ""),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            return _json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return {"ok": False, "msg": f"local api failed: {exc}"}


def _run_autostart_chain() -> None:
    """Boot maintenance: heal the Startup shortcut whenever start_on_boot
    is enabled, then (only for --autostart launches) auto update, delayed
    single-attempt farm start. Never prompts, only logs."""
    try:
        _, farm = _require_configured()
        cfg = farm.cfg_mgr
    except Exception as exc:
        flog_kv("BOOT", "autostart_no_config", "warning", error=str(exc))
        return
    if "--autostart" in sys.argv:
        flog_kv("BOOT", "autostart_begin")
    else:
        flog_kv("BOOT", "manual_boot")
    if _SHUTDOWN_REQUESTED.is_set():
        return
    if bool(cfg.get("start_on_boot", False)):
        try:
            from services import startup_manager
            import app_paths

            res = startup_manager.heal_shortcut(str(app_paths.EXECUTABLE_PATH or ""))
            flog_kv("BOOT", "startup_healed", changed=bool(res.get("changed")), msg=str(res.get("msg") or ""))
        except Exception as exc:
            flog_kv("BOOT", "startup_heal_failed", "warning", error=str(exc))
    if "--autostart" not in sys.argv:
        flog_kv("BOOT", "autostart_done", mode="manual")
        return
    if bool(cfg.get("auto_update_on_boot", False)):
        try:
            snap = _autostart_api("/api/update/check")
            if isinstance(snap, dict) and snap.get("update_available") and snap.get("latest_version"):
                version = str(snap.get("latest_version") or "")
                flog_kv("BOOT", "auto_update_applying", version=version)
                applied = _autostart_api("/api/update/apply", "POST", {"confirm_stop_farm": True})
                if isinstance(applied, dict) and applied.get("accepted"):
                    flog_kv("BOOT", "auto_update_restarting", version=version)
                    return
                reason = str((applied or {}).get("msg") or "not accepted") if isinstance(applied, dict) else "not accepted"
                flog_kv("BOOT", "auto_update_skipped", "warning", msg=reason)
            else:
                flog_kv("BOOT", "auto_update_uptodate")
        except Exception as exc:
            flog_kv("BOOT", "auto_update_failed", "warning", error=str(exc))
    if bool(cfg.get("start_farming_on_boot", False)):
        if _SHUTDOWN_REQUESTED.wait(BOOT_FARM_DELAY_SECONDS):
            return
        try:
            if getattr(farm, "running", False):
                flog_kv("BOOT", "farm_already_running")
                return
            farm.start()
            flog_kv("BOOT", "farm_started")
        except Exception as exc:
            flog_kv("BOOT", "farm_start_failed", "error", error=str(exc))
    flog_kv("BOOT", "autostart_done")


def _maybe_prompt_startup_update() -> bool:
    """Blocking Yes/No prompt when a newer app version exists at boot.

    Shows a native Windows dialog like:
      [Update Available]
      A new version is available!
      Current: vX  New: vY
      Would you like to update now?  [Yes] [No]

    Returns True when the user accepted and an update is now running
    (the AppUpdater relaunches the app itself, so the caller must NOT
    open the desktop window). Returns False to boot normally.
    """
    if "--post-update" in sys.argv:
        return False
    # Source checkouts cannot replace their running Python files. Keep the
    # update notice in the dashboard instead of prompting at every startup.
    try:
        import app_paths

        if not app_paths.IS_COMPILED:
            return False
    except Exception:
        return False
    try:
        _, farm = _require_configured()
        cfg = farm.cfg_mgr
    except Exception:
        return False
    try:
        if "--autostart" in sys.argv and bool(cfg.get("auto_update_on_boot", False)):
            return False
    except Exception:
        pass
    # The GitHub check used to run synchronously here (up to 20s on a bad
    # link), stalling every launch before the window even opened. Run it in
    # a thread and give up fast: a slow check just boots normally and the
    # dashboard button surfaces the update when its own poll completes.
    try:
        from services.app_version_check import check_app_update

        _box: dict = {}

        def _check() -> None:
            try:
                _box["snap"] = check_app_update()
            except Exception as exc:  # noqa: BLE001 - reported below
                _box["error"] = exc

        _checker = threading.Thread(target=_check, daemon=True, name="CronusStartupUpdateCheck")
        _checker.start()
        _checker.join(timeout=8.0)
        if _checker.is_alive():
            flog_kv("UPDATE", "startup_check_slow_skipped", "warning", timeout_s=8.0)
            return False
        if "error" in _box:
            raise _box["error"]
        snap = _box.get("snap")
    except Exception as exc:
        flog_kv("UPDATE", "startup_check_failed", "warning", error=str(exc))
        return False
    if not isinstance(snap, dict):
        return False
    if not snap.get("update_available"):
        return False
    latest = str(snap.get("latest_version") or "").strip()
    if not latest:
        return False
    current = str(snap.get("current_version") or "").strip()
    cur_label = f"v{current}" if current and not current.lower().startswith("v") else (current or "unknown")
    new_label = f"v{latest}" if not latest.lower().startswith("v") else latest
    text = (
        "A new version is available!\n"
        f"Current: {cur_label}\n"
        f"New: {new_label}\n"
        "\n"
        "Would you like to update now?"
    )
    try:
        choice = ctypes.windll.user32.MessageBoxW(
            None,
            text,
            "Update Available",
            0x00000004 | 0x00000040,  # MB_YESNO | MB_ICONINFORMATION
        )
    except Exception as exc:
        flog_kv("UPDATE", "startup_prompt_failed", "warning", error=str(exc))
        return False
    if int(choice or 0) != 6:  # IDYES
        flog_kv("UPDATE", "startup_prompt_declined", version=latest)
        return False
    flog_kv("UPDATE", "startup_prompt_accepted", version=latest)
    try:
        applied = _autostart_api("/api/update/apply", "POST", {"confirm_stop_farm": True})
    except Exception as exc:
        flog_kv("UPDATE", "startup_apply_failed", "warning", error=str(exc))
        return False
    if not isinstance(applied, dict) or not applied.get("accepted"):
        reason = str((applied or {}).get("msg") or "not accepted") if isinstance(applied, dict) else "not accepted"
        flog_kv("UPDATE", "startup_apply_rejected", "warning", msg=reason)
        return False
    # End the boot progress bar first so the update status starts on its own
    # clean line (it used to jam onto the bar: "88%Updating to vX...").
    # No separate "Updating to vX..." line here: the progress line below
    # carries the version ("Downloading v2.3.8 ..."), and the swap script
    # prints its own single "> updating to vX..." header when it takes
    # over the console.
    _console_finish_startup(clear=False)
    _restart_announced = False
    try:
        while True:
            time.sleep(0.5)
            try:
                snap_status = _autostart_api("/api/update/status")
            except Exception:
                continue
            job = snap_status.get("job") if isinstance(snap_status, dict) else None
            if not isinstance(job, dict):
                continue
            if not job.get("active"):
                _console_finish_startup(clear=False)
                if job.get("state") == "failed":
                    err = str(job.get("error") or job.get("msg") or "update failed")
                    flog_kv("UPDATE", "startup_update_failed", "error", error=err)
                    try:
                        ctypes.windll.user32.MessageBoxW(
                            None,
                            f"Update failed:\n{err}",
                            "Update Available",
                            0x00000000 | 0x00000010,  # MB_OK | MB_ICONERROR
                        )
                    except Exception:
                        pass
                    return False
                _console_write(f"Updated to v{latest} - launching...")
                return True
            phase = str(job.get("state") or "updating").strip().lower() or "updating"
            if phase == "restarting":
                # The swap script owns the console from here on (it prints
                # "> updating to vX..." and each phase step); just finish
                # the progress line cleanly instead of announcing a dupe.
                if not _restart_announced:
                    _restart_announced = True
                    _console_seal_inline_line()
                continue
            label = _UPDATE_PHASE_LABELS.get(phase, phase.replace("_", " ").capitalize())
            if latest and phase in {"downloading", "verifying"}:
                label = f"{label} v{latest}"
            pct = _parse_update_percent(job)
            if pct is None:
                pct = 100 if phase == "verifying" else 0
            # Render every tick so the spinner keeps moving even while the
            # backend reports KB counts instead of percents.
            _render_update_progress(pct, label)
    except Exception:
        return True
    return True


def run_desktop(fastapi_app: Any = None, farm_controller: Any = None):
    if fastapi_app is not None or farm_controller is not None:
        configure(fastapi_app, farm_controller)
    global PORT, _backend_host
    _console_header()
    _console_status("startup", "Preparing single-instance guard")
    if "--post-update" in sys.argv:
        _console_status("startup", "Post-update boot: sweeping leftover instances")
        flog_kv("MAIN", "post_update_boot", argv=" ".join(sys.argv[1:]))
        try:
            from desktop import instance_guard as _ig

            def _leftover_app_process() -> Optional[int]:
                pid = _ig._find_same_app_process()
                if pid:
                    try:
                        import psutil

                        cmd = " ".join(psutil.Process(int(pid)).cmdline() or []).lower()
                        if "cronus_updater_" in cmd:
                            return None
                    except Exception:
                        pass
                    return pid
                return None

            for _ in range(40):
                _stop_previous_instance()
                _stop_same_app_processes()
                if _leftover_app_process() is None and _ig._find_existing_dashboard(DEFAULT_PORT) is None:
                    break
                time.sleep(0.5)
        except Exception as exc:
            flog_kv("MAIN", "post_update_sweep_failed", "warning", error=str(exc))
    else:
        _stop_previous_instance()
        _stop_same_app_processes()
    mutex_ok = _acquire_single_instance_mutex()
    socket_ok = _acquire_instance_socket()
    if (not mutex_ok) or (not socket_ok):
        _console_status("startup", "Existing Cronus instance detected; requesting cleanup")
        _stop_previous_instance()
        _stop_same_app_processes()
        mutex_ok = _acquire_single_instance_mutex()
        if not socket_ok:
            socket_ok = _acquire_instance_socket()
    if not mutex_ok:
        flog_kv("MAIN", "duplicate_instance_refused", "error", socket_ok=socket_ok)
        _console_status("shutdown", "Another Cronus instance is running; close it first")
        return
    PORT = _find_free_port(DEFAULT_PORT)
    _console_status("port", f"Selected http://{HOST}:{PORT}")
    _write_instance_state(PORT)
    _console_status("backend", "Starting FastAPI server")
    app, farm = _require_configured()
    _backend_host = BackendHost(HOST, PORT)
    _backend_host.configure(app, PORT)
    server_thread = _backend_host.start()
    ready, detail = _backend_host.wait_ready(server_thread)
    if ready:
        flog(f"[MAIN] FastAPI ready on http://{HOST}:{PORT}")
        _console_status("backend", f"Ready ({detail})")
        _console_status("dashboard", f"http://{HOST}:{PORT}")
    else:
        flog_kv("MAIN", "fastapi_not_ready", "error", port=PORT, detail=detail)
        _console_status("backend", f"Not ready: {detail}")
        _console_status("log", LOG_FILE)
    if ready and _maybe_prompt_startup_update():
        # User hit Yes and the updater took over: it downloads, swaps the
        # exe, and os._exit()s into the new version. Park here instead of
        # opening the desktop window; if the update failed the prompt
        # returns False and we boot normally below.
        try:
            while not _SHUTDOWN_REQUESTED.wait(1.0):
                pass
        except KeyboardInterrupt:
            pass
        return
    threading.Thread(target=_run_autostart_chain, daemon=True, name="CronusAutostart").start()
    _console_status("desktop", "Opening desktop window (WebView2)")
    _console_clear_after_window_show(ready)
    try:
        window = DesktopWindow()
        if window.run(
            f"http://{HOST}:{PORT}",
            farm,
            _SHUTDOWN_REQUESTED,
            on_first_show=_console_finish_after_window_show,
        ):
            _console_status("shutdown", "Cronus window closed")
            return
    except Exception as exc:
        flog_kv("MAIN", "desktop_webview_failed", "warning", error=str(exc))
    _console_status("desktop", "Desktop window unavailable; opening browser fallback")
    _console_clear_after_window_show(False)
    _console_finish_startup(clear=ready)
    webbrowser.open(f"http://{HOST}:{PORT}")
    try:
        while not _SHUTDOWN_REQUESTED.wait(1.0):
            pass
    except KeyboardInterrupt:
        _console_status("shutdown", "Ctrl+C received; stopping farm")
        _, farm = _require_configured()
        if farm.running:
            farm.stop()
        _clear_instance_state()
        sys.exit(0)


INSTANCE_TOKEN = _INSTANCE_TOKEN
SHUTDOWN_REQUESTED = _SHUTDOWN_REQUESTED
