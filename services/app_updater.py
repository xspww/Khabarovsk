"""One-click in-app updater for the compiled Windows exe.

Notify-only checking lives in app_version_check; this module performs the
update opencode-installer-style: download the versioned exe asset from the
GitHub Release, verify it against the published checksums.txt, then hand
over to a console swap-and-relaunch script and exit so the new exe boots.

Safety rules (deliberate, do not soften without a product decision):
- Source runs (``python main.py``) are refused: there is no exe to swap.
- A running farm refuses unless the caller confirms the stop. The updater
  never auto-starts a farm: after the swap the user starts it themselves.
- Only HTTPS GitHub Release assets; the exe must pass SHA256 and pinned
  Authenticode publisher checks before it can replace the installed build.
- The app only exits after the swap script proves it started (first log
  marker). A stillborn script fails loudly instead of killing the app.
- The previous executable stays in place until the new build answers a
  process- and version-bound readiness check.
- The new exe is launched with retries and a long settle wait, because a
  PyInstaller onefile cold extract plus Defender scan can take a minute.
  The script only gives up after every attempt clearly fails.
"""

from __future__ import annotations

import hashlib
import base64
import ctypes
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlparse
from typing import Any, Callable, Dict, List, Optional

import app_paths
from services.app_version_check import HTTP_TIMEOUT_SECONDS, UPDATE_USER_AGENT, check_app_update
from version import RELEASE_SIGNER_PUBLIC_KEY

CHECKSUMS_ASSET = "checksums.txt"
EXE_ASSET_PREFIX = "CronusLauncher-"
EXE_ASSET_SUFFIX = ".exe"
STAGE_DIRNAME = "app_update"
UPDATER_LOG_NAME = "updater.log"
SWAP_TIMEOUT_SECONDS = 300.0
_PROGRESS_CHUNK = 256 * 1024
MAX_EXE_SIZE_BYTES = 1024 * 1024 * 1024
MAX_CHECKSUMS_SIZE_BYTES = 1024 * 1024
_GITHUB_RELEASE_HOST = "github.com"


def _validate_release_asset_url(url: str) -> str:
    """Accept only HTTPS release assets owned by this project."""
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme.lower() != "https" or parsed.hostname != _GITHUB_RELEASE_HOST:
        raise RuntimeError("release asset URL is not a trusted HTTPS GitHub URL")
    if parsed.username or parsed.password or parsed.port:
        raise RuntimeError("release asset URL contains an unexpected authority")
    if not parsed.path.startswith("/xspww/Khabarovsk/releases/download/"):
        raise RuntimeError("release asset URL is outside the project releases")
    return url


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(str(newurl or "")).scheme.lower() != "https":
            raise RuntimeError("release asset redirected to a non-HTTPS URL")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _release_assets(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
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


def asset_size(snapshot: Dict[str, Any], filename: str) -> int:
    wanted = str(filename or "").lower()
    for item in _release_assets(snapshot):
        if str(item.get("name") or "").lower() == wanted:
            try:
                return max(0, int(item.get("size") or 0))
            except (TypeError, ValueError):
                return 0
    return 0


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


def _verify_authenticode(path: str, expected_public_key: str) -> bool:
    """Ask Windows to validate the Authenticode chain and pin its signer key."""
    expected = str(expected_public_key or "").strip().upper()
    if not expected or not os.path.isfile(path):
        return False
    encoded_path = base64.b64encode(os.path.abspath(path).encode("utf-8")).decode("ascii")
    encoded_key = base64.b64encode(expected.encode("ascii")).decode("ascii")
    script = (
        f"$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_path}'));"
        f"$k=[Text.Encoding]::ASCII.GetString([Convert]::FromBase64String('{encoded_key}'));"
        "$s=Get-AuthenticodeSignature -LiteralPath $p;"
        "if ($s.Status -ne 'Valid' -or -not $s.SignerCertificate -or "
        "$s.SignerCertificate.GetPublicKeyString().ToUpperInvariant() -ne $k) { exit 1 }; exit 0"
    )
    command = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-EncodedCommand", command],
            capture_output=True,
            timeout=45,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0
    except Exception:
        return False


DOWNLOAD_RETRIES = 3


def _download_once(
    url: str,
    dest: str,
    progress: Optional[Callable[[int, int], None]] = None,
    *,
    expected_size: int = 0,
    max_size: int = 0,
) -> None:
    tmp = dest + ".part"
    resume_from = 0
    try:
        if os.path.exists(tmp):
            resume_from = max(0, int(os.path.getsize(tmp)))
    except Exception:
        resume_from = 0
    headers = {"User-Agent": UPDATE_USER_AGENT}
    if resume_from > 0:
        headers["Range"] = f"bytes={resume_from}-"
    request = urllib.request.Request(_validate_release_asset_url(url), headers=headers)
    opener = urllib.request.build_opener(_HttpsOnlyRedirectHandler())
    with opener.open(request, timeout=max(120.0, float(HTTP_TIMEOUT_SECONDS or 20.0))) as response:
        try:
            status = int(getattr(response, "status", 200) or 200)
        except Exception:
            status = 200
        try:
            total = int(response.headers.get("Content-Length") or 0)
        except Exception:
            total = 0
        mode = "ab" if (status == 206 and resume_from > 0) else "wb"
        received = resume_from if mode == "ab" else 0
        if mode == "ab":
            total = total + resume_from if total > 0 else 0
        if expected_size > 0:
            total = expected_size
        with open(tmp, mode) as handle:
            while True:
                chunk = response.read(_PROGRESS_CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                received += len(chunk)
                if max_size > 0 and received > max_size:
                    raise RuntimeError(f"download exceeds size limit ({max_size} bytes)")
                if progress:
                    try:
                        progress(received, total)
                    except Exception:
                        pass
        if total > 0 and received != total:
            raise RuntimeError(f"truncated download ({received}/{total} bytes)")
        if expected_size > 0 and received != expected_size:
            raise RuntimeError(f"unexpected asset size ({received}/{expected_size} bytes)")
    os.replace(tmp, dest)


def _download(
    url: str,
    dest: str,
    progress: Optional[Callable[[int, int], None]] = None,
    *,
    expected_size: int = 0,
    max_size: int = 0,
) -> None:
    """Download with retries: a 200MB+ exe over a flaky link often drops once."""
    last_exc: Optional[BaseException] = None
    try:
        part = dest + ".part"
        if os.path.exists(part):
            os.remove(part)
    except Exception:
        pass
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            _download_once(url, dest, progress, expected_size=expected_size, max_size=max_size)
            return
        except Exception as exc:
            last_exc = exc
            if progress:
                try:
                    progress(0, 0)
                except Exception:
                    pass
            time.sleep(2.0 * attempt)
    raise RuntimeError(f"download failed after {DOWNLOAD_RETRIES} tries: {last_exc}")


_UPDATER_PS1 = r"""# Cronus one-click updater (generated, user-initiated only).
# Waits for the old app PID to exit, atomically installs the verified exe,
# relaunches it and checks its PID/version readiness before cleanup. It
# downloads nothing; its only network probe is the local readiness endpoint.
param([int]$ParentPid, [string]$CurrentExe, [string]$StagedExe, [string]$LogFile, [string]$Version, [string]$AppArgs, [string]$ExpectedHash, [string]$ExpectedSignerPublicKey)
$ErrorActionPreference = "Stop"
function Log([string]$m) { Add-Content -LiteralPath $LogFile ("[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m) }
# Console progress: the main app is dead during swap/launch, so progress
# goes to this console window (the exe runs with console=True).
try { $Host.UI.RawUI.WindowTitle = "Cronus Launcher Update" } catch {}
Write-Host "> updating to v$Version..." -ForegroundColor Blue
function Phase([string]$m) {
  Log($m)
  # One clean status line per phase: dim the trailing ellipsis, and never
  # repeat the same line twice (the attempt loop used to print "starting
  # the new version..." twice back to back).
  try {
    if ($m.EndsWith("...")) {
      Write-Host $m.Substring(0, $m.Length - 3) -ForegroundColor Gray -NoNewline
      Write-Host "..." -ForegroundColor DarkGray
    } else {
      Write-Host $m -ForegroundColor Gray
    }
  } catch { try { Write-Host $m -ForegroundColor Gray } catch {} }
}
function WaitPump([int]$seconds) {
  Start-Sleep -Seconds $seconds
}
$pendingExe = ""
$backupExe = ""
$swapped = $false
$launchedPid = 0
try {
  Phase("> waiting for the app to close...")
  Log("waiting for PID $ParentPid")
  $deadline = (Get-Date).AddSeconds(__TIMEOUT__)
  while ($true) {
    try { $p = Get-Process -Id $ParentPid -ErrorAction Stop; Start-Sleep -Milliseconds 400 }
    catch { break }
    if ((Get-Date) -gt $deadline) { Log("parent still alive; aborting"); exit 3 }
  }
  WaitPump 1
  if (-not (Test-Path -LiteralPath $StagedExe)) { Phase("staged exe missing; aborting"); exit 4 }
  Phase("> preparing files...")
  # Copy beside the target first: stage may be on another volume, and a
  # cross-volume Move-Item is not atomic. The old install remains untouched.
  $updateId = [guid]::NewGuid().ToString("N")
  $pendingExe = "$CurrentExe.pending-$updateId"
  $backupExe = "$CurrentExe.previous-$updateId"
  Copy-Item -LiteralPath $StagedExe -Destination $pendingExe -Force
  $copiedHash = (Get-FileHash -LiteralPath $pendingExe -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($copiedHash -ne $ExpectedHash.ToLowerInvariant()) { throw "copied package checksum mismatch" }
  $signature = Get-AuthenticodeSignature -LiteralPath $pendingExe
  if ($signature.Status -ne "Valid" -or -not $signature.SignerCertificate -or
      $signature.SignerCertificate.GetPublicKeyString().ToUpperInvariant() -ne $ExpectedSignerPublicKey.ToUpperInvariant()) {
    throw "package Authenticode signature is invalid or from an unexpected publisher"
  }
  $swapped = $false
  for ($i = 1; $i -le 10; $i++) {
    try {
      if (Test-Path -LiteralPath $CurrentExe) {
        try {
          [System.IO.File]::Replace($pendingExe, $CurrentExe, $backupExe)
        } catch {
          # Some Windows filesystems do not support ReplaceFile. Keep a
          # reversible same-directory rename fallback for portable drives.
          Log("atomic replace unavailable; trying reversible rename: $($_.Exception.Message)")
          [System.IO.File]::Move($CurrentExe, $backupExe)
          try { [System.IO.File]::Move($pendingExe, $CurrentExe) }
          catch {
            [System.IO.File]::Move($backupExe, $CurrentExe)
            throw
          }
        }
      } else {
        [System.IO.File]::Move($pendingExe, $CurrentExe)
      }
      $swapped = Test-Path -LiteralPath $CurrentExe
      if ($swapped) { break }
    } catch {
      Log("swap try $i failed: $($_.Exception.Message)")
    }
    WaitPump 2
  }
  if (-not $swapped) { throw "could not install verified package; previous app is still available" }
  # Pre-launch sweep: make sure no same-app process is still around (a
  # lingering old instance trips the single-instance guard and the new
  # app would exit right away). The script itself is excluded.
  $exeBase = [System.IO.Path]::GetFileNameWithoutExtension($CurrentExe)
  Phase("> sweeping leftover processes...")
  $sweepDeadline = (Get-Date).AddSeconds(30)
  while ((Get-Date) -lt $sweepDeadline) {
    $left = @()
    try {
      $left = @(Get-Process -Name $exeBase -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $PID })
    } catch {}
    if ($left.Count -eq 0) { break }
    Log("leftover same-app PIDs: $(($left | ForEach-Object { $_.Id }) -join ',')" + " - waiting")
    WaitPump 2
  }
  try {
    $still = @(Get-Process -Name $exeBase -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $PID })
    if ($still.Count -gt 0) {
      Log("leftover PIDs still present after sweep: $(($still | ForEach-Object { $_.Id }) -join ',')")
    }
  } catch {}
  Phase("> starting the new version...")
  $workDir = Split-Path -Parent $CurrentExe
  $launchedPid = 0
  $readyUrl = ""
  function ProbeReady([int]$ExpectedPid, [string]$ExpectedVersion) {
    for ($port = 7777; $port -le 7796; $port++) {
      try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:${port}/api/app/ready" -TimeoutSec 2 -ErrorAction Stop
        if ([int]$r.pid -eq $ExpectedPid -and [string]$r.version -eq $ExpectedVersion) { return $true }
      } catch {}
    }
    return $false
  }
  # A onefile cold extract plus Defender scan can take a minute, so give
  # each attempt a long settle window instead of a few seconds.
  if (-not $AppArgs) { $AppArgs = "--post-update" }
  for ($attempt = 1; $attempt -le 5; $attempt++) {
    try {
      if ($attempt -gt 1) { Phase("> starting the new version... (attempt $attempt)") }
      $p = Start-Process -FilePath $CurrentExe -WorkingDirectory $workDir -WindowStyle Normal -ArgumentList $AppArgs -PassThru
      if ($null -eq $p) {
        Log("launch attempt ${attempt}: no process handle returned")
      } else {
        Log("launch attempt $attempt started PID $($p.Id); waiting")
        WaitPump 10
        try {
          $null = Get-Process -Id $p.Id -ErrorAction Stop
        } catch {
          Log("launch attempt ${attempt}: process exited within 10s")
          WaitPump 2
          continue
        }
        # PID alive is not enough: wait until the dashboard API answers.
        $readyDeadline = (Get-Date).AddSeconds(75)
        while ((Get-Date) -lt $readyDeadline) {
          try { $null = Get-Process -Id $p.Id -ErrorAction Stop }
          catch { Log("launch attempt ${attempt}: process gone while waiting for API"); break }
          if (ProbeReady -ExpectedPid $p.Id -ExpectedVersion $Version) { $readyUrl = "ready"; break }
          WaitPump 3
        }
        if ($readyUrl -ne "") {
          $launchedPid = $p.Id
          Phase("> new version running")
          break
        }
        Log("launch attempt ${attempt}: API never answered")
        try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch {}
        WaitPump 2
      }
    } catch {
      Log("launch attempt $attempt failed: $($_.Exception.Message)")
    }
    WaitPump 3
  }
  if ($launchedPid -eq 0) {
    if (Test-Path -LiteralPath $backupExe) {
      try {
        Remove-Item -LiteralPath $CurrentExe -Force -ErrorAction SilentlyContinue
        [System.IO.File]::Move($backupExe, $CurrentExe)
        Log("restored prior target executable")
      } catch { Log("rollback failed: $($_.Exception.Message)") }
    } else {
      Remove-Item -LiteralPath $CurrentExe -Force -ErrorAction SilentlyContinue
    }
    $msg = "Could not start the new version. The previous app was restored at: $CurrentExe (details: $LogFile)"
    Log("launch failed after retries; " + $msg)
    try { Write-Host ("> " + $msg) -ForegroundColor Red } catch {}
    WaitPump 15
    exit 7
  }
  Log("done ($readyUrl)")
  if (Test-Path -LiteralPath $backupExe) { Remove-Item -LiteralPath $backupExe -Force -ErrorAction SilentlyContinue }
  try {
    Remove-Item -LiteralPath $StagedExe -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path (Split-Path -Parent $StagedExe) "checksums.txt") -Force -ErrorAction SilentlyContinue
  } catch {}
  exit 0
} catch {
  if ($launchedPid -eq 0 -and $swapped) {
    try {
      if (Test-Path -LiteralPath $backupExe) {
        Remove-Item -LiteralPath $CurrentExe -Force -ErrorAction SilentlyContinue
        [System.IO.File]::Move($backupExe, $CurrentExe)
        Log("restored previous executable after updater error")
      } else {
        Remove-Item -LiteralPath $CurrentExe -Force -ErrorAction SilentlyContinue
      }
    } catch { Log("automatic rollback failed: $($_.Exception.Message)") }
  }
  Log("failed: $($_.Exception.Message)")
  try { Write-Host "> update failed - see $LogFile" -ForegroundColor Red } catch {}
  WaitPump 10
  exit 5
} finally {
  try { Remove-Item -LiteralPath $pendingExe -Force -ErrorAction SilentlyContinue } catch {}
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
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version):
            message = "Update rejected: invalid release version"
            self._set_job(active=False, state="failed", ok=False, error=message, msg=message,
                           finished_at=time.time())
            return {"ok": False, "accepted": False, "msg": message}
        exe_name = f"{EXE_ASSET_PREFIX}{version}{EXE_ASSET_SUFFIX}"
        exe_url = pick_exe_asset(snap, version)
        sums_url = pick_checksums_asset(snap)
        if not exe_url or not sums_url:
            self._set_job(active=False, state="idle", ok=False, msg="Release assets missing")
            return {"ok": False, "accepted": False, "msg": "Release assets missing for v" + version}
        exe_size = asset_size(snap, exe_name)
        sums_size = asset_size(snap, CHECKSUMS_ASSET)
        if exe_size <= 0 or exe_size > MAX_EXE_SIZE_BYTES or sums_size <= 0 or sums_size > MAX_CHECKSUMS_SIZE_BYTES:
            message = "Update rejected: release asset size is missing or outside safe limits"
            self._set_job(active=False, state="failed", ok=False, error=message, msg=message,
                           version=version, finished_at=time.time())
            return {"ok": False, "accepted": False, "msg": message}
        requires_elevation = False
        try:
            install_dir = os.path.dirname(os.path.abspath(app_paths.EXECUTABLE_PATH))
            with tempfile.NamedTemporaryFile(prefix=".cronus-update-check-", dir=install_dir, delete=False) as probe:
                probe_path = probe.name
            os.remove(probe_path)
        except Exception:
            # Protected install locations (for example Program Files) are
            # supported through a user-approved UAC handoff at swap time.
            requires_elevation = True
        farm_running = bool(getattr(self._farm, "running", False))
        if farm_running and not confirm_stop_farm:
            self._set_job(active=False, state="idle", ok=False, need_confirm_stop=True,
                           msg="Farm is running", version=version)
            return {"ok": False, "accepted": False, "need_confirm_stop": True,
                    "msg": "Farm is running. Confirm to stop it and update.", "version": version}
        self._set_job(state="downloading", version=version, msg=f"Downloading v{version}")
        thread = threading.Thread(
            target=self._run,
            args=(version, exe_url, sums_url, exe_size, sums_size,
                  bool(farm_running and confirm_stop_farm), requires_elevation),
            name="CronusAppUpdate", daemon=True,
        )
        thread.start()
        return {"ok": True, "accepted": True, "msg": f"Updating to v{version}", "job": self.status()["job"]}

    def _run(
        self,
        version: str,
        exe_url: str,
        sums_url: str,
        exe_size: int,
        sums_size: int,
        stop_farm: bool,
        requires_elevation: bool,
    ) -> None:
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
            _download(exe_url, staged_exe, progress("downloading"),
                      expected_size=exe_size, max_size=MAX_EXE_SIZE_BYTES)
            self._set_job(state="verifying", progress="verifying", msg="Verifying checksum")
            _download(sums_url, staged_sums, expected_size=sums_size, max_size=MAX_CHECKSUMS_SIZE_BYTES)
            with open(staged_sums, "r", encoding="utf-8", errors="replace") as handle:
                expected = parse_checksums(handle.read(), exe_name)
            if not expected:
                raise RuntimeError("checksum entry missing for " + exe_name)
            actual = sha256_file(staged_exe)
            if actual != expected:
                raise RuntimeError("checksum mismatch (download corrupted?)")
            if not _verify_authenticode(staged_exe, RELEASE_SIGNER_PUBLIC_KEY):
                raise RuntimeError("Authenticode signature is invalid or release signer is not pinned in this build")
            self._log("UPDATE", "package_verified", version=version)

            current_exe = os.path.abspath(app_paths.EXECUTABLE_PATH)
            # Keep the installed path stable so desktop/taskbar shortcuts and
            # startup entries continue to point at the same executable.
            new_exe = current_exe
            script = os.path.join(stage, f"cronus_updater_{version}.ps1")
            with open(script, "w", encoding="utf-8") as handle:
                handle.write(_UPDATER_PS1)
            log_file = os.path.join(stage, UPDATER_LOG_NAME)

            # Snapshot first: the script may log its marker within
            # milliseconds of starting.
            try:
                log_pos = os.path.getsize(log_file)
            except Exception:
                log_pos = 0
            # Forward boot mode so an update during --autostart resumes the
            # boot chain (auto farm) in the new process.
            app_args = "--post-update"
            if "--autostart" in sys.argv:
                app_args += " --autostart"
            ps_args = ["-NoProfile", "-NonInteractive", "-WindowStyle", "Normal",
                 "-ExecutionPolicy", "Bypass", "-File", script,
                 "-ParentPid", str(os.getpid()),
                 "-CurrentExe", new_exe,
                 "-StagedExe", staged_exe,
                 "-LogFile", log_file,
                 "-Version", version,
                 "-AppArgs", app_args,
                 "-ExpectedHash", expected,
                 "-ExpectedSignerPublicKey", RELEASE_SIGNER_PUBLIC_KEY]
            proc = None
            if requires_elevation:
                # Ask for UAC before stopping the farm. Declining elevation
                # leaves the running app and farm untouched.
                shell_execute = ctypes.windll.shell32.ShellExecuteW
                shell_execute.restype = ctypes.c_void_p
                result = shell_execute(
                    None, "runas", "powershell.exe", subprocess.list2cmdline(ps_args), None, 1
                )
                if not result or int(result) <= 32:
                    raise RuntimeError("administrator permission was not granted for the protected install folder")
            else:
                proc = subprocess.Popen(
                    ["powershell.exe", *ps_args],
                    close_fds=True,
                )
            # Verified handoff: a stillborn swap script (e.g. a syntax error)
            # exits instantly and writes nothing. Never suicide the app until
            # the script proves it started via its first log marker.
            marker = f"waiting for PID {os.getpid()}"
            script_started = False
            script_exit: Optional[int] = None
            deadline = time.time() + 15.0
            while time.time() < deadline:
                script_exit = proc.poll() if proc is not None else None
                if proc is not None and script_exit is not None:
                    break
                try:
                    with open(log_file, "rb") as handle:
                        handle.seek(log_pos)
                        chunk = handle.read().decode("utf-8", errors="replace")
                        log_pos = handle.tell()
                except Exception:
                    chunk = ""
                if marker in chunk:
                    script_started = True
                    break
                time.sleep(0.5)
            if not script_started:
                if proc is not None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                if script_exit is not None:
                    raise RuntimeError(f"updater script exited immediately (code {script_exit}) - see {log_file}")
                raise RuntimeError(f"updater script did not start (no log marker in 15s) - see {log_file}")
            if stop_farm:
                try:
                    self._farm.stop()
                except Exception as exc:
                    self._log("UPDATE", "farm_stop_failed", "warning", error=exc)
            self._set_job(state="restarting", progress="restarting",
                          msg="Verified. Restarting into the new version…")
            self._log("UPDATE", "relaunching", version=version)
            time.sleep(2.0)
            # Single writer: the swap script already printed the progress
            # line and the "> updating to vX..." header, and it announces
            # "> new version running" itself. Anything typed here lands
            # mid-stream between the script's lines, so stay silent.
            os._exit(0)
        except Exception as exc:
            self._log("UPDATE", "update_failed", "error", error=exc, version=version)
            self._set_job(active=False, state="failed", ok=False, error=str(exc),
                           msg=f"Update failed: {exc}", finished_at=time.time())
