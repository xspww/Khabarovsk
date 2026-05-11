from __future__ import annotations

import os
import shutil
import sys
from typing import Iterable


APP_NAME = "Argus Launcher"
APP_FOLDER_NAME = "Argus Launcher"
APP_ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
IS_FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE_DIR = os.path.dirname(sys.executable) if IS_FROZEN else APP_ROOT_DIR
RESOURCE_ROOT = getattr(sys, "_MEIPASS", BUNDLE_DIR if IS_FROZEN else APP_ROOT_DIR)


def _default_user_root() -> str:
    override = os.environ.get("ARGUS_USER_ROOT", "").strip()
    if override:
        return os.path.abspath(os.path.expandvars(os.path.expanduser(override)))
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return os.path.join(local, APP_FOLDER_NAME)
    return os.path.join(os.path.expanduser("~"), ".argus_launcher")


USER_DATA_ROOT = _default_user_root()
APP_DATA_DIR = os.path.join(USER_DATA_ROOT, "data")
LEGACY_APP_DATA_DIR = APP_ROOT_DIR
LEGACY_DATA_DIR = os.path.join(APP_ROOT_DIR, "data")


def ensure_user_dirs() -> None:
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    os.makedirs(USER_DATA_ROOT, exist_ok=True)


def resource_path(*parts: str) -> str:
    return os.path.join(RESOURCE_ROOT, *parts)


def bundle_path(*parts: str) -> str:
    return os.path.join(BUNDLE_DIR, *parts)


def migrate_legacy_data_files(filenames: Iterable[str]) -> None:
    ensure_user_dirs()
    legacy_dirs = [LEGACY_DATA_DIR, LEGACY_APP_DATA_DIR]
    for filename in filenames:
        rel = str(filename).replace("/", os.sep).replace("\\", os.sep)
        target = os.path.join(APP_DATA_DIR, rel)
        if os.path.exists(target):
            continue
        for legacy_dir in legacy_dirs:
            source = os.path.join(legacy_dir, rel)
            if os.path.normcase(os.path.abspath(source)) == os.path.normcase(os.path.abspath(target)):
                continue
            if not os.path.exists(source):
                continue
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(source, target)
            except Exception:
                pass
            break


ensure_user_dirs()
