from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    import psutil
except Exception:  # pragma: no cover - runtime fallback only
    psutil = None


ERROR_PATTERNS = (
    "--- Logging error ---",
    "Traceback (most recent call last)",
    "Unhandled exception",
    "PermissionError",
)


def _app_data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / "Cronus Launcher" / "data"
    return Path.home() / ".cronus_launcher" / "data"


def _now() -> float:
    return time.time()


def _fmt_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


class SoakFailure(RuntimeError):
    pass


class MonitorLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, level: str, event: str, **fields: Any) -> None:
        payload = {
            "ts": round(_now(), 3),
            "level": level,
            "event": event,
            **fields,
        }
        line = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(line, flush=True)


class ApiClient:
    def __init__(self, base_url: str, log: MonitorLog):
        self.base_url = base_url.rstrip("/")
        self.log = log
        self.token = ""

    def refresh_token(self) -> str:
        token = ""
        try:
            html = self.get_text("/")
            match = re.search(r'name="cronus-api-token"\s+content="([^"]+)"', html)
            if match:
                token = match.group(1)
        except Exception:
            token = ""
        if not token:
            instance_file = _app_data_dir() / "cronus_rt_instance.json"
            try:
                payload = json.loads(instance_file.read_text(encoding="utf-8"))
                token = str(payload.get("token") or "")
            except Exception:
                token = ""
        self.token = token
        return token

    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None, timeout: float = 10.0) -> Any:
        url = self.base_url + path
        data = None
        headers = {"User-Agent": "CronusSoakMonitor/1"}
        if method.upper() != "GET":
            if not self.token:
                self.refresh_token()
            headers["X-Cronus-Token"] = self.token
            headers["X-Cronus-Idempotency-Key"] = f"soak-{path.strip('/').replace('/', '-')}-{int(_now() * 1000)}"
            headers["Content-Type"] = "application/json"
            data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                if not raw:
                    return {}
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise SoakFailure(f"{method} {path} failed: HTTP {exc.code} {raw}") from exc

    def get_json(self, path: str, timeout: float = 10.0) -> Any:
        return self.request("GET", path, timeout=timeout)

    def get_text(self, path: str, timeout: float = 10.0) -> str:
        req = urllib.request.Request(self.base_url + path, headers={"User-Agent": "CronusSoakMonitor/1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def post_json(self, path: str, body: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Any:
        return self.request("POST", path, body=body, timeout=timeout)


class LogScanner:
    def __init__(self, paths: Iterable[Path]):
        self._offsets: Dict[Path, int] = {}
        for path in paths:
            try:
                self._offsets[path] = path.stat().st_size
            except OSError:
                self._offsets[path] = 0

    def scan(self) -> List[Dict[str, str]]:
        hits: List[Dict[str, str]] = []
        for path, old_offset in list(self._offsets.items()):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            offset = 0 if size < old_offset else old_offset
            self._offsets[path] = size
            if size <= offset:
                continue
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(offset)
                    for line in fh:
                        if any(pattern in line for pattern in ERROR_PATTERNS):
                            hits.append({"file": str(path), "line": line.strip()[:500]})
            except OSError:
                continue
        return hits


def roblox_processes() -> List[Dict[str, Any]]:
    if psutil is None:
        return []
    result: List[Dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name", "create_time", "memory_info"]):
        try:
            if str(proc.info.get("name") or "").lower() != "robloxplayerbeta.exe":
                continue
            result.append(
                {
                    "pid": int(proc.info["pid"]),
                    "rss_mb": round(float(proc.info["memory_info"].rss) / (1024 * 1024), 1),
                    "created_at": float(proc.info.get("create_time") or 0.0),
                }
            )
        except Exception:
            continue
    return result


def system_metrics() -> Dict[str, Any]:
    if psutil is None:
        return {"cpu_percent": 0.0, "memory_percent": 0.0, "roblox": roblox_processes()}
    return {
        "cpu_percent": float(psutil.cpu_percent(interval=1.0)),
        "memory_percent": float(psutil.virtual_memory().percent),
        "roblox": roblox_processes(),
    }


def thermal_reading() -> Dict[str, Any]:
    if os.name != "nt":
        return {"available": False}
    try:
        command = [
            "powershell",
            "-NoProfile",
            "-Command",
            (
                "Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature "
                "-ErrorAction Stop | Select-Object -First 3 CurrentTemperature | ConvertTo-Json -Compress"
            ),
        ]
        proc = subprocess.run(command, capture_output=True, text=True, timeout=8)
        if proc.returncode != 0 or not proc.stdout.strip():
            return {"available": False, "error": proc.stderr.strip()[:200]}
        raw = json.loads(proc.stdout)
        rows = raw if isinstance(raw, list) else [raw]
        temps = []
        for row in rows:
            kelvin_tenths = float(row.get("CurrentTemperature") or 0.0)
            if kelvin_tenths > 0:
                temps.append(round((kelvin_tenths / 10.0) - 273.15, 1))
        return {"available": bool(temps), "celsius": temps}
    except Exception as exc:
        return {"available": False, "error": str(exc)[:200]}


def find_account(status: Dict[str, Any], username: str) -> Dict[str, Any]:
    target = username.strip().lower()
    for account in status.get("accounts") or []:
        keys = {
            str(account.get("username") or "").strip().lower(),
            str(account.get("account_id") or "").strip().lower(),
            str(account.get("display") or "").strip().lower(),
            str(account.get("cookie_username") or "").strip().lower(),
        }
        if target in keys:
            return account
    raise SoakFailure(f"Target account not found in /api/status: {username}")


def stop_runtime(api: ApiClient, log: MonitorLog, reason: str) -> None:
    log.write("warning", "stopping_runtime", reason=reason)
    for path in ("/api/stop", "/api/roblox/close-all"):
        try:
            log.write("info", "stop_call", path=path, response=api.post_json(path, {}, timeout=90))
        except Exception as exc:
            log.write("warning", "stop_call_failed", path=path, error=str(exc))


def configure_for_soak(api: ApiClient, account: str, args: argparse.Namespace, log: MonitorLog) -> None:
    payload = {
        "runtime_account_allowlist": [account],
        "max_concurrent_accounts": 1,
        "queue_duration_seconds": 0,
        "auto_close_enabled": False,
        "machine_supervisor_enabled": True,
        "machine_supervisor_max_launching_accounts": 1,
        "machine_supervisor_cpu_high_percent": args.cpu_guard_percent,
        "machine_supervisor_memory_high_percent": args.memory_guard_percent,
        "roblox_memory_guard_enabled": True,
        "roblox_memory_guard_mb": args.roblox_memory_guard_mb,
        "roblox_memory_guard_hold_seconds": args.roblox_memory_guard_hold_seconds,
        "relaunch_loop_fatal": False,
        "relaunch_loop_cooldown_seconds": args.relaunch_loop_cooldown_seconds,
        "popup_disconnected_enabled": True,
        "popup_scan_interval_seconds": 30,
        "popup_scan_max_parallel": 1,
        "fps_limiter_enabled": True,
        "fps_limit": args.fps_limit,
        "graphics_low_enabled": True,
        "graphics_quality_level": 1,
        "auto_process_priority_enabled": True,
        "process_priority": "low",
        "cpu_limiter_enabled": True,
        "cpu_limiter_mode": "hard",
        "cpu_limiter_default_percent": args.roblox_cpu_limit_percent,
        "cpu_limiter_apply_all": True,
        "multi_roblox_enabled": False,
    }
    response = api.post_json("/api/config", payload, timeout=30)
    log.write("info", "config_applied", response=response, payload=payload)


def wait_backend(api: ApiClient, timeout_seconds: float, log: MonitorLog) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        try:
            api.get_json("/api/status", timeout=5)
            api.refresh_token()
            if api.token:
                log.write("info", "backend_ready")
                return
        except Exception as exc:
            last_error = str(exc)
        time.sleep(2)
    raise SoakFailure(f"Backend did not become ready: {last_error}")


def start_response_allows_monitoring(response: Dict[str, Any]) -> bool:
    if response.get("ok") or response.get("duplicate"):
        return True
    msg = str(response.get("msg") or "").strip().lower()
    return "already running" in msg


def build_soak_summary(
    *,
    account: str,
    reached_in_game: bool,
    fatal_hits: List[Dict[str, Any]],
    orphan_processes: List[Dict[str, Any]],
    runtime_warnings: List[str],
    duration_seconds: float,
) -> Dict[str, Any]:
    failures: List[str] = []
    if not reached_in_game:
        failures.append(f"{account} never reached IN_GAME")
    if fatal_hits:
        failures.append(f"{len(fatal_hits)} fatal log pattern(s) detected")
    if orphan_processes:
        failures.append(f"{len(orphan_processes)} orphan Roblox process(es) after cleanup")
    if runtime_warnings:
        failures.append(f"runtime health warnings: {', '.join(runtime_warnings[:5])}")
    return {
        "ok": not failures,
        "account": account,
        "duration_seconds": int(max(0.0, float(duration_seconds or 0.0))),
        "failures": failures,
        "fatal_hits": fatal_hits[:10],
        "orphan_processes": orphan_processes[:10],
        "runtime_warnings": runtime_warnings[:10],
    }


def write_summary_json(path: str, summary: Dict[str, Any]) -> None:
    if not path:
        return
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def monitor(args: argparse.Namespace, evidence: Optional[Dict[str, Any]] = None) -> int:
    evidence = evidence if evidence is not None else {}
    evidence.setdefault("reached_in_game", False)
    evidence.setdefault("fatal_hits", [])
    evidence.setdefault("orphan_processes", [])
    evidence.setdefault("runtime_warnings", [])
    evidence.setdefault("duration_seconds", 0.0)
    data_dir = _app_data_dir()
    log_dir = data_dir / "logs"
    run_id = time.strftime("%Y%m%d-%H%M%S")
    report_path = log_dir / f"soak_monitor_{run_id}.jsonl"
    log = MonitorLog(report_path)
    api = ApiClient(args.base_url, log)
    log.write("info", "soak_monitor_started", account=args.account, duration_seconds=args.duration_seconds, report=str(report_path))

    log_paths = [
        log_dir / "cronus_rt1.log",
        log_dir / "cronus_rt1_events.jsonl",
    ]
    log_paths.extend(log_dir.glob("cronus_backend_*.err.log"))
    log_paths.extend(Path("logs").glob("*.err.log"))
    scanner = LogScanner(log_paths)

    wait_backend(api, args.backend_timeout_seconds, log)
    configure_for_soak(api, args.account, args, log)
    start_response = api.post_json("/api/start", {}, timeout=120)
    log.write("info", "farm_start_response", response=start_response)
    if not start_response_allows_monitoring(start_response):
        raise SoakFailure(f"Farm start rejected: {start_response}")
    if not start_response.get("ok"):
        log.write("warning", "farm_start_already_running", response=start_response)

    wall_started = time.monotonic()
    stable_started: Optional[float] = None
    cumulative_good_seconds = 0.0
    last_loop_at = time.monotonic()
    last_good_status: Optional[Dict[str, Any]] = None
    not_good_since: Optional[float] = None
    high_cpu_since: Optional[float] = None
    high_memory_since: Optional[float] = None
    last_thermal_at = 0.0
    last_heartbeat_log = 0.0

    while True:
        loop_started = time.monotonic()
        loop_delta = max(0.0, loop_started - last_loop_at)
        last_loop_at = loop_started
        wall_elapsed = time.monotonic() - wall_started
        if wall_elapsed > args.max_wall_seconds:
            raise SoakFailure(f"Max wall time exceeded before stable soak completed: {_fmt_elapsed(wall_elapsed)}")

        status = api.get_json("/api/status", timeout=10)
        health = api.get_json("/api/runtime/health", timeout=10)
        account = find_account(status, args.account)
        metrics = system_metrics()
        log_hits = scanner.scan()
        roblox_count = len(metrics.get("roblox") or [])

        if log_hits:
            evidence["fatal_hits"].extend(log_hits)
            raise SoakFailure(f"New fatal log pattern detected: {log_hits[:3]}")
        if roblox_count > args.max_roblox_processes:
            raise SoakFailure(f"Too many Roblox processes for single-account soak: {roblox_count}")

        cpu = float(metrics.get("cpu_percent") or 0.0)
        memory = float(metrics.get("memory_percent") or 0.0)
        now_mono = time.monotonic()
        if cpu >= args.cpu_guard_percent:
            high_cpu_since = high_cpu_since or now_mono
        else:
            high_cpu_since = None
        if memory >= args.memory_guard_percent:
            high_memory_since = high_memory_since or now_mono
        else:
            high_memory_since = None
        if high_cpu_since and now_mono - high_cpu_since >= args.guard_hold_seconds:
            raise SoakFailure(f"CPU guard tripped: cpu={cpu:.1f}% hold={_fmt_elapsed(now_mono - high_cpu_since)}")
        if high_memory_since and now_mono - high_memory_since >= args.guard_hold_seconds:
            raise SoakFailure(f"Memory guard tripped: memory={memory:.1f}% hold={_fmt_elapsed(now_mono - high_memory_since)}")

        if time.monotonic() - last_thermal_at >= args.thermal_interval_seconds:
            last_thermal_at = time.monotonic()
            log.write("info", "thermal_probe", reading=thermal_reading())

        runtime_health = health.get("runtime_health") or {}
        warnings = list(runtime_health.get("warnings") or [])
        evidence["runtime_warnings"] = [str(item) for item in warnings]
        state = str(account.get("state") or "")
        if state == "IN_GAME":
            evidence["reached_in_game"] = True
        process_alive = bool(account.get("process_alive"))
        liveness = str(account.get("liveness_state") or "")
        blocked = str(account.get("blocked_reason") or "")
        good = (
            bool(status.get("running"))
            and bool(health.get("ok"))
            and not warnings
            and state == "IN_GAME"
            and process_alive
            and liveness in {"alive", "active", "unknown"}
            and not blocked
        )

        if blocked and "allowlist" not in blocked.lower():
            raise SoakFailure(f"Target account became blocked: {blocked}")
        if state == "FAILED":
            raise SoakFailure(f"Target account failed: {account}")

        if good:
            stable_started = stable_started or time.monotonic()
            cumulative_good_seconds += loop_delta
            not_good_since = None
            last_good_status = account
        else:
            if stable_started is not None:
                log.write(
                    "warning",
                    "stable_window_reset",
                    state=state,
                    process_alive=process_alive,
                    liveness=liveness,
                    warnings=warnings,
                    previous_stable_elapsed=_fmt_elapsed(time.monotonic() - stable_started),
                )
            stable_started = None
            not_good_since = not_good_since or time.monotonic()
            if time.monotonic() - not_good_since >= args.max_outage_seconds:
                raise SoakFailure(
                    "Account did not return to stable IN_GAME in time: "
                    f"state={state} process_alive={process_alive} liveness={liveness} warnings={warnings}"
                )

        stable_elapsed = 0.0 if stable_started is None else time.monotonic() - stable_started
        if time.monotonic() - last_heartbeat_log >= args.heartbeat_log_seconds:
            last_heartbeat_log = time.monotonic()
            log.write(
                "info",
                "soak_heartbeat",
                wall_elapsed=_fmt_elapsed(wall_elapsed),
                stable_elapsed=_fmt_elapsed(stable_elapsed),
                cumulative_good_elapsed=_fmt_elapsed(cumulative_good_seconds),
                success_mode=args.success_mode,
                state=state,
                pid=account.get("pid"),
                process_alive=process_alive,
                liveness=liveness,
                cpu_percent=round(cpu, 1),
                memory_percent=round(memory, 1),
                roblox=metrics.get("roblox"),
                warnings=warnings,
                running=status.get("running"),
                status_revision=status.get("status_revision"),
            )

        success_elapsed = cumulative_good_seconds if args.success_mode == "cumulative" else stable_elapsed
        if success_elapsed >= args.duration_seconds:
            log.write(
                "success",
                "soak_passed",
                stable_elapsed=_fmt_elapsed(stable_elapsed),
                cumulative_good_elapsed=_fmt_elapsed(cumulative_good_seconds),
                success_mode=args.success_mode,
                wall_elapsed=_fmt_elapsed(wall_elapsed),
                final_account=last_good_status or account,
            )
            evidence["duration_seconds"] = wall_elapsed
            return 0

        time.sleep(args.interval_seconds)


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a guarded single-account Cronus soak test.")
    parser.add_argument("--base-url", default="http://127.0.0.1:7777")
    parser.add_argument("--account", required=True)
    parser.add_argument("--duration-seconds", type=float, default=6 * 3600)
    parser.add_argument("--max-wall-seconds", type=float, default=8 * 3600)
    parser.add_argument("--interval-seconds", type=float, default=30)
    parser.add_argument("--max-outage-seconds", type=float, default=300)
    parser.add_argument("--backend-timeout-seconds", type=float, default=120)
    parser.add_argument("--heartbeat-log-seconds", type=float, default=300)
    parser.add_argument("--thermal-interval-seconds", type=float, default=900)
    parser.add_argument("--cpu-guard-percent", type=float, default=88.0)
    parser.add_argument("--memory-guard-percent", type=float, default=92.0)
    parser.add_argument("--guard-hold-seconds", type=float, default=300)
    parser.add_argument("--roblox-cpu-limit-percent", type=float, default=35.0)
    parser.add_argument("--roblox-memory-guard-mb", type=float, default=8192.0)
    parser.add_argument("--roblox-memory-guard-hold-seconds", type=float, default=30.0)
    parser.add_argument("--relaunch-loop-cooldown-seconds", type=float, default=300.0)
    parser.add_argument("--fps-limit", type=int, default=30)
    parser.add_argument("--max-roblox-processes", type=int, default=1)
    parser.add_argument("--success-mode", choices=("continuous", "cumulative"), default="continuous")
    parser.add_argument("--summary-json", default="")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    data_dir = _app_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    log = MonitorLog(data_dir / "logs" / f"soak_monitor_boot_{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    evidence: Dict[str, Any] = {
        "reached_in_game": False,
        "fatal_hits": [],
        "orphan_processes": [],
        "runtime_warnings": [],
        "duration_seconds": 0.0,
    }
    try:
        code = monitor(args, evidence)
        summary = build_soak_summary(
            account=args.account,
            reached_in_game=bool(evidence.get("reached_in_game")),
            fatal_hits=list(evidence.get("fatal_hits") or []),
            orphan_processes=list(evidence.get("orphan_processes") or []),
            runtime_warnings=list(evidence.get("runtime_warnings") or []),
            duration_seconds=float(evidence.get("duration_seconds") or args.duration_seconds),
        )
        write_summary_json(args.summary_json, summary)
        return 0 if code == 0 and summary["ok"] else 1
    except Exception as exc:
        api = ApiClient(args.base_url, log)
        try:
            api.refresh_token()
            stop_runtime(api, log, str(exc))
        except Exception as stop_exc:
            log.write("error", "emergency_stop_failed", error=str(stop_exc))
        log.write("error", "soak_failed", error=str(exc))
        warnings = [str(item) for item in (evidence.get("runtime_warnings") or [])]
        warnings.append(str(exc))
        summary = build_soak_summary(
            account=args.account,
            reached_in_game=bool(evidence.get("reached_in_game")),
            fatal_hits=list(evidence.get("fatal_hits") or []),
            orphan_processes=list(evidence.get("orphan_processes") or []),
            runtime_warnings=warnings,
            duration_seconds=float(evidence.get("duration_seconds") or 0.0),
        )
        write_summary_json(args.summary_json, summary)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
