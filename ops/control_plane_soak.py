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


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ERROR_PATTERNS = (
    "--- Logging error ---",
    "Traceback (most recent call last)",
    "Unhandled exception",
    "PermissionError",
)


def app_data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / "Cronus Launcher" / "data"
    return Path.home() / ".cronus_launcher" / "data"


def percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int((pct / 100.0) * len(ordered) + 0.999999) - 1))
    return round(float(ordered[idx]), 2)


def request_json(base_url: str, path: str, token: str = "", timeout: float = 5.0) -> tuple[Any, float]:
    headers = {"User-Agent": "CronusControlPlaneSoak/1"}
    if token:
        headers["X-Cronus-Token"] = token
    started = time.perf_counter()
    req = urllib.request.Request(base_url.rstrip("/") + path, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return (json.loads(raw) if raw else {}), elapsed_ms


def post_json(base_url: str, path: str, body: Dict[str, Any], token: str, timeout: float = 10.0) -> Any:
    headers = {
        "User-Agent": "CronusControlPlaneSoak/1",
        "Content-Type": "application/json",
    }
    if token:
        headers["X-Cronus-Token"] = token
    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw else {}


def read_instance_token(data_dir: Path) -> str:
    try:
        payload = json.loads((data_dir / "cronus_rt_instance.json").read_text(encoding="utf-8"))
        return str(payload.get("token") or "")
    except Exception:
        return ""


def read_instance_pid(data_dir: Path) -> int:
    try:
        payload = json.loads((data_dir / "cronus_rt_instance.json").read_text(encoding="utf-8"))
        return int(payload.get("pid") or 0)
    except Exception:
        return 0


class LogScanner:
    def __init__(self, paths: Iterable[Path]):
        self.offsets: Dict[Path, int] = {}
        for path in paths:
            try:
                self.offsets[path] = path.stat().st_size
            except OSError:
                self.offsets[path] = 0

    def scan(self) -> List[Dict[str, str]]:
        hits: List[Dict[str, str]] = []
        for path, old_offset in list(self.offsets.items()):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            offset = 0 if size < old_offset else old_offset
            self.offsets[path] = size
            if size <= offset:
                continue
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(offset)
                    for line in handle:
                        if any(pattern in line for pattern in ERROR_PATTERNS):
                            hits.append({"file": str(path), "line": line.strip()[:500]})
            except OSError:
                continue
        return hits


class JsonlReport:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: Any) -> None:
        row = {"ts": round(time.time(), 3), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str, separators=(",", ":")) + "\n")
        print(json.dumps(row, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)


def process_metrics(pid: int) -> Dict[str, Any]:
    if not pid or psutil is None:
        return {"pid": pid, "exists": bool(pid)}
    try:
        proc = psutil.Process(pid)
        return {
            "pid": pid,
            "exists": True,
            "cpu_percent": float(proc.cpu_percent(interval=None)),
            "rss_mb": round(float(proc.memory_info().rss) / (1024 * 1024), 1),
            "threads": int(proc.num_threads()),
        }
    except Exception:
        return {"pid": pid, "exists": False}


def roblox_process_count() -> int:
    if psutil is None:
        return 0
    total = 0
    for proc in psutil.process_iter(["name"]):
        try:
            if str(proc.info.get("name") or "").lower() == "robloxplayerbeta.exe":
                total += 1
        except Exception:
            continue
    return total


def start_backend(args: argparse.Namespace, log_dir: Path, report: JsonlReport) -> subprocess.Popen:
    stdout = log_dir / f"control_plane_backend_{time.strftime('%Y%m%d-%H%M%S')}.out.log"
    stderr = log_dir / f"control_plane_backend_{time.strftime('%Y%m%d-%H%M%S')}.err.log"
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "ops" / "run_backend.py"),
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--log-level",
        "warning",
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=stdout.open("w", encoding="utf-8"),
        stderr=stderr.open("w", encoding="utf-8"),
        env=env,
    )
    report.write("backend_started", launcher_pid=proc.pid, stdout=str(stdout), stderr=str(stderr))
    return proc


def wait_ready(base_url: str, data_dir: Path, timeout_seconds: float) -> str:
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    last_error = ""
    token = ""
    while time.monotonic() < deadline:
        token = token or read_instance_token(data_dir)
        try:
            request_json(base_url, "/api/status", timeout=3.0)
            if token:
                return token
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"backend not ready: {last_error}")


def run_soak(args: argparse.Namespace) -> int:
    data_dir = app_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir = data_dir / "logs"
    run_id = time.strftime("%Y%m%d-%H%M%S")
    report = JsonlReport(log_dir / f"control_plane_soak_{run_id}.jsonl")
    base_url = f"http://{args.host}:{args.port}"
    backend = start_backend(args, log_dir, report)
    token = ""
    latencies: List[float] = []
    errors: List[str] = []
    fatal_log_hits: List[Dict[str, str]] = []
    start = time.monotonic()
    last_heartbeat = 0.0
    scanner = LogScanner([
        log_dir / "cronus_rt1.log",
        log_dir / "cronus_rt1_events.jsonl",
    ])

    try:
        token = wait_ready(base_url, data_dir, args.backend_timeout_seconds)
        backend_pid = read_instance_pid(data_dir)
        report.write("backend_ready", backend_pid=backend_pid, token_present=bool(token))
        while time.monotonic() - start < args.duration_seconds:
            iteration_started = time.monotonic()
            for path, needs_token in (
                ("/api/status", False),
                ("/api/farm/health", False),
                ("/api/runtime/health", False),
                ("/api/runtime/diagnostics", True),
            ):
                try:
                    payload, elapsed_ms = request_json(base_url, path, token if needs_token else "", timeout=args.request_timeout_seconds)
                    latencies.append(elapsed_ms)
                    if path == "/api/status" and bool(payload.get("running")):
                        errors.append("farm unexpectedly running during control-plane soak")
                    if path == "/api/farm/health" and str(payload.get("state") or "") != "stopped":
                        errors.append(f"farm health state unexpected: {payload.get('state')}")
                except urllib.error.HTTPError as exc:
                    errors.append(f"{path}: HTTP {exc.code}")
                except Exception as exc:
                    errors.append(f"{path}: {exc}")

            hits = scanner.scan()
            if hits:
                fatal_log_hits.extend(hits)
            if roblox_process_count() > 0:
                errors.append("RobloxPlayerBeta.exe appeared during control-plane soak")

            now = time.monotonic()
            if now - last_heartbeat >= args.heartbeat_seconds:
                last_heartbeat = now
                report.write(
                    "heartbeat",
                    elapsed_seconds=round(now - start, 1),
                    request_count=len(latencies),
                    error_count=len(errors),
                    fatal_log_hit_count=len(fatal_log_hits),
                    backend=process_metrics(read_instance_pid(data_dir)),
                    latency_ms_p50=percentile(latencies, 50),
                    latency_ms_p95=percentile(latencies, 95),
                    roblox_process_count=roblox_process_count(),
                )
            sleep_for = max(0.0, args.interval_seconds - (time.monotonic() - iteration_started))
            time.sleep(sleep_for)

        summary = {
            "ok": not errors and not fatal_log_hits,
            "duration_seconds": round(time.monotonic() - start, 1),
            "request_count": len(latencies),
            "error_count": len(errors),
            "error_samples": errors[:10],
            "fatal_log_hit_count": len(fatal_log_hits),
            "fatal_log_samples": fatal_log_hits[:5],
            "latency_ms_min": round(min(latencies), 2) if latencies else 0.0,
            "latency_ms_p50": percentile(latencies, 50),
            "latency_ms_p95": percentile(latencies, 95),
            "latency_ms_max": round(max(latencies), 2) if latencies else 0.0,
            "backend": process_metrics(read_instance_pid(data_dir)),
            "report": str(report.path),
        }
        report.write("summary", **summary)
        return 0 if summary["ok"] else 1
    finally:
        try:
            if token:
                post_json(base_url, "/api/app/shutdown", {"token": token}, token, timeout=5.0)
                report.write("shutdown_requested")
        except Exception as exc:
            report.write("shutdown_failed", error=str(exc))
        time.sleep(2.0)
        if backend.poll() is None:
            backend.kill()
            report.write("backend_launcher_killed", launcher_pid=backend.pid)


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a backend/control-plane-only Cronus long soak.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument("--duration-seconds", type=float, default=2 * 3600)
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=300.0)
    parser.add_argument("--backend-timeout-seconds", type=float, default=45.0)
    parser.add_argument("--request-timeout-seconds", type=float, default=5.0)
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    return run_soak(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
