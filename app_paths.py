from __future__ import annotations

import os
import shutil
import sys


APP_NAME = "Cronus Launcher"
APP_FOLDER_NAME = "Cronus Launcher"
APP_ROOT_DIR = os.path.dirname(os.path.abspath(__file__))


def _is_compiled_runtime() -> bool:
    main_module = sys.modules.get("__main__")
    return bool(
        getattr(sys, "frozen", False)
        or "__compiled__" in globals()
        or bool(main_module and hasattr(main_module, "__compiled__"))
    )


def _compiled_executable_path() -> str:
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    if _is_compiled_runtime() and sys.argv:
        return os.path.abspath(sys.argv[0])
    return os.path.abspath(sys.executable)


IS_FROZEN = _is_compiled_runtime()
IS_COMPILED = IS_FROZEN
EXECUTABLE_PATH = _compiled_executable_path()
BUNDLE_DIR = os.path.dirname(EXECUTABLE_PATH) if IS_COMPILED else APP_ROOT_DIR
RESOURCE_ROOT = getattr(sys, "_MEIPASS", APP_ROOT_DIR)


def _default_user_root() -> str:
    override = os.environ.get("CRONUS_USER_ROOT", "").strip()
    if override:
        return os.path.abspath(os.path.expandvars(os.path.expanduser(override)))
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return os.path.join(local, APP_FOLDER_NAME)
    return os.path.join(os.path.expanduser("~"), ".cronus_launcher")


USER_DATA_ROOT = _default_user_root()
APP_DATA_DIR = os.path.join(USER_DATA_ROOT, "data")
LOG_DIR = os.path.join(APP_DATA_DIR, "logs")
CACHE_DIR = os.path.join(APP_DATA_DIR, "cache")


def ensure_user_dirs() -> None:
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    os.makedirs(USER_DATA_ROOT, exist_ok=True)


def ensure_log_dir() -> None:
    ensure_user_dirs()
    os.makedirs(LOG_DIR, exist_ok=True)


def ensure_cache_dir() -> None:
    ensure_user_dirs()
    os.makedirs(CACHE_DIR, exist_ok=True)


def resource_path(*parts: str) -> str:
    return os.path.join(RESOURCE_ROOT, *parts)


def bundle_path(*parts: str) -> str:
    return os.path.join(BUNDLE_DIR, *parts)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def path_targets_current_exe(path: str, cwd: str = "") -> bool:
    if not IS_COMPILED or not path:
        return False
    try:
        expected = os.path.normcase(os.path.abspath(EXECUTABLE_PATH))
        candidate = str(path or "").strip().strip('"')
        if not candidate:
            return False
        if not os.path.isabs(candidate) and cwd:
            candidate = os.path.join(cwd, candidate)
        return os.path.normcase(os.path.abspath(candidate)) == expected
    except Exception:
        return False


def _collision_path(path: str) -> str:
    candidate = f"{path}.migrated"
    index = 1
    while os.path.exists(candidate):
        index += 1
        candidate = f"{path}.migrated{index}"
    return candidate


def move_app_data_file(source_rel: str, target_rel: str, *, discard_if_target_exists: bool = False) -> None:
    source = os.path.join(APP_DATA_DIR, str(source_rel).replace("/", os.sep).replace("\\", os.sep))
    target = os.path.join(APP_DATA_DIR, str(target_rel).replace("/", os.sep).replace("\\", os.sep))
    if _norm(source) == _norm(target) or not os.path.exists(source):
        return
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.exists(target):
            if discard_if_target_exists:
                os.remove(source)
                return
            target = _collision_path(target)
        shutil.move(source, target)
    except Exception:
        pass


ensure_user_dirs()
