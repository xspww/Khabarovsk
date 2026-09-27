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

import ctypes
import json
import subprocess
import base64
import os
from ctypes import wintypes
from typing import Any, Dict

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None  # type: ignore

VM_MIN_GB = 1
VM_MAX_GB = 64
VM_PRESET_MULTIPLIERS = (1.0, 1.5, 2.0, 3.0)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


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
        pass
    try:
        if psutil is not None:
            return float(psutil.virtual_memory().total) / (1024 ** 3)
    except Exception:
        pass
    return 16.0


def system_drive_free_gb(drive: str = "C:\\") -> float:
    try:
        if psutil is not None:
            return float(psutil.disk_usage(drive).free) / (1024 ** 3)
    except Exception:
        pass
    try:
        free = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        avail = ctypes.c_ulonglong(0)
        if ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                drive, ctypes.byref(avail), ctypes.byref(total), ctypes.byref(free)):
            return float(free.value) / (1024 ** 3)
    except Exception:
        pass
    return 0.0


def _run_ps_json(script: str, timeout: int = 30) -> Dict[str, Any]:
    """Run non-elevated PowerShell CIM query, return parsed JSON (or error)."""
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=timeout)
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


def status() -> Dict[str, Any]:
    total_gb = round(installed_ram_gb(), 1)
    free_gb = round(system_drive_free_gb("C:\\"), 1)
    presets = [
        {"label": f"{m:g}x RAM", "multiplier": m, "gb": int(round(total_gb * m))}
        for m in VM_PRESET_MULTIPLIERS
    ]
    payload: Dict[str, Any] = {
        "ok": True,
        "total_ram_gb": total_gb,
        "system_drive": "C:",
        "free_gb": free_gb,
        "presets": presets,
        "is_admin": is_admin(),
        "managed_by_windows": True,
        "current": {"mode": "system_managed", "size_gb": None},
        "settings": [],
        "usage": [],
    }
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
        auto = bool(data.get("auto", True))
        settings = data.get("settings") or []
        if isinstance(settings, dict):
            settings = [settings]
        usage = data.get("usage") or []
        if isinstance(usage, dict):
            usage = [usage]
        payload["managed_by_windows"] = auto
        payload["settings"] = settings
        payload["usage"] = usage
        c_entry = None
        for s in settings:
            name = str((s or {}).get("Name", "") or "")
            if name.lower().startswith("c:"):
                c_entry = s
                break
        if auto and not c_entry:
            payload["current"] = {"mode": "system_managed", "size_gb": None}
        elif c_entry:
            try:
                mx = int(c_entry.get("MaximumSize") or 0)
                ini = int(c_entry.get("InitialSize") or 0)
                size_gb = round(max(mx, ini) / 1024, 1) if max(mx, ini) else None
            except Exception:
                size_gb = None
            payload["current"] = {"mode": "custom", "size_gb": size_gb,
                                  "initial_mb": c_entry.get("InitialSize"),
                                  "maximum_mb": c_entry.get("MaximumSize")}
        else:
            payload["current"] = {"mode": "system_managed" if auto else "custom",
                                  "size_gb": None}
    except Exception as exc:
        payload["ok"] = False
        payload["msg"] = f"parse failed: {exc}"
    return payload


def normalize_virtual_memory_settings(source: Dict[str, Any]) -> Dict[str, Any]:
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
""" % (size_mb,)


def _encoded_command(script: str) -> list:
    """PowerShell -EncodedCommand (UTF-16LE base64): no temp file, no race."""
    blob = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
            "-ExecutionPolicy", "Bypass", "-EncodedCommand", blob]


def apply(mode: str, size_gb: int) -> Dict[str, Any]:
    """Apply pagefile setting with UAC elevation. Returns ok/reboot_required."""
    if os.name != "nt":
        return {"ok": False, "msg": "Virtual memory control supports Windows only"}
    settings = normalize_virtual_memory_settings({"mode": mode, "size_gb": size_gb})
    mode = settings["mode"]
    size_gb = settings["size_gb"]
    # Safety: refuse if free space is smaller than requested (leave 5GB headroom).
    # free <= 0 means the query failed -> skip the check, let Windows report.
    free = system_drive_free_gb("C:\\")
    if mode == "custom" and free > 0 and free < (size_gb + 5):
        return {"ok": False,
                "msg": f"Not enough free space on C: ({free:.1f} GB free, need ~{size_gb + 5} GB headroom)"}
    script = _build_apply_script(mode, size_gb)
    cmd = _encoded_command(script)
    try:
        if is_admin():
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            out = (proc.stdout or "").strip()
            if proc.returncode != 0:
                return {"ok": False,
                        "msg": (proc.stderr or out or f"exit {proc.returncode}").strip()[-800:]}
            current = status()
            return {"ok": True, "mode": mode, "size_gb": size_gb if mode == "custom" else None,
                    "reboot_required": True,
                    "msg": "Applied — reboot Windows to take effect",
                    "current": current.get("current")}
        # UAC handoff (same pattern as services/app_updater.py runas).
        # EncodedCommand carries the script — no temp file for the
        # elevated process to race against.
        import subprocess as _sp
        cmdline = _sp.list2cmdline(cmd[1:])
        shell_execute = ctypes.windll.shell32.ShellExecuteW
        shell_execute.restype = ctypes.c_void_p
        rc = shell_execute(None, "runas", "powershell.exe", cmdline, None, 0)
        try:
            code = int(rc) if rc is not None else 0
        except Exception:
            code = 0
        if not code or code <= 32:
            return {"ok": False, "requires_admin": True,
                    "msg": "Administrator approval was not granted"}
        import time as _t
        _t.sleep(4)
        current = status()
        return {"ok": True, "mode": mode, "size_gb": size_gb if mode == "custom" else None,
                "reboot_required": True,
                "msg": "Applied — reboot Windows to take effect",
                "current": current.get("current")}
    except Exception as exc:
        return {"ok": False, "msg": str(exc)[-800:]}
