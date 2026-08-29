from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any, Dict


EXECUTOR_NAMES = ("Volt", "Potassium", "Real", "Madium")


class ExecutorRelaunchService:
    """Relaunch the selected executor once per WEAO version transition."""

    def __init__(self, cfg_mgr: Any, farm: Any, tracker: Any, logger: Any = None):
        self.cfg_mgr, self.farm, self.tracker = cfg_mgr, farm, tracker
        self.logger = logger or (lambda *_args, **_kwargs: None)
        self._lock = threading.RLock()
        self._running = False
        self._launch_inflight: Dict[str, float] = {}
        self._status: Dict[str, Any] = {"state": "idle", "attempt": 0, "max_attempts": 10, "message": ""}

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            return self.cfg_mgr.get(key, default)
        except Exception:
            return default

    @staticmethod
    def path_key(name: str) -> str:
        return f"executor_path_{str(name or '').strip().lower()}"

    def paths(self) -> Dict[str, str]:
        return {name: str(self._cfg(self.path_key(name), "") or "").strip() for name in EXECUTOR_NAMES}

    def selected_path(self) -> str:
        return self.paths().get(str(self._cfg("executor_selected", "") or "").strip(), "")

    def relaunch_support(self) -> tuple[bool, str]:
        """Return whether the selected executor can be relaunched safely."""
        selected = str(self._cfg("executor_selected", "") or "").strip()
        if selected not in EXECUTOR_NAMES:
            return False, "Selected Executor is not supported"
        path = self.selected_path()
        if not path:
            return False, "Browse the selected Executor .exe first"
        if not os.path.isfile(path) or not path.lower().endswith(".exe"):
            return False, "Selected Executor .exe path is missing"
        return True, ""

    def status(self) -> Dict[str, Any]:
        with self._lock:
            supported, reason = self.relaunch_support()
            return {**self._status, "running": self._running, "paths": self.paths(), "selected_path": self.selected_path(), "relaunch_supported": supported, "relaunch_support_reason": reason}

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
        selected = str(self._cfg("executor_selected", "") or "").strip().lower()
        filename = os.path.basename(configured)
        stem, _ext = os.path.splitext(filename)
        names = {filename.lower()}

        # Madium's Browse target is its installer, while the running process
        # is always the updated binary under <root>\\Bin\\Madium.exe.
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

        roots = []
        current = os.path.dirname(configured)
        for _ in range(3):
            if not current or current in roots:
                break
            roots.append(current)
            current = os.path.dirname(current)

        # Find same-named runtime binaries only inside the selected install
        # tree.  This covers versioned auto-update folders without matching a
        # different executor elsewhere on the machine.
        for root in roots:
            try:
                for dirpath, _dirnames, filenames in os.walk(root):
                    depth = os.path.relpath(dirpath, root).count(os.sep)
                    if depth > 2:
                        continue
                    for candidate in filenames:
                        if candidate.lower() in names:
                            targets.add(os.path.normcase(os.path.abspath(os.path.join(dirpath, candidate))))
            except OSError:
                continue
        return targets

    def _kill_selected_processes(self, path: str) -> int:
        try:
            import psutil
        except ImportError:
            return 0
        targets = self._process_targets(path)
        victims = []
        for proc in psutil.process_iter(["pid"]):
            try:
                exe = proc.exe()
                if exe and os.path.normcase(os.path.abspath(exe)) in targets:
                    victims.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                continue
        for proc in victims:
            try:
                proc.terminate()
            except Exception:
                pass
        if victims:
            _gone, alive = psutil.wait_procs(victims, timeout=10)
            for proc in alive:
                try:
                    proc.kill()
                except Exception:
                    pass
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
        if not bool(self._cfg("executor_relaunch_enabled", False)):
            return True
        selected = str(self._cfg("executor_selected", "") or "").strip()
        path = self.selected_path()
        if not selected or not path:
            self._set_status(state="error", message="Select an Executor and Browse its .exe path before Start")
            return False
        if not os.path.isfile(path) or not path.lower().endswith(".exe"):
            self._set_status(state="error", message="Selected Executor .exe path is missing")
            return False
        with self._lock:
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
                self._status.update(state="error", message=f"Failed to open {selected}: {exc}")
                self.logger("EXECUTOR", "start_failed", "warning", error=exc, executor=selected)
                return False
            self._launch_inflight[launch_key] = now
            self._status.update(state="started", attempt=0, message=f"Started {selected}")
            return True

    def _loader_ready(self, started_at: float) -> bool:
        for account in list(getattr(self.farm, "_accounts", []) or []):
            try:
                if float(getattr(account, "lua_last_event_at", 0) or 0) >= started_at and str(getattr(account, "lua_last_event", "") or "") in {"loaded", "in_game"}:
                    return True
            except Exception:
                continue
        return False

    def request_relaunch(self, reason: str = "executor_version_changed") -> bool:
        with self._lock:
            if self._running or not bool(self._cfg("executor_relaunch_enabled", False)):
                return False
            if not bool(self._cfg("auto_rejoin", True)):
                return False
            # A WEAO poll must never start the Executor or Farm on its own.
            # Automatic relaunch is only allowed during an active Farm session;
            # an explicit Retry button remains allowed while stopped.
            if not getattr(self.farm, "running", False) and reason != "manual_retry":
                return False
            path = self.selected_path()
            if not path or not os.path.isfile(path) or not path.lower().endswith(".exe"):
                self._status = {"state": "error", "attempt": 0, "max_attempts": 10, "message": "Selected Executor .exe path is missing"}
                return False
            self._running = True
        prior_attempt = int(self._status.get("attempt", 0) or 0) if reason == "manual_retry" else 0
        threading.Thread(target=self._run, args=(path, reason, prior_attempt), name="ExecutorRelaunch", daemon=True).start()
        return True

    def _run(self, path: str, reason: str, prior_attempt: int = 0) -> None:
        auto_limit = 5
        max_attempts = 10
        first_attempt = max(1, prior_attempt + 1)
        last_attempt = min(max_attempts, first_attempt + (auto_limit - 1))
        try:
            for attempt in range(first_attempt, last_attempt + 1):
                self._set_status(state="relaunching", attempt=attempt, max_attempts=max_attempts, message=f"Relaunching Executor ({attempt}/{max_attempts})")
                try:
                    if hasattr(self.farm, "stop"):
                        self.farm.stop()
                    if hasattr(self.farm, "close_all_roblox"):
                        self.farm.close_all_roblox(reason="executor_relaunch")
                    self._kill_selected_processes(path)
                    time.sleep(1.0)
                    subprocess.Popen([path], cwd=os.path.dirname(path), close_fds=True)
                    self._set_status(state="waiting_loader", message="Waiting for Lua loader")
                    started_at = time.time()
                    if hasattr(self.farm, "start"):
                        self.farm.start()
                    deadline = time.time() + 60
                    while time.time() < deadline:
                        if self._loader_ready(started_at):
                            self._set_status(state="ready", message="Executor relaunched and Lua loader loaded")
                            return
                        time.sleep(1)
                    self._set_status(state="retrying", message="Lua loader timeout; checking WEAO version again")
                    self.tracker.refresh()
                except Exception as exc:
                    self.logger("EXECUTOR relaunch attempt failed", "warning", error=exc, attempt=attempt, reason=reason)
                    self._set_status(state="retrying", message=str(exc))
            self._set_status(state="failed", message=f"Executor relaunch failed after {last_attempt} attempts; press Retry" if last_attempt < max_attempts else "Executor relaunch failed after 10 attempts")
        finally:
            with self._lock:
                self._running = False

    def retry(self) -> bool:
        return self.request_relaunch("manual_retry")
