"""Virtual Memory (pagefile) service — real system control, C: only.

Reads:
    total RAM  via kernel32.GetPhysicallyInstalledSystemMemory
    free space via psutil.disk_usage('C:\\')
    current    via PowerShell CIM Win32_ComputerSystem.AutomaticManagedPagefile
               + Win32_PageFileSetting (C:\\pagefile.sys Initial/Maximum MB)

Applies (admin only, elevated PowerShell with runas handoff like app_updater):
    custom: AutomaticManagedPagefile=False + Win32_PageFileSetting C: fixed
            InitialSize=MaximumSize=size_gb*1024 MB (Q13 Fixed)
    system_managed: AutomaticManagedPagefile=True (+ remove C: custom entry)
Other drives are never touched. Apply always requires reboot to take effect;
the API reports reboot_required=True and the UI prompts reboot now/later.

Failures -> ok=False with msg (toast warning, never crash) per Q10.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import subprocess
import time
from ctypes import wintypes
from typing import Any

from services._util import is_admin

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

VM_MIN_GB = 1
VM_MAX_GB = 64
VM_PRESET_MULTIPLIERS = (1.0, 1.5, 2.0, 3.0)
# Windows write is refused unless this much free space stays untouched.
VM_HEADROOM_GB = 5
# How long the UAC (non-admin) path waits for the elevated write to land.
VM_VERIFY_TIMEOUT_S = 15
VM_VERIFY_INTERVAL_S = 2


def installed_ram_gb() -> float:
    try:
        kernel32 = ctypes.windll.kernel32
        fn = kernel32.GetPhysicallyInstalledSystemMemory
        fn.restype = wintypes.BOOL
        fn.argtypes = [ctypes.POINTER(ctypes.c_ulonglong)]
        kb = ctypes.c_ulonglong(0)
        if fn(ctypes.byref(kb)):
            return float(int(kb.value)) / (1024 * 1024)
    except Exception:
        "intentional: ignore exception"
    try:
        if psutil is not None:
            return float(psutil.virtual_memory().total) / (1024 ** 3)
    except Exception:
        "intentional: ignore exception"
    return 16.0


def system_drive_free_gb(drive: str = "C:\\") -> float:
    try:
        if psutil is not None:
            return float(psutil.disk_usage(drive).free) / (1024 ** 3)
    except Exception:
        "intentional: ignore exception"
    try:
        free = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        avail = ctypes.c_ulonglong(0)
        if ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                drive, ctypes.byref(avail), ctypes.byref(total), ctypes.byref(free)):
            return float(free.value) / (1024 ** 3)
    except Exception:
        "intentional: ignore exception"
    return 0.0


def _run_ps_json(script: str, timeout: int = 30) -> dict[str, Any]:
    """Run non-elevated PowerShell CIM query, return parsed JSON (or error)."""
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=timeout, check=False)
    except Exception as exc:
        return {"ok": False, "msg": f"powershell unavailable: {exc}"}
    out = (proc.stdout or "").strip()
    if not out:
        return {"ok": False, "msg": (proc.stderr or "empty powershell output").strip()[-500:]}
    try:
        data = json.loads(out)
        return {"ok": True, "data": data}
    except Exception:
        return {"ok": False, "msg": f"unparseable output: {out[-500:]}"}


_PS_STATUS = r"""
$cs = Get-CimInstance Win32_ComputerSystem;
$pfs = @(Get-CimInstance Win32_PageFileSetting | Select-Object Name, InitialSize, MaximumSize);
$use = @(Get-CimInstance Win32_PageFileUsage | Select-Object Name, AllocatedBaseSize, CurrentUsage, PeakUsage);
@{ auto = [bool]$cs.AutomaticManagedPagefile; settings = $pfs; usage = $use } | ConvertTo-Json -Depth 3 -Compress
"""


def _preset_rows(total_gb: float) -> list[dict[str, Any]]:
    return [
        {"label": f"{multiplier:g}x RAM", "multiplier": multiplier, "gb": int(round(total_gb * multiplier))}
        for multiplier in VM_PRESET_MULTIPLIERS
    ]


def _max_custom_gb(free_gb: float) -> int:
    if free_gb <= 0:
        return VM_MAX_GB
    return max(VM_MIN_GB, min(VM_MAX_GB, int(free_gb - VM_HEADROOM_GB)))


def _base_status_payload(total_gb: float, free_gb: float) -> dict[str, Any]:
    return {
        "ok": True,
        "total_ram_gb": total_gb,
        "system_drive": "C:",
        "free_gb": free_gb,
        "min_gb": VM_MIN_GB,
        "max_gb": VM_MAX_GB,
        # Largest custom size the backend will accept (free - headroom).
        # free <= 0 means the query failed -> fall back to VM_MAX_GB and
        # let apply() report the real error.
        "max_custom_gb": _max_custom_gb(free_gb),
        "headroom_gb": VM_HEADROOM_GB,
        "presets": _preset_rows(total_gb),
        "is_admin": is_admin(),
        "managed_by_windows": True,
        "current": {"mode": "system_managed", "size_gb": None},
        "settings": [],
        "usage": [],
    }


def _status_lists(data: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    settings = data.get("settings") or []
    if isinstance(settings, dict):
        settings = [settings]
    usage = data.get("usage") or []
    if isinstance(usage, dict):
        usage = [usage]
    return settings, usage


def _c_drive_entry(settings: list[Any]) -> Any:
    for entry in settings:
        name = str((entry or {}).get("Name", "") or "")
        if name.lower().startswith("c:"):
            return entry
    return None


def _custom_current(entry: dict[str, Any]) -> dict[str, Any]:
    try:
        mx = int(entry.get("MaximumSize") or 0)
        ini = int(entry.get("InitialSize") or 0)
        maximum = max(mx, ini)
        size_gb = round(maximum / 1024, 1) if maximum else None
    except Exception:
        size_gb = None
    return {
        "mode": "custom",
        "size_gb": size_gb,
        "initial_mb": entry.get("InitialSize"),
        "maximum_mb": entry.get("MaximumSize"),
    }


def _current_mode(auto: bool, c_entry: Any) -> dict[str, Any]:
    if auto:
        return {"mode": "system_managed", "size_gb": None}
    if c_entry:
        return _custom_current(c_entry)
    # auto is handled above, so reaching here means unmanaged
    # with no usable C: entry.
    return {"mode": "custom", "size_gb": None}


def status() -> dict[str, Any]:
    total_gb = round(installed_ram_gb(), 1)
    free_gb = round(system_drive_free_gb("C:\\"), 1)
    payload = _base_status_payload(total_gb, free_gb)
    if os.name != "nt":
        payload["ok"] = False
        payload["msg"] = "Virtual memory control supports Windows only"
        return payload
    res = _run_ps_json(_PS_STATUS)
    if not res.get("ok"):
        payload["ok"] = False
        payload["msg"] = str(res.get("msg", "query failed"))
        return payload
    try:
        data = res["data"] or {}
        settings, usage = _status_lists(data)
        auto = bool(data.get("auto", True))
        payload["managed_by_windows"] = auto
        payload["settings"] = settings
        payload["usage"] = usage
        payload["current"] = _current_mode(auto, _c_drive_entry(settings))
    except Exception as exc:
        payload["ok"] = False
        payload["msg"] = f"parse failed: {exc}"
    return payload


def normalize_virtual_memory_settings(source: dict[str, Any]) -> dict[str, Any]:
    mode = str(source.get("virtual_memory_mode", source.get("mode", "system_managed")) or
               "system_managed").strip().lower()
    if mode not in ("system_managed", "custom", "managed", "auto"):
        raise ValueError("mode must be system_managed or custom")
    if mode in ("managed", "auto"):
        mode = "system_managed"
    try:
        size_gb = int(float(source.get("virtual_memory_size_gb",
                                       source.get("size_gb", 16)) or 16))
    except Exception:
        size_gb = 16
    size_gb = max(VM_MIN_GB, min(VM_MAX_GB, size_gb))
    return {"mode": mode, "size_gb": size_gb}


def _build_apply_script(mode: str, size_gb: int) -> str:
    size_mb = int(size_gb) * 1024
    if mode == "system_managed":
        return r"""
$cs = Get-CimInstance Win32_ComputerSystem
$cs.AutomaticManagedPagefile = $true
Set-CimInstance -InputObject $cs | Out-Null
Get-CimInstance Win32_PageFileSetting | Where-Object { $_.Name -like 'C:*' } | Remove-CimInstance -ErrorAction SilentlyContinue
@{ ok = $true } | ConvertTo-Json -Compress
"""
    return r"""
$sizeMb = %d
$cs = Get-CimInstance Win32_ComputerSystem
$cs.AutomaticManagedPagefile = $false
Set-CimInstance -InputObject $cs | Out-Null
$pf = Get-CimInstance Win32_PageFileSetting | Where-Object { $_.Name -like 'C:*' } | Select-Object -First 1
if ($pf) {
  $pf.InitialSize = $sizeMb
  $pf.MaximumSize = $sizeMb
  Set-CimInstance -InputObject $pf | Out-Null
} else {
  New-CimInstance -ClassName Win32_PageFileSetting -Property @{ Name = 'C:\pagefile.sys'; InitialSize = $sizeMb; MaximumSize = $sizeMb } | Out-Null
}
@{ ok = $true; sizeMb = $sizeMb } | ConvertTo-Json -Compress
""".replace("%d", str(int(size_mb)))


def _encoded_command(script: str) -> list:
    """PowerShell -EncodedCommand (UTF-16LE base64): no temp file, no race."""
    blob = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
            "-ExecutionPolicy", "Bypass", "-EncodedCommand", blob]


def _current_matches(current: Any, mode: str, size_gb: int) -> bool:
    """True when a fresh status() reading matches what apply() requested."""
    if not isinstance(current, dict):
        return False
    if mode == "system_managed":
        return current.get("mode") == "system_managed"
    if current.get("mode") != "custom":
        return False
    try:
        # Writes are always whole GB (int * 1024 MB), so the re-read
        # must be exact; 0.1 only absorbs float/round noise, never 0.5.
        return abs(float(current.get("size_gb") or 0) - float(size_gb)) < 0.1
    except Exception:
        return False


def _describe_current(current: Any) -> str:
    """Human-readable current setting for failure messages (never 'None')."""
    if isinstance(current, dict):
        mode = current.get("mode")
        size = current.get("size_gb")
        if mode == "custom" and size is not None:
            return f"custom {size} GB"
        if mode:
            return str(mode).replace("_", " ")
    return "unknown — check after reboot"


def _wait_for_setting(mode: str, size_gb: int,
                      timeout_s: int = VM_VERIFY_TIMEOUT_S) -> tuple[bool, dict[str, Any]]:
    """Poll status() until the requested setting is visible (UAC path)."""
    last: dict[str, Any] = {}
    deadline = time.monotonic() + max(1, int(timeout_s))
    while True:
        try:
            last = status()
        except Exception as exc:
            last = {"ok": False, "msg": str(exc)}
        if last.get("ok") and _current_matches(last.get("current"), mode, size_gb):
            return True, last
        if time.monotonic() >= deadline:
            return False, last
        time.sleep(VM_VERIFY_INTERVAL_S)


def _space_ok(mode: str, free: float, size_gb: int) -> bool:
    if mode != "custom" or free <= 0:
        return True
    return free >= (size_gb + VM_HEADROOM_GB)


def _failure_payload(message: str, current: dict[str, Any]) -> dict[str, Any]:
    failure = {"ok": False, "msg": message}
    if isinstance(current.get("current"), dict):
        failure["current"] = current.get("current")
    return failure


def _success_payload(
    mode: str,
    size_gb: int,
    current: dict[str, Any],
    verified: bool,
) -> dict[str, Any]:
    return {
        "ok": True,
        "mode": mode,
        "size_gb": size_gb if mode == "custom" else None,
        "reboot_required": True,
        "verified": verified,
        "msg": "Applied — reboot Windows to take effect",
        "current": current.get("current"),
    }


def _run_admin_apply(cmd: list[str], mode: str, size_gb: int) -> dict[str, Any]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return {
            "ok": False,
            "msg": (proc.stderr or out or f"exit {proc.returncode}").strip()[-800:],
        }
    current = status()
    if current.get("ok") and not _current_matches(current.get("current"), mode, size_gb):
        return _failure_payload(
            f"Windows did not confirm the new setting (still {_describe_current(current.get('current'))})",
            current,
        )
    return _success_payload(mode, size_gb, current, bool(current.get("ok")))


def _launch_elevated(cmd: list[str]) -> int:
    # UAC handoff (same pattern as services/app_updater.py runas).
    # EncodedCommand carries the script — no temp file for the
    # elevated process to race against.
    import subprocess as _sp

    cmdline = _sp.list2cmdline(cmd[1:])
    shell_execute = ctypes.windll.shell32.ShellExecuteW
    shell_execute.restype = ctypes.c_void_p
    rc = shell_execute(None, "runas", "powershell.exe", cmdline, None, 0)
    try:
        return int(rc) if rc is not None else 0
    except Exception:
        return 0


def _run_elevated_apply(cmd: list[str], mode: str, size_gb: int) -> dict[str, Any]:
    code = _launch_elevated(cmd)
    if not code or code <= 32:
        return {
            "ok": False,
            "requires_admin": True,
            "msg": "Administrator approval was not granted",
        }
    matched, current = _wait_for_setting(mode, size_gb)
    if not matched:
        return _failure_payload(
            f"Elevated change not confirmed (still {_describe_current(current.get('current'))}) — approve the UAC prompt and retry",
            current,
        )
    return _success_payload(mode, size_gb, current, True)


def apply(mode: str, size_gb: int) -> dict[str, Any]:
    """Apply pagefile setting with UAC elevation. Returns ok/reboot_required."""
    if os.name != "nt":
        return {"ok": False, "msg": "Virtual memory control supports Windows only"}
    settings = normalize_virtual_memory_settings({"mode": mode, "size_gb": size_gb})
    mode = settings["mode"]
    size_gb = settings["size_gb"]
    # Safety: refuse if free space is smaller than requested (leave headroom).
    # free <= 0 means the query failed -> skip the check, let Windows report.
    free = system_drive_free_gb("C:\\")
    if not _space_ok(mode, free, size_gb):
        return {"ok": False,
                "msg": f"Not enough free space on C: ({free:.1f} GB free, need ~{size_gb + VM_HEADROOM_GB} GB headroom)"}
    script = _build_apply_script(mode, size_gb)
    cmd = _encoded_command(script)
    try:
        if is_admin():
            return _run_admin_apply(cmd, mode, size_gb)
        return _run_elevated_apply(cmd, mode, size_gb)
    except Exception as exc:
        return {"ok": False, "msg": str(exc)[-800:]}
