from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any

from services._util import _cfg

EXECUTOR_NAMES = ("Volt", "Potassium", "Real", "Madium")


def read_windows_file_version(exe_path: str) -> str:
    """Return FileVersion/ProductVersion of a Windows .exe, or ''.

    Used to report which Executor build is actually on disk / running
    locally. Returns '' when the version resource is absent or unreadable
    (no exception is raised).
    """
    target = str(exe_path or "").strip()
    if not target or not os.path.isfile(target):
        return ""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        version = ctypes.windll.version
        size = version.GetFileVersionInfoSizeW(target, None)
        if not size:
            return ""
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(target, 0, size, buffer):
            return ""
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
            return ""
        class _FixedFileInfo(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD),
                ("dwProductVersionLS", wintypes.DWORD),
                ("dwFileFlagsMask", wintypes.DWORD),
                ("dwFileFlags", wintypes.DWORD),
                ("dwFileOS", wintypes.DWORD),
                ("dwFileType", wintypes.DWORD),
                ("dwFileSubtype", wintypes.DWORD),
                ("dwFileDateMS", wintypes.DWORD),
                ("dwFileDateLS", wintypes.DWORD),
            ]

        info = ctypes.cast(pointer, ctypes.POINTER(_FixedFileInfo)).contents
        file_version = (
            f"{(info.dwFileVersionMS >> 16) & 0xFFFF}."
            f"{info.dwFileVersionMS & 0xFFFF}."
            f"{(info.dwFileVersionLS >> 16) & 0xFFFF}."
            f"{info.dwFileVersionLS & 0xFFFF}"
        )
        product_version = (
            f"{(info.dwProductVersionMS >> 16) & 0xFFFF}."
            f"{info.dwProductVersionMS & 0xFFFF}."
            f"{(info.dwProductVersionLS >> 16) & 0xFFFF}."
            f"{info.dwProductVersionLS & 0xFFFF}"
        )
        # Prefer the non-zero one; strip trailing .0 for display.
        chosen = file_version if file_version.strip("0.") else product_version
        if not chosen.strip("0."):
            chosen = product_version if product_version.strip("0.") else file_version
        return chosen.strip()
    except Exception:
        return ""


def executor_version_from_path(exe_path: str) -> str:
    """Derive the vendor version from a versioned install folder, or ''.

    Executor updaters (Real: ``real-2.7.5``) keep the real app in a
    versioned directory while the Browse target (``Update.exe``) and even
    the runtime's own FileVersion resource report unrelated stub numbers
    (``1.0.0.0``). The folder name is the trustworthy local signal and
    matches the version WEAO reports.
    """
    import re

    text = str(exe_path or "").replace("\\", "/")
    if not text:
        return ""
    best = ""
    for part in text.split("/"):
        match = re.search(r"(?:^|-)v?(\d+\.\d+(?:\.\d+){0,2})$", part.strip(), re.IGNORECASE)
        if match:
            best = match.group(1)
    return best


def _pick_executor_version(candidates: list[str]) -> tuple[str, str]:
    for candidate in candidates:
        folder_version = executor_version_from_path(candidate)
        if folder_version:
            return folder_version, "folder"
    for candidate in candidates:
        file_version = read_windows_file_version(candidate)
        if file_version:
            return file_version, "file"
    return "", ""


def _running_executor_exes(targets: set[str]) -> list[tuple[int, str]]:
    try:
        import psutil
    except Exception:
        return []
    found: list[tuple[int, str]] = []
    try:
        for proc in psutil.process_iter(["pid"]):
            try:
                exe = proc.exe()
                if exe and os.path.normcase(os.path.abspath(exe)) in targets:
                    found.append((int(proc.pid), str(exe)))
            except Exception:
                continue
    except Exception:
        return []
    return found


def _version_candidates(primary: str, runtime_candidates: list[str], configured: str) -> list[str]:
    others = [candidate for candidate in runtime_candidates if candidate.lower() != primary.lower()]
    return [primary] + others + ([configured] if configured else [])


def _runtime_version_candidates(runtime_candidates: list[str], configured: str) -> list[str]:
    return list(runtime_candidates) + ([configured] if configured else [])


def _apply_local_executor_versions(
    info: dict[str, Any],
    runtime_candidates: list[str],
    running_exes: list[tuple],
    configured: str,
) -> dict[str, Any]:
    if running_exes:
        info["running"] = True
        info["count"] = len(running_exes)
        info["pids"] = [pid for pid, _exe in running_exes]
        info["exe"] = str(running_exes[0][1] or "")
        ordered = _version_candidates(info["exe"], runtime_candidates, configured)
    else:
        info["exe"] = info["runtime_exe"] or configured
        ordered = _runtime_version_candidates(runtime_candidates, configured)
    info["version"], info["version_source"] = _pick_executor_version(ordered)
    return info


def _executor_target_names(selected: str, filename: str) -> set[str]:
    stem, _ext = os.path.splitext(filename)
    names = {filename.lower()}
    # Madium's Browse target is its installer, while the running process
    # is always the updated binary under <root>\Bin\Madium.exe.
    if selected == "madium":
        names.add("madium.exe")
    # Real's Browse target is commonly Update.exe; the running binary is
    # placed in the current real-* version directory after auto-update.
    if selected == "real":
        names.add("real.exe")
    if stem.lower().endswith("installer"):
        names.add((stem[:-9] + ".exe").lower())
    if stem.lower().endswith("update"):
        names.add((stem[:-6] + ".exe").lower())
    return names


def _install_roots(configured: str) -> list[str]:
    roots: list[str] = []
    current = os.path.dirname(configured)
    for _ in range(3):
        if not current or current in roots:
            break
        roots.append(current)
        current = os.path.dirname(current)
    return roots


def _named_binaries_in_tree(root: str, names: set[str]) -> set[str]:
    found: set[str] = set()
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            depth = os.path.relpath(dirpath, root).count(os.sep)
            if depth > 2:
                continue
            for candidate in filenames:
                if candidate.lower() in names:
                    found.add(os.path.normcase(os.path.abspath(os.path.join(dirpath, candidate))))
    except OSError:
        return found
    return found


def _selected_process_victims(psutil: Any, targets: set[str]) -> list[Any]:
    victims: list[Any] = []
    for proc in psutil.process_iter(["pid"]):
        try:
            exe = proc.exe()
            if exe and os.path.normcase(os.path.abspath(exe)) in targets:
                victims.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            continue
    return victims


def _terminate_selected_processes(psutil: Any, victims: list[Any]) -> None:
    for proc in victims:
        try:
            proc.terminate()
        except Exception:
            "intentional: ignore exception"
    if not victims:
        return
    _gone, alive = psutil.wait_procs(victims, timeout=10)
    for proc in alive:
        try:
            proc.kill()
        except Exception:
            "intentional: ignore exception"


class ExecutorRelaunchService:
    """Relaunch the selected executor once per WEAO version transition."""

    def __init__(self, cfg_mgr: Any, farm: Any, tracker: Any, logger: Any = None):
        self.cfg_mgr, self.farm, self.tracker = cfg_mgr, farm, tracker
        self.logger = logger or (lambda *_args, **_kwargs: None)
        self._lock = threading.RLock()
        self._running = False
        self._launch_inflight: dict[str, float] = {}
        self._status: dict[str, Any] = {"state": "idle", "attempt": 0, "max_attempts": 10, "message": ""}

    @staticmethod
    def path_key(name: str) -> str:
        return f"executor_path_{str(name or '').strip().lower()}"

    def paths(self) -> dict[str, str]:
        return {name: str(_cfg(self.cfg_mgr, self.path_key(name), "") or "").strip() for name in EXECUTOR_NAMES}

    def selected_path(self) -> str:
        return self.paths().get(str(_cfg(self.cfg_mgr, "executor_selected", "") or "").strip(), "")

    def discovered_runtime_exes(self, configured: str = "") -> list[str]:
        """List installed runtime binaries besides the Browse target, newest first."""
        base = str(configured or self.selected_path() or "").strip()
        if not base:
            return []
        try:
            targets = self._process_targets(base)
        except Exception:
            return []
        configured_norm = os.path.normcase(os.path.abspath(base))
        found: list[str] = []
        for target in targets:
            if target == configured_norm:
                continue
            try:
                # Targets are normcase+abspath strings; Windows resolves
                # them fine for existence/mtime checks.
                if os.path.isfile(target):
                    found.append(target)
            except Exception:
                continue

        def _mtime(path: str) -> float:
            try:
                return float(os.path.getmtime(path))
            except OSError:
                return 0.0

        found.sort(key=_mtime, reverse=True)
        return found

    def resolve_launch_path(self) -> tuple[str, bool]:
        """Return the exe to launch plus whether it is a fallback.

        The Browse target can go stale (Madium's installer is removed after
        its first update while ``Bin\\Madium.exe`` remains). In that case
        fall back to the newest discovered runtime binary in the same
        install tree instead of reporting the executor unusable.
        """
        configured = str(self.selected_path() or "").strip()
        if configured and configured.lower().endswith(".exe") and os.path.isfile(configured):
            return configured, False
        for candidate in self.discovered_runtime_exes(configured):
            if candidate.lower().endswith(".exe"):
                return candidate, True
        return configured, False

    def _lua_enabled(self) -> bool:
        try:
            if hasattr(self.cfg_mgr, "snapshot"):
                data = self.cfg_mgr.snapshot()
                if isinstance(data, dict) and "lua_enabled" in data:
                    return bool(data.get("lua_enabled", True))
        except Exception:
            "intentional: ignore exception"
        return bool(_cfg(self.cfg_mgr, "lua_enabled", _cfg(self.cfg_mgr, "use_lua", True)))

    def relaunch_support(self) -> tuple[bool, str]:
        """Return whether the selected executor can be relaunched safely."""
        if not self._lua_enabled():
            return False, "Enable Use Lua to use Roblox Executor"
        selected = str(_cfg(self.cfg_mgr, "executor_selected", "") or "").strip()
        if selected not in EXECUTOR_NAMES:
            return False, "Selected Executor is not supported"
        path, _fallback = self.resolve_launch_path()
        if not path:
            return False, "Browse the selected Executor .exe first"
        if not os.path.isfile(path) or not path.lower().endswith(".exe"):
            return False, "Selected Executor .exe path is missing"
        return True, ""

    def status(self) -> dict[str, Any]:
        with self._lock:
            supported, reason = self.relaunch_support()
            snapshot = {**self._status, "running": self._running, "paths": self.paths(), "selected_path": self.selected_path(), "relaunch_supported": supported, "relaunch_support_reason": reason}
        try:
            snapshot["local"] = self.local_executor_info()
            snapshot["local_running"] = bool(snapshot["local"].get("running"))
            snapshot["local_version"] = str(snapshot["local"].get("version") or "")
            snapshot["local_version_source"] = str(snapshot["local"].get("version_source") or "")
            snapshot["local_pids"] = list(snapshot["local"].get("pids") or [])
            snapshot["local_exe"] = str(snapshot["local"].get("exe") or "")
            snapshot["launch_path"] = str(snapshot["local"].get("launch_path") or "")
            snapshot["launch_fallback"] = bool(snapshot["local"].get("launch_fallback"))
        except Exception:
            snapshot.setdefault("local", {})
            snapshot.setdefault("local_running", False)
            snapshot.setdefault("local_version", "")
            snapshot.setdefault("local_version_source", "")
            snapshot.setdefault("local_pids", [])
            snapshot.setdefault("local_exe", "")
            snapshot.setdefault("launch_path", "")
            snapshot.setdefault("launch_fallback", False)
        return snapshot

    def local_executor_info(self) -> dict[str, Any]:
        """Detect the selected Executor build actually present/running locally.

        Returns {name, configured_path, launch_path, launch_fallback,
        running, count, pids, exe, runtime_exe, version, version_source}.
        ``version`` prefers the versioned install folder (``real-2.7.5``)
        because updater stubs and even runtime FileVersions report
        unrelated numbers (``1.0.0.0``). ``version_source`` is ``folder``,
        ``file`` or ``''`` (unknown). Never raises.
        """

        selected = str(_cfg(self.cfg_mgr, "executor_selected", "") or "").strip()
        configured = str(self.selected_path() or "").strip()
        launch_path, launch_fallback = self.resolve_launch_path()
        info: dict[str, Any] = {
            "name": selected,
            "configured_path": configured,
            "launch_path": launch_path,
            "launch_fallback": launch_fallback,
            "running": False,
            "count": 0,
            "pids": [],
            "exe": "",
            "runtime_exe": "",
            "version": "",
            "version_source": "",
        }
        if not selected or not configured:
            return info
        try:
            runtime_candidates = self.discovered_runtime_exes(configured)
        except Exception:
            runtime_candidates = []
        if runtime_candidates:
            info["runtime_exe"] = str(runtime_candidates[0])
        try:
            targets = self._process_targets(configured)
        except Exception:
            targets = set()
        running_exes = _running_executor_exes(targets)
        return _apply_local_executor_versions(info, runtime_candidates, running_exes, configured)

    def _set_status(self, **values: Any) -> None:
        with self._lock:
            self._status.update(values)

    def _process_targets(self, path: str) -> set[str]:
        """Return the configured executable plus its updater-owned runtime exe(s).

        Executors commonly store the Browse target in an Installer/Update
        directory, then launch the real app from a versioned or Bin directory.
        Comparing only the Browse target makes the start guard launch a second
        copy every time an executor has updated itself.  The extra targets are
        restricted to the selected executor's install tree.
        """
        configured = os.path.normcase(os.path.abspath(path))
        targets = {configured}
        selected = str(_cfg(self.cfg_mgr, "executor_selected", "") or "").strip().lower()
        names = _executor_target_names(selected, os.path.basename(configured))
        for root in _install_roots(configured):
            targets |= _named_binaries_in_tree(root, names)
        return targets

    def _kill_selected_processes(self, path: str) -> int:
        try:
            import psutil
        except ImportError:
            return 0
        victims = _selected_process_victims(psutil, self._process_targets(path))
        _terminate_selected_processes(psutil, victims)
        return len(victims)

    def _selected_process_count(self, path: str) -> int:
        try:
            import psutil
        except ImportError:
            return 0
        targets = self._process_targets(path)
        count = 0
        for proc in psutil.process_iter(["pid"]):
            try:
                exe = proc.exe()
                if exe and os.path.normcase(os.path.abspath(exe)) in targets:
                    count += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                continue
        return count

    def ensure_started(self) -> bool:
        """Ensure the configured Executor is running before Farm Start."""
        if self._tracker_incompatible():
            # Farm is starting while the executor is already unusable: no
            # transition fires (the state did not change), so arm the
            # Wait-for-patch hold here, otherwise launches proceed with
            # no executor support and no Wait status on the dashboard.
            self._arm_incompatible_hold()
        if not self._lua_enabled():
            return True
        if not bool(_cfg(self.cfg_mgr, "executor_relaunch_enabled", False)):
            return True
        target = self._start_target()
        if target is None:
            self._set_status(state="error", message="Select an Executor and Browse its .exe path before Start")
            return False
        selected, _configured = target
        path = self._relaunch_path()
        if not path:
            self._set_status(state="error", message="Selected Executor .exe path is missing")
            return False
        with self._lock:
            return self._launch_executor_locked(path, selected)

    def _tracker_incompatible(self) -> bool:
        try:
            state = str((self.tracker.status() or {}).get("state") or "")
        except Exception:
            state = ""
        return state == "incompatible"

    def _start_target(self) -> tuple[str, str] | None:
        selected = str(_cfg(self.cfg_mgr, "executor_selected", "") or "").strip()
        configured = self.selected_path()
        if not selected or not configured:
            return None
        return selected, configured

    def _arm_incompatible_hold(self) -> None:
        try:
            _arm = getattr(self.farm, "set_executor_hold", None)
            if callable(_arm):
                _arm(True, reason="wait_exploit_patch", detail="executor_incompatible_at_start")
        except Exception:
            "intentional: ignore exception"

    def _start_failure(self, selected: str, exc: Exception) -> bool:
        self._status.update(state="error", message=f"Failed to open {selected}: {exc}")
        self.logger("EXECUTOR", "start_failed", "warning", error=exc, executor=selected)
        return False

    def _launch_executor_locked(self, path: str, selected: str) -> bool:
        if self._selected_process_count(path) > 0:
            self._launch_inflight.pop(os.path.normcase(os.path.abspath(path)), None)
            self._status.update(state="running", attempt=0, message=f"{selected} is already running")
            return True
        launch_key = os.path.normcase(os.path.abspath(path))
        now = time.time()
        self._launch_inflight = {key: started for key, started in self._launch_inflight.items() if now - started < 15}
        if launch_key in self._launch_inflight:
            self._status.update(state="starting", attempt=0, message=f"Starting {selected}; duplicate launch suppressed")
            return True
        try:
            subprocess.Popen([path], cwd=os.path.dirname(path), close_fds=True)
        except Exception as exc:
            return self._start_failure(selected, exc)
        self._launch_inflight[launch_key] = now
        self._status.update(state="started", attempt=0, message=f"Started {selected}")
        return True

    def restart_executor_only(self, reason: str = "executor_hold_resume") -> bool:
        """Restart only the Executor process, keeping Roblox/farm untouched.

        Used when resuming from the Wait-for-patch hold: the farm kept
        running, so a full relaunch (which kills all Roblox clients)
        would throw away the sessions we deliberately preserved.
        Returns True when the executor was (re)started or already running.
        """
        if not self._lua_enabled():
            return True
        if not bool(_cfg(self.cfg_mgr, "executor_relaunch_enabled", False)):
            return True
        selected = str(_cfg(self.cfg_mgr, "executor_selected", "") or "").strip()
        path, _fallback = self.resolve_launch_path()
        if not selected or not path or not os.path.isfile(path) or not path.lower().endswith(".exe"):
            return False
        return self._spawn_executor(path, selected, reason)

    def _spawn_executor(self, path: str, selected: str, reason: str) -> bool:
        try:
            if self._selected_process_count(path) > 0:
                self._kill_selected_processes(path)
                time.sleep(1.0)
            subprocess.Popen([path], cwd=os.path.dirname(path), close_fds=True)
        except Exception as exc:
            self._set_status(state="error", message=f"Failed to restart {selected}: {exc}")
            self._log_quiet("hold_restart_failed", "warning", error=exc, executor=selected, reason=reason)
            return False
        self._set_status(state="started", attempt=0, message=f"Restarted {selected} ({reason})")
        self._log_quiet("hold_restarted", executor=selected, reason=reason)
        return True

    def _log_quiet(self, event: str, *args: Any, **fields: Any) -> None:
        try:
            self.logger("EXECUTOR", event, *args, **fields)
        except Exception:
            "intentional: ignore exception"

    def _loader_ready(self, started_at: float) -> bool:
        for account in list(getattr(self.farm, "_accounts", []) or []):
            try:
                if float(getattr(account, "lua_last_event_at", 0) or 0) >= started_at and str(getattr(account, "lua_last_event", "") or "") in {"loaded", "in_game"}:
                    return True
            except Exception:
                continue
        return False

    def request_relaunch(self, reason: str = "executor_version_changed", allow_stopped_farm: bool = False) -> bool:
        with self._lock:
            if not self._lua_enabled():
                return False
            if self._running or not bool(_cfg(self.cfg_mgr, "executor_relaunch_enabled", False)):
                return False
            # A compatibility poll must never start the Executor or Farm on
            # its own while everything is stopped. Automatic relaunch is
            # allowed (a) during an active Farm session, (b) via explicit
            # Retry, or (c) as a resume-after-update/incompatible pause when
            # the caller passes allow_stopped_farm=True (the farm was stopped
            # by the compatibility monitor and must rejoin now that the
            # selected Executor is usable again).
            if not self._relaunch_allowed(reason, allow_stopped_farm):
                return False
            path = self._relaunch_path()
            if not path:
                self._status = {"state": "error", "attempt": 0, "max_attempts": 10, "message": "Selected Executor .exe path is missing"}
                return False
            self._running = True
        prior_attempt = int(self._status.get("attempt", 0) or 0) if reason == "manual_retry" else 0
        threading.Thread(target=self._run, args=(path, reason, prior_attempt), name="ExecutorRelaunch", daemon=True).start()
        return True

    def _relaunch_allowed(self, reason: str, allow_stopped_farm: bool) -> bool:
        if not bool(_cfg(self.cfg_mgr, "auto_rejoin", True)):
            return False
        farm_running = bool(getattr(self.farm, "running", False))
        if farm_running:
            return True
        return reason == "manual_retry" or bool(allow_stopped_farm)

    def _relaunch_path(self) -> str:
        path, _fallback = self.resolve_launch_path()
        if path and os.path.isfile(path) and path.lower().endswith(".exe"):
            return path
        return ""

    def _close_all_roblox_for_relaunch(self, reason: str) -> None:
        """Close Roblox clients even when the farm is currently stopped."""
        try:
            if bool(getattr(self.farm, "running", False)) and hasattr(self.farm, "close_all_roblox"):
                self.farm.close_all_roblox(reason=reason)
                return
        except Exception:
            "intentional: ignore exception"
        try:
            from services.process_service import ProcessManager

            if hasattr(ProcessManager, "kill_all_roblox_clients"):
                ProcessManager.kill_all_roblox_clients(wait_seconds=4.0)
                return
        except Exception:
            "intentional: ignore exception"
        try:
            from services.roblox_processes import ProcessManager as _Backend

            if hasattr(_Backend, "kill_all_roblox_clients"):
                _Backend.kill_all_roblox_clients(wait_seconds=4.0)
        except Exception:
            "intentional: ignore exception"

    def _run(self, path: str, reason: str, prior_attempt: int = 0) -> None:
        auto_limit = 5
        max_attempts = 10
        first_attempt = max(1, prior_attempt + 1)
        last_attempt = min(max_attempts, first_attempt + (auto_limit - 1))
        try:
            for attempt in range(first_attempt, last_attempt + 1):
                self._set_status(state="relaunching", attempt=attempt, max_attempts=max_attempts, message=f"Relaunching Executor ({attempt}/{max_attempts})")
                try:
                    if self._attempt_relaunch(path, reason):
                        return
                except Exception as exc:
                    self.logger("EXECUTOR relaunch attempt failed", "warning", error=exc, attempt=attempt, reason=reason)
                    self._set_status(state="retrying", message=str(exc))
            self._set_status(
                state="failed",
                message=self._relaunch_failure_message(last_attempt, max_attempts),
            )
        finally:
            with self._lock:
                self._running = False

    def _attempt_relaunch(self, path: str, reason: str) -> bool:
        # Auto Relaunch Executor: close Roblox + Executor, reopen Executor,
        # verify Lua loader, then rejoin (farm.start). Works both mid-session
        # and as a stopped-farm resume.
        if bool(getattr(self.farm, "running", False)) and hasattr(self.farm, "stop"):
            try:
                self.farm.stop()
            except Exception:
                "intentional: ignore exception"
        self._close_all_roblox_for_relaunch("executor_relaunch")
        self._kill_selected_processes(path)
        time.sleep(1.0)
        subprocess.Popen([path], cwd=os.path.dirname(path), close_fds=True)
        self._set_waiting_loader()
        started_at = time.time()
        if hasattr(self.farm, "start"):
            try:
                self.farm.start()
            except Exception as exc:
                # Farm.start runs the executor start-guard which sees the
                # executor we just launched, so a failure here is fatal for
                # this attempt.
                self._set_status(state="retrying", message=f"Farm restart blocked: {exc}")
                time.sleep(5)
                return False
        return self._wait_for_loader(started_at)

    def _set_waiting_loader(self) -> None:
        try:
            local = self.local_executor_info()
            detail = f"{local.get('name') or ''} {local.get('version') or ''}".strip()
            waiting_msg = f"Waiting for Lua loader{(' (' + detail + ')') if detail else ''}"
            self._set_status(state="waiting_loader", message=waiting_msg)
        except Exception:
            self._set_status(state="waiting_loader", message="Waiting for Lua loader")

    def _wait_for_loader(self, started_at: float) -> bool:
        deadline = time.time() + 60
        while time.time() < deadline:
            if self._loader_ready(started_at):
                self._set_status(state="ready", message=self._ready_message())
                return True
            # Executor may have been closed by the user mid-wait; keep
            # waiting for the loader until the deadline.
            time.sleep(1)
        self._set_status(state="retrying", message="Lua loader timeout; checking Official Roblox version again")
        try:
            self.tracker.refresh()
        except Exception:
            "intentional: ignore exception"
        return False

    def _ready_message(self) -> str:
        try:
            local = self.local_executor_info()
            detail = f"{local.get('name') or ''} {local.get('version') or ''}".strip()
            return f"Executor relaunched and Lua loader loaded{(' (' + detail + ')') if detail else ''}"
        except Exception:
            return "Executor relaunched and Lua loader loaded"

    def _relaunch_failure_message(self, last_attempt: int, max_attempts: int) -> str:
        if last_attempt < max_attempts:
            return f"Executor relaunch failed after {last_attempt} attempts; press Retry"
        return "Executor relaunch failed after 10 attempts"

    def retry(self) -> bool:
        return self.request_relaunch("manual_retry")
