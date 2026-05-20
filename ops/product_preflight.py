from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
WATCHDOG_STATUS_CACHE = "watchdog_task_last.json"


def _status(ok: bool, name: str, msg: str, **fields: Any) -> Dict[str, Any]:
    return {"status": "pass" if ok else "fail", "name": name, "msg": msg, **fields}


def _warn(name: str, msg: str, **fields: Any) -> Dict[str, Any]:
    return {"status": "warn", "name": name, "msg": msg, **fields}


def _is_port_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=0.35):
            return True
    except OSError:
        return False


def _account_name(account: Mapping[str, Any]) -> str:
    return str(account.get("username") or account.get("account") or account.get("display") or "").strip()


def _account_has_target(account: Mapping[str, Any]) -> bool:
    return bool(str(account.get("place_id") or "").strip() or list(account.get("vip_links") or []))


def evaluate_launch_target_readiness(
    cfg: Mapping[str, Any],
    accounts: Iterable[Mapping[str, Any]],
) -> Dict[str, Any]:
    rows = [row for row in accounts if isinstance(row, Mapping)]
    has_global_target = bool(
        str(cfg.get("game_place_id") or "").strip()
        or str(cfg.get("game_private_server_url") or "").strip()
    )
    missing = [] if has_global_target else [_account_name(row) for row in rows if not _account_has_target(row)]
    missing = [name or "<blank>" for name in missing]
    if not rows:
        return {
            "status": "fail",
            "name": "launch_targets",
            "msg": "No accounts are configured.",
            "missing_target_count": 0,
            "missing_targets": [],
            "required_action": "Import at least one account before running live smoke.",
        }
    if missing:
        return {
            "status": "fail",
            "name": "launch_targets",
            "msg": f"{len(missing)} account(s) do not have a launch target.",
            "missing_target_count": len(missing),
            "missing_targets": missing[:10],
            "required_action": "Set game_place_id, game_private_server_url, per-account place_id, or a VIP link before /api/start.",
        }
    return {
        "status": "pass",
        "name": "launch_targets",
        "msg": "Launch target is configured.",
        "missing_target_count": 0,
        "missing_targets": [],
        "required_action": "",
    }


def evaluate_watchdog_readiness(status: Mapping[str, Any]) -> Dict[str, Any]:
    if status.get("_inspection_error"):
        return _warn(
            "watchdog_task",
            f"Watchdog task could not be inspected: {status.get('_inspection_error')}",
            **dict(status),
        )
    installed = bool(status.get("TaskInstalled"))
    root_matches = bool(status.get("ProjectRootMatches"))
    script_exists = bool(status.get("WatchdogScriptExists"))
    if not installed:
        return _status(False, "watchdog_task", "Watchdog task is not installed.", **dict(status))
    if not script_exists:
        return _status(False, "watchdog_task", "Watchdog script is missing.", **dict(status))
    if not root_matches:
        return _status(
            False,
            "watchdog_task",
            "Watchdog task is stale; reinstall it from the current repo.",
            **dict(status),
        )
    return _status(True, "watchdog_task", "Watchdog task points to the current repo.", **dict(status))


def _default_data_dir() -> Path:
    from app_paths import APP_DATA_DIR

    return Path(APP_DATA_DIR)


def write_watchdog_status_cache(status: Mapping[str, Any], *, data_dir: Path | None = None) -> Path:
    root = Path(data_dir) if data_dir is not None else _default_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    payload = dict(status)
    payload["cached_at"] = time.time()
    path = root / WATCHDOG_STATUS_CACHE
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def _git_dirty_check(root: Path) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        return _warn("git_status", f"Git status unavailable: {exc}")
    if proc.returncode != 0:
        return _warn("git_status", (proc.stderr or proc.stdout or "git status failed").strip()[:300])
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if lines:
        return _warn("git_status", f"Working tree has {len(lines)} changed path(s).", changed_count=len(lines))
    return _status(True, "git_status", "Working tree is clean.", changed_count=0)


def _load_runtime_inputs() -> tuple[Mapping[str, Any], List[Mapping[str, Any]], Dict[str, str]]:
    from account_hybrid import ACCOUNT_DATA_FILE, AccountDataStore
    from config_store import CONFIG_FILE, ConfigManager

    cfg = ConfigManager().snapshot()
    records = AccountDataStore().read_records(include_cookies=False)
    paths = {"config_file": CONFIG_FILE, "account_data_file": ACCOUNT_DATA_FILE}
    return cfg, records, paths


def _collect_watchdog_status(port: int) -> Dict[str, Any]:
    script = PROJECT_ROOT / "ops" / "watchdog_status.ps1"
    command = (
        f"& {{ & '{script}' -Port {int(port)} | "
        "ConvertTo-Json -Depth 6 -Compress }"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=12,
        )
    except Exception as exc:
        return {"_inspection_error": str(exc)[:300]}
    if proc.returncode != 0:
        return {"_inspection_error": (proc.stderr or proc.stdout or "watchdog status failed").strip()[:500]}
    try:
        data = json.loads(proc.stdout.strip() or "{}")
    except Exception as exc:
        return {"_inspection_error": f"invalid watchdog status JSON: {exc}"}
    return data if isinstance(data, dict) else {"_inspection_error": "watchdog status returned non-object JSON"}


def collect_preflight(*, host: str = "127.0.0.1", port: int = 7777, allow_running: bool = False) -> Dict[str, Any]:
    cfg, accounts, paths = _load_runtime_inputs()
    checks: List[Dict[str, Any]] = []
    checks.append(_status(Path(paths["config_file"]).exists(), "config_file", paths["config_file"]))
    checks.append(_status(Path(paths["account_data_file"]).exists(), "account_data_file", paths["account_data_file"]))
    checks.append(_status(bool(accounts), "accounts", f"{len(accounts)} account(s) configured.", count=len(accounts)))

    names = [_account_name(row).lower() for row in accounts if _account_name(row)]
    duplicate_count = len(names) - len(set(names))
    checks.append(_status(duplicate_count == 0, "duplicate_accounts", f"{duplicate_count} duplicate username(s).", duplicate_count=duplicate_count))
    cookie_count = sum(1 for row in accounts if str(row.get("encrypted_cookie") or row.get("cookie") or "").strip())
    checks.append(_status(cookie_count == len(accounts) and bool(accounts), "cookies", f"{cookie_count}/{len(accounts)} account(s) have stored cookies.", cookie_count=cookie_count))
    checks.append(evaluate_launch_target_readiness(cfg, accounts))

    port_busy = _is_port_listening(host, port)
    port_ok = allow_running or not port_busy
    checks.append(_status(port_ok, "backend_port", f"{host}:{port} {'is listening' if port_busy else 'is free'}.", listening=port_busy))
    checks.append(_status((PROJECT_ROOT / "ops" / "run_backend.py").exists(), "backend_runner", "ops/run_backend.py exists."))
    checks.append(_status((PROJECT_ROOT / "ops" / "soak_monitor.py").exists(), "soak_monitor", "ops/soak_monitor.py exists."))
    checks.append(_status((PROJECT_ROOT / "ops" / "install_watchdog_task.ps1").exists(), "watchdog_install", "ops/install_watchdog_task.ps1 exists."))
    watchdog_status = _collect_watchdog_status(port)
    try:
        write_watchdog_status_cache(watchdog_status)
    except Exception:
        pass
    checks.append(evaluate_watchdog_readiness(watchdog_status))
    checks.append(_git_dirty_check(PROJECT_ROOT))

    fail_count = sum(1 for item in checks if item.get("status") == "fail")
    warn_count = sum(1 for item in checks if item.get("status") == "warn")
    return {
        "ok": fail_count == 0,
        "fail_count": fail_count,
        "warn_count": warn_count,
        "project_root": str(PROJECT_ROOT),
        "checks": checks,
    }


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Cronus product readiness preflight checks.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument("--allow-running", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    report = collect_preflight(host=args.host, port=args.port, allow_running=args.allow_running)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        for item in report["checks"]:
            print(f"[{item['status'].upper()}] {item['name']}: {item['msg']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
