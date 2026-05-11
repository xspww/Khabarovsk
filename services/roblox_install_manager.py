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
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional


VERSION_RE = re.compile(r"^(?:version-)?[0-9a-fA-F]{16,64}$")
LATEST_VERSION_URL = "https://clientsettingscdn.roblox.com/v2/client-version/WindowsPlayer"
WEAO_CURRENT_VERSION_URL = "https://weao.xyz/api/versions/current"
SETUP_BASE_URL = "https://setup.rbxcdn.com"
ROBLOX_EXE = "RobloxPlayerBeta.exe"
WEAO_USER_AGENT = "WEAO-3PService"
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


class RobloxInstallManager:
    def __init__(
        self,
        *,
        guard_running: Callable[[], bool],
        roblox_running: Callable[[], bool],
        logger: Optional[Callable[[str], None]] = None,
    ):
        self.guard_running = guard_running
        self.roblox_running = roblox_running
        self.logger = logger or (lambda _msg: None)
        self._lock = threading.RLock()
        self._job: Dict[str, Any] = self._new_job("Ready")

    def _new_job(self, state: str, **extra: Any) -> Dict[str, Any]:
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

    def status(self) -> Dict[str, Any]:
        with self._lock:
            job = dict(self._job)
        installed = self.detect_installed()
        blockers = self.find_install_blockers()
        block_msg = self._format_blockers(blockers)
        return {
            "ok": True,
            "installed": installed,
            "installed_version": installed.get("version") or "",
            "installed_path": installed.get("path") or "",
            "running_blocked": bool(self.guard_running() or self.roblox_running() or blockers),
            "blockers": blockers,
            "block_msg": block_msg,
            "job": job,
        }

    def start_uninstall(self) -> Dict[str, Any]:
        return self._start_job("uninstall", self._run_uninstall)

    def start_latest(self) -> Dict[str, Any]:
        return self._start_job("latest", self._run_latest)

    def _start_job(self, action: str, target: Callable[..., None], *args: Any) -> Dict[str, Any]:
        if self.guard_running() or self.roblox_running():
            return {"ok": False, "accepted": False, "msg": "Stop Argus and close Roblox first."}
        blockers = self.find_install_blockers()
        if blockers:
            return {"ok": False, "accepted": False, "msg": self._format_blockers(blockers), "blockers": blockers}
        with self._lock:
            if self._job.get("active"):
                return {"ok": False, "accepted": False, "msg": "Roblox install job already running.", "job": dict(self._job)}
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
            pass

    def _run_uninstall(self) -> None:
        self._set_job("Uninstalling")
        self.full_wipe()

    def _run_latest(self) -> None:
        self._set_job("Downloading", progress="Fetching version")
        version = self.fetch_latest_version()
        self._run_install_version(version)

    def _run_install_version(self, version: str) -> None:
        normalized = normalize_roblox_version(version)
        self._set_job("Uninstalling", version=normalized, progress="Wiping Roblox")
        self.full_wipe()
        self._set_job("Downloading", version=normalized, progress="Downloading packages")
        install_path = self.install_version(normalized)
        self._set_job("Installing", version=normalized, progress="Registering Roblox")
        self.register_protocols(install_path)
        self._set_job("Installing", version=normalized, progress="Validating")
        self.validate_install(install_path, require_protocol=True)
        self._set_job("Done", version=normalized, msg=f"Installed {normalized}", progress="Done")

    def roblox_roots(self) -> List[Path]:
        roots: List[Path] = []
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if local:
            roots.append(Path(local) / "Roblox")
        for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
            value = os.environ.get(env_name, "").strip()
            if value:
                roots.append(Path(value) / "Roblox")
        unique: List[Path] = []
        seen = set()
        for root in roots:
            key = str(root).lower()
            if key not in seen:
                seen.add(key)
                unique.append(root)
        return unique

    def detect_installed(self) -> Dict[str, Any]:
        candidates: List[Dict[str, Any]] = []
        for root in self.roblox_roots():
            versions_dir = root / "Versions"
            if not versions_dir.exists():
                continue
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
                candidates.append({"version": child.name, "path": str(exe), "root": str(root), "modified": modified})
        if not candidates:
            return {"installed": False, "version": "", "path": "", "root": ""}
        best = max(candidates, key=lambda item: float(item.get("modified") or 0))
        best["installed"] = True
        return best

    def find_install_blockers(self) -> List[Dict[str, Any]]:
        blockers: List[Dict[str, Any]] = []
        try:
            import psutil
        except Exception:
            return blockers

        current_pid = os.getpid()
        root_markers = [str(root).lower() for root in self.roblox_roots()]
        seen = set()
        for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
            try:
                pid = int(proc.info.get("pid") or 0)
                if not pid or pid == current_pid:
                    continue
                name = str(proc.info.get("name") or "").strip()
                name_l = name.lower()
                exe = str(proc.info.get("exe") or "").strip()
                cmdline_parts = proc.info.get("cmdline") or []
                cmdline = " ".join(str(part) for part in cmdline_parts)
                haystack = f"{exe} {cmdline}".lower()
            except Exception:
                continue

            reason = ""
            if name_l in ROBLOX_INSTALL_BLOCKER_NAMES:
                reason = "roblox_process"
            elif any(marker and marker in haystack for marker in root_markers):
                reason = "uses_roblox_path"
            if not reason:
                continue
            if pid in seen:
                continue
            seen.add(pid)
            blockers.append({
                "pid": pid,
                "name": name or f"PID {pid}",
                "path": exe,
                "reason": reason,
            })
        blockers.sort(key=lambda item: (str(item.get("name") or "").lower(), int(item.get("pid") or 0)))
        return blockers

    def _format_blockers(self, blockers: List[Dict[str, Any]]) -> str:
        if not blockers:
            return ""
        shown = ", ".join(
            f"{item.get('name') or 'Process'} (PID {item.get('pid')})"
            for item in blockers[:4]
        )
        if len(blockers) > 4:
            shown += f", +{len(blockers) - 4} more"
        return f"Close Roblox-related apps first: {shown}."

    def full_wipe(self) -> Dict[str, Any]:
        removed: List[str] = []
        failed: List[Dict[str, str]] = []
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
            or parent.endswith("\\appdata\\local")
            or parent.endswith("\\program files")
            or parent.endswith("\\program files (x86)")
        )

    def _remove_roblox_tree(self, root: Path) -> None:
        if not self._safe_roblox_root(root):
            raise RuntimeError("unsafe path")
        if not root.exists():
            return

        last_error: Optional[BaseException] = None
        for attempt in range(4):
            self._clear_tree_attributes(root)
            if attempt == 1:
                self._repair_tree_permissions(root)
            try:
                shutil.rmtree(root, onerror=self._rmtree_onerror)
                return
            except PermissionError as exc:
                last_error = exc
                time.sleep(0.15 * (attempt + 1))
            except OSError as exc:
                last_error = exc
                if getattr(exc, "winerror", None) != 5:
                    raise
                time.sleep(0.15 * (attempt + 1))

        if root.exists():
            blockers = self.find_install_blockers()
            if blockers:
                raise RuntimeError(self._format_blockers(blockers))
            detail = self._first_remaining_path(root)
            suffix = f": {last_error}" if last_error else ""
            raise RuntimeError(f"Cannot remove {detail}. Close apps using Roblox files or run Argus as Administrator{suffix}")
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
            pass
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
            pass

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
            pass
        return str(root)

    def remove_protocol_registry(self) -> Dict[str, Any]:
        removed: List[str] = []
        failed: List[Dict[str, str]] = []
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

        try:
            with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
                while True:
                    try:
                        child = winreg.EnumKey(key, 0)
                    except OSError:
                        break
                    self._delete_registry_tree(hive, f"{key_path}\\{child}")
        except FileNotFoundError:
            raise
        winreg.DeleteKey(hive, key_path)

    def fetch_latest_version(self) -> str:
        try:
            return self.fetch_weao_windows_version("current")
        except Exception as exc:
            try:
                self.logger(f"[ROBLOX_INSTALL] WEAO current failed: {exc}")
            except Exception:
                pass
        return self.fetch_official_latest_version()

    def fetch_weao_windows_version(self, channel: str = "current") -> str:
        if channel != "current":
            raise ValueError("Unsupported WEAO version channel")
        url = WEAO_CURRENT_VERSION_URL
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": WEAO_USER_AGENT}), timeout=20) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        version = data.get("Windows")
        if not version and isinstance(data.get("WindowsResponse"), dict):
            version = data["WindowsResponse"].get("clientVersionUpload")
        if not version:
            raise RuntimeError("WEAO current Windows version missing")
        return normalize_roblox_version(str(version))

    def fetch_official_latest_version(self) -> str:
        with urllib.request.urlopen(urllib.request.Request(LATEST_VERSION_URL, headers={"User-Agent": "ArgusLauncher/RT"}), timeout=20) as response:
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
        with tempfile.TemporaryDirectory(prefix="argus-roblox-install-") as temp_dir:
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

    def _parse_pkg_manifest(self, text: str) -> List[Dict[str, str]]:
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        packages: List[Dict[str, str]] = []
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
            raw_name = str(member.filename or "").replace("\\", "/").lstrip("/")
            if not raw_name:
                continue
            parts = [part for part in PurePosixPath(raw_name).parts if part not in ("", ".")]
            if not parts or any(part == ".." for part in parts) or ":" in parts[0]:
                raise RuntimeError(f"Unsafe Roblox package path: {member.filename}")
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

    def validate_install(self, exe_path: Path, *, require_protocol: bool) -> Dict[str, Any]:
        exe = Path(exe_path)
        version_root = exe.parent
        missing: List[str] = []
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

    def install_from_installer(self, version: str) -> None:
        with tempfile.TemporaryDirectory(prefix="argus-roblox-installer-") as temp_dir:
            installer = Path(temp_dir) / f"{version}-RobloxPlayerInstaller.exe"
            self._download_file(f"{SETUP_BASE_URL}/{version}-RobloxPlayerInstaller.exe", installer)
            completed = subprocess.run([str(installer)], cwd=str(installer.parent), timeout=180, check=False)
            if completed.returncode not in (0, None):
                raise RuntimeError(f"Roblox installer exited with code {completed.returncode}")

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
            pass

    def _download_text(self, url: str) -> str:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ArgusLauncher/RT"}), timeout=45) as response:
            return response.read().decode("utf-8", "replace")

    def _download_file(self, url: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ArgusLauncher/RT"}), timeout=90) as response:
            with open(path, "wb") as fh:
                shutil.copyfileobj(response, fh)
