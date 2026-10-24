from __future__ import annotations

import ctypes
import os
import re
import sys
import threading
import time
import urllib.request
import webbrowser
from typing import Any

from core import LOG_FILE, flog, flog_kv
from desktop import console_output
from desktop.backend_host import BackendHost
from desktop.console_icon import set_console_window_icon as _set_console_window_icon
from desktop.constants import DEFAULT_PORT
from desktop.instance_guard import (
    INSTANCE_TOKEN as _INSTANCE_TOKEN,
)
from desktop.instance_guard import (
    _acquire_instance_socket,
    _acquire_single_instance_mutex,
    _clear_instance_state,
    _find_free_port,
    _stop_previous_instance,
    _stop_same_app_processes,
    _write_instance_state,
)
from desktop.instance_guard import (
    clear_instance_state as clear_instance_state,
)
from desktop.webview_window import DesktopWindow
from infra.app_paths import APP_NAME, APP_ROOT_DIR, IS_COMPILED
from infra.console_activity import format_console_line

BASE_DIR = APP_ROOT_DIR
HOST = "127.0.0.1"
PORT = DEFAULT_PORT
_SHUTDOWN_REQUESTED = threading.Event()
_STARTUP_PROGRESS_WIDTH = 44
_STARTUP_PROGRESS_TOTAL_STEPS = 6
_STARTUP_PROGRESS_FRAME_DELAY = 0.012
_STARTUP_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_COLOR_DIM = "\x1b[90m"
_COLOR_NAVY_BLUE = "\x1b[38;2;30;64;175m"
_COLOR_NAVY_TEXT = "\x1b[38;2;96;165;250m"


class _HostState:
    def __init__(self) -> None:
        self.app: Any = None
        self.farm: Any = None
        self.backend: BackendHost | None = None
        self.port = DEFAULT_PORT

    def configure(self, fastapi_app: Any, farm_controller: Any) -> None:
        self.app = fastapi_app
        self.farm = farm_controller

    def require_configured(self) -> tuple[Any, Any]:
        if self.app is None or self.farm is None:
            raise RuntimeError("desktop_host is not configured")
        return self.app, self.farm


class _StartupProgress:
    def __init__(self) -> None:
        self.active = False
        self.last_len = 0
        self.percent = 0
        self.spinner_index = 0
        self.clear_after_window_show = False
        self.color_support: bool | None = None

    def colors_enabled(self) -> bool:
        if not console_output.color_requested():
            return False
        if self.color_support is None:
            self.color_support = _startup_enable_virtual_terminal()
        return bool(self.color_support)

    def progress_line(self, percent: int, detail: str) -> str:
        try:
            pct = int(percent)
        except (TypeError, ValueError):
            pct = 0
        pct = max(0, min(100, pct))
        filled = int(round(_STARTUP_PROGRESS_WIDTH * (pct / 100.0)))
        bar = _startup_paint("█" * filled, _COLOR_NAVY_BLUE) + _startup_paint(
            "░" * (_STARTUP_PROGRESS_WIDTH - filled), _COLOR_DIM
        )
        spinner = _STARTUP_SPINNER_FRAMES[
            self.spinner_index % len(_STARTUP_SPINNER_FRAMES)
        ]
        self.spinner_index += 1
        detail_text = str(detail or "").strip() or "Starting"
        label = _startup_paint(detail_text, _COLOR_NAVY_TEXT)
        step = f"{_startup_progress_step(pct)}/{_STARTUP_PROGRESS_TOTAL_STEPS}"
        return f"{spinner} {label}  [{bar}] {step} {pct:3d}%"

    def render(self, percent: int, detail: str) -> None:
        line = self.progress_line(percent, detail)
        visible_len = _startup_visible_len(line)
        padding = " " * max(0, self.last_len - visible_len)
        _console_write_inline(f"\r{line}{padding}")
        self.last_len = visible_len

    def advance(self, percent: int, detail: str) -> None:
        try:
            target = int(percent)
        except (TypeError, ValueError):
            target = 0
        target = max(0, min(100, target))
        start = self.percent if self.active else target
        if self.active and target > start:
            frame_count = min(10, max(3, target - start))
            for index in range(1, frame_count + 1):
                frame_percent = start + round(
                    (target - start) * (index / frame_count)
                )
                self.render(frame_percent, detail)
                if index < frame_count:
                    time.sleep(_STARTUP_PROGRESS_FRAME_DELAY)
        else:
            self.render(target, detail)
        self.active = True
        self.percent = target

    def finish(self, *, clear: bool) -> None:
        if self.active:
            _console_write_inline("\r" + (" " * self.last_len) + "\r")
        self.active = False
        self.last_len = 0
        self.percent = 0
        self.spinner_index = 0
        if clear:
            _console_clear_startup_screen()

    def seal_inline_line(self) -> None:
        if self.last_len > 0:
            _console_write_inline("\n")
            self.last_len = 0

    def set_clear_after_window_show(self, enabled: bool = True) -> None:
        self.clear_after_window_show = bool(enabled)

    def finish_after_window_show(self) -> None:
        clear = bool(self.clear_after_window_show)
        self.clear_after_window_show = False
        self.finish(clear=clear)

    def render_update(self, percent: int, label: str) -> None:
        try:
            pct = max(0, min(100, int(percent)))
        except (TypeError, ValueError):
            pct = 0
        filled = int(round(_UPDATE_BAR_WIDTH * (pct / 100.0)))
        bar = _startup_paint("█" * filled, _COLOR_NAVY_BLUE) + _startup_paint(
            "░" * (_UPDATE_BAR_WIDTH - filled), _COLOR_DIM
        )
        spinner = _STARTUP_SPINNER_FRAMES[
            self.spinner_index % len(_STARTUP_SPINNER_FRAMES)
        ]
        self.spinner_index += 1
        text = _startup_paint(str(label or "Updating"), _COLOR_NAVY_TEXT)
        line = f"{spinner} {text}  [{bar}] {pct:3d}%"
        visible_len = _startup_visible_len(line)
        padding = " " * max(0, self.last_len - visible_len)
        _console_write_inline(f"\r{line}{padding}")
        self.active = True
        self.last_len = visible_len


_HOST = _HostState()
_STARTUP = _StartupProgress()


def _console_write(message: str = "") -> None:
    console_output.write_line(message, {"→": "->"})


def _console_write_inline(message: str = "") -> None:
    console_output.write_inline(message, {"→": "->", "█": "#", "░": "."}, _STARTUP_SPINNER_FRAMES)


def _startup_enable_virtual_terminal() -> bool:
    return console_output.enable_virtual_terminal(sys.stdout)


def _startup_colors_enabled() -> bool:
    return _STARTUP.colors_enabled()


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
    return _STARTUP.progress_line(percent, detail)


def _render_startup_progress(percent: int, detail: str) -> None:
    _STARTUP.render(percent, detail)


def _console_startup_progress(percent: int, detail: str) -> None:
    _STARTUP.advance(percent, detail)


def _console_clear_startup_screen() -> None:
    console_output.clear_screen()


def _console_finish_startup(*, clear: bool) -> None:
    _STARTUP.finish(clear=clear)


def _console_seal_inline_line() -> None:
    """Finish the current inline progress line cleanly.

    The progress line is written inline (no trailing newline), so a plain
    write after it would concatenate onto the same line ("100%Restarting").
    Sealing keeps the completed line and moves the cursor to the next line
    for whoever writes next (usually the swap script's own header).
    """
    _STARTUP.seal_inline_line()


def _console_clear_after_window_show(enabled: bool = True) -> None:
    _STARTUP.set_clear_after_window_show(enabled)


def _console_finish_after_window_show() -> None:
    _STARTUP.finish_after_window_show()


def _console_event(icon: str, message: str, *, indent: bool = False) -> None:
    _console_write(format_console_line(icon, message, indent=indent))


_UPDATE_BAR_WIDTH = 30
_UPDATE_PHASE_LABELS = {
    "starting": "Preparing update",
    "downloading": "Downloading",
    "verifying": "Verifying checksum",
    "restarting": "Restarting into the new version",
}


def _parse_update_percent(job: dict) -> int | None:
    try:
        match = re.search(r"(\d{1,3})\s*%", str((job or {}).get("progress") or ""))
        if match:
            return max(0, min(100, int(match.group(1))))
    except Exception:
        "intentional: ignore exception"
    return None


def _render_update_progress(percent: int, label: str) -> None:
    """Single-line updater progress reusing the boot bar style (no step x/6)."""
    _STARTUP.render_update(percent, label)


_STATUS_PROGRESS = {
    "port": (35, "Selecting local port"),
    "dashboard": (88, "Dashboard ready"),
    "desktop": (96, "Opening desktop window"),
}


def _status_startup(detail_text: str) -> None:
    if "existing" in detail_text.lower():
        _console_startup_progress(18, detail_text)
    else:
        _console_startup_progress(8, detail_text or "Preparing startup")


def _status_backend(detail_text: str) -> None:
    if detail_text.lower().startswith("not ready"):
        _console_finish_startup(clear=False)
        _console_event(
            "XX",
            f"Cronus backend not ready: {detail_text.replace('Not ready:', '', 1).strip()}",
        )
    elif detail_text.lower().startswith("ready"):
        _console_startup_progress(78, detail_text)
    else:
        _console_startup_progress(55, detail_text or "Starting FastAPI server")


def _status_fixed(label_key: str, detail_text: str) -> None:
    entry = _STATUS_PROGRESS.get(label_key)
    if entry is None:
        return
    percent, fallback = entry
    _console_startup_progress(percent, detail_text or fallback)


def _console_status(label: str, detail: str) -> None:
    label_key = str(label or "").strip().lower()
    detail_text = str(detail or "").strip()
    if label_key == "startup":
        _status_startup(detail_text)
        return
    if label_key == "backend":
        _status_backend(detail_text)
        return
    _status_fixed(label_key, detail_text)


def _console_header() -> None:
    os.environ.setdefault("CRONUS_CONSOLE_ACTIVITY", "1")
    os.environ.setdefault("CRONUS_CONSOLE_COLOR", "1")
    try:
        if os.name == "nt":
            ctypes.windll.kernel32.SetConsoleTitleW(f"{APP_NAME} Console")
            _set_console_window_icon()
    except Exception:
        "intentional: ignore exception"
    return


def configure(fastapi_app: Any, farm_controller: Any) -> None:
    _HOST.configure(fastapi_app, farm_controller)


def _require_configured() -> tuple[Any, Any]:
    return _HOST.require_configured()


BOOT_FARM_DELAY_SECONDS = 30.0


def _autostart_api(path: str, method: str = "GET", body: Any = None) -> Any:
    import json as _json

    data = _json.dumps(body or {}).encode("utf-8") if body is not None else None
    # Same-process loopback calls must carry the API token: the middleware
    # rejects every mutating POST /api/* without X-Cronus-Token (403), which
    # used to make the startup update prompt and --autostart updates fail
    # silently and fall back to a normal boot with the update button shown.
    req = urllib.request.Request(
        f"http://{HOST}:{_HOST.port}{path}",
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


def _heal_startup_shortcut(cfg: Any) -> None:
    if not bool(cfg.get("start_on_boot", False)):
        return
    try:
        from infra import app_paths
        from services import startup_manager

        res = startup_manager.heal_shortcut(str(app_paths.EXECUTABLE_PATH or ""))
        flog_kv(
            "BOOT",
            "startup_healed",
            changed=bool(res.get("changed")),
            msg=str(res.get("msg") or ""),
        )
    except Exception as exc:
        flog_kv("BOOT", "startup_heal_failed", "warning", error=str(exc))


def _update_skip_reason(applied: Any) -> str:
    if not isinstance(applied, dict):
        return "not accepted"
    return str(applied.get("msg") or "not accepted")


def _autostart_update(cfg: Any) -> bool:
    if not bool(cfg.get("auto_update_on_boot", False)):
        return False
    try:
        snap = _autostart_api("/api/update/check")
        if (
            not isinstance(snap, dict)
            or not snap.get("update_available")
            or not snap.get("latest_version")
        ):
            flog_kv("BOOT", "auto_update_uptodate")
            return False
        version = str(snap.get("latest_version") or "")
        flog_kv("BOOT", "auto_update_applying", version=version)
        applied = _autostart_api(
            "/api/update/apply", "POST", {"confirm_stop_farm": True}
        )
        if isinstance(applied, dict) and applied.get("accepted"):
            flog_kv("BOOT", "auto_update_restarting", version=version)
            return True
        reason = _update_skip_reason(applied)
        flog_kv("BOOT", "auto_update_skipped", "warning", msg=reason)
    except Exception as exc:
        flog_kv("BOOT", "auto_update_failed", "warning", error=str(exc))
    return False


def _autostart_start_farm(farm: Any, cfg: Any) -> bool:
    if not bool(cfg.get("start_farming_on_boot", False)):
        return True
    if _SHUTDOWN_REQUESTED.wait(BOOT_FARM_DELAY_SECONDS):
        return False
    try:
        if getattr(farm, "running", False):
            flog_kv("BOOT", "farm_already_running")
            return False
        farm.start()
        flog_kv("BOOT", "farm_started")
    except Exception as exc:
        flog_kv("BOOT", "farm_start_failed", "error", error=str(exc))
    return True


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
    _heal_startup_shortcut(cfg)
    if "--autostart" not in sys.argv:
        flog_kv("BOOT", "autostart_done", mode="manual")
        return
    if _autostart_update(cfg):
        return
    if _autostart_start_farm(farm, cfg):
        flog_kv("BOOT", "autostart_done")


def _app_is_compiled() -> bool:
    try:
        from infra import app_paths

        return bool(app_paths.IS_COMPILED)
    except Exception:
        return False


def _startup_prompt_skipped(cfg: Any) -> bool:
    try:
        return "--autostart" in sys.argv and bool(
            cfg.get("auto_update_on_boot", False)
        )
    except Exception:
        return False


def _check_startup_update() -> dict | None:
    box: dict = {}
    try:
        from services.app_version_check import check_app_update

        def _check() -> None:
            try:
                box["snap"] = check_app_update()
            except Exception as exc:
                box["error"] = exc

        checker = threading.Thread(
            target=_check, daemon=True, name="CronusStartupUpdateCheck"
        )
        checker.start()
        checker.join(timeout=8.0)
        if checker.is_alive():
            flog_kv(
                "UPDATE",
                "startup_check_slow_skipped",
                "warning",
                timeout_s=8.0,
            )
            return None
        if "error" in box:
            raise box["error"]
        snap = box.get("snap")
    except Exception as exc:
        flog_kv("UPDATE", "startup_check_failed", "warning", error=str(exc))
        return None
    return snap if isinstance(snap, dict) else None


def _version_label(version: str, fallback: str = "") -> str:
    if version and not version.lower().startswith("v"):
        return f"v{version}"
    return version or fallback


def _update_prompt_text(cur_label: str, new_label: str) -> str:
    return (
        "A new version is available!\n"
        f"Current: {cur_label}\n"
        f"New: {new_label}\n"
        "\n"
        "Would you like to update now?"
    )


def _ask_update_prompt(text: str) -> int | None:
    try:
        choice = ctypes.windll.user32.MessageBoxW(
            None,
            text,
            "Update Available",
            0x00000004 | 0x00000040,  # MB_YESNO | MB_ICONINFORMATION
        )
    except Exception as exc:
        flog_kv("UPDATE", "startup_prompt_failed", "warning", error=str(exc))
        return None
    return int(choice or 0)


def _confirm_startup_update(text: str, latest: str) -> bool:
    choice = _ask_update_prompt(text)
    if choice is None:
        return False
    if choice != 6:  # IDYES
        flog_kv("UPDATE", "startup_prompt_declined", version=latest)
        return False
    flog_kv("UPDATE", "startup_prompt_accepted", version=latest)
    return True


def _start_update() -> bool:
    try:
        applied = _autostart_api(
            "/api/update/apply", "POST", {"confirm_stop_farm": True}
        )
    except Exception as exc:
        flog_kv("UPDATE", "startup_apply_failed", "warning", error=str(exc))
        return False
    if not isinstance(applied, dict) or not applied.get("accepted"):
        flog_kv(
            "UPDATE",
            "startup_apply_rejected",
            "warning",
            msg=_update_skip_reason(applied),
        )
        return False
    return True


def _update_phase(job: dict) -> str:
    return str(job.get("state") or "updating").strip().lower() or "updating"


def _update_phase_label(phase: str, latest: str) -> str:
    label = _UPDATE_PHASE_LABELS.get(
        phase, phase.replace("_", " ").capitalize()
    )
    if latest and phase in {"downloading", "verifying"}:
        return f"{label} v{latest}"
    return label


def _update_progress_args(
    job: dict, phase: str, latest: str
) -> tuple[int, str]:
    label = _update_phase_label(phase, latest)
    pct = _parse_update_percent(job)
    if pct is None:
        pct = 100 if phase == "verifying" else 0
    return pct, label


def _update_error_message(job: dict) -> str:
    return str(job.get("error") or job.get("msg") or "update failed")


def _show_update_failure(error: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            f"Update failed:\n{error}",
            "Update Available",
            0x00000000 | 0x00000010,  # MB_OK | MB_ICONERROR
        )
    except Exception:
        "intentional: ignore exception"


def _finish_update_job(job: dict, latest: str) -> bool:
    if job.get("state") == "failed":
        error = _update_error_message(job)
        flog_kv("UPDATE", "startup_update_failed", "error", error=error)
        _show_update_failure(error)
        return False
    _console_write(f"Updated to v{latest} - launching...")
    return True


def _fetch_update_job() -> dict | None:
    try:
        status = _autostart_api("/api/update/status")
    except Exception:
        return None
    job = status.get("job") if isinstance(status, dict) else None
    return job if isinstance(job, dict) else None


def _monitor_startup_update(latest: str) -> bool:
    _console_finish_startup(clear=False)
    restart_announced = False
    try:
        while True:
            time.sleep(0.5)
            job = _fetch_update_job()
            if job is None:
                continue
            if not job.get("active"):
                return _finish_update_job(job, latest)
            phase = _update_phase(job)
            if phase == "restarting":
                # The updater takes over only after this process exits. Seal
                # the final progress line, then stay silent during handoff.
                if not restart_announced:
                    restart_announced = True
                    _console_seal_inline_line()
                continue
            # Render every tick so the spinner keeps moving even while the
            # backend reports KB counts instead of percents.
            pct, label = _update_progress_args(job, phase, latest)
            _render_update_progress(pct, label)
    except Exception:
        return True


def _snapshot_versions(snap: dict) -> tuple[str, str]:
    latest = str(snap.get("latest_version") or "").strip()
    current = str(snap.get("current_version") or "").strip()
    return latest, current


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
    if not _app_is_compiled():
        return False
    try:
        _, farm = _require_configured()
        cfg = farm.cfg_mgr
    except Exception:
        return False
    if _startup_prompt_skipped(cfg):
        return False
    # The GitHub check used to run synchronously here (up to 20s on a bad
    # link), stalling every launch before the window even opened. Run it in
    # a thread and give up fast: a slow check just boots normally and the
    # dashboard button surfaces the update when its own poll completes.
    snap = _check_startup_update()
    if snap is None:
        return False
    if not snap.get("update_available"):
        return False
    latest, current = _snapshot_versions(snap)
    if not latest:
        return False
    text = _update_prompt_text(
        _version_label(current, "unknown"), _version_label(latest)
    )
    if not _confirm_startup_update(text, latest):
        return False
    if not _start_update():
        return False
    # End the boot progress bar first so the update status starts on its own
    # clean line (it used to jam onto the bar: "88%Updating to vX...").
    # No separate update line here: the progress line carries the version.
    # The swap script waits for this process to exit before writing to the
    # console, so the two progress displays cannot overwrite each other.
    return _monitor_startup_update(latest)


def _fast_exit(code: int = 0) -> None:
    """Exit immediately so the console/terminal closes with the window.

    Window-close already did the load-bearing cleanup (fast farm stop +
    instance-state clear). Anything left is daemon threads (uvicorn,
    workers, monitors) that must not keep the terminal alive. os._exit
    skips interpreter teardown/joins, so the cmd window closes at once.
    """
    try:
        _clear_instance_state()
    except Exception:
        "intentional: ignore exception"
    try:
        sys.stdout.flush()
    except Exception:
        "intentional: ignore exception"
    try:
        sys.stderr.flush()
    except Exception:
        "intentional: ignore exception"
    os._exit(int(code))


def _find_leftover_app_process() -> int | None:
    from desktop import instance_guard as instance_guard

    pid = instance_guard._find_same_app_process()
    if not pid:
        return None
    try:
        import psutil

        cmd = " ".join(psutil.Process(int(pid)).cmdline() or []).lower()
        if "cronus_updater_" in cmd:
            return None
    except Exception:
        "intentional: ignore exception"
    return pid


def _post_update_sweep() -> None:
    try:
        from desktop import instance_guard as instance_guard

        for _ in range(40):
            _stop_previous_instance()
            _stop_same_app_processes()
            if (
                _find_leftover_app_process() is None
                and instance_guard._find_existing_dashboard(DEFAULT_PORT) is None
            ):
                break
            time.sleep(0.5)
    except Exception as exc:
        flog_kv("MAIN", "post_update_sweep_failed", "warning", error=str(exc))


def _initial_instance_sweep() -> None:
    if "--post-update" not in sys.argv:
        _stop_previous_instance()
        _stop_same_app_processes()
        return
    _console_status(
        "startup", "Post-update boot: sweeping leftover instances"
    )
    flog_kv("MAIN", "post_update_boot", argv=" ".join(sys.argv[1:]))
    _post_update_sweep()


def _acquire_instance_guard() -> tuple[bool, bool]:
    mutex_ok = _acquire_single_instance_mutex()
    socket_ok = _acquire_instance_socket()
    if mutex_ok and socket_ok:
        return mutex_ok, socket_ok
    _console_status(
        "startup", "Existing Cronus instance detected; requesting cleanup"
    )
    _stop_previous_instance()
    _stop_same_app_processes()
    # Keep the mutex already acquired by this process. Recreating it would
    # report ERROR_ALREADY_EXISTS against our own still-open handle.
    if not mutex_ok:
        mutex_ok = _acquire_single_instance_mutex()
    if not socket_ok:
        socket_ok = _acquire_instance_socket()
    return mutex_ok, socket_ok


def _start_backend(port: int) -> tuple[Any, bool, str]:
    app, farm = _require_configured()
    backend = BackendHost(HOST, port)
    _HOST.backend = backend
    backend.configure(app, port)
    server_thread = backend.start()
    ready, detail = backend.wait_ready(server_thread)
    return farm, ready, detail


def _report_backend_ready(port: int, ready: bool, detail: str) -> None:
    if ready:
        flog(f"[MAIN] FastAPI ready on http://{HOST}:{port}")
        _console_status("backend", f"Ready ({detail})")
        _console_status("dashboard", f"http://{HOST}:{port}")
        return
    flog_kv("MAIN", "fastapi_not_ready", "error", port=port, detail=detail)
    _console_status("backend", f"Not ready: {detail}")
    _console_status("log", LOG_FILE)


def _wait_for_shutdown() -> None:
    try:
        while not _SHUTDOWN_REQUESTED.wait(1.0):
            pass
    except KeyboardInterrupt:
        "intentional: ignore exception"


def _stop_farm_on_interrupt() -> None:
    _console_status("shutdown", "Ctrl+C received; stopping farm")
    try:
        _, farm = _require_configured()
        fast_stop = getattr(farm, "stop_fast", None)
        if callable(fast_stop):
            fast_stop()
        elif farm.running:
            farm.stop()
    except Exception:
        "intentional: ignore exception"


def _run_browser_fallback(ready: bool) -> None:
    _console_status(
        "desktop", "Desktop window unavailable; opening browser fallback"
    )
    _console_clear_after_window_show(False)
    _console_finish_startup(clear=ready)
    webbrowser.open(f"http://{HOST}:{_HOST.port}")
    try:
        while not _SHUTDOWN_REQUESTED.wait(1.0):
            pass
    except KeyboardInterrupt:
        _stop_farm_on_interrupt()
        _fast_exit(0)
    # Shutdown was requested via /api/app/shutdown (already os._exit(0)s
    # itself) or the loop above ended: never linger, exit at once so the
    # terminal closes together with the program.
    _fast_exit(0)


def _run_desktop_window(farm: Any, ready: bool) -> None:
    _console_status("desktop", "Opening desktop window (WebView2)")
    _console_clear_after_window_show(ready)
    try:
        window = DesktopWindow()
        if window.run(
            f"http://{HOST}:{_HOST.port}",
            farm,
            _SHUTDOWN_REQUESTED,
            on_first_show=_console_finish_after_window_show,
        ):
            _console_status("shutdown", "Cronus window closed")
            _fast_exit(0)
            return
    except Exception as exc:
        flog_kv("MAIN", "desktop_webview_failed", "warning", error=str(exc))
    _run_browser_fallback(ready)


def run_desktop(fastapi_app: Any = None, farm_controller: Any = None):
    if fastapi_app is not None or farm_controller is not None:
        configure(fastapi_app, farm_controller)
    if "--post-update" in sys.argv and IS_COMPILED:
        # Self-heal for updates applied by a pre-fix updater, which boots
        # this build with stdout/stderr redirected into a log file. Without
        # this the whole session runs with a dead terminal until relaunch.
        # (Fixed updaters no longer redirect; this covers the one update
        # that delivers the fix, whose swap script came from the old build.)
        console_output.reattach_console_if_redirected()
    _console_header()
    _console_status("startup", "Preparing single-instance guard")
    _initial_instance_sweep()
    mutex_ok, socket_ok = _acquire_instance_guard()
    if not mutex_ok:
        flog_kv("MAIN", "duplicate_instance_refused", "error", socket_ok=socket_ok)
        _console_status(
            "shutdown", "Another Cronus instance is running; close it first"
        )
        return
    port = _find_free_port(DEFAULT_PORT)
    _HOST.port = port
    _console_status("port", f"Selected http://{HOST}:{port}")
    _write_instance_state(port)
    _console_status("backend", "Starting FastAPI server")
    farm, ready, detail = _start_backend(port)
    _report_backend_ready(port, ready, detail)
    if ready and _maybe_prompt_startup_update():
        # User hit Yes and the updater took over: it downloads, swaps the
        # exe, and os._exit()s into the new version. Park here instead of
        # opening the desktop window; if the update failed the prompt
        # returns False and we boot normally below.
        _wait_for_shutdown()
        return
    threading.Thread(
        target=_run_autostart_chain, daemon=True, name="CronusAutostart"
    ).start()
    _run_desktop_window(farm, ready)


INSTANCE_TOKEN = _INSTANCE_TOKEN
SHUTDOWN_REQUESTED = _SHUTDOWN_REQUESTED
