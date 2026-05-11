from __future__ import annotations

import argparse
import ctypes
import os
import signal
import sys
import time
from ctypes import wintypes
from typing import List, Optional, Tuple


KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
KERNEL32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
KERNEL32.CreateMutexW.restype = wintypes.HANDLE
KERNEL32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
KERNEL32.CreateEventW.restype = wintypes.HANDLE
KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
KERNEL32.CloseHandle.restype = wintypes.BOOL
KERNEL32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
KERNEL32.OpenProcess.restype = wintypes.HANDLE
KERNEL32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
KERNEL32.GetExitCodeProcess.restype = wintypes.BOOL

MUTEX_NAME = "ROBLOX_singletonMutex"
EVENT_NAME = "ROBLOX_singletonEvent"
ERROR_ALREADY_EXISTS = 183
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


def _last_error_suffix() -> str:
    err = ctypes.get_last_error()
    return "existing" if err == ERROR_ALREADY_EXISTS else f"err={err}"


def create_multi_roblox_handles(mode: str = "both") -> List[Tuple[str, int]]:
    mode = str(mode or "both").strip().lower()
    handles: List[Tuple[str, int]] = []
    if mode in {"mutex", "both"}:
        ctypes.set_last_error(0)
        handle = KERNEL32.CreateMutexW(None, True, MUTEX_NAME)
        if handle:
            handles.append((f"{MUTEX_NAME}:{_last_error_suffix()}", int(handle)))
    if mode in {"event", "both"}:
        ctypes.set_last_error(0)
        handle = KERNEL32.CreateEventW(None, True, False, EVENT_NAME)
        if handle:
            handles.append((f"{EVENT_NAME}:{_last_error_suffix()}", int(handle)))
    return handles


def close_handles(handles: List[Tuple[str, int]]) -> None:
    for _name, handle in handles:
        try:
            KERNEL32.CloseHandle(ctypes.c_void_p(handle))
        except Exception:
            pass


def _parent_alive(parent_pid: Optional[int]) -> bool:
    if not parent_pid:
        return True
    handle = KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(parent_pid))
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD(0)
        if not KERNEL32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return int(exit_code.value) == STILL_ACTIVE
    finally:
        try:
            KERNEL32.CloseHandle(handle)
        except Exception:
            pass


def _parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hold Roblox singleton handles for RoboGuard Multi Roblox.")
    parser.add_argument("mode", nargs="?", default="both", choices=["mutex", "event", "both"])
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--heartbeat", type=float, default=1.0)
    return parser.parse_args(argv)


def main() -> int:
    args = _parse_args(sys.argv[1:])
    handles = create_multi_roblox_handles(args.mode)
    if not handles:
        print("multi_roblox_guard_failed no_handles", flush=True)
        return 2
    handle_names = ",".join(name for name, _handle in handles)
    print(f"multi_roblox_guard_ready {handle_names} pid={os.getpid()}", flush=True)
    stop = False

    def _stop(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        while not stop:
            if not _parent_alive(args.parent_pid):
                print("multi_roblox_guard_stopping parent_dead", flush=True)
                break
            time.sleep(max(0.2, float(args.heartbeat or 1.0)))
    finally:
        close_handles(handles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
