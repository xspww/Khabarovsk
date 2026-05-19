from __future__ import annotations

import os
import shutil
import sys
from typing import Dict, Iterable, List


APP_NAME = "Cronus Launcher"
APP_FOLDER_NAME = "Cronus Launcher"
LEGACY_APP_FOLDER_NAMES = ("Argus Launcher",)
LEGACY_DOT_FOLDER_NAMES = (".argus_launcher",)
APP_ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

LEGACY_FILENAME_ALIASES: Dict[str, List[str]] = {
    "cronus_rt1.log": ["roboguard_rt1.log"],
    "cronus_rt1_events.jsonl": ["roboguard_rt1_events.jsonl"],
    "cronus_rt1_config.json": ["roboguard_rt1_config.json"],
    "cronus_rt1_cookies.json": ["roboguard_rt1_cookies.json"],
    "cronus_rt12_accounts.txt": ["roboguard_rt12_accounts.txt"],
    "cronus_rt12_runtime.txt": ["roboguard_rt12_runtime.txt"],
    "cronus_runtime.db": ["roboguard_runtime.db"],
    "cronus_runtime.db-shm": ["roboguard_runtime.db-shm"],
    "cronus_runtime.db-wal": ["roboguard_runtime.db-wal"],
}


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
    legacy_override = os.environ.get("ARGUS_USER_ROOT", "").strip()
    if legacy_override:
        return os.path.abspath(os.path.expandvars(os.path.expanduser(legacy_override)))
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return os.path.join(local, APP_FOLDER_NAME)
    return os.path.join(os.path.expanduser("~"), ".cronus_launcher")


USER_DATA_ROOT = _default_user_root()
APP_DATA_DIR = os.path.join(USER_DATA_ROOT, "data")
LEGACY_APP_DATA_DIR = BUNDLE_DIR if IS_COMPILED else APP_ROOT_DIR
LEGACY_DATA_DIR = os.path.join(LEGACY_APP_DATA_DIR, "data")


def ensure_user_dirs() -> None:
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    os.makedirs(USER_DATA_ROOT, exist_ok=True)


def resource_path(*parts: str) -> str:
    return os.path.join(RESOURCE_ROOT, *parts)


def bundle_path(*parts: str) -> str:
    return os.path.join(BUNDLE_DIR, *parts)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _legacy_user_roots() -> List[str]:
    roots: List[str] = []
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        roots.extend(os.path.join(local, name) for name in LEGACY_APP_FOLDER_NAMES)
    roots.extend(os.path.join(os.path.expanduser("~"), name) for name in LEGACY_DOT_FOLDER_NAMES)
    legacy_override = os.environ.get("ARGUS_USER_ROOT", "").strip()
    if legacy_override:
        roots.insert(0, os.path.abspath(os.path.expandvars(os.path.expanduser(legacy_override))))
    seen = {_norm(USER_DATA_ROOT)}
    clean: List[str] = []
    for root in roots:
        if not root:
            continue
        key = _norm(root)
        if key in seen:
            continue
        seen.add(key)
        clean.append(root)
    return clean


def _legacy_source_dirs() -> List[str]:
    candidates = [LEGACY_DATA_DIR, LEGACY_APP_DATA_DIR]
    for root in _legacy_user_roots():
        candidates.extend([os.path.join(root, "data"), root])
    seen = set()
    clean: List[str] = []
    for path in candidates:
        key = _norm(path)
        if key in seen:
            continue
        seen.add(key)
        clean.append(path)
    return clean


def _candidate_relatives(rel: str) -> List[str]:
    key = rel.replace("\\", "/")
    candidates = [rel]
    for alias in LEGACY_FILENAME_ALIASES.get(key, []):
        candidates.append(alias.replace("/", os.sep).replace("\\", os.sep))
    return candidates


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


def migrate_legacy_data_files(filenames: Iterable[str]) -> None:
    ensure_user_dirs()
    legacy_dirs = _legacy_source_dirs()
    for filename in filenames:
        rel = str(filename).replace("/", os.sep).replace("\\", os.sep)
        target = os.path.join(APP_DATA_DIR, rel)
        if os.path.exists(target):
            continue
        for legacy_dir in legacy_dirs:
            migrated = False
            for source_rel in _candidate_relatives(rel):
                source = os.path.join(legacy_dir, source_rel)
                if _norm(source) == _norm(target):
                    continue
                if not os.path.exists(source):
                    continue
                try:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    shutil.copy2(source, target)
                    migrated = True
                except Exception:
                    pass
                break
            if migrated:
                break


ensure_user_dirs()
