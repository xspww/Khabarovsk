from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, List, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_LIMIT = 4000
RELEASE_GATE_CACHE = "release_gate_last.json"


def _tail(text: str, limit: int = OUTPUT_LIMIT) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[-limit:]


def command_result(
    name: str,
    returncode: int,
    stdout: str,
    stderr: str,
    *,
    command: Sequence[str] | None = None,
    duration_seconds: float = 0.0,
) -> dict:
    return {
        "name": name,
        "status": "pass" if int(returncode) == 0 else "fail",
        "returncode": int(returncode),
        "command": list(command or []),
        "duration_seconds": round(float(duration_seconds or 0.0), 2),
        "stdout": _tail(stdout),
        "stderr": _tail(stderr),
    }


def build_gate_report(results: List[dict]) -> dict:
    fail_count = sum(1 for item in results if item.get("status") == "fail")
    warn_count = sum(1 for item in results if item.get("status") == "warn")
    return {
        "ok": fail_count == 0,
        "fail_count": fail_count,
        "warn_count": warn_count,
        "generated_at": round(time.time(), 3),
        "project_root": str(PROJECT_ROOT),
        "results": results,
    }


def _default_data_dir() -> Path:
    from app_paths import APP_DATA_DIR

    return Path(APP_DATA_DIR)


def write_gate_report_cache(report: dict, *, data_dir: Path | None = None) -> Path:
    root = Path(data_dir) if data_dir is not None else _default_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_result": "pass" if bool(report.get("ok", False)) else "fail",
        "last_run_at": float(report.get("generated_at") or time.time()),
        "fail_count": int(report.get("fail_count") or 0),
        "warn_count": int(report.get("warn_count") or 0),
        "project_root": str(report.get("project_root") or PROJECT_ROOT),
        "cached_at": time.time(),
    }
    path = root / RELEASE_GATE_CACHE
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def _run_command(name: str, command: Sequence[str], *, timeout: float) -> dict:
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            list(command),
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration = time.perf_counter() - started
        return command_result(
            name,
            proc.returncode,
            proc.stdout,
            proc.stderr,
            command=command,
            duration_seconds=duration,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.perf_counter() - started
        return command_result(
            name,
            124,
            str(exc.stdout or ""),
            f"command timed out after {timeout}s: {exc}",
            command=command,
            duration_seconds=duration,
        )
    except Exception as exc:
        duration = time.perf_counter() - started
        return command_result(
            name,
            1,
            "",
            str(exc),
            command=command,
            duration_seconds=duration,
        )


def _ui_js_files() -> List[Path]:
    ui_dir = PROJECT_ROOT / "ui"
    if not ui_dir.exists():
        return []
    return sorted(path for path in ui_dir.rglob("*.js") if path.is_file())


def _run_js_syntax_check() -> dict:
    files = _ui_js_files()
    if not files:
        return {
            "name": "ui_js_syntax",
            "status": "warn",
            "returncode": 0,
            "command": ["node", "--check", "ui/**/*.js"],
            "duration_seconds": 0.0,
            "stdout": "",
            "stderr": "No UI JavaScript files found.",
            "checked_count": 0,
        }
    started = time.perf_counter()
    failures: List[str] = []
    checked: List[str] = []
    for path in files:
        rel = str(path.relative_to(PROJECT_ROOT))
        checked.append(rel)
        proc = subprocess.run(
            ["node", "--check", str(path)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            failures.append(f"{rel}\n{proc.stdout}{proc.stderr}".strip())
    duration = time.perf_counter() - started
    return {
        "name": "ui_js_syntax",
        "status": "pass" if not failures else "fail",
        "returncode": 0 if not failures else 1,
        "command": ["node", "--check", "ui/**/*.js"],
        "duration_seconds": round(duration, 2),
        "stdout": _tail("\n".join(checked)),
        "stderr": _tail("\n\n".join(failures)),
        "checked_count": len(checked),
    }


def run_gate(args: argparse.Namespace) -> dict:
    results: List[dict] = []
    results.append(_run_command("compileall", [sys.executable, "-m", "compileall", "-q", "."], timeout=180))
    results.append(_run_command("unittest", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"], timeout=300))
    results.append(_run_js_syntax_check())
    results.append(_run_command("product_preflight", [sys.executable, ".\\ops\\product_preflight.py", "--json"], timeout=90))
    if not args.skip_idle_soak:
        results.append(
            _run_command(
                "control_plane_idle_soak",
                [
                    sys.executable,
                    ".\\ops\\control_plane_soak.py",
                    "--host",
                    args.host,
                    "--port",
                    str(args.port),
                    "--duration-seconds",
                    str(args.idle_soak_seconds),
                ],
                timeout=max(120.0, float(args.idle_soak_seconds) + 90.0),
            )
        )
    return build_gate_report(results)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Cronus release readiness gate.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument("--idle-soak-seconds", type=float, default=35.0)
    parser.add_argument("--skip-idle-soak", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(list(argv))


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    report = run_gate(args)
    try:
        write_gate_report_cache(report)
    except Exception:
        pass
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        for item in report["results"]:
            print(f"[{item['status'].upper()}] {item['name']} ({item.get('duration_seconds', 0)}s)")
            if item.get("stderr"):
                print(item["stderr"])
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
