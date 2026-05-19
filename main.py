from __future__ import annotations

import os
import sys
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app_paths import APP_NAME, resource_path

APP_USER_AGENT = "CronusLauncher/RT"
APP_ICON_FILE = "cronus_icon.png"

if sys.platform != "win32":
    print(f"{APP_NAME} requires Windows.")
    sys.exit(1)

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
