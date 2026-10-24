from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from infra import net_tls

VERSION_RE = re.compile(r"^(?:version-)?[0-9a-fA-F]{16,64}$")
# Official Roblox endpoints only. Do NOT use third-party version trackers
# (e.g. weao.xyz) to decide which client to download.
LATEST_VERSION_URL = "https://clientsettingscdn.roblox.com/v2/client-version/WindowsPlayer"
SETUP_BASE_URL = "https://setup.rbxcdn.com"
ROBLOX_EXE = "RobloxPlayerBeta.exe"
CRONUS_USER_AGENT = "CronusLauncher/RT"
LATEST_VERSION_TTL_SECONDS = 300
ROBLOX_INSTALL_BLOCKER_NAMES = {
    "robloxplayerbeta.exe",
    "robloxplayerlauncher.exe",
    "robloxplayerinstaller.exe",
    "robloxcrashhandler.exe",
    "robloxstudiobeta.exe",
    "roblox account manager.exe",
}

PACKAGE_EXTRACT_DIRS = {
    "RobloxApp.zip": "",
    "Libraries.zip": "",
    "shaders.zip": "shaders",
    "ssl.zip": "ssl",
    "WebView2.zip": "",
    "WebView2RuntimeInstaller.zip": "WebView2RuntimeInstaller",
    "content-avatar.zip": os.path.join("content", "avatar"),
    "content-configs.zip": os.path.join("content", "configs"),
    "content-fonts.zip": os.path.join("content", "fonts"),
    "content-models.zip": os.path.join("content", "models"),
    "content-sky.zip": os.path.join("content", "sky"),
    "content-sounds.zip": os.path.join("content", "sounds"),
    "content-textures2.zip": os.path.join("content", "textures"),
    "content-textures3.zip": os.path.join("PlatformContent", "pc", "textures"),
    "content-terrain.zip": os.path.join("PlatformContent", "pc", "terrain"),
    "content-platform-fonts.zip": os.path.join("PlatformContent", "pc", "fonts"),
    "content-platform-dictionaries.zip": "",
    "extracontent-places.zip": os.path.join("ExtraContent", "places"),
    "extracontent-luapackages.zip": os.path.join("ExtraContent", "LuaPackages"),
    "extracontent-translations.zip": os.path.join("ExtraContent", "translations"),
    "extracontent-models.zip": os.path.join("ExtraContent", "models"),
    "extracontent-textures.zip": os.path.join("ExtraContent", "textures"),
}


def normalize_roblox_version(value: str) -> str:
    raw = str(value or "").strip()
    if not VERSION_RE.match(raw):
        raise ValueError("Invalid version hash")
    return raw if raw.lower().startswith("version-") else f"version-{raw}"


def _roblox_roots() -> list[Path]:
    """Single owner for Roblox installation root enumeration.

    The version-pinned launch path in roblox_http used to repeat this env
    scan inline; it now leverages this one.
    """
    roots: list[Path] = []
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        roots.append(Path(local) / "Roblox")
    for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
        value = os.environ.get(env_name, "").strip()
        if value:
            roots.append(Path(value) / "Roblox")
    unique: list[Path] = []
    seen = set()
    for root in roots:
        key = str(root).lower()
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _install_status_payload(
    installed: dict[str, Any],
    latest: dict[str, Any],
    blockers: list[dict[str, Any]],
    job: dict[str, Any],
    block_msg: str,
    running_blocked: bool,
) -> dict[str, Any]:
    installed_version = installed.get("version") or ""
    latest_version = latest.get("version") or ""
    return {
        "ok": True,
        "installed": installed,
        "installed_version": installed_version,
        "installed_path": installed.get("path") or "",
        "latest_version": latest_version,
        "latest_version_checked_at": latest.get("checked_at") or 0,
        "latest_version_error": latest.get("error") or "",
        "latest_version_refreshing": bool(latest.get("refreshing")),
        "installed_up_to_date": bool(
            installed_version and latest_version and installed_version.lower() == latest_version.lower()
        ),
        "running_blocked": running_blocked,
        "blockers": blockers,
        "block_msg": block_msg,
        "job": job,
    }


def _iter_removable_versions(
    root: Path,
    normalized: str,
    is_safe_root: Callable[[Path], bool],
) -> list[Path]:
    versions_dir = root / "Versions"
    if not versions_dir.is_dir():
        return []
    if not (root.name.lower() == "roblox" and is_safe_root(root)):
        return []
    removable: list[Path] = []
    for child in versions_dir.iterdir():
        if not child.is_dir() or not child.name.lower().startswith("version-"):
            continue
        if child.name.lower() == normalized.lower():
            continue
        removable.append(child)
    return removable


def _version_candidates(root: Path) -> list[dict[str, Any]]:
    versions_dir = root / "Versions"
    if not versions_dir.exists():
        return []
    found: list[dict[str, Any]] = []
    for child in versions_dir.iterdir():
        if not child.is_dir() or not child.name.lower().startswith("version-"):
            continue
        exe = child / ROBLOX_EXE
        if not exe.exists():
            continue
        try:
            modified = exe.stat().st_mtime
        except OSError:
            modified = 0
        # `root` is the Roblox installation root; callers opening a
        # version need the concrete version directory instead.
        found.append({"version": child.name, "path": str(exe), "root": str(child), "modified": modified})
    return found


def _dedupe_installed(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates.sort(key=lambda item: float(item.get("modified") or 0), reverse=True)
    deduped: list[dict[str, Any]] = []
    seen_versions = set()
    for item in candidates:
        key = str(item.get("version") or "").strip().lower()
        if not key or key in seen_versions:
            continue
        seen_versions.add(key)
        deduped.append(item)
    return deduped


def _process_blocker_info(proc: Any) -> dict[str, Any] | None:
    try:
        pid = int(proc.info.get("pid") or 0)
        if not pid:
            return None
        name = str(proc.info.get("name") or "").strip()
        exe = str(proc.info.get("exe") or "").strip()
        cmdline_parts = proc.info.get("cmdline") or []
        cmdline = " ".join(str(part) for part in cmdline_parts)
    except Exception:
        return None
    return {
        "pid": pid,
        "name": name,
        "name_l": name.lower(),
        "exe": exe,
        "haystack": f"{exe} {cmdline}".lower(),
    }


def _blocker_reason(info: dict[str, Any], root_markers: list[str]) -> str:
    if info["name_l"] in ROBLOX_INSTALL_BLOCKER_NAMES:
        return "roblox_process"
    if any(marker and marker in info["haystack"] for marker in root_markers):
        return "uses_roblox_path"
    return ""


def _collect_blockers(psutil: Any, current_pid: int, root_markers: list[str]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    seen = set()
    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        info = _process_blocker_info(proc)
        if info is None or info["pid"] == current_pid:
            continue
        reason = _blocker_reason(info, root_markers)
        if not reason or info["pid"] in seen:
            continue
        seen.add(info["pid"])
        blockers.append({
            "pid": info["pid"],
            "name": info["name"] or f"PID {info['pid']}",
            "path": info["exe"],
            "reason": reason,
        })
    return blockers


def _safe_member_parts(filename: Any) -> tuple[str, list[str]]:
    raw_name = str(filename or "").replace("\\", "/").lstrip("/")
    if not raw_name:
        return "", []
    parts = [part for part in PurePosixPath(raw_name).parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts) or ":" in parts[0]:
        raise RuntimeError(f"Unsafe Roblox package path: {filename}")
    return raw_name, parts


class RobloxInstallManager:
    def __init__(
        self,
        *,
        guard_running: Callable[[], bool],
        roblox_running: Callable[[], bool],
        logger: Callable[[str], None] | None = None,
    ):
        self.guard_running = guard_running
        self.roblox_running = roblox_running
        self.logger = logger or (lambda _msg: None)
        self._lock = threading.RLock()
        self._job: dict[str, Any] = self._new_job("Ready")
        self._latest_version = ""
        self._latest_version_error = ""
        self._latest_version_checked_at = 0.0
        self._latest_version_refreshing = False

    def _new_job(self, state: str, **extra: Any) -> dict[str, Any]:
        payload = {
            "active": False,
            "state": state,
            "action": "",
            "ok": state in {"Ready", "Done"},
            "msg": "",
            "version": "",
            "started_at": None,
            "finished_at": None,
            "error": "",
            "progress": "",
        }
        payload.update(extra)
        return payload

    def status(self) -> dict[str, Any]:
        with self._lock:
            job = dict(self._job)
        installed = self.detect_installed()
        latest = self._latest_version_status()
        blockers = self.find_install_blockers()
        block_msg = self._format_blockers(blockers)
        running_blocked = bool(self.guard_running() or self.roblox_running() or blockers)
        return _install_status_payload(installed, latest, blockers, job, block_msg, running_blocked)

    def _latest_version_status(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            version = self._latest_version
            error = self._latest_version_error
            checked_at = float(self._latest_version_checked_at or 0)
            refreshing = bool(self._latest_version_refreshing)
            should_refresh = not refreshing and (not checked_at or now - checked_at >= LATEST_VERSION_TTL_SECONDS)
            if should_refresh:
                self._latest_version_refreshing = True
                refreshing = True
        if should_refresh:
            threading.Thread(
                target=self._refresh_latest_version,
                name="RobloxLatestVersionRefresh",
                daemon=True,
            ).start()
        return {
            "version": version,
            "error": error,
            "checked_at": checked_at,
            "refreshing": refreshing,
        }

    def _refresh_latest_version(self) -> None:
        try:
            version = self.fetch_latest_version()
            self._store_latest_version(version)
        except Exception as exc:
            with self._lock:
                self._latest_version_error = str(exc)
                self._latest_version_checked_at = time.time()
                self._latest_version_refreshing = False

    def _store_latest_version(self, version: str) -> None:
        normalized = normalize_roblox_version(version)
        with self._lock:
            self._latest_version = normalized
            self._latest_version_error = ""
            self._latest_version_checked_at = time.time()
            self._latest_version_refreshing = False

    def start_uninstall(self) -> dict[str, Any]:
        return self._start_job("uninstall", self._run_uninstall)

    def start_latest(self) -> dict[str, Any]:
        return self._start_job("latest", self._run_latest)

    def start_update_official(self, version: str) -> dict[str, Any]:
        normalized = normalize_roblox_version(version)
        return self._start_job("update_official", self._run_update_official, normalized)

    def _start_job(self, action: str, target: Callable[..., None], *args: Any) -> dict[str, Any]:
        if self.guard_running() or self.roblox_running():
            return {"ok": False, "accepted": False, "msg": "Stop Cronus and close Roblox first"}
        blockers = self.find_install_blockers()
        if blockers:
            return {"ok": False, "accepted": False, "msg": self._format_blockers(blockers), "blockers": blockers}
        with self._lock:
            if self._job.get("active"):
                return {"ok": False, "accepted": False, "msg": "Roblox install job already running", "job": dict(self._job)}
            self._job = self._new_job("Starting", active=True, action=action, ok=False, started_at=time.time())
            job = dict(self._job)
        thread = threading.Thread(target=self._job_wrapper, args=(target, args), name=f"RobloxInstall-{action}", daemon=True)
        thread.start()
        return {"ok": True, "accepted": True, "job": job}

    def _job_wrapper(self, target: Callable[..., None], args: tuple) -> None:
        try:
            target(*args)
            with self._lock:
                self._job.update(active=False, ok=True, finished_at=time.time(), progress="Done")
                if self._job.get("state") != "Done":
                    self._job.update(state="Done", msg="Done")
        except Exception as exc:
            with self._lock:
                self._job.update(active=False, state="Failed", ok=False, msg=str(exc), error=str(exc), finished_at=time.time())

    def _set_job(self, state: str, **extra: Any) -> None:
        with self._lock:
            self._job.update(state=state, progress=extra.pop("progress", state), **extra)
        try:
            self.logger(f"[ROBLOX_INSTALL] {state} {extra}")
        except Exception:
            "intentional: ignore exception"

    def _run_uninstall(self) -> None:
        self._set_job("Uninstalling")
        self.full_wipe()

    def _run_latest(self) -> None:
        self._set_job("Downloading", progress="Fetching version")
        version = self.fetch_latest_version()
        self._store_latest_version(version)
        self._run_install_version(version)

    def is_job_active(self) -> bool:
        with self._lock:
            return bool(self._job.get("active"))

    def _run_update_official(self, version: str) -> None:
        # Official-only: `version` must be the exact
        # clientVersionUpload string from clientsettingscdn.roblox.com.
        # Packages are fetched only from setup.rbxcdn.com manifests and
        # installed byte-identical (no patches/mods) so the client stays
        # an official Roblox build.
        normalized = normalize_roblox_version(version)
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if not local:
            raise RuntimeError("LOCALAPPDATA is not available")
        # Install target: official Local Roblox only (registered + protocol).
        root = Path(local) / "Roblox"
        self._set_job("Downloading", version=normalized, progress="Updating Roblox client")
        target = root / "Versions" / normalized
        target.mkdir(parents=True, exist_ok=True)
        self._set_job("Installing", version=normalized, progress="Installing Roblox")
        self.install_from_manifest(normalized, target)
        self.write_app_settings(target)
        self.register_protocols(target / ROBLOX_EXE)
        self.validate_install(target / ROBLOX_EXE, require_protocol=True)
        # Keep the previous client until the new manifest has downloaded and
        # passed validation. Only then remove older version
        # directories (Auto update deletes old versions).
        removed = self.remove_old_versions(normalized)
        self._set_job("Done", version=normalized, removed_old_versions=removed, msg=f"Updated Roblox to {normalized} (removed {removed} old)", progress="Done")

    def _run_install_version(self, version: str) -> None:
        normalized = normalize_roblox_version(version)
        self._set_job("Downloading", version=normalized, progress="Downloading packages")
        install_path = self.install_version(normalized)
        self._set_job("Installing", version=normalized, progress="Registering Roblox")
        self.register_protocols(install_path)
        self._set_job("Installing", version=normalized, progress="Validating")
        self.validate_install(install_path, require_protocol=True)
        # Keep the previous client until the new manifest has downloaded and
        # passed validation. Only then remove older version directories.
        removed = self.remove_old_versions(normalized)
        self._set_job("Done", version=normalized, removed_old_versions=removed, msg=f"Installed {normalized}", progress="Done")

    def remove_old_versions(self, version: str, roots: list[Path] | None = None) -> int:
        """Single owner for old-version cleanup.

        Both the update and the install paths used to repeat this loop
        inline with slightly different error handling; the install decision
        now leverages this one. Keeps the current version, removes older
        version-* directories, skips anything else. Never raises.
        """
        normalized = str(version or "")
        removed = 0
        candidates = list(roots) if roots is not None else list(self.roblox_roots())
        for root in candidates:
            for child in _iter_removable_versions(root, normalized, self._safe_roblox_root):
                try:
                    shutil.rmtree(child, ignore_errors=False)
                    removed += 1
                except Exception as exc:
                    self.logger(f"[ROBLOX_INSTALL] remove old version failed: {child} ({exc})")
        return removed

    def roblox_roots(self) -> list[Path]:
        return _roblox_roots()

    def list_installed(self) -> list[dict[str, Any]]:
        """Return every installed Roblox player version, newest-modified first.

        Deduplicated by version hash: the same ``version-xxxx`` folder can
        exist under multiple roots (LocalAppData vs Program Files). Without
        dedup the Join-game picker renders two identical "Roblox LAST" rows,
        which then flicker when post-render JS tries to remove the duplicate.
        """
        candidates: list[dict[str, Any]] = []
        for root in self.roblox_roots():
            candidates.extend(_version_candidates(root))
        return _dedupe_installed(candidates)

    def detect_installed(self) -> dict[str, Any]:
        candidates = self.list_installed()
        if not candidates:
            return {"installed": False, "version": "", "path": "", "root": ""}
        best = dict(candidates[0])
        best["installed"] = True
        return best

    def find_install_blockers(self) -> list[dict[str, Any]]:
        try:
            import psutil
        except Exception:
            return []

        current_pid = os.getpid()
        root_markers = [str(root).lower() for root in self.roblox_roots()]
        blockers = _collect_blockers(psutil, current_pid, root_markers)
        blockers.sort(key=lambda item: (str(item.get("name") or "").lower(), int(item.get("pid") or 0)))
        return blockers

    def _format_blockers(self, blockers: list[dict[str, Any]]) -> str:
        if not blockers:
            return ""
        # Keep the install banner to one short line so it does not wrap to
        # 2 lines and shift layout on every poll. Full PID details stay in
        # the `blockers` array for debugging/tooltips.
        count = len(blockers)
        if count == 1:
            return "Close Roblox first (1 app running)"
        return f"Close Roblox first ({count} apps running)"

    def full_wipe(self) -> dict[str, Any]:
        removed: list[str] = []
        failed: list[dict[str, str]] = []
        for root in self.roblox_roots():
            if not self._safe_roblox_root(root):
                failed.append({"path": str(root), "error": "unsafe path"})
                continue
            if not root.exists():
                continue
            try:
                self._remove_roblox_tree(root)
                removed.append(str(root))
            except Exception as exc:
                failed.append({"path": str(root), "error": str(exc)})
        registry = self.remove_protocol_registry()
        if failed:
            raise RuntimeError("Failed to remove Roblox: " + "; ".join(f"{x['path']}: {x['error']}" for x in failed))
        return {"removed": removed, "registry": registry}

    def _safe_roblox_root(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path.absolute()
        name_ok = resolved.name.lower() == "roblox"
        parent = str(resolved.parent).lower()
        allowed_parents = set()
        for env_name in ("LOCALAPPDATA", "ProgramFiles(x86)", "ProgramFiles"):
            value = os.environ.get(env_name, "").strip()
            if value:
                try:
                    allowed_parents.add(str(Path(value).resolve()).lower())
                except Exception:
                    allowed_parents.add(str(Path(value).absolute()).lower())
        return name_ok and (
            parent in allowed_parents
            or parent.endswith(("\\appdata\\local", "\\program files", "\\program files (x86)"))
        )

    def _remove_tree_attempts(self, root: Path) -> tuple[bool, BaseException | None]:
        last_error: BaseException | None = None
        for attempt in range(4):
            self._clear_tree_attributes(root)
            if attempt == 1:
                self._repair_tree_permissions(root)
            try:
                shutil.rmtree(root, onerror=self._rmtree_onerror)
                return True, None
            except PermissionError as exc:
                last_error = exc
                time.sleep(0.15 * (attempt + 1))
            except OSError as exc:
                last_error = exc
                if getattr(exc, "winerror", None) != 5:
                    raise
                time.sleep(0.15 * (attempt + 1))
        return False, last_error

    def _remove_roblox_tree(self, root: Path) -> None:
        if not self._safe_roblox_root(root):
            raise RuntimeError("unsafe path")
        if not root.exists():
            return

        removed, last_error = self._remove_tree_attempts(root)
        if removed:
            return
        if root.exists():
            blockers = self.find_install_blockers()
            if blockers:
                raise RuntimeError(self._format_blockers(blockers))
            detail = self._first_remaining_path(root)
            suffix = f": {last_error}" if last_error else ""
            raise RuntimeError(f"Cannot remove {detail}. Close apps using Roblox files or run Cronus as Administrator{suffix}")
        if last_error:
            raise RuntimeError(str(last_error))

    def _rmtree_onerror(self, func: Callable[..., Any], path: str, exc_info: Any) -> None:
        self._clear_file_attributes(Path(path))
        try:
            func(path)
        except PermissionError:
            raise
        except OSError as exc:
            if getattr(exc, "winerror", None) == 5:
                raise PermissionError(str(exc)) from exc
            raise

    def _clear_tree_attributes(self, root: Path) -> None:
        self._clear_file_attributes(root)
        if not root.exists():
            return
        for current, dirs, files in os.walk(root, topdown=False):
            for name in files:
                self._clear_file_attributes(Path(current) / name)
            for name in dirs:
                self._clear_file_attributes(Path(current) / name)
            self._clear_file_attributes(Path(current))

    def _clear_file_attributes(self, path: Path) -> None:
        try:
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
        except OSError:
            "intentional: ignore exception"
        if os.name != "nt":
            return
        try:
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
            if attrs == 0xFFFFFFFF:
                return
            clean_attrs = attrs & ~(0x1 | 0x2 | 0x4)
            if clean_attrs != attrs:
                ctypes.windll.kernel32.SetFileAttributesW(str(path), clean_attrs)
        except Exception:
            "intentional: ignore exception"

    def _repair_tree_permissions(self, root: Path) -> None:
        if os.name != "nt" or not self._safe_roblox_root(root) or not root.exists():
            return
        commands = [
            ["attrib", "-R", "-S", "-H", str(root)],
            ["attrib", "-R", "-S", "-H", str(root / "*"), "/S", "/D"],
            ["takeown", "/F", str(root), "/R", "/D", "Y"],
            ["icacls", str(root), "/grant", "*S-1-5-32-544:(OI)(CI)F", "/T", "/C", "/Q"],
        ]
        username = os.environ.get("USERNAME", "").strip()
        domain = os.environ.get("USERDOMAIN", "").strip()
        if username:
            account = f"{domain}\\{username}" if domain else username
            commands.append(["icacls", str(root), "/grant", f"{account}:(OI)(CI)F", "/T", "/C", "/Q"])
        for command in commands:
            try:
                subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45, check=False)
            except Exception:
                continue

    def _first_remaining_path(self, root: Path) -> str:
        try:
            for current, dirs, files in os.walk(root):
                for name in files:
                    return str(Path(current) / name)
                for name in dirs:
                    return str(Path(current) / name)
        except Exception:
            "intentional: ignore exception"
        return str(root)

    def remove_protocol_registry(self) -> dict[str, Any]:
        removed: list[str] = []
        failed: list[dict[str, str]] = []
        if os.name != "nt":
            return {"removed": removed, "failed": failed}
        try:
            import winreg
        except Exception as exc:
            return {"removed": removed, "failed": [{"path": "HKCU", "error": str(exc)}]}
        for key in (
            r"Software\Classes\roblox",
            r"Software\Classes\roblox-player",
            r"Software\ROBLOX Corporation",
            r"Software\Microsoft\Windows\CurrentVersion\Uninstall\RobloxPlayer",
        ):
            try:
                self._delete_registry_tree(winreg.HKEY_CURRENT_USER, key)
                removed.append(f"HKCU\\{key}")
            except FileNotFoundError:
                continue
            except Exception as exc:
                failed.append({"path": f"HKCU\\{key}", "error": str(exc)})
        return {"removed": removed, "failed": failed}

    def _delete_registry_tree(self, hive: Any, key_path: str) -> None:
        import winreg

        with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                self._delete_registry_tree(hive, f"{key_path}\\{child}")
        winreg.DeleteKey(hive, key_path)

    def fetch_latest_version(self) -> str:
        # Official-only: always ask clientsettingscdn.roblox.com directly.
        return self.fetch_official_latest_version()

    def fetch_official_latest_version(self) -> str:
        with net_tls.urlopen(urllib.request.Request(LATEST_VERSION_URL, headers={"User-Agent": CRONUS_USER_AGENT}), timeout=20) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        version = data.get("clientVersionUpload") or data.get("version")
        return normalize_roblox_version(str(version or ""))

    def install_version(self, version: str) -> Path:
        normalized = normalize_roblox_version(version)
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if not local:
            raise RuntimeError("LOCALAPPDATA is not available")
        target = Path(local) / "Roblox" / "Versions" / normalized
        target.mkdir(parents=True, exist_ok=True)
        self.install_from_manifest(normalized, target)
        self.write_app_settings(target)
        exe = target / ROBLOX_EXE
        self.validate_install(exe, require_protocol=False)
        return exe

    def install_from_manifest(self, version: str, target: Path) -> None:
        manifest_url = f"{SETUP_BASE_URL}/{version}-rbxPkgManifest.txt"
        manifest_text = self._download_text(manifest_url)
        packages = self._parse_pkg_manifest(manifest_text)
        if not packages:
            raise RuntimeError("No Roblox packages found in manifest")
        with tempfile.TemporaryDirectory(prefix="cronus-roblox-install-") as temp_dir:
            temp = Path(temp_dir)
            for package in packages:
                name = str(package.get("name") or "").strip()
                if not name.lower().endswith(".zip"):
                    continue
                self._set_job("Downloading", progress=name, version=version)
                zip_path = temp / name
                self._download_file(f"{SETUP_BASE_URL}/{version}-{name}", zip_path)
                self._set_job("Installing", progress=f"Extracting {name}", version=version)
                extract_dir = self.package_extract_target(target, name)
                with zipfile.ZipFile(zip_path) as archive:
                    self._safe_extract_zip(archive, extract_dir)

    def _parse_pkg_manifest(self, text: str) -> list[dict[str, str]]:
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        packages: list[dict[str, str]] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            if line.lower().endswith(".zip"):
                packages.append({"name": line})
                index += 4
                continue
            index += 1
        return packages

    def package_extract_target(self, version_root: Path, package_name: str) -> Path:
        relative = PACKAGE_EXTRACT_DIRS.get(str(package_name or "").strip(), "")
        target = version_root / relative if relative else version_root
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _safe_extract_zip(self, archive: zipfile.ZipFile, target: Path) -> None:
        root = target.resolve()
        for member in archive.infolist():
            raw_name, parts = _safe_member_parts(member.filename)
            if not parts:
                continue
            destination = target.joinpath(*parts).resolve()
            if destination != root and root not in destination.parents:
                raise RuntimeError(f"Unsafe Roblox package path: {member.filename}")
            if member.is_dir() or raw_name.endswith("/"):
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, open(destination, "wb") as dst:
                shutil.copyfileobj(src, dst)

    def write_app_settings(self, version_root: Path) -> Path:
        path = Path(version_root) / "AppSettings.xml"
        path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\r\n'
            "<Settings>\r\n"
            " <ContentFolder>content</ContentFolder>\r\n"
            " <BaseUrl>http://www.roblox.com</BaseUrl>\r\n"
            "</Settings>\r\n",
            encoding="utf-8",
            newline="",
        )
        return path

    def validate_install(self, exe_path: Path, *, require_protocol: bool) -> dict[str, Any]:
        exe = Path(exe_path)
        version_root = exe.parent
        missing: list[str] = []
        if not exe.exists():
            missing.append(ROBLOX_EXE)
        if not (version_root / "AppSettings.xml").exists():
            missing.append("AppSettings.xml")
        for name in ("content", "PlatformContent", "ExtraContent", "shaders", "ssl"):
            if not (version_root / name).exists():
                missing.append(name)
        if require_protocol:
            for scheme in ("roblox", "roblox-player"):
                if not self.protocol_points_to(exe, scheme):
                    missing.append(f"{scheme} protocol")
        if missing:
            raise RuntimeError("Roblox install incomplete: " + ", ".join(missing))
        return {"ok": True, "path": str(exe), "root": str(version_root)}

    def protocol_points_to(self, exe_path: Path, scheme: str) -> bool:
        if os.name != "nt":
            return True
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{scheme}\shell\open\command") as key:
                command = str(winreg.QueryValueEx(key, "")[0])
        except Exception:
            return False
        expected = str(Path(exe_path)).lower()
        return expected in command.lower()

    def register_protocols(self, exe_path: Path) -> None:
        if os.name != "nt":
            return
        import winreg

        command = f'"{exe_path}" "%1"'
        for scheme in ("roblox", "roblox-player"):
            base = rf"Software\Classes\{scheme}"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f"URL:{scheme} Protocol")
                winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{base}\shell\open\command") as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)
        try:
            ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
        except Exception:
            "intentional: ignore exception"

    def _download_text(self, url: str) -> str:
        with net_tls.urlopen(urllib.request.Request(url, headers={"User-Agent": "CronusLauncher/RT"}), timeout=45) as response:
            return response.read().decode("utf-8", "replace")

    def _download_file(self, url: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with (
            net_tls.urlopen(urllib.request.Request(url, headers={"User-Agent": "CronusLauncher/RT"}), timeout=90) as response,
            open(path, "wb") as fh,
        ):
            shutil.copyfileobj(response, fh)
