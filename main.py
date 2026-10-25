from __future__ import annotations

import os
import re
import sys
import threading
import time
from functools import partial
from importlib import metadata as importlib_metadata
from importlib import util as importlib_util

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from desktop import console_output
from infra.app_paths import APP_NAME, IS_COMPILED, resource_path
from version import parse_version_tuple as _startup_version_tuple

REQUIREMENTS_FILE = os.path.join(BASE_DIR, "requirements.txt")
_COLOR_GREEN = "\x1b[92m"
_COLOR_RED = "\x1b[91m"
_COLOR_DIM = "\x1b[90m"
_REQUIREMENT_IMPORT_NAMES: dict[str, str] = {
    "pillow": "PIL",
    "pyside6": "PySide6",
    "python-dotenv": "dotenv",
}


def _startup_console_write(message: str = "") -> None:
    console_output.write_line(message, {"✓": "OK", "×": "XX"})  # noqa: RUF001 - intentional console glyphs


def _startup_enable_virtual_terminal() -> bool:
    return console_output.enable_virtual_terminal(sys.stdout)


class _StartupColorState:
    def __init__(self) -> None:
        self.support: bool | None = None

    def enabled(self) -> bool:
        if not console_output.color_requested():
            return False
        if self.support is None:
            self.support = _startup_enable_virtual_terminal()
        return bool(self.support)


_STARTUP_COLORS = _StartupColorState()


def _startup_colors_enabled() -> bool:
    return _STARTUP_COLORS.enabled()


_startup_paint = partial(console_output.paint_when, enabled=_startup_colors_enabled)


def _startup_tick_line(package: str, version: str) -> str:
    return f"{_startup_paint('✓', _COLOR_GREEN)} {package} {_startup_paint('v' + version, _COLOR_DIM)}"


def _startup_fail_line(package: str, reason: str) -> str:
    return f"{_startup_paint('×', _COLOR_RED)} {package} {reason}".rstrip()  # noqa: RUF001 - intentional console glyph


def _startup_distribution_version(name: str) -> str | None:
    try:
        return importlib_metadata.version(name)
    except importlib_metadata.PackageNotFoundError:
        return None
    except Exception:
        return None


def _startup_import_available(name: str) -> bool:
    try:
        return importlib_util.find_spec(name) is not None
    except Exception:
        return False


def _startup_requirement_rows(requirements_file: str = REQUIREMENTS_FILE) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    try:
        with open(requirements_file, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return rows
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(("#", "-")):
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?\s*(>=\s*([^;#\s]+))?", line)
        if not match:
            continue
        package = match.group(1)
        minimum = match.group(3) or ""
        import_name = _REQUIREMENT_IMPORT_NAMES.get(package.lower(), package.replace("-", "_"))
        rows.append((package, import_name, minimum))
    return rows


def _startup_version_ok(installed: str, minimum: str) -> bool:
    if not minimum:
        return True
    return _startup_version_tuple(installed) >= _startup_version_tuple(minimum)


def _run_startup_dependency_checks(
    requirements_file: str = REQUIREMENTS_FILE,
    *,
    exit_on_failure: bool = True,
    animate: bool = True,
) -> bool:
    rows = _startup_requirement_rows(requirements_file)
    failures: list[str] = []
    for package, import_name, minimum in rows:
        installed = _startup_distribution_version(package)
        import_ok = _startup_import_available(import_name)
        if not installed or not import_ok:
            failures.append(package)
            need = f" >= {minimum}" if minimum else ""
            _startup_console_write(_startup_fail_line(package, f"missing{need}"))
            continue
        if not _startup_version_ok(installed, minimum):
            failures.append(package)
            _startup_console_write(_startup_fail_line(package, f"v{installed} below required >= {minimum}"))
            continue
        _startup_console_write(_startup_tick_line(package, installed))
        if animate:
            time.sleep(0.035)
    if failures:
        _startup_console_write("")
        _startup_console_write("Missing startup dependency. Run:")
        _startup_console_write("python -m pip install -r requirements.txt")
        if exit_on_failure:
            sys.exit(1)
        return False
    return True

if sys.platform != "win32":
    print(f"{APP_NAME} requires Windows.")
    sys.exit(1)

if "--version" in sys.argv:
    from version import app_display_version

    print(f"{APP_NAME} {app_display_version()}")
    sys.exit(0)

if __name__ == "__main__" and "--multi-roblox-guard" not in sys.argv:
    # Earliest visible output. The imports below (and onefile extraction
    # before them) otherwise leave the console blank for seconds, before
    # the startup progress paints. ASCII-only: the exe console may not be
    # UTF-8 (Run.cmd sets it, double-click does not).
    try:
        from version import app_display_version as _early_version_fn

        _early_version = str(_early_version_fn() or "").strip()
    except Exception:
        _early_version = ""
    _early_banner = f"{APP_NAME} {_early_version} - starting...".strip()
    try:
        print(_early_banner, flush=True)
    except Exception:
        "intentional: ignore exception"

if __name__ == "__main__":
    if not IS_COMPILED:
        _run_startup_dependency_checks()

try:
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles
except ImportError:
    print("pip install -r requirements.txt")
    sys.exit(1)

from api_routes import ApiContext, register_api_routes
from core import LOG_FILE, Account, ConfigManager, flog, flog_kv
from desktop.host import (
    INSTANCE_TOKEN,
    SHUTDOWN_REQUESTED,
    clear_instance_state,
    run_desktop,
)
from desktop.ui_dashboard import get_html_ui
from runtime.farm import FarmController
from services.account_hybrid import ACCOUNT_STORE
from services.app_updater import AppUpdater
from services.app_version_check import cleanup_legacy_update_stage
from services.discord_presence import DISCORD_PRESENCE
from services.executor_compatibility import ExecutorCompatibilityService
from services.executor_relauncher import ExecutorRelaunchService
from services.performance_settings import (
    apply_graphics_settings_file,
    apply_performance_settings_file,
    apply_process_priority_to_roblox,
)
from services.process_service import ProcessManager
from services.roblox_install_manager import RobloxInstallManager

cfg_mgr = ConfigManager()
try:
    accounts = [Account.from_dict(item) for item in ACCOUNT_STORE.to_cronus_accounts()]
except Exception as e:
    flog_kv("ACCOUNT_DATA", "load_failed_fallback_text_accounts", "warning", error=e)
    accounts = cfg_mgr.get_accounts()

farm = FarmController(cfg_mgr)
farm.set_accounts(accounts)
ROBLOX_INSTALLER = RobloxInstallManager(
    guard_running=lambda: bool(getattr(farm, "running", False)),
    roblox_running=lambda: bool(ProcessManager.snapshot_pids()),
    logger=flog,
)


class _ExecutorUpdateState:
    def __init__(self) -> None:
        self.resume_after_update = False
        self.in_progress = ""

    def set_resume(self, enabled: bool) -> None:
        self.resume_after_update = bool(enabled)

    def clear_resume(self) -> None:
        self.resume_after_update = False

    def begin(self, version: str) -> None:
        self.in_progress = version

    def finish(self, version: str) -> None:
        if str(version or "").strip().lower() == self.in_progress:
            self.in_progress = ""


_EXECUTOR_UPDATE_LOCK = threading.RLock()
_EXECUTOR_UPDATE = _ExecutorUpdateState()


def _executor_auto_rejoin_wanted() -> bool:
    try:
        return bool(cfg_mgr.get("auto_rejoin", True))
    except Exception:
        return True


def _executor_hold_on(detail: str = "") -> None:
    try:
        farm.set_executor_hold(True, reason="wait_exploit_patch", detail=detail)
    except Exception:
        "intentional: ignore exception"
    try:
        flog_kv("EXECUTOR", "hold_wait_exploit_patch", detail=str(detail or ""))
    except Exception:
        "intentional: ignore exception"


def _executor_hold_resume(reason: str, restart_executor: bool = False) -> bool:
    """Clear the Wait-for-patch hold and let parked accounts requeue.

    Keeps Roblox clients alive: never stops the farm here. Optionally
    restarts only the Executor process to pick up a new build.
    """
    try:
        was_held = bool(farm.is_executor_hold())
    except Exception:
        was_held = False
    try:
        farm.set_executor_hold(False)
    except Exception:
        "intentional: ignore exception"
    _EXECUTOR_UPDATE.clear_resume()
    if not _executor_auto_rejoin_wanted():
        return False
    if not bool(getattr(farm, "running", False)):
        # Hold resume must never start a stopped farm on its own
        # (a manual stop stays stopped). The auto-update path resumes
        # via _executor_resume_farm directly.
        try:
            farm.set_executor_hold(False)
        except Exception:
            "intentional: ignore exception"
        return False
    try:
        if restart_executor:
            EXECUTOR_RELAUNCHER.restart_executor_only(reason)
        else:
            EXECUTOR_RELAUNCHER.ensure_started()
    except Exception as exc:
        flog_kv("EXECUTOR", "hold_resume_executor_warning", "warning", error=exc, reason=reason)
    try:
        farm._runtime_orchestrator.request_reconcile_all(
            list(getattr(farm, "_accounts", []) or []), trigger=f"executor_hold_resume:{reason}"
        )
    except Exception as exc:
        flog_kv("EXECUTOR", "hold_reconcile_failed", "warning", error=exc, reason=reason)
        return False
    flog_kv("EXECUTOR", "hold_resumed", reason=reason, was_held=was_held, restart_executor=restart_executor)
    return True


def _executor_wait_roblox_gone(timeout_seconds: float = 30.0) -> bool:
    deadline = time.time() + max(1.0, float(timeout_seconds or 30.0))
    while time.time() < deadline:
        try:
            if not ProcessManager.snapshot_pids():
                blockers = ROBLOX_INSTALLER.find_install_blockers()
                if not blockers:
                    return True
        except Exception:
            return True
        time.sleep(1.0)
    try:
        return not bool(ProcessManager.snapshot_pids()) and not bool(ROBLOX_INSTALLER.find_install_blockers())
    except Exception:
        return False


def _executor_resume_farm(reason: str) -> bool:
    """Restart farm after update/pause. Uses relaunch flow when enabled."""
    if not _executor_auto_rejoin_wanted():
        _EXECUTOR_UPDATE.clear_resume()
        return False
    if bool(getattr(farm, "running", False)):
        _EXECUTOR_UPDATE.clear_resume()
        return True
    try:
        relaunch_enabled = bool(cfg_mgr.get("executor_relaunch_enabled", False))
    except Exception:
        relaunch_enabled = False
    if relaunch_enabled:
        try:
            supported, _why = EXECUTOR_RELAUNCHER.relaunch_support()
        except Exception:
            supported = False
        if supported:
            # Compatibility monitor + Auto Relaunch: executor is usable
            # again, so close Roblox/Executor, reopen, verify loader, rejoin.
            # allow_stopped_farm=True is the resume path (farm was stopped).
            if EXECUTOR_RELAUNCHER.request_relaunch(reason, allow_stopped_farm=True):
                _EXECUTOR_UPDATE.clear_resume()
                flog_kv("EXECUTOR", "resume_relaunch_requested", reason=reason)
                return True
    try:
        farm.start()
        _EXECUTOR_UPDATE.clear_resume()
        flog_kv("EXECUTOR", "auto_resume_started", reason=reason)
        return True
    except Exception as exc:
        flog_kv("EXECUTOR", "auto_resume_failed", "warning", error=exc, reason=reason)
        return False


def _stop_farm_for_update() -> None:
    # Stop farm first so the install job is not blocked by guard_running.
    try:
        if bool(getattr(farm, "running", False)):
            farm.stop()
    except Exception as exc:
        flog_kv("EXECUTOR", "auto_update_stop_failed", "warning", error=exc)
    try:
        if hasattr(farm, "close_all_roblox"):
            farm.close_all_roblox(reason="roblox_version_update")
    except Exception:
        "intentional: ignore exception"
    try:
        ProcessManager.kill_all_roblox_clients(wait_seconds=4.0)
    except Exception:
        "intentional: ignore exception"
    _executor_wait_roblox_gone(30.0)


def _start_update_attempt(
    version: str, attempt: int
) -> tuple[bool, bool, str]:
    """Return (accepted, retry_after_kill, last_message)."""
    try:
        result = ROBLOX_INSTALLER.start_update_official(version)
    except Exception as exc:
        last_msg = str(exc)
        flog_kv(
            "EXECUTOR",
            "auto_update_start_failed",
            "warning",
            error=exc,
            attempt=attempt,
        )
        return False, False, last_msg
    if isinstance(result, dict) and result.get("accepted"):
        return True, False, ""
    last_msg = str((result or {}).get("msg") or "install blocked")
    flog_kv(
        "EXECUTOR",
        "auto_update_blocked_retry",
        "warning",
        msg=last_msg,
        attempt=attempt,
    )
    return False, True, last_msg


def _start_update_with_retries(version: str) -> tuple[bool, str]:
    last_msg = ""
    for attempt in range(1, 4):
        accepted, retry_after_kill, last_msg = _start_update_attempt(
            version, attempt
        )
        if accepted:
            return True, ""
        if not retry_after_kill:
            time.sleep(5)
            continue
        try:
            ProcessManager.kill_all_roblox_clients(wait_seconds=4.0)
        except Exception:
            "intentional: ignore exception"
        _executor_wait_roblox_gone(15.0)
        time.sleep(5)
    return False, last_msg


def _wait_install_job(timeout: float = 600.0) -> dict:
    # Wait for the install job (official CDN download + validate +
    # old-version cleanup) before rejoining on the new client.
    deadline = time.time() + timeout
    final_job: dict = {}
    while time.time() < deadline:
        try:
            final_job = dict(ROBLOX_INSTALLER.status().get("job") or {})
        except Exception:
            final_job = {}
        if not final_job.get("active"):
            break
        time.sleep(2.0)
    return final_job


def _refresh_executor() -> dict:
    try:
        return EXECUTOR_TRACKER.refresh()
    except Exception:
        return {}


def _finish_compatible_update(fresh: dict, resume: bool, version: str) -> None:
    # Rejoin only when the farm was running/paused for this update AND
    # the selected Executor is usable on the new Roblox version.
    # Otherwise stay stopped (manual stop stays stopped; incompatible
    # executor waits for the compatible transition below to resume).
    if str(fresh.get("state") or "") != "compatible":
        flog_kv(
            "EXECUTOR",
            "auto_update_wait_executor",
            state=str(fresh.get("state") or "unknown"),
            version=version,
        )
        return
    if resume:
        _executor_resume_farm("roblox_version_updated_compatible")
        return
    flog_kv("EXECUTOR", "auto_update_done_staying_stopped", version=version)


def _executor_auto_update_worker(latest: str, resume: bool) -> None:
    normalized = str(latest or "").strip()
    try:
        flog_kv(
            "EXECUTOR",
            "auto_update_worker_started",
            version=normalized,
            resume=resume,
        )
        if resume:
            _EXECUTOR_UPDATE.set_resume(True)
        _stop_farm_for_update()
        accepted, last_msg = _start_update_with_retries(normalized)
        if not accepted:
            flog_kv(
                "EXECUTOR",
                "auto_update_gave_up",
                "warning",
                msg=last_msg,
                version=normalized,
            )
            _refresh_executor()
            return
        final_job = _wait_install_job()
        if final_job.get("active"):
            flog_kv(
                "EXECUTOR", "auto_update_timeout", "warning", version=normalized
            )
            return
        if not final_job.get("ok", True):
            flog_kv(
                "EXECUTOR",
                "auto_update_job_failed",
                "warning",
                error=final_job.get("error") or final_job.get("msg"),
                version=normalized,
            )
            _refresh_executor()
            return
        flog_kv(
            "EXECUTOR",
            "auto_update_done",
            version=normalized,
            removed_old_versions=final_job.get("removed_old_versions", ""),
            msg=final_job.get("msg", ""),
        )
        _finish_compatible_update(_refresh_executor(), resume, normalized)
    finally:
        with _EXECUTOR_UPDATE_LOCK:
            _EXECUTOR_UPDATE.finish(normalized)


def _installed_roblox_versions() -> list[dict]:
    try:
        return ROBLOX_INSTALLER.list_installed()
    except Exception:
        return []


def _needs_roblox_update(latest: str, installed: list[dict]) -> bool:
    if not latest:
        return False
    if not installed:
        return True
    return any(
        str(item.get("version") or "").strip().lower() != latest
        for item in installed
    )


def _begin_auto_update(latest: str, normalized: str) -> None:
    with _EXECUTOR_UPDATE_LOCK:
        if (
            latest == _EXECUTOR_UPDATE.in_progress
            and ROBLOX_INSTALLER.is_job_active()
        ):
            return
        if _EXECUTOR_UPDATE.in_progress:
            return
        _EXECUTOR_UPDATE.begin(latest)
        # Resume only if the farm was running or was paused by the monitor.
        # A manually stopped farm must stay stopped (a poll must never
        # start the farm on its own).
        resume = (
            bool(getattr(farm, "running", False))
            or _EXECUTOR_UPDATE.resume_after_update
        )
    # Heavy stop/download work must not block the tracker poll thread.
    threading.Thread(
        target=_executor_auto_update_worker,
        args=(normalized, resume),
        name="ExecutorAutoUpdate",
        daemon=True,
    ).start()


def _maybe_start_auto_update(event: str, payload: dict) -> bool:
    latest = str(payload.get("latest_version") or "").strip().lower()
    auto_update = bool(cfg_mgr.get("roblox_auto_update_enabled", False))
    installed = _installed_roblox_versions()
    needs_update = _needs_roblox_update(latest, installed)
    if not (
        auto_update
        and needs_update
        and event not in {"api_error", "disabled"}
    ):
        return False
    normalized = str(payload.get("latest_version") or "").strip()
    _begin_auto_update(latest, normalized)
    return True


def _executor_hold_detail(payload: dict) -> str:
    try:
        selected = str(cfg_mgr.get("executor_selected", "") or "").strip()
        latest_v = str(payload.get("latest_version") or "").strip()
        return f"{selected} {latest_v}".strip()
    except Exception:
        return ""


def _executor_incompatible(payload: dict) -> None:
    # Wait-for-patch hold: keep the farm running so in-game clients
    # stay alive; only new launches/rejoins are parked as
    # "Wait for exploit patch" until the executor is usable again.
    # (The Roblox auto-update path above still stops to install the
    # new client when the installed version is outdated.)
    # A stopped farm stays stopped: never arm the hold while idle,
    # otherwise a later compatible poll could start it on its own.
    _EXECUTOR_UPDATE.clear_resume()
    if not bool(getattr(farm, "running", False)):
        try:
            farm.set_executor_hold(False)
        except Exception:
            "intentional: ignore exception"
        return
    try:
        _executor_hold_on(_executor_hold_detail(payload))
    except Exception as exc:
        flog_kv("EXECUTOR", "hold_failed", "warning", error=exc)


def _executor_held() -> bool:
    try:
        return bool(farm.is_executor_hold())
    except Exception:
        return False


def _payload_compatible_flags(payload: dict) -> tuple[bool, bool]:
    return (
        bool(payload.get("became_compatible")),
        bool(payload.get("executor_version_changed")),
    )


def _executor_compatible(payload: dict) -> None:
    became, changed = _payload_compatible_flags(payload)
    if _executor_held() and not became and not changed:
        _executor_hold_resume("executor_became_compatible", restart_executor=False)
        return
    if not _EXECUTOR_UPDATE.resume_after_update:
        return
    # Plain resume (no executor version change). Version-change resumes
    # go through the relaunch flow in _executor_transition below.
    if not became and not changed:
        _executor_resume_farm("executor_became_compatible")


def _executor_base_transition(event: str, payload: dict) -> None:
    """Keep the farm safe around known executor incompatibility transitions."""
    if _maybe_start_auto_update(event, payload):
        return
    if event == "incompatible":
        _executor_incompatible(payload)
    elif event == "compatible":
        _executor_compatible(payload)


def _executor_transition(event: str, payload: dict) -> None:
    _executor_base_transition(event, payload)
    became, changed = _payload_compatible_flags(payload)
    if event != "compatible" or not (became or changed):
        return
    # Hold resume: the farm kept running through the patch wait, so
    # just clear the hold and requeue — never kill Roblox here.
    # Only the Executor process itself is restarted on a version
    # change to pick up the new build.
    if _executor_held():
        _executor_hold_resume(
            "weao_compatibility_or_executor_version_changed",
            restart_executor=changed,
        )
        return
    # Selected Executor is usable again (Roblox updated and/or executor
    # updated): close Roblox + Executor, reopen, verify Lua loader, rejoin.
    # Mid-session this relaunches in place; after an update/pause resume
    # it is allowed to start the stopped farm again.
    farm_running = bool(getattr(farm, "running", False))
    if farm_running:
        if EXECUTOR_RELAUNCHER.request_relaunch(
            "weao_compatibility_or_executor_version_changed"
        ):
            _EXECUTOR_UPDATE.clear_resume()
    elif _EXECUTOR_UPDATE.resume_after_update:
        _executor_resume_farm("weao_compatibility_or_executor_version_changed")


EXECUTOR_TRACKER = ExecutorCompatibilityService(
    cfg_mgr,
    on_transition=_executor_transition,
    get_installed_versions=lambda: [
        *(item.get("version", "") for item in ROBLOX_INSTALLER.list_installed()),
    ],
    logger=flog,
)
EXECUTOR_RELAUNCHER = ExecutorRelaunchService(cfg_mgr, farm, EXECUTOR_TRACKER, logger=flog_kv)
APP_UPDATER = AppUpdater(farm, logger=flog_kv)
farm.set_executor_start_guard(EXECUTOR_RELAUNCHER.ensure_started)
cleanup_legacy_update_stage()

app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)


@app.middleware("http")
async def _no_store_ui_assets(request, call_next):
    # Localhost desktop UI: never let the WebView cache HTML/JS/CSS.
    # Otherwise edits (or updates) only appear after a manual cache clear.
    response = await call_next(request)
    try:
        if request.url.path == "/" or request.url.path.startswith(("/ui/", "/assets/")):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
    except Exception:
        "intentional: ignore exception"
    return response


app.mount("/assets", StaticFiles(directory=resource_path("assets")), name="assets")
app.mount("/ui", StaticFiles(directory=resource_path("ui")), name="ui")
api_context = ApiContext(
    cfg_mgr=cfg_mgr,
    farm=farm,
    roblox_installer=ROBLOX_INSTALLER,
    app_updater=APP_UPDATER,
    executor_tracker=EXECUTOR_TRACKER,
    executor_relauncher=EXECUTOR_RELAUNCHER,
    html_ui=get_html_ui,
    instance_token=INSTANCE_TOKEN,
    shutdown_requested=SHUTDOWN_REQUESTED,
    clear_instance_state=clear_instance_state,
    get_log_file=lambda: LOG_FILE,
    get_apply_graphics_settings_file=lambda: apply_graphics_settings_file,
    get_apply_performance_settings_file=lambda: apply_performance_settings_file,
    get_apply_process_priority_to_roblox=lambda: apply_process_priority_to_roblox,
)
register_api_routes(app, api_context)


if __name__ == "__main__":
    if "--multi-roblox-guard" in sys.argv:
        from services import multi_roblox_guard

        idx = sys.argv.index("--multi-roblox-guard")
        sys.argv = [sys.argv[0], *sys.argv[idx + 1:]]
        raise SystemExit(multi_roblox_guard.main())
    EXECUTOR_TRACKER.start()
    try:
        DISCORD_PRESENCE.configure(
            cfg_getter=lambda key, default=None: cfg_mgr.get(key, default),
            status_getter=farm.get_status,
        )
        DISCORD_PRESENCE.start()
    except Exception:
        "intentional: ignore exception"
    try:
        run_desktop(app, farm)
        # Normal return (duplicate instance / update park / fallback):
        # never linger in interpreter teardown — close the terminal at once.
        try:
            import os as _os

            _os._exit(0)
        except Exception:
            "intentional: ignore exception"
    except Exception as exc:
        # A fatal boot error used to kill the new version silently during
        # self-update: its window closed, the updater saw "API never answered"
        # and rolled back with no evidence. Persist the traceback and use a
        # distinctive exit code (11) so the updater log can point at it.
        try:
            flog_kv("MAIN", "fatal_boot_error", "error", error=str(exc))
        except Exception:
            "intentional: ignore exception"
        try:
            import traceback as _traceback

            with open(LOG_FILE, "a", encoding="utf-8", errors="replace") as _handle:
                _handle.write(f"[FATAL] boot failed: {exc}\n{_traceback.format_exc()}\n")
        except Exception:
            "intentional: ignore exception"
        try:
            import ctypes as _ctypes

            _ctypes.windll.user32.MessageBoxW(
                None,
                f"Cronus Launcher failed to start:\n{exc}\n\nLog: {LOG_FILE}",
                "Cronus Launcher",
                0x00000000 | 0x00000010,  # MB_OK | MB_ICONERROR
            )
        except Exception:
            "intentional: ignore exception"
        raise SystemExit(11)
