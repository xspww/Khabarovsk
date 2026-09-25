"""One-click in-app updater for the compiled Windows exe.

Notify-only checking lives in app_version_check; this module performs the
update opencode-installer-style: download the versioned exe asset from the
GitHub Release, verify it against the published checksums.txt, then hand
over to a console swap-and-relaunch script and exit so the new exe boots.

Safety rules (deliberate, do not soften without a product decision):
- Source runs (``python main.py``) are refused: there is no exe to swap.
- A running farm refuses unless the caller confirms the stop. The updater
  never auto-starts a farm: after the swap the user starts it themselves.
- Only HTTPS GitHub Release assets; the exe never runs before its SHA256
  matches the release's checksums.txt entry.
- The app only exits after the swap script proves it started (first log
  marker). A stillborn script fails loudly instead of killing the app.
- No .bak backup is kept: the verified staged exe overwrites the current
  exe directly (a leftover ``*.bak`` from an older version is deleted).
- The new exe is launched with retries and a long settle wait, because a
  PyInstaller onefile cold extract plus Defender scan can take a minute.
  The script only gives up after every attempt clearly fails.
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


DOWNLOAD_RETRIES = 3


def _download_once(url: str, dest: str, progress: Optional[Callable[[int, int], None]] = None) -> None:
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
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=max(120.0, float(HTTP_TIMEOUT_SECONDS or 20.0))) as response:
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
        with open(tmp, mode) as handle:
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
        if total > 0 and received != total:
            raise RuntimeError(f"truncated download ({received}/{total} bytes)")
    os.replace(tmp, dest)


def _download(url: str, dest: str, progress: Optional[Callable[[int, int], None]] = None) -> None:
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
            _download_once(url, dest, progress)
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
# Waits for the old app PID to exit, moves the verified exe into place
# (overwriting directly, no .bak kept), relaunches it, then deletes
# itself. No network, no payload.
param([int]$ParentPid, [string]$CurrentExe, [string]$StagedExe, [string]$LogFile, [string]$Version, [string]$OldExe, [string]$AppArgs)
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
  Phase("> replacing files...")
  # Drop any leftover backup from an older updater version: no .bak is kept.
  $oldBak = "$CurrentExe.bak"
  if (Test-Path -LiteralPath $oldBak) {
    try { Remove-Item -LiteralPath $oldBak -Force; Log("removed leftover backup") } catch { Log("note: could not remove leftover backup") }
  }
  # The exe may be briefly locked (Defender / indexer) right after the old
  # process exits, so retry the replace instead of failing on first lock.
  $swapped = $false
  for ($i = 1; $i -le 10; $i++) {
    try {
      if (Test-Path -LiteralPath $CurrentExe) { Remove-Item -LiteralPath $CurrentExe -Force }
      Move-Item -LiteralPath $StagedExe -Destination $CurrentExe -Force
      if (Test-Path -LiteralPath $CurrentExe) { $swapped = $true; break }
    } catch {
      Log("swap try $i locked: $($_.Exception.Message)")
    }
    WaitPump 2
  }
  if (-not $swapped) { Log("swap failed after retries; staged exe left at $StagedExe"); exit 6 }
  # Pre-launch sweep: make sure no same-app process is still around (a
  # lingering old instance trips the single-instance guard and the new
  # app would exit right away). The script itself is excluded.
  $exeBase = [System.IO.Path]::GetFileNameWithoutExtension($CurrentExe)
  $oldBase = ""
  if ($OldExe -and $OldExe -ne $CurrentExe) { $oldBase = [System.IO.Path]::GetFileNameWithoutExtension($OldExe) }
  Phase("> sweeping leftover processes...")
  $sweepDeadline = (Get-Date).AddSeconds(30)
  while ((Get-Date) -lt $sweepDeadline) {
    $left = @()
    try {
      $left = @(Get-Process -Name $exeBase -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $PID })
    if ($oldBase) { $left += @(Get-Process -Name $oldBase -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $PID }) }
    } catch {}
    if ($left.Count -eq 0) { break }
    Log("leftover same-app PIDs: $(($left | ForEach-Object { $_.Id }) -join ',')" + " - waiting")
    WaitPump 2
  }
  try {
    $still = @(Get-Process -Name $exeBase -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $PID })
    if ($oldBase) { $still += @(Get-Process -Name $oldBase -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $PID }) }
    if ($still.Count -gt 0) {
      Log("leftover PIDs still present after sweep: $(($still | ForEach-Object { $_.Id }) -join ',')")
    }
  } catch {}
  Phase("> starting the new version...")
  $workDir = Split-Path -Parent $CurrentExe
  $launchedPid = 0
  $readyUrl = ""
  function ProbeReady() {
    for ($port = 7777; $port -le 7796; $port++) {
      try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:${port}/api/status" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        if ($r.StatusCode -eq 200) { return "http://127.0.0.1:${port}" }
      } catch {}
    }
    return ""
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
          $readyUrl = ProbeReady
          if ($readyUrl -ne "") { break }
          WaitPump 3
        }
        if ($readyUrl -ne "") {
          $launchedPid = $p.Id
          Phase("> new version running")
          break
        }
        Log("launch attempt ${attempt}: API never answered")
      }
    } catch {
      Log("launch attempt $attempt failed: $($_.Exception.Message)")
    }
    WaitPump 3
  }
  if ($launchedPid -eq 0) {
    $msg = "Could not start the new version. It is in place - start it manually: $CurrentExe (details: $LogFile)"
    Log("launch failed after retries; " + $msg)
    try { Write-Host ("> " + $msg) -ForegroundColor Red } catch {}
    WaitPump 15
    exit 7
  }
  Log("done ($readyUrl)")
  if ($OldExe -and $OldExe -ne $CurrentExe) { try { if (Test-Path -LiteralPath $OldExe) { Remove-Item -LiteralPath $OldExe -Force; Log("removed previous version exe") } } catch { Log("note: could not remove previous version exe") } }
  exit 0
} catch {
  Log("failed: $($_.Exception.Message)")
  try { Write-Host "> update failed - see $LogFile" -ForegroundColor Red } catch {}
  WaitPump 10
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
            # Versioned install name: the new exe keeps its release filename
            # (e.g. CronusLauncher-2.1.15.exe) next to the old one until the
            # swap script removes the old file after a successful launch.
            new_exe = os.path.join(os.path.dirname(current_exe), exe_name)
            old_exe_arg = current_exe if os.path.normcase(new_exe) != os.path.normcase(current_exe) else ""
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
            proc = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Normal",
                 "-ExecutionPolicy", "Bypass", "-File", script,
                 "-ParentPid", str(os.getpid()),
                 "-CurrentExe", new_exe,
                 "-StagedExe", staged_exe,
                 "-LogFile", log_file,
                 "-Version", version,
                 "-OldExe", old_exe_arg,
                 "-AppArgs", app_args],
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
                script_exit = proc.poll()
                if script_exit is not None:
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
                try:
                    proc.kill()
                except Exception:
                    pass
                if script_exit is not None:
                    raise RuntimeError(f"updater script exited immediately (code {script_exit}) - see {log_file}")
                raise RuntimeError(f"updater script did not start (no log marker in 15s) - see {log_file}")
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
