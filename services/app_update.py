from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from app_paths import APP_DATA_DIR, EXECUTABLE_PATH, IS_COMPILED
from version import (
    APP_VERSION,
    CHECKSUMS_ASSET,
    exe_asset_name,
    is_newer_version,
    portable_asset_name,
    release_tag_url,
    releases_api_latest,
    releases_api_list,
    releases_page_url,
    strip_tag_prefix,
)

UPDATE_USER_AGENT = f"CronusLauncher-Update/{APP_VERSION}"
STAGE_DIRNAME = "update_stage"
RESULT_FILENAME = "update_result.json"
UPDATER_FILENAME = "cronus_updater.cmd"
HTTP_TIMEOUT_SECONDS = 20.0
INITIAL_CHECK_DELAY_SECONDS = 5.0
ROLLBACK_PROBE_TIMEOUT_SECONDS = 60
DOWNLOAD_CHUNK_BYTES = 1024 * 256


def _log_kv(scope: str, name: str, level: str = "info", **fields: Any) -> None:
    try:
        from core import flog_kv

        flog_kv(scope, name, level, **fields)
    except Exception:
        pass


def _normalize_channel(value: Any) -> str:
    channel = str(value or "").strip().lower()
    return channel if channel in {"stable", "beta"} else "stable"


def _parse_checksums(text: str) -> Dict[str, str]:
    """Parse `sha256sum` output (<hash><spaces>[*]<filename>) into {filename: hash}."""
    out: Dict[str, str] = {}
    for raw_line in str(text or "").lstrip("\ufeff").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        digest = parts[0].strip().lower()
        name = parts[-1].strip().lstrip("*").strip()
        name = os.path.basename(name.replace("\\", "/"))
        if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest) and name:
            out[name] = digest
    return out


def _sha256_file(path: str, progress=None) -> str:
    digest = hashlib.sha256()
    total = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if progress:
                try:
                    progress(total)
                except Exception:
                    pass
    return digest.hexdigest()


def build_updater_script(
    *,
    parent_pid: int,
    current_exe: str,
    staged_new_exe: str,
    backup_exe: str,
    expected_version: str,
    result_file: str,
    probe_timeout_seconds: int = ROLLBACK_PROBE_TIMEOUT_SECONDS,
) -> str:
    """Windows batch that swaps the launcher exe after this process exits.

    Steps: wait parent exit -> backup current -> move staged in place ->
    launch new -> probe /api/status for the expected version -> on failure
    kill new, restore backup, relaunch old, and always write a result file.
    """
    probe_endpoints = " ".join(f"http://127.0.0.1:{port}/api/status" for port in range(7777, 7797))
    lines = [
        "@echo off",
        "setlocal",
        f'set "PARENT_PID={int(parent_pid)}"',
        f'set "CURRENT_EXE={current_exe}"',
        f'set "STAGED_EXE={staged_new_exe}"',
        f'set "BACKUP_EXE={backup_exe}"',
        f'set "RESULT_FILE={result_file}"',
        f'set "EXPECTED={expected_version}"',
        f'set "TIMEOUT_S={max(15, int(probe_timeout_seconds or ROLLBACK_PROBE_TIMEOUT_SECONDS))}',
        'for %%F in ("%CURRENT_EXE%") do set "CURRENT_NAME=%%~nxF"',
        'for %%F in ("%BACKUP_EXE%") do set "BACKUP_NAME=%%~nxF"',
        "",
        ":wait_parent",
        'tasklist /FI "PID eq %PARENT_PID%" 2>nul | findstr /I /C:"%PARENT_PID%" >nul',
        "if %errorlevel%==0 ( timeout /t 1 /nobreak >nul & goto wait_parent )",
        "timeout /t 2 /nobreak >nul",
        "",
        'if exist "%BACKUP_EXE%" del /f /q "%BACKUP_EXE%"',
        'if exist "%CURRENT_EXE%" ren "%CURRENT_EXE%" "%BACKUP_NAME%"',
        'if not exist "%BACKUP_EXE%" (',
        '  >"%RESULT_FILE%" echo {"ok":false,"action":"backup_failed","version":"%EXPECTED%"}',
        "  exit /b 1",
        ")",
        'move /y "%STAGED_EXE%" "%CURRENT_EXE%" >nul',
        'if errorlevel 1 (',
        '  ren "%BACKUP_EXE%" "%CURRENT_NAME%"',
        '  >"%RESULT_FILE%" echo {"ok":false,"action":"replace_failed","version":"%EXPECTED%"}',
        "  exit /b 1",
        ")",
        "",
        'start "" "%CURRENT_EXE%"',
        # Wall-clock budget: a sweep over 20 ports can take 40s+ on hosts
        # where the firewall drops localhost SYNs, so counting sweeps is not
        # a clock. Epoch comes from PowerShell; SWEEPS is the fallback.
        'for /f %%T in (\'powershell -NoProfile -Command "[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()" 2^>nul\') do set "START_EPOCH=%%T"',
        'if not defined START_EPOCH set "START_EPOCH=0"',
        "set /a SWEEPS=0",
        ":probe",
        "for %%U in (",
        f"  {probe_endpoints}",
        ") do (",
        '  curl.exe -fsSL --connect-timeout 1 --max-time 2 --noproxy * "%%U" -o "%TEMP%\\cronus_update_probe.json" 2>nul',
        "  if not errorlevel 1 (",
        '    find "app_version" "%TEMP%\\cronus_update_probe.json" >nul',
        "    if not errorlevel 1 (",
        '      find "%EXPECTED%" "%TEMP%\\cronus_update_probe.json" >nul',
        "      if not errorlevel 1 goto healthy",
        "    )",
        "  )",
        ")",
        "timeout /t 1 /nobreak >nul",
        "set /a SWEEPS+=1",
        'for /f %%T in (\'powershell -NoProfile -Command "[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()" 2^>nul\') do set "NOW_EPOCH=%%T"',
        "if %START_EPOCH% GTR 0 if defined NOW_EPOCH goto epoch_check",
        "goto sweep_cap",
        ":epoch_check",
        "set /a ELAPSED=NOW_EPOCH-START_EPOCH",
        "if %ELAPSED% GEQ %TIMEOUT_S% goto rollback",
        ":sweep_cap",
        "if %SWEEPS% GEQ 30 goto rollback",
        "goto probe",
        "",
        ":healthy",
        'del /f /q "%TEMP%\\cronus_update_probe.json" 2>nul',
        '>"%RESULT_FILE%" echo {"ok":true,"action":"installed","version":"%EXPECTED%"}',
        "exit /b 0",
        "",
        ":rollback",
        'for /f "tokens=2 delims=," %%P in (\'tasklist /FI "IMAGENAME eq %CURRENT_NAME%" /FO CSV /NH 2^>nul\') do (',
        '  set "NEWPID=%%~P"',
        ")",
        'if defined NEWPID taskkill /F /PID %NEWPID% >nul 2>&1',
        'del /f /q "%CURRENT_EXE%"',
        'ren "%BACKUP_EXE%" "%CURRENT_NAME%"',
        'start "" "%CURRENT_EXE%"',
        '>"%RESULT_FILE%" echo {"ok":false,"action":"rolled_back","version":"%EXPECTED%"}',
        "exit /b 2",
    ]
    return "\r\n".join(lines) + "\r\n"


class AppUpdateService:
    """Self-update for the launcher itself via GitHub Releases.

    Stable channel reads /releases/latest (GitHub excludes prereleases
    there). Beta channel lists /releases and accepts prereleases too.
    Install is only allowed while the farm is stopped, because replacing
    the exe requires this process to exit and farm.stop() closes Roblox.
    """

    def __init__(self, cfg_mgr: Any = None, farm: Any = None):
        self._cfg_mgr = cfg_mgr
        self._farm = farm
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._checking = False
        self._downloading = False
        self._state: Dict[str, Any] = {
            "state": "idle",
            "channel": "stable",
            "current_version": APP_VERSION,
            "latest_version": "",
            "latest_tag": "",
            "latest_notes": "",
            "latest_url": "",
            "asset_name": "",
            "asset_size": 0,
            "progress_percent": 0.0,
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "staged_file": "",
            "verified": False,
            "error": "",
            "checked_at": 0.0,
            "last_install": {},
        }
        self._pickup_last_install_result()

    # -- config ---------------------------------------------------------
    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            if self._cfg_mgr is not None:
                return self._cfg_mgr.get(key, default)
        except Exception:
            pass
        return default

    def _stage_dir(self) -> str:
        path = os.path.join(APP_DATA_DIR, STAGE_DIRNAME)
        os.makedirs(path, exist_ok=True)
        return path

    def _result_file(self) -> str:
        return os.path.join(self._stage_dir(), RESULT_FILENAME)

    def _target_exe(self) -> str:
        if IS_COMPILED and EXECUTABLE_PATH:
            return os.path.abspath(EXECUTABLE_PATH)
        return ""

    def _farm_running(self) -> bool:
        try:
            return bool(getattr(self._farm, "running", False))
        except Exception:
            return False

    def _bump_status(self) -> None:
        try:
            bump = getattr(self._farm, "_bump_status_revision", None)
            if callable(bump):
                bump()
        except Exception:
            pass

    def _set_state(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)

    # -- public status --------------------------------------------------
    def status_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            snap = dict(self._state)
        snap["compiled"] = bool(IS_COMPILED)
        snap["farm_running"] = self._farm_running()
        snap["update_available"] = bool(
            snap.get("latest_version") and is_newer_version(str(snap["latest_version"]), APP_VERSION)
        )
        snap["can_install"] = bool(
            snap["update_available"]
            and snap.get("verified")
            and snap.get("staged_file")
            and os.path.isfile(str(snap.get("staged_file") or ""))
            and IS_COMPILED
            and not snap["farm_running"]
            and not self._downloading
        )
        return snap

    def status_summary(self) -> Dict[str, Any]:
        snap = self.status_snapshot()
        return {
            "state": snap.get("state", "idle"),
            "current_version": snap.get("current_version", APP_VERSION),
            "latest_version": snap.get("latest_version", ""),
            "update_available": snap.get("update_available", False),
            "progress_percent": round(float(snap.get("progress_percent") or 0.0), 1),
            "can_install": snap.get("can_install", False),
            "error": snap.get("error", ""),
        }

    # -- http -----------------------------------------------------------
    def _api_get_json(self, url: str) -> Any:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": UPDATE_USER_AGENT,
                "Accept": "application/vnd.github+json",
            },
        )
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            body = response.read()
        return json.loads(body.decode("utf-8", errors="replace"))

    def _fetch_release(self, channel: str) -> Optional[Dict[str, Any]]:
        channel = _normalize_channel(channel)
        try:
            if channel == "beta":
                items = self._api_get_json(releases_api_list())
                if not isinstance(items, list):
                    return None
                for item in items:
                    if not isinstance(item, dict) or item.get("draft"):
                        continue
                    tag = str(item.get("tag_name") or "").strip()
                    if tag and is_newer_version(tag, APP_VERSION):
                        return item
                return None
            payload = self._api_get_json(releases_api_latest())
            if not isinstance(payload, dict) or payload.get("draft"):
                return None
            tag = str(payload.get("tag_name") or "").strip()
            if tag and is_newer_version(tag, APP_VERSION):
                return payload
            return None
        except urllib.error.HTTPError as exc:
            if getattr(exc, "code", 0) == 404:
                return None
            raise
        except Exception:
            raise

    @staticmethod
    def _release_assets(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        assets = payload.get("assets")
        return [a for a in assets if isinstance(a, dict)] if isinstance(assets, list) else []

    def _pick_asset(self, payload: Dict[str, Any], latest_version: str) -> Tuple[str, str, int]:
        wanted = exe_asset_name(latest_version) if IS_COMPILED else portable_asset_name(latest_version)
        tag = str(payload.get("tag_name") or "").strip()
        for asset in self._release_assets(payload):
            if str(asset.get("name") or "").strip() == wanted:
                url = str(asset.get("browser_download_url") or "").strip()
                if url:
                    return wanted, url, int(asset.get("size") or 0)
        fallback = f"https://github.com/{_owner_repo_path()}/releases/download/{tag}/{wanted}"
        return wanted, fallback, 0

    # -- check ----------------------------------------------------------
    def check_for_updates(self, manual: bool = False) -> Dict[str, Any]:
        with self._lock:
            if self._checking or self._downloading:
                snap = self.status_snapshot()
                snap["ok"] = False
                snap["msg"] = "Update check already in progress"
                return snap
            self._checking = True
        try:
            if not manual and not bool(self._cfg("auto_check_update", True)):
                self._set_state(state="idle", error="")
                snap = self.status_snapshot()
                snap["ok"] = True
                snap["msg"] = "Automatic update check is disabled"
                return snap
            channel = _normalize_channel(self._cfg("update_channel", "stable"))
            self._set_state(state="checking", channel=channel, error="")
            self._bump_status()
            try:
                payload = self._fetch_release(channel)
            except Exception as exc:
                _log_kv("UPDATE", "check_failed", "warning", channel=channel, error=str(exc))
                self._set_state(state="error", error=f"check failed: {exc}")
                self._bump_status()
                snap = self.status_snapshot()
                snap["ok"] = False
                snap["msg"] = f"Update check failed: {exc}"
                return snap
            if payload is None:
                self._set_state(
                    state="up_to_date",
                    latest_version="",
                    latest_tag="",
                    latest_notes="",
                    latest_url="",
                    asset_name="",
                    asset_size=0,
                    error="",
                    checked_at=time.time(),
                )
                self._bump_status()
                snap = self.status_snapshot()
                snap["ok"] = True
                snap["msg"] = f"Up to date (v{APP_VERSION})"
                return snap
            tag = str(payload.get("tag_name") or "").strip()
            latest_version = strip_tag_prefix(tag)
            asset_name, asset_url, asset_size = self._pick_asset(payload, latest_version)
            with self._lock:
                previous_asset = str(self._state.get("asset_name") or "")
                previous_tag = str(self._state.get("latest_tag") or "")
                if previous_tag != tag or previous_asset != asset_name:
                    self._state.update({"staged_file": "", "verified": False, "progress_percent": 0.0,
                                        "downloaded_bytes": 0, "total_bytes": 0})
            self._set_state(
                state="available",
                latest_version=latest_version,
                latest_tag=tag,
                latest_notes=str(payload.get("body") or ""),
                latest_url=str(payload.get("html_url") or release_tag_url(tag)),
                asset_name=asset_name,
                asset_size=int(asset_size or 0),
                asset_url=asset_url,
                error="",
                checked_at=time.time(),
            )
            _log_kv("UPDATE", "available", current=APP_VERSION, latest=latest_version, channel=channel)
            self._bump_status()
            snap = self.status_snapshot()
            snap["ok"] = True
            snap["msg"] = f"Update available: v{latest_version}"
            return snap
        finally:
            with self._lock:
                self._checking = False

    # -- download + verify ----------------------------------------------
    def _download_text(self, url: str) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": UPDATE_USER_AGENT})
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8", errors="replace")

    def download_release(self) -> Dict[str, Any]:
        with self._lock:
            if self._downloading or self._checking:
                return {"ok": False, "msg": "Update operation already in progress", **self.status_snapshot()}
            latest_tag = str(self._state.get("latest_tag") or "").strip()
            asset_name = str(self._state.get("asset_name") or "").strip()
            asset_url = str(self._state.get("asset_url") or "").strip()
            if not latest_tag or not asset_name or not asset_url:
                return {"ok": False, "msg": "Check for updates first", **self.status_snapshot()}
            self._downloading = True
        try:
            stage_dir = self._stage_dir()
            target_path = os.path.join(stage_dir, asset_name)
            self._set_state(state="downloading", progress_percent=0.0, downloaded_bytes=0,
                            total_bytes=int(self._state.get("asset_size") or 0), error="")
            self._bump_status()
            try:
                request = urllib.request.Request(asset_url, headers={"User-Agent": UPDATE_USER_AGENT})
                with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                    try:
                        total = int(response.headers.get("Content-Length") or 0)
                    except Exception:
                        total = 0
                    if total <= 0:
                        total = int(self._state.get("asset_size") or 0)
                    self._set_state(total_bytes=total)
                    tmp_path = target_path + ".part"
                    downloaded = 0
                    last_emit = 0.0
                    with open(tmp_path, "wb") as handle:
                        while True:
                            chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                            if not chunk:
                                break
                            handle.write(chunk)
                            downloaded += len(chunk)
                            now = time.time()
                            if now - last_emit >= 0.5:
                                percent = (downloaded / total * 100.0) if total > 0 else 0.0
                                self._set_state(progress_percent=round(percent, 1), downloaded_bytes=downloaded)
                                last_emit = now
                    os.replace(tmp_path, target_path)
            except Exception as exc:
                _log_kv("UPDATE", "download_failed", "warning", asset=asset_name, error=str(exc))
                self._set_state(state="error", error=f"download failed: {exc}")
                self._bump_status()
                return {"ok": False, "msg": f"Download failed: {exc}", **self.status_snapshot()}
            self._set_state(state="verifying", progress_percent=100.0, downloaded_bytes=downloaded)
            try:
                checksums_url = (
                    f"https://github.com/{_owner_repo_path()}/releases/download/{latest_tag}/{CHECKSUMS_ASSET}"
                )
                checksums = _parse_checksums(self._download_text(checksums_url))
            except Exception as exc:
                _log_kv("UPDATE", "checksum_fetch_failed", "warning", error=str(exc))
                self._set_state(state="error", verified=False, error=f"checksum fetch failed: {exc}")
                self._bump_status()
                return {"ok": False, "msg": f"Checksum fetch failed: {exc}", **self.status_snapshot()}
            expected = checksums.get(asset_name, "")
            if not expected:
                self._set_state(state="error", verified=False, error="checksum missing for asset")
                self._bump_status()
                return {"ok": False, "msg": "Checksum missing for asset", **self.status_snapshot()}
            actual = _sha256_file(target_path)
            if actual.lower() != expected.lower():
                _log_kv("UPDATE", "checksum_mismatch", "warning", asset=asset_name)
                try:
                    os.remove(target_path)
                except Exception:
                    pass
                self._set_state(state="error", verified=False, staged_file="", error="checksum mismatch")
                self._bump_status()
                return {"ok": False, "msg": "Checksum mismatch, file removed", **self.status_snapshot()}
            self._set_state(state="downloaded", staged_file=target_path, verified=True, error="")
            _log_kv("UPDATE", "downloaded_verified", asset=asset_name, version=self._state.get("latest_version"))
            self._bump_status()
            snap = self.status_snapshot()
            snap["ok"] = True
            snap["msg"] = f"Downloaded and verified v{self._state.get('latest_version')}"
            return snap
        finally:
            with self._lock:
                self._downloading = False

    # -- install ----------------------------------------------------------
    def prepare_install(self) -> Dict[str, Any]:
        snap = self.status_snapshot()
        if not snap.get("update_available"):
            return {"ok": False, "msg": "No update available", **snap}
        if not IS_COMPILED or not self._target_exe():
            return {
                "ok": False,
                "msg": "One-click install needs the compiled exe; download from Releases instead",
                "manual_url": snap.get("latest_url") or releases_page_url(),
                **snap,
            }
        if self._farm_running():
            return {"ok": False, "msg": "Stop Auto Rejoin before installing the update", **snap}
        staged = str(snap.get("staged_file") or "")
        if not snap.get("verified") or not staged or not os.path.isfile(staged):
            return {"ok": False, "msg": "Download and verify the update first", **snap}
        current_exe = self._target_exe()
        if os.path.normcase(os.path.abspath(staged)) == os.path.normcase(os.path.abspath(current_exe)):
            return {"ok": False, "msg": "Staged file is the running exe", **snap}
        backup_exe = current_exe + ".bak"
        updater_path = os.path.join(self._stage_dir(), UPDATER_FILENAME)
        script = build_updater_script(
            parent_pid=os.getpid(),
            current_exe=current_exe,
            staged_new_exe=staged,
            backup_exe=backup_exe,
            expected_version=str(snap.get("latest_version") or ""),
            result_file=self._result_file(),
        )
        try:
            with open(updater_path, "w", encoding="utf-8", newline="") as handle:
                handle.write(script)
        except Exception as exc:
            return {"ok": False, "msg": f"Could not write updater: {exc}", **snap}
        self._set_state(state="installing", error="")
        self._bump_status()
        _log_kv("UPDATE", "install_prepared", version=snap.get("latest_version"))
        result = self.status_snapshot()
        result["ok"] = True
        result["msg"] = "Updater ready, app will exit now"
        result["updater"] = updater_path
        result["target"] = current_exe
        return result

    @staticmethod
    def launch_updater_detached(updater_path: str) -> bool:
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "DETACHED_PROCESS", 0
            )
            subprocess.Popen(
                ["cmd.exe", "/c", updater_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
                close_fds=True,
            )
            return True
        except Exception as exc:
            _log_kv("UPDATE", "updater_launch_failed", "warning", error=str(exc))
            return False

    # -- last install result ----------------------------------------------
    def _pickup_last_install_result(self) -> None:
        try:
            path = os.path.join(APP_DATA_DIR, STAGE_DIRNAME, RESULT_FILENAME)
            if not os.path.isfile(path):
                return
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                with self._lock:
                    self._state["last_install"] = payload
                _log_kv(
                    "UPDATE",
                    "last_install_result",
                    ok=bool(payload.get("ok")),
                    action=str(payload.get("action") or ""),
                    version=str(payload.get("version") or ""),
                )
            try:
                os.remove(path)
            except Exception:
                pass
        except Exception:
            pass

    # -- background loop ----------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(daemon=True, name="AppUpdateCheck", target=self._run)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        if self._stop.wait(INITIAL_CHECK_DELAY_SECONDS):
            return
        while not self._stop.is_set():
            try:
                if bool(self._cfg("auto_check_update", True)):
                    self.check_for_updates(manual=False)
            except Exception as exc:
                _log_kv("UPDATE", "background_check_failed", "warning", error=str(exc))
            try:
                interval_hours = float(self._cfg("update_check_interval_hours", 6) or 6)
            except Exception:
                interval_hours = 6.0
            interval_hours = min(168.0, max(1.0, interval_hours))
            if self._stop.wait(interval_hours * 3600.0):
                return


def _owner_repo_path() -> str:
    try:
        from version import GITHUB_OWNER, GITHUB_REPO

        return f"{GITHUB_OWNER}/{GITHUB_REPO}"
    except Exception:
        return "q0HtHHftAS/Roblox-Auto-Rejoin-windown"
