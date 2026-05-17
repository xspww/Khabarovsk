from __future__ import annotations

import os
import re
import json
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


RULE_PREFIX = "CronusLauncher_Test_Block_Roblox"
RULE_GROUP = "Cronus Launcher Test Network Fault"
ROBLOX_EXE_NAME = "robloxplayerbeta.exe"


@dataclass
class CommandResult:
    ok: bool
    returncode: int
    stdout: str = ""
    stderr: str = ""
    script: str = ""


Runner = Callable[[str], CommandResult]


def _ps_quote(value: str) -> str:
    return "'" + str(value or "").replace("'", "''") + "'"


def _sanitize_rule_suffix(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return cleaned[:48] or "manual"


def _default_runner(script: str) -> CommandResult:
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=flags,
        )
        return CommandResult(
            ok=completed.returncode == 0,
            returncode=int(completed.returncode),
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            script=script,
        )
    except Exception as exc:
        return CommandResult(ok=False, returncode=1, stderr=str(exc), script=script)


class NetworkFaultInjector:
    def __init__(self, runner: Optional[Runner] = None):
        self._runner = runner or _default_runner
        self._lock = threading.RLock()
        self._timer: Optional[threading.Timer] = None
        self._active: Dict[str, Any] = {}

    @staticmethod
    def build_restore_script() -> str:
        prefix = _ps_quote(f"{RULE_PREFIX}*")
        return (
            "$ErrorActionPreference='Stop'\n"
            f"$rules = Get-NetFirewallRule -DisplayName {prefix} -ErrorAction SilentlyContinue\n"
            "if ($rules) { $rules | Remove-NetFirewallRule }\n"
            "@{ ok = $true; removed = @($rules).Count } | ConvertTo-Json -Compress\n"
        )

    @staticmethod
    def build_status_script() -> str:
        prefix = _ps_quote(f"{RULE_PREFIX}*")
        return (
            "$ErrorActionPreference='Stop'\n"
            f"$rules = @(Get-NetFirewallRule -DisplayName {prefix} -ErrorAction SilentlyContinue)\n"
            "$items = @($rules | ForEach-Object { @{ display_name = $_.DisplayName; enabled = $_.Enabled.ToString(); direction = $_.Direction.ToString(); action = $_.Action.ToString() } })\n"
            "@{ ok = $true; active = ($items.Count -gt 0); count = $items.Count; rules = $items } | ConvertTo-Json -Compress\n"
        )

    @staticmethod
    def build_block_script(program_path: str, rule_name: str) -> str:
        return (
            "$ErrorActionPreference='Stop'\n"
            f"$program = {_ps_quote(program_path)}\n"
            "if (!(Test-Path -LiteralPath $program)) { throw \"Roblox executable not found: $program\" }\n"
            f"{NetworkFaultInjector.build_restore_script()}\n"
            "New-NetFirewallRule "
            f"-DisplayName {_ps_quote(rule_name)} "
            f"-Group {_ps_quote(RULE_GROUP)} "
            "-Direction Outbound -Action Block -Program $program -Profile Any -Enabled True "
            f"-Description {_ps_quote('Temporary Cronus Launcher reliability test fault')} | Out-Null\n"
            "@{ ok = $true; active = $true; display_name = "
            f"{_ps_quote(rule_name)}; program = $program }} | ConvertTo-Json -Compress\n"
        )

    @staticmethod
    def validate_roblox_pid(pid: Any) -> Dict[str, Any]:
        try:
            parsed = int(pid)
        except Exception:
            return {"ok": False, "reason": "invalid_pid", "pid": pid}
        if parsed <= 0:
            return {"ok": False, "reason": "invalid_pid", "pid": parsed}
        try:
            import psutil

            proc = psutil.Process(parsed)
            name = str(proc.name() or "")
            exe = str(proc.exe() or "")
            if name.lower() != ROBLOX_EXE_NAME:
                return {"ok": False, "reason": "not_roblox_process", "pid": parsed, "name": name, "exe": exe}
            if not exe:
                return {"ok": False, "reason": "missing_executable", "pid": parsed, "name": name}
            return {
                "ok": True,
                "pid": parsed,
                "name": name,
                "exe": exe,
                "create_time": float(proc.create_time() or 0.0),
            }
        except Exception as exc:
            return {"ok": False, "reason": "pid_validation_failed", "pid": parsed, "error": str(exc)}

    @staticmethod
    def find_live_roblox_processes() -> List[Dict[str, Any]]:
        try:
            import psutil
        except Exception:
            return []
        found: List[Dict[str, Any]] = []
        for proc in psutil.process_iter(["pid", "name", "create_time", "exe"]):
            try:
                name = str(proc.info.get("name") or "")
                if name.lower() != ROBLOX_EXE_NAME:
                    continue
                exe = str(proc.info.get("exe") or proc.exe() or "")
                if not exe:
                    continue
                found.append({
                    "pid": int(proc.info.get("pid") or proc.pid),
                    "name": name,
                    "exe": exe,
                    "create_time": float(proc.info.get("create_time") or proc.create_time() or 0.0),
                })
            except Exception:
                continue
        return sorted(found, key=lambda item: (float(item.get("create_time") or 0.0), int(item.get("pid") or 0)), reverse=True)

    def _schedule_restore_locked(self, duration_seconds: float) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None
        if duration_seconds <= 0:
            return
        timer = threading.Timer(duration_seconds, self.restore)
        timer.daemon = True
        timer.start()
        self._timer = timer

    def block_roblox(
        self,
        program_path: str,
        *,
        duration_seconds: float = 90.0,
        account_id: str = "",
        pid: Optional[int] = None,
    ) -> Dict[str, Any]:
        program = os.path.abspath(str(program_path or ""))
        if not program:
            return {"ok": False, "msg": "Roblox executable path is required"}
        duration = max(0.0, min(float(duration_seconds or 0.0), 3600.0))
        suffix = _sanitize_rule_suffix(account_id or pid or int(time.time()))
        rule_name = f"{RULE_PREFIX}_{suffix}"
        with self._lock:
            result = self._runner(self.build_block_script(program, rule_name))
            payload = {
                "ok": bool(result.ok),
                "msg": "Roblox outbound blocked" if result.ok else "Failed to block Roblox outbound",
                "active": bool(result.ok),
                "display_name": rule_name,
                "program": program,
                "account_id": account_id,
                "pid": pid,
                "duration_seconds": duration,
                "active_until": time.time() + duration if result.ok and duration > 0 else 0.0,
                "returncode": result.returncode,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            }
            if result.ok:
                self._active = dict(payload)
                self._schedule_restore_locked(duration)
            return payload

    def restore(self) -> Dict[str, Any]:
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            result = self._runner(self.build_restore_script())
            payload = {
                "ok": bool(result.ok),
                "msg": "Roblox outbound restored" if result.ok else "Failed to restore Roblox outbound",
                "active": False if result.ok else bool(self._active),
                "returncode": result.returncode,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            }
            if result.ok:
                self._active = {}
            return payload

    def status(self) -> Dict[str, Any]:
        with self._lock:
            result = self._runner(self.build_status_script())
            rules: List[Dict[str, Any]] = []
            firewall_active = False
            if result.ok and result.stdout.strip():
                try:
                    parsed = json.loads(result.stdout.strip())
                    if isinstance(parsed, dict):
                        rules_value = parsed.get("rules", [])
                        if isinstance(rules_value, dict):
                            rules = [rules_value]
                        elif isinstance(rules_value, list):
                            rules = [item for item in rules_value if isinstance(item, dict)]
                        firewall_active = bool(parsed.get("active") or rules)
                except Exception:
                    firewall_active = False
            active_until = float(self._active.get("active_until") or 0.0)
            payload = {
                "ok": bool(result.ok),
                "active": bool(firewall_active or (bool(self._active) and (active_until <= 0 or active_until > time.time()))),
                "active_until": active_until,
                "remaining_seconds": round(max(0.0, active_until - time.time()), 1) if active_until else 0.0,
                "prefix": RULE_PREFIX,
                "rules": rules,
                "returncode": result.returncode,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            }
            return payload


NETWORK_FAULT_INJECTOR = NetworkFaultInjector()
