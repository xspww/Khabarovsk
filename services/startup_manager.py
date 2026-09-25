from __future__ import annotations

"""Windows Startup shortcut manager for Cronus Launcher.

Uses the per-user Startup folder (no admin, no registry) via the built-in
WScript.Shell COM object through PowerShell, so no new Python dependency.
The shortcut carries the ``--autostart`` flag so the app can tell a boot
launch apart from a manual double-click.

Toggle is the source of truth: the UI calls ensure/remove, and every boot
(plus every successful update, which arrives with --autostart) heals a
stale target caused by versioned exe filenames.
"""

import os
import subprocess
from typing import Any, Dict, Optional

SHORTCUT_NAME = "Cronus Launcher.lnk"
AUTOSTART_FLAG = "--autostart"


def startup_dir() -> str:
    base = os.environ.get("APPDATA", "").strip() or os.path.expanduser("~")
    return os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def shortcut_path() -> str:
    return os.path.join(startup_dir(), SHORTCUT_NAME)


def _ps_single(value: str) -> str:
    return "'" + str(value or "").replace("'", "''") + "'"


def _run_ps(script: str, timeout: float = 30.0) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=timeout, close_fds=True,
        )
    except Exception as exc:
        return {"ok": False, "msg": f"powershell failed: {exc}", "stdout": "", "stderr": ""}
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return {"ok": False, "msg": err or f"powershell exit {proc.returncode}", "stdout": out, "stderr": err}
    return {"ok": True, "msg": "", "stdout": out, "stderr": err}


_READ_SNIPPET = (
    "$p = __PATH__; "
    "if (-not (Test-Path -LiteralPath $p)) { 'MISSING'; exit 0 }; "
    "try { $s = (New-Object -ComObject WScript.Shell).CreateShortcut($p); "
    "'TARGET=' + $s.TargetPath; 'ARGS=' + $s.Arguments } "
    "catch { 'ERROR:' + $_.Exception.Message; exit 1 }"
)

_WRITE_SNIPPET = (
    "$p = __PATH__; $t = __TARGET__; $a = __ARGS__; $w = __WORKDIR__; "
    "try { "
    "New-Item -ItemType Directory -Force -Path (Split-Path -Parent $p) | Out-Null; "
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($p); "
    "$s.TargetPath = $t; $s.Arguments = $a; $s.WorkingDirectory = $w; "
    "$s.Save(); 'SAVED' } "
    "catch { 'ERROR:' + $_.Exception.Message; exit 1 }"
)


def read_shortcut(path: Optional[str] = None) -> Dict[str, Any]:
    target = path or shortcut_path()
    script = _READ_SNIPPET.replace("__PATH__", _ps_single(target))
    res = _run_ps(script)
    if not res["ok"]:
        return {"ok": False, "present": False, "target": "", "args": "", "msg": res["msg"]}
    lines = (res["stdout"] or "").splitlines()
    if lines and lines[0].strip() == "MISSING":
        return {"ok": True, "present": False, "target": "", "args": "", "msg": ""}
    info = {"ok": True, "present": True, "target": "", "args": "", "msg": ""}
    for line in lines:
        if line.startswith("TARGET="):
            info["target"] = line[len("TARGET="):].strip()
        elif line.startswith("ARGS="):
            info["args"] = line[len("ARGS="):].strip()
    return info


def _norm(path: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(path))
    except Exception:
        return os.path.normcase(str(path or ""))


def ensure_shortcut(exe_path: str) -> Dict[str, Any]:
    exe = os.path.abspath(exe_path)
    if not os.path.isfile(exe):
        return {"ok": False, "msg": f"exe not found: {exe}"}
    current = read_shortcut()
    if current.get("present") and _norm(current.get("target", "")) == _norm(exe) \
            and AUTOSTART_FLAG in str(current.get("args") or ""):
        return {"ok": True, "msg": "already installed", "changed": False}
    script = (_WRITE_SNIPPET
              .replace("__PATH__", _ps_single(shortcut_path()))
              .replace("__TARGET__", _ps_single(exe))
              .replace("__ARGS__", _ps_single(AUTOSTART_FLAG))
              .replace("__WORKDIR__", _ps_single(os.path.dirname(exe))))
    res = _run_ps(script)
    if not res["ok"] or "SAVED" not in (res["stdout"] or ""):
        return {"ok": False, "msg": res.get("msg") or res.get("stdout") or "save failed"}
    return {"ok": True, "msg": "installed", "changed": True}


def remove_shortcut() -> Dict[str, Any]:
    target = shortcut_path()
    try:
        if not os.path.exists(target):
            return {"ok": True, "msg": "already removed", "changed": False}
        os.remove(target)
        return {"ok": True, "msg": "removed", "changed": True}
    except Exception as exc:
        return {"ok": False, "msg": f"remove failed: {exc}"}


def shortcut_status(exe_path: str = "") -> Dict[str, Any]:
    info = read_shortcut()
    if not info.get("present"):
        return {"ok": True, "present": False, "healthy": False, "target": "", "args": ""}
    healthy = True
    if exe_path:
        healthy = _norm(info.get("target", "")) == _norm(os.path.abspath(exe_path)) \
            and AUTOSTART_FLAG in str(info.get("args") or "")
    return {"ok": True, "present": True, "healthy": bool(healthy),
            "target": info.get("target", ""), "args": info.get("args", "")}


def heal_shortcut(exe_path: str) -> Dict[str, Any]:
    """Rewrite a missing/stale shortcut. Call only when start_on_boot is on."""
    status = shortcut_status(exe_path)
    if status.get("healthy"):
        return {"ok": True, "msg": "healthy", "changed": False}
    return ensure_shortcut(exe_path)
