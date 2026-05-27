from __future__ import annotations

import os
import re
import sys
import time
import urllib.request
from importlib import metadata as importlib_metadata
from importlib import util as importlib_util
from typing import Dict, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app_paths import APP_NAME, resource_path
from desktop import console_output

APP_USER_AGENT = "CronusLauncher/RT"
APP_ICON_FILE = "cronus_icon.png"
REQUIREMENTS_FILE = os.path.join(BASE_DIR, "requirements.txt")
_STARTUP_COLOR_SUPPORT: Optional[bool] = None
_COLOR_GREEN = "\x1b[92m"
_COLOR_RED = "\x1b[91m"
_COLOR_DIM = "\x1b[90m"
_REQUIREMENT_IMPORT_NAMES: Dict[str, str] = {
    "pillow": "PIL",
    "pyside6": "PySide6",
}


def _startup_console_write(message: str = "") -> None:
    console_output.write_line(message, {"✓": "OK", "×": "XX"})


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


def _startup_tick_line(package: str, version: str) -> str:
    return f"{_startup_paint('✓', _COLOR_GREEN)} {package} {_startup_paint('v' + version, _COLOR_DIM)}"


def _startup_fail_line(package: str, reason: str) -> str:
    return f"{_startup_paint('×', _COLOR_RED)} {package} {reason}".rstrip()


def _startup_distribution_version(name: str) -> Optional[str]:
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


def _startup_requirement_rows(requirements_file: str = REQUIREMENTS_FILE) -> List[Tuple[str, str, str]]:
    rows: List[Tuple[str, str, str]] = []
    try:
        with open(requirements_file, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return rows
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?\s*(>=\s*([^;#\s]+))?", line)
        if not match:
            continue
        package = match.group(1)
        minimum = match.group(3) or ""
        import_name = _REQUIREMENT_IMPORT_NAMES.get(package.lower(), package.replace("-", "_"))
        rows.append((package, import_name, minimum))
    return rows


def _startup_version_tuple(value: str) -> Tuple[int, ...]:
    parts = [int(item) for item in re.findall(r"\d+", str(value or ""))[:4]]
    return tuple(parts or [0])


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
    failures: List[str] = []
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
    _startup_console_write(f"{_startup_paint('✓', _COLOR_GREEN)} Runtime models: none required")
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

if __name__ == "__main__":
    _run_startup_dependency_checks()

try:
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles
    import uvicorn
except ImportError:
    print("pip install -r requirements.txt")
    sys.exit(1)

from account_hybrid import ACCOUNT_STORE
from api_routes import ApiContext, register_api_routes
from core import Account, ConfigManager, LOG_FILE, flog, flog_kv
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
from farm import FarmController
from services.cpu_limiter import CPU_LIMITER
from services.network_fault_injector import NETWORK_FAULT_INJECTOR
from services.process_service import ProcessManager
from services.roblox_install_manager import RobloxInstallManager
from performance_settings import (
    apply_graphics_settings_file,
    apply_performance_settings_file,
    apply_process_priority_to_roblox,
)
from ui_dashboard import HTML_UI
from api_routes.accounts_routes import _AVATAR_CACHE


cfg_mgr = ConfigManager()
legacy_accounts = cfg_mgr.get_accounts()
try:
    ACCOUNT_STORE.ensure_from_legacy([account.to_dict() for account in legacy_accounts])
    accounts = [Account.from_dict(item) for item in ACCOUNT_STORE.to_cronus_accounts()]
except Exception as e:
    flog_kv("ACCOUNT_DATA", "load_failed_fallback_legacy", "warning", error=e)
    accounts = legacy_accounts

farm = FarmController(cfg_mgr)
farm.set_accounts(accounts)
ROBLOX_INSTALLER = RobloxInstallManager(
    guard_running=lambda: bool(getattr(farm, "running", False)),
    roblox_running=lambda: bool(ProcessManager.snapshot_pids()),
    logger=flog,
)

app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)
app.mount("/ui", StaticFiles(directory=resource_path("ui")), name="ui")
api_context = ApiContext(
    cfg_mgr=cfg_mgr,
    farm=farm,
    roblox_installer=ROBLOX_INSTALLER,
    html_ui=HTML_UI,
    instance_token=INSTANCE_TOKEN,
    shutdown_requested=SHUTDOWN_REQUESTED,
    clear_instance_state=clear_instance_state,
    get_network_fault_injector=lambda: NETWORK_FAULT_INJECTOR,
    get_log_file=lambda: LOG_FILE,
    get_apply_graphics_settings_file=lambda: apply_graphics_settings_file,
    get_apply_performance_settings_file=lambda: apply_performance_settings_file,
    get_apply_process_priority_to_roblox=lambda: apply_process_priority_to_roblox,
)
register_api_routes(app, api_context)


if __name__ == "__main__":
    if "--multi-roblox-guard" in sys.argv:
        import multi_roblox_guard

        idx = sys.argv.index("--multi-roblox-guard")
        sys.argv = [sys.argv[0], *sys.argv[idx + 1:]]
        raise SystemExit(multi_roblox_guard.main())
    run_desktop(app, farm)
