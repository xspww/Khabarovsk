"""One-click in-app updater for the compiled Windows exe.

Notify-only checking lives in app_version_check; this module performs the
update opencode-installer-style: download the versioned exe asset from the
GitHub Release, verify it against the published checksums.txt, then hand
over to a hidden swap-and-relaunch script and exit so the new exe boots.

Safety rules (deliberate, do not soften without a product decision):
- Source runs (``python main.py``) are refused: there is no exe to swap.
- A running farm refuses unless the caller confirms the stop. The updater
  never auto-starts a farm: after the swap the user starts it themselves.
- Only HTTPS GitHub Release assets; the exe never runs before its SHA256
  matches the release's checksums.txt entry.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional

import app_paths
from services.app_version_check import HTTP_TIMEOUT_SECONDS, UPDATE_USER_AGENT, check_app_update

CHECKSUMS_ASSET = "checksums.txt"
EXE_ASSET_PREFIX = "CronusLauncher-"
EXE_ASSET_SUFFIX = ".exe"
STAGE_DIRNAME = "app_update"
UPDATER_LOG_NAME = "updater.log"
SWAP_TIMEOUT_SECONDS = 90.0
_PROGRESS_CHUNK = 256 * 1024


def _release_assets(snapshot: Dict[str, Any]) -> List[Dict[str, str]]:
    assets = snapshot.get("assets")
    if not isinstance(assets, list):
        return []
    return [dict(item) for item in assets if isinstance(item, dict)]


def pick_exe_asset(snapshot: Dict[str, Any], version: str) -> str:
    """Return the download URL of the versioned exe asset, or ''."""
    wanted = f"{EXE_ASSET_PREFIX}{version}{EXE_ASSET_SUFFIX}".lower()
    for item in _release_assets(snapshot):
        name = str(item.get("name") or "")
        url = str(item.get("browser_download_url") or item.get("url") or "")
        if name.lower() == wanted and url:
            return url
    return ""


def pick_checksums_asset(snapshot: Dict[str, Any]) -> str:
    for item in _release_assets(snapshot):
        name = str(item.get("name") or "")
        url = str(item.get("browser_download_url") or item.get("url") or "")
        if name.lower() == CHECKSUMS_ASSET and url:
            return url
    return ""


def parse_checksums(text: str, filename: str) -> str:
    """Parse GNU sha256sum output; return the hex digest for filename."""
    wanted = str(filename or "").strip().lower()
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        digest, name = parts[0], parts[-1].lstrip("*").strip()
        if name.lower() == wanted and len(digest) == 64:
            return digest.lower()
    return ""


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, dest: str, progress: Optional[Callable[[int, int], None]] = None) -> None:
    tmp = dest + ".part"
    request = urllib.request.Request(url, headers={"User-Agent": UPDATE_USER_AGENT})
    with urllib.request.urlopen(request, timeout=max(30.0, float(HTTP_TIMEOUT_SECONDS or 20.0))) as response:
        try:
            total = int(response.headers.get("Content-Length") or 0)
        except Exception:
            total = 0
        received = 0
        with open(tmp, "wb") as handle:
            while True:
                chunk = response.read(_PROGRESS_CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                received += len(chunk)
                if progress:
                    try:
                        progress(received, total)
                    except Exception:
                        pass
    os.replace(tmp, dest)


_UPDATER_PS1 = r"""# Cronus one-click updater (generated, user-initiated only).
# Waits for the old app PID to exit, swaps the verified exe into place,
# relaunches it, then deletes itself. No network, no payload.
param([int]$ParentPid, [string]$CurrentExe, [string]$StagedExe, [string]$LogFile)
$ErrorActionPreference = "Stop"
function Log([string]$m) { Add-Content -LiteralPath $LogFile ("[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m) }
try {
  Log("waiting for PID $ParentPid")
  $deadline = (Get-Date).AddSeconds(__TIMEOUT__)
  while ($true) {
    try { $p = Get-Process -Id $ParentPid -ErrorAction Stop; Start-Sleep -Milliseconds 500 }
    catch { break }
    if ((Get-Date) -gt $deadline) { Log("parent still alive; aborting"); exit 3 }
  }
  if (-not (Test-Path -LiteralPath $StagedExe)) { Log("staged exe missing; aborting"); exit 4 }
  $bak = "$CurrentExe.bak"
  if (Test-Path -LiteralPath $bak) { Remove-Item -LiteralPath $bak -Force }
  [System.IO.File]::Move($CurrentExe, $bak)
  [System.IO.File]::Move($StagedExe, $CurrentExe)
  Log("swapped; launching")
  Start-Process -FilePath $CurrentExe -WorkingDirectory (Split-Path -Parent $CurrentExe)
  Log("done")
  exit 0
} catch {
  Log("failed: $($_.Exception.Message)")
  exit 5
} finally {
  try { Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue } catch {}
}
""".replace("__TIMEOUT__", str(int(SWAP_TIMEOUT_SECONDS)))


class AppUpdater:
    """Small interface, deep implementation: start_update() + status()."""

    def __init__(self, farm: Any, logger: Optional[Callable[..., None]] = None):
        self._farm = farm
        self._log = logger or (lambda *args, **kwargs: None)
        self._lock = threading.RLock()
        self._job = self._new_job("idle")

    @staticmethod
    def _new_job(state: str, **extra: Any) -> Dict[str, Any]:
        payload = {
            "active": False,
            "state": state,
            "ok": state in {"idle", "done"},
            "msg": "",
            "version": "",
            "progress": "",
            "error": "",
            "need_confirm_stop": False,
            "started_at": None,
            "finished_at": None,
        }
        payload.update(extra)
        return payload

    def _set_job(self, **values: Any) -> Dict[str, Any]:
        with self._lock:
            self._job.update(values)
            return dict(self._job)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            job = dict(self._job)
        snap = check_app_update()
        job["available"] = bool(snap.get("update_available"))
        job["latest_version"] = str(snap.get("latest_version") or "")
        job["latest_url"] = str(snap.get("latest_url") or "")
        job["current_version"] = str(snap.get("current_version") or "")
        job["compiled"] = bool(app_paths.IS_COMPILED)
        return {"ok": True, "job": job}

    def start_update(self, confirm_stop_farm: bool = False) -> Dict[str, Any]:
        if not app_paths.IS_COMPILED:
            return {"ok": False, "accepted": False, "msg": "Source run: pull the latest code instead"}
        with self._lock:
            if self._job.get("active"):
                return {"ok": True, "accepted": False, "duplicate": True,
                        "msg": "Update already running", "job": dict(self._job)}
            self._job = self._new_job("starting", active=True, ok=False, started_at=time.time())
        snap = check_app_update()
        version = str(snap.get("latest_version") or "")
        if not snap.get("update_available") or not version:
            self._set_job(active=False, state="idle", ok=False, msg="No update available")
            return {"ok": False, "accepted": False, "msg": "No update available"}
        exe_url = pick_exe_asset(snap, version)
        sums_url = pick_checksums_asset(snap)
        if not exe_url or not sums_url:
            self._set_job(active=False, state="idle", ok=False, msg="Release assets missing")
            return {"ok": False, "accepted": False, "msg": "Release assets missing for v" + version}
        farm_running = bool(getattr(self._farm, "running", False))
        if farm_running and not confirm_stop_farm:
            self._set_job(active=False, state="idle", ok=False, need_confirm_stop=True,
                           msg="Farm is running", version=version)
            return {"ok": False, "accepted": False, "need_confirm_stop": True,
                    "msg": "Farm is running. Confirm to stop it and update.", "version": version}
        self._set_job(state="downloading", version=version, msg=f"Downloading v{version}")
        thread = threading.Thread(
            target=self._run,
            args=(version, exe_url, sums_url, bool(farm_running and confirm_stop_farm)),
            name="CronusAppUpdate", daemon=True,
        )
        thread.start()
        return {"ok": True, "accepted": True, "msg": f"Updating to v{version}", "job": self.status()["job"]}

    def _run(self, version: str, exe_url: str, sums_url: str, stop_farm: bool) -> None:
        try:
            stage = os.path.join(app_paths.APP_DATA_DIR, STAGE_DIRNAME)
            os.makedirs(stage, exist_ok=True)
            exe_name = f"{EXE_ASSET_PREFIX}{version}{EXE_ASSET_SUFFIX}"
            staged_exe = os.path.join(stage, exe_name)
            staged_sums = os.path.join(stage, CHECKSUMS_ASSET)

            def progress(kind: str):
                def report(received: int, total: int):
                    pct = f"{received * 100 // total}%" if total > 0 else f"{received // 1024}KB"
                    self._set_job(progress=f"{kind} {pct}")
                return report

            self._set_job(state="downloading", msg=f"Downloading v{version}")
            _download(exe_url, staged_exe, progress("downloading"))
            self._set_job(state="verifying", progress="verifying", msg="Verifying checksum")
            _download(sums_url, staged_sums)
            with open(staged_sums, "r", encoding="utf-8", errors="replace") as handle:
                expected = parse_checksums(handle.read(), exe_name)
            if not expected:
                raise RuntimeError("checksum entry missing for " + exe_name)
            actual = sha256_file(staged_exe)
            if actual != expected:
                raise RuntimeError("checksum mismatch (download corrupted?)")
            self._log("UPDATE", "package_verified", version=version)

            current_exe = os.path.abspath(app_paths.EXECUTABLE_PATH)
            script = os.path.join(stage, f"cronus_updater_{version}.ps1")
            with open(script, "w", encoding="utf-8") as handle:
                handle.write(_UPDATER_PS1)
            log_file = os.path.join(stage, UPDATER_LOG_NAME)

            if stop_farm:
                try:
                    self._farm.stop()
                except Exception as exc:
                    self._log("UPDATE", "farm_stop_failed", "warning", error=exc)
            self._set_job(state="restarting", progress="restarting",
                          msg="Verified. Restarting into the new version…")
            self._log("UPDATE", "relaunching", version=version)
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                 "-ExecutionPolicy", "Bypass", "-File", script,
                 "-ParentPid", str(os.getpid()),
                 "-CurrentExe", current_exe,
                 "-StagedExe", staged_exe,
                 "-LogFile", log_file],
                close_fds=True,
            )
            time.sleep(2.0)
            try:
                from desktop import console_output
                console_output.write_line(f"Updated to v{version} - restarting…")
            except Exception:
                pass
            os._exit(0)
        except Exception as exc:
            self._log("UPDATE", "update_failed", "error", error=exc, version=version)
            self._set_job(active=False, state="failed", ok=False, error=str(exc),
                           msg=f"Update failed: {exc}", finished_at=time.time())
