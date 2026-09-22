from __future__ import annotations

import threading
import time
import traceback
import urllib.error
import urllib.request
from typing import Any, Tuple

import uvicorn

from core import flog_kv
from desktop.constants import APP_USER_AGENT


class BackendHost:
    """Deep module: owns FastAPI/uvicorn lifecycle behind a small interface.

    Interface:
      configure(fastapi_app) -> void
      start() -> threading.Thread
      wait_ready(timeout_s) -> (bool, str)

    Everything else (uvicorn opts, readiness poll, progress paint) stays inside.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 7777) -> None:
        self._host = host
        self._port = int(port)
        self._app: Any = None
        self._error = ""

    def configure(self, fastapi_app: Any, port: int = 0) -> None:
        self._app = fastapi_app
        if port:
            self._port = int(port)

    @property
    def port(self) -> int:
        return int(self._port)

    @property
    def host(self) -> str:
        return str(self._host)

    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def start(self) -> threading.Thread:
        if self._app is None:
            raise RuntimeError("BackendHost is not configured")
        thread = threading.Thread(
            target=self._serve,
            daemon=True,
            name="UvicornServer",
        )
        thread.start()
        return thread

    def _serve(self) -> None:
        try:
            uvicorn.run(
                self._app,
                host=self._host,
                port=self._port,
                log_level="warning",
                access_log=False,
                log_config=None,
            )
            flog_kv("MAIN", "fastapi_thread_exited", "error", port=self._port)
        except BaseException as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            flog_kv(
                "MAIN",
                "fastapi_thread_failed",
                "error",
                port=self._port,
                error=self._error,
                traceback=traceback.format_exc(),
            )

    def wait_ready(self, server_thread: threading.Thread, timeout: float = 20.0) -> Tuple[bool, str]:
        wait_seconds = max(1.0, float(timeout or 20.0))
        started_at = time.time()
        deadline = started_at + wait_seconds
        url = f"{self.base_url()}/api/status"
        last_error = ""
        while time.time() < deadline:
            if not server_thread.is_alive():
                return False, self._error or "backend thread exited before ready"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": APP_USER_AGENT})
                with urllib.request.urlopen(req, timeout=0.8) as resp:
                    if 200 <= int(resp.status) < 500:
                        return True, f"status={resp.status}"
            except urllib.error.HTTPError as exc:
                if 200 <= int(exc.code) < 500:
                    return True, f"status={exc.code}"
                last_error = f"HTTPError: {exc.code}"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.25)
        if not server_thread.is_alive():
            return False, self._error or "backend thread exited before ready"
        return False, last_error or "backend readiness timeout"
