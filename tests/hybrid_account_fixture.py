import os
import atexit
import base64
import hashlib
import json
import re
import shutil
import stat
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

_TEST_USER_ROOT = tempfile.mkdtemp(prefix="cronus-test-user-root-")
if "CRONUS_USER_ROOT" not in os.environ:
    os.environ["CRONUS_USER_ROOT"] = _TEST_USER_ROOT
    atexit.register(shutil.rmtree, _TEST_USER_ROOT, ignore_errors=True)
else:
    shutil.rmtree(_TEST_USER_ROOT, ignore_errors=True)

from account_hybrid import AccountDataStore, decrypt_cookie, dpapi_protect, encrypt_cookie, parse_cookie_line
from core import Account, AccountState, ServerType, account_launch_block_reason
from domain.session_identity import build_launch_intent
from farm import Dispatcher, FarmController, SystemMaintenance
from performance_settings import (
    apply_graphics_settings_file,
    apply_performance_settings_file,
    apply_fps_limiter_file,
    normalize_graphics_quality,
    normalize_process_priority,
    is_readonly,
    normalize_fps_limit,
    priority_to_psutil_value,
    read_fps_settings,
    set_readonly,
)
from services.roblox_install_manager import RobloxInstallManager, normalize_roblox_version
from services.cpu_limiter import CpuLimiter, normalize_cpu_limiter_settings
from services.process_service import ProcessService
from services.captcha_guard import CAPTCHA_BLOCK_REASON, CAPTCHA_REASON, captcha_detail, clear_account_captcha_hold, is_account_captcha_required, set_account_captcha_hold
from runtime.runtime_state_manager import RuntimeStateManager
from process_net import ProcessManager
from roblox_hybrid import (
    HybridLauncher,
    build_owned_private_server_link,
    build_place_launcher_url,
    build_roblox_player_uri,
    ensure_owned_private_server,
    ensure_multi_roblox_guard,
    multi_roblox_guard_status,
    parse_launch_destination_from_cmdline,
    parse_vip_access_code_html,
    parse_vip_link,
    record_multi_roblox_guard_failure,
    release_multi_roblox_guard,
    validate_record_cookie_identity,
)


def auth_headers(extra=None):
    import main

    headers = {"X-Cronus-Token": main.INSTANCE_TOKEN}
    if extra:
        headers.update(extra)
    return headers


def auth_post(client, path, **kwargs):
    import main

    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("X-Cronus-Token", main.INSTANCE_TOKEN)
    return client.post(path, headers=headers, **kwargs)


def auth_get(client, path, **kwargs):
    import main

    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("X-Cronus-Token", main.INSTANCE_TOKEN)
    return client.get(path, headers=headers, **kwargs)
