from __future__ import annotations

import argparse
import os
import sys


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Cronus backend without opening the desktop shell.")
    parser.add_argument("--host", default=os.environ.get("CRONUS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CRONUS_PORT", "7777")))
    parser.add_argument("--log-level", default=os.environ.get("CRONUS_LOG_LEVEL", "warning"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = _project_root()
    os.chdir(root)
    if root not in sys.path:
        sys.path.insert(0, root)
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    try:
        import uvicorn
    except ImportError:
        print("Missing dependency: run `python -m pip install -r requirements.txt`.", file=sys.stderr)
        return 2

    try:
        from desktop_host import clear_instance_state, prepare_backend_single_instance
    except Exception as exc:
        print(f"Failed to initialize Cronus single-instance guard: {exc}", file=sys.stderr)
        return 3

    if not prepare_backend_single_instance(int(args.port)):
        print("Cronus is already running; backend runner refused to start a hidden duplicate.", file=sys.stderr)
        return 0

    try:
        uvicorn.run(
            "main:app",
            host=str(args.host),
            port=int(args.port),
            log_level=str(args.log_level or "warning"),
            access_log=False,
            log_config=None,
        )
    finally:
        clear_instance_state()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
