from __future__ import annotations

import atexit
import ctypes
import json
import os
import secrets
import socket
import time
import urllib.request
from typing import Any, Dict, Optional, List, Tuple
from app_paths import APP_DATA_DIR, APP_ROOT_DIR, IS_COMPILED, path_targets_current_exe
from core import flog_kv
from desktop.constants import APP_USER_AGENT, DEFAULT_PORT, INSTANCE_LOCK_PORT, LOOPBACK_HOST, PORT_SCAN_COUNT


BASE_DIR = APP_ROOT_DIR
HOST = LOOPBACK_HOST
INSTANCE_TOKEN = secrets.token_urlsafe(32)
_APP_MUTEX = None
_INSTANCE_SOCKET = None
_INSTANCE_STATE_FILE = os.path.join(APP_DATA_DIR, "cronus_rt_instance.json")
_PYTHON_ENTRYPOINTS: Tuple[Tuple[str, ...], ...] = (
    ("main.py",),
)


def _find_free_port(start: int = DEFAULT_PORT) -> int:
    for p in range(start, start + PORT_SCAN_COUNT):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((HOST, p))
                return p
        except OSError:
            continue
    return start


def _find_existing_dashboard(start: int = DEFAULT_PORT) -> Optional[int]:
    for p in range(start, start + PORT_SCAN_COUNT):
        try:
            req = urllib.request.Request(
                f"http://{HOST}:{p}/api/status",
                headers={"User-Agent": APP_USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=0.8) as resp:
                if resp.status == 200:
                    return p
        except Exception:
            continue
    return None


def _normalize_path(path: str, cwd: str = "") -> str:
    text = str(path or "").strip().strip('"')
    if not text:
        return ""
    if not os.path.isabs(text) and cwd:
        text = os.path.join(cwd, text)
    elif not os.path.isabs(text):
        return ""
    return os.path.normcase(os.path.abspath(text))


def _entrypoint_path(parts: Tuple[str, ...]) -> str:
    return os.path.normcase(os.path.abspath(os.path.join(BASE_DIR, *parts)))


def _cmdline_part_matches_entrypoint(part: str, cwd: str, entrypoint: str) -> bool:
    candidate = _normalize_path(part, cwd)
    return bool(candidate and candidate == entrypoint)


def _cmdline_targets_this_app(cmdline: List[str], cwd: str = "") -> bool:
    try:
        cwd_norm = os.path.normcase(os.path.abspath(cwd or "")) if cwd else ""
    except Exception:
        cwd_norm = ""
    parts = [str(part or "").strip() for part in (cmdline or []) if str(part or "").strip()]
    has_python = any("python" in os.path.basename(part).lower() for part in parts)
    if not has_python:
        return False
    entrypoints = tuple(_entrypoint_path(item) for item in _PYTHON_ENTRYPOINTS)
    for part in parts:
        if any(_cmdline_part_matches_entrypoint(str(part or ""), cwd_norm, entrypoint) for entrypoint in entrypoints):
            return True
    return False


def _is_same_cronus_process(pid: int) -> bool:
    if not pid or int(pid) == os.getpid():
        return False
    try:
        import psutil

        proc = psutil.Process(int(pid))
        try:
            proc_name = os.path.basename(str(proc.name() or "")).lower()
            proc_exe_path = str(proc.exe() or "")
            proc_exe = os.path.basename(proc_exe_path).lower()
        except Exception:
            proc_name = ""
            proc_exe_path = ""
            proc_exe = ""
        try:
            cmdline = proc.cmdline()
            cwd = proc.cwd()
        except Exception:
            cmdline = []
            cwd = ""
        if IS_COMPILED:
            if path_targets_current_exe(proc_exe_path, cwd):
                return True
            if any(path_targets_current_exe(part, cwd) for part in cmdline):
                return True
            return False
        if "python" not in proc_name and "python" not in proc_exe:
            return False
        return _cmdline_targets_this_app(cmdline, cwd)
    except Exception:
        return False


def _is_pid_alive(pid: int) -> bool:
    try:
        import psutil

        return bool(pid and psutil.pid_exists(int(pid)) and psutil.Process(int(pid)).status() != "zombie")
    except Exception:
        return False


def _read_instance_state() -> Dict[str, Any]:
    try:
        if not os.path.exists(_INSTANCE_STATE_FILE):
            return {}
        with open(_INSTANCE_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_instance_state(port: int) -> None:
    payload = {
        "schema_version": 2,
        "pid": os.getpid(),
        "host": HOST,
        "port": int(port),
        "token": INSTANCE_TOKEN,
        "base_dir": BASE_DIR,
        "started_at": time.time(),
    }
    try:
        os.makedirs(os.path.dirname(_INSTANCE_STATE_FILE), exist_ok=True)
        with open(_INSTANCE_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception as exc:
        flog_kv("MAIN", "instance_state_write_failed", "warning", error=str(exc))


def _clear_instance_state() -> None:
    try:
        state = _read_instance_state()
        if int(state.get("pid") or 0) == os.getpid() or state.get("token") == INSTANCE_TOKEN:
            if os.path.exists(_INSTANCE_STATE_FILE):
                os.remove(_INSTANCE_STATE_FILE)
    except Exception:
        pass


def _request_instance_shutdown(port: int, token: str) -> bool:
    try:
        body = json.dumps({"token": token}).encode("utf-8")
        req = urllib.request.Request(
            f"http://{HOST}:{int(port)}/api/app/shutdown",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "X-Cronus-Token": token},
        )
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            return 200 <= int(resp.status) < 300
    except Exception:
        return False


def _terminate_instance_tree(pid: int) -> bool:
    if not _is_same_cronus_process(pid):
        return False
    try:
        import psutil

        proc = psutil.Process(int(pid))
        current = psutil.Process(os.getpid())
        if int(proc.pid) in {int(parent.pid) for parent in current.parents()}:
            return False
        children = proc.children(recursive=True)
        if any(int(getattr(child, "pid", 0) or 0) == os.getpid() for child in children):
            return False
        for child in children:
            try:
                child.terminate()
            except Exception:
                pass
        proc.terminate()
        gone, alive = psutil.wait_procs([proc] + children, timeout=4.0)
        for item in alive:
            try:
                item.kill()
            except Exception:
                pass
        return True
    except Exception as exc:
        flog_kv("MAIN", "instance_tree_terminate_failed", "warning", pid=pid, error=str(exc))
        return False


def _stop_previous_instance(wait_seconds: float = 8.0) -> bool:
    state = _read_instance_state()
    pid = int(state.get("pid") or 0)
    port = int(state.get("port") or 0)
    token = str(state.get("token") or "")
    if not pid or not _is_pid_alive(pid) or not _is_same_cronus_process(pid):
        return False
    flog_kv("MAIN", "previous_instance_detected", pid=pid, port=port)
    if port and token:
        _request_instance_shutdown(port, token)
    deadline = time.time() + max(1.0, float(wait_seconds or 8.0))
    while time.time() < deadline:
        if not _is_pid_alive(pid):
            return True
        time.sleep(0.25)
    return _terminate_instance_tree(pid)


def _stop_same_app_processes() -> int:
    stopped = 0
    try:
        import psutil

        current_ancestors = {int(parent.pid) for parent in psutil.Process(os.getpid()).parents()}
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            pid = int(proc.info.get("pid") or 0)
            if pid == os.getpid() or pid in current_ancestors:
                continue
            if _is_same_cronus_process(pid) and _terminate_instance_tree(pid):
                stopped += 1
    except Exception as exc:
        flog_kv("MAIN", "stop_same_app_processes_failed", "warning", error=str(exc))
    return stopped


def _find_same_app_process() -> Optional[int]:
    try:
        import psutil

        current_ancestors = {int(parent.pid) for parent in psutil.Process(os.getpid()).parents()}
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            pid = int(proc.info.get("pid") or 0)
            if pid == os.getpid() or pid in current_ancestors:
                continue
            if _is_same_cronus_process(pid):
                return pid
    except Exception as exc:
        flog_kv("MAIN", "find_same_app_process_failed", "warning", error=str(exc))
    return None


def _acquire_single_instance_mutex() -> bool:
    global _APP_MUTEX
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
        create_mutex.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (ctypes.c_void_p,)
        close_handle.restype = ctypes.c_bool
        ctypes.set_last_error(0)
        mutex = create_mutex(None, False, "Local\\Cronus_RT_1_0")
        if not mutex:
            return True
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            # CreateMutex still returns a handle when the named mutex exists.
            # Close our reference so a later retry can observe the previous
            # instance's handle being released instead of seeing our own.
            close_handle(mutex)
            return False
        _APP_MUTEX = mutex
        return True
    except Exception:
        return True


def _acquire_instance_socket() -> bool:
    global _INSTANCE_SOCKET
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((HOST, INSTANCE_LOCK_PORT))
        sock.listen(1)
        _INSTANCE_SOCKET = sock
        return True
    except OSError:
        return False


clear_instance_state = _clear_instance_state

atexit.register(_clear_instance_state)
