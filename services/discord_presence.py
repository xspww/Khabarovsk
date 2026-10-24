"""Discord Rich Presence for Cronus Manager.

Stdlib-only Discord IPC client (Windows named pipe ``\\\\.\\pipe\\discord-ipc-N``).
No extra dependency. Runs a background thread that polls config + farm status
every 1s and pushes activity when enabled.

Activity shape:
  details: "Farming X/Y accounts" (or "Idle" when farm stopped)
  state:   "Place <id>" when show_game is on, else Cronus version / uptime
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
from collections.abc import Callable
from typing import Any, NamedTuple

_OP_HANDSHAKE = 0
_OP_FRAME = 1

_DEFAULT_UPDATE_INTERVAL = 1.0

# ── Built-in Discord Application ID (one-click mode) ──
# End users just toggle "Enable" — no Client ID typing needed.
# Maintainer: create ONE app at https://discord.com/developers/applications,
# upload assets/cronus_icon.png as Rich Presence asset named "cronus_icon",
# then paste the Application ID below.
DEFAULT_DISCORD_CLIENT_ID = "1528494483904270386"


def _flog_kv(scope: str, name: str, level: str = "info", **fields: Any) -> None:
    try:
        from core import flog_kv as _fn

        _fn(scope, name, level, **fields)
    except Exception:
        "intentional: ignore exception"


def _releases_url() -> str:
    try:
        from version import releases_page_url as _url

        return _url()
    except Exception:
        return "https://github.com/xspww/Khabarovsk/releases"


class _DiscordIpc:
    """Minimal Discord IPC transport: named pipe on Windows, unix socket elsewhere."""

    def __init__(self, client_id: str):
        self.client_id = str(client_id or "").strip()
        self._sock: Any | None = None
        self._pipe_file: Any | None = None

    def close(self) -> None:
        try:
            if self._pipe_file is not None:
                try:
                    self._pipe_file.close()
                except Exception:
                    "intentional: ignore exception"
        finally:
            self._pipe_file = None
        try:
            if self._sock is not None:
                try:
                    self._sock.close()
                except Exception:
                    "intentional: ignore exception"
        finally:
            self._sock = None

    def _send(self, op: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        header = struct.pack("<II", op, len(data))
        blob = header + data
        if self._pipe_file is not None:
            self._pipe_file.write(blob)
            self._pipe_file.flush()
            return
        if self._sock is not None:
            self._sock.sendall(blob)
            return
        raise OSError("not connected")

    def _recv(self, timeout: float = 5.0) -> dict[str, Any]:
        raw_header = self._recv_exact(8, timeout)
        if len(raw_header) < 8:
            raise OSError("short ipc header")
        _op, length = struct.unpack("<II", raw_header)
        if length <= 0 or length > 64 * 1024:
            raise OSError(f"bad ipc length: {length}")
        raw_body = self._recv_exact(int(length), timeout)
        try:
            return json.loads(raw_body.decode("utf-8") or "{}")
        except Exception:
            return {}

    def _recv_exact(self, size: int, timeout: float) -> bytes:
        chunks = b""
        deadline = time.time() + max(0.5, float(timeout))
        if self._pipe_file is not None:
            # Named-pipe file object: blocking read with deadline polling.
            self._pipe_file.flush()
            while len(chunks) < size and time.time() < deadline:
                try:
                    part = self._pipe_file.read(size - len(chunks))
                except Exception as exc:
                    raise OSError(f"pipe read failed: {exc}")
                if not part:
                    time.sleep(0.05)
                    continue
                chunks += part if isinstance(part, bytes) else bytes(part)
            return chunks
        sock = self._sock
        assert sock is not None
        sock.settimeout(max(0.5, min(5.0, timeout)))
        while len(chunks) < size:
            part = sock.recv(size - len(chunks))
            if not part:
                break
            chunks += part
        return chunks

    def connect(self, timeout: float = 5.0) -> None:
        self.close()
        if not self.client_id:
            raise ValueError("missing client_id")
        handshake = {"v": 1, "client_id": self.client_id}
        last_error: Exception | None = None
        # Windows: try discord-ipc-0..9 named pipes.
        if os.name == "nt":
            for i in range(10):
                pipe_name = rf"\\.\pipe\discord-ipc-{i}"
                try:
                    pipe = open(pipe_name, "r+b", buffering=0)  # noqa: SIM115 - pipe ownership moves to self._pipe_file
                    self._pipe_file = pipe
                    self._send(_OP_HANDSHAKE, handshake)
                    self._recv(timeout=timeout)
                    return
                except Exception as exc:  # pipe busy / Discord closed
                    last_error = exc
                    self.close()
                    continue
            raise OSError(f"Discord IPC unavailable: {last_error}")
        # Non-Windows fallback: discord unix socket (dev / tests).
        for i in range(10):
            for base in (
                os.environ.get("XDG_RUNTIME_DIR", "/tmp"),
                "/tmp",
            ):
                path = os.path.join(base, f"discord-ipc-{i}")
                if not os.path.exists(path):
                    continue
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.settimeout(timeout)
                    sock.connect(path)
                    self._sock = sock
                    self._send(_OP_HANDSHAKE, handshake)
                    self._recv(timeout=timeout)
                    return
                except Exception as exc:
                    last_error = exc
                    self.close()
                    continue
        raise OSError(f"Discord IPC unavailable: {last_error}")

    def set_activity(self, activity: dict[str, Any], pid: int) -> dict[str, Any]:
        payload = {
            "cmd": "SET_ACTIVITY",
            "args": {"pid": int(pid or os.getpid()), "activity": activity},
            "nonce": f"{time.time():.3f}",
        }
        self._send(_OP_FRAME, payload)
        try:
            return self._recv(timeout=5.0)
        except Exception:
            return {}

    def clear_activity(self, pid: int) -> None:
        try:
            self._send(
                _OP_FRAME,
                {
                    "cmd": "SET_ACTIVITY",
                    "args": {"pid": int(pid or os.getpid()), "activity": None},
                    "nonce": f"{time.time():.3f}",
                },
            )
        except Exception:
            "intentional: ignore exception"


class _PresenceConfig(NamedTuple):
    enabled: bool
    client_id: str
    show_accounts: bool
    show_game: bool


class _PresenceStatus(NamedTuple):
    running: bool
    total: int
    in_game: int
    place_id: str


def _num(value: Any) -> int:
    try:
        return max(0, int(float(value or 0)))
    except Exception:
        return 0


class DiscordPresenceService:
    """Background presence loop. Single owner for Discord RPC state."""

    def __init__(
        self,
        cfg_getter: Callable[[str, Any], Any] | None = None,
        status_getter: Callable[[], dict[str, Any]] | None = None,
    ):
        self._cfg_getter = cfg_getter
        self._status_getter = status_getter
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ipc: _DiscordIpc | None = None
        self._activity_start: int = 0
        self._last_push_at: float = 0.0
        self._state: dict[str, Any] = {
            "enabled": False,
            "connected": False,
            "client_id": "",
            "last_update_at": 0.0,
            "last_error": "",
            "last_activity": {},
        }

    # -- wiring ---------------------------------------------------------
    def configure(
        self,
        cfg_getter: Callable[[str, Any], Any],
        status_getter: Callable[[], dict[str, Any]],
    ) -> None:
        with self._lock:
            self._cfg_getter = cfg_getter
            self._status_getter = status_getter

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="DiscordPresence", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            ipc, self._ipc = self._ipc, None
        if ipc is not None:
            try:
                ipc.clear_activity(os.getpid())
            except Exception:
                "intentional: ignore exception"
            try:
                ipc.close()
            except Exception:
                "intentional: ignore exception"
        with self._lock:
            self._state["connected"] = False

    # -- public snapshot -------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            snap = dict(self._state)
        try:
            snap["effective_client_id"] = self.effective_client_id()
            snap["has_app"] = bool(self.effective_client_id())
        except Exception:
            snap["effective_client_id"] = ""
            snap["has_app"] = False
        return snap

    def test(self, client_id: str = "") -> dict[str, Any]:
        """One-shot connect + sample activity. Used by the Test button."""
        cid = str(client_id or "").strip() or self._current_client_id()
        if not cid:
            return {"ok": False, "msg": "Discord app not configured yet — set DEFAULT_DISCORD_CLIENT_ID in services/discord_presence.py"}
        ipc = _DiscordIpc(cid)
        try:
            ipc.connect(timeout=5.0)
            activity = self._build_activity(
                running=False, total=0, in_game=0, place_id="", show_accounts=True, show_game=True
            )
            activity["details"] = "Cronus Launcher — test"
            ipc.set_activity(activity, os.getpid())
            # NOTE: no clear_activity here on purpose — Discord keys activity
            # by pid, so clearing would wipe the live presence set by the loop.
            # Record the test text so the loop re-pushes the real activity
            # on its next 1s tick instead of leaving "— test" lingering.
            with self._lock:
                self._state["last_activity"] = dict(activity)
            return {"ok": True, "msg": "Discord test activity sent (check your profile)"}
        except Exception as exc:
            return {"ok": False, "msg": f"Discord test failed: {exc}"}
        finally:
            try:
                ipc.close()
            except Exception:
                "intentional: ignore exception"

    # -- internals -------------------------------------------------------
    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            if self._cfg_getter is not None:
                return self._cfg_getter(key, default)
        except Exception:
            "intentional: ignore exception"
        return default

    def effective_client_id(self) -> str:
        """One-click mode: always the built-in app ID (ignore stale custom)."""
        return DEFAULT_DISCORD_CLIENT_ID.strip()

    def _current_client_id(self) -> str:
        return self.effective_client_id()

    def _build_activity(
        self,
        running: bool,
        total: int,
        in_game: int,
        place_id: str,
        show_accounts: bool,
        show_game: bool,
    ) -> dict[str, Any]:
        # Stable session timer: set once per enable-session so Discord counts
        # up instead of resetting on every update.
        if not self._activity_start:
            self._activity_start = int(time.time())
        now = self._activity_start
        account_word = "account" if int(total) == 1 else "accounts"
        if running:
            details = f"Farming {in_game}/{total} {account_word}" if show_accounts else "Farming"
        else:
            details = "Idle"
            if show_accounts and total:
                details = f"Idle — {total} {account_word}"
        if show_game and place_id:
            state = f"Place {place_id}"
        else:
            try:
                from version import app_display_version as _ver

                state = f"Cronus {_ver()}"
            except Exception:
                state = "Cronus Launcher"
        return {
            "details": str(details)[:128],
            "state": str(state)[:128],
            "timestamps": {"start": now},
            "assets": {
                # Program logo: upload assets/cronus_icon.png to the Discord
                # app's Rich Presence asset with key name "cronus_icon".
                "large_image": "cronus_icon",
                "large_text": "Cronus Launcher",
            },
            "buttons": [
                {"label": "Download", "url": _releases_url()},
            ],
            "instance": False,
        }

    def _read_loop_config(self) -> _PresenceConfig:
        return _PresenceConfig(
            enabled=bool(
                self._cfg("discord_presence_enabled", False)
            ),
            client_id=self._current_client_id(),
            show_accounts=bool(
                self._cfg("discord_show_accounts", True)
            ),
            show_game=bool(
                self._cfg("discord_show_game", True)
            ),
        )

    def _teardown_presence(self, config: _PresenceConfig) -> None:
        with self._lock:
            ipc, self._ipc = self._ipc, None
            self._activity_start = 0
            self._last_push_at = 0.0
            if config.enabled and not config.client_id:
                self._state["last_error"] = (
                    "Discord app not configured "
                    "(set DEFAULT_DISCORD_CLIENT_ID)"
                )
        if ipc is None:
            return
        try:
            ipc.clear_activity(os.getpid())
        except Exception:
            "intentional: ignore exception"
        try:
            ipc.close()
        except Exception:
            "intentional: ignore exception"
        with self._lock:
            self._state["connected"] = False

    def _presence_place_id(
        self,
        status: dict[str, Any],
    ) -> str:
        place_id = str(
            status.get("game_place_id")
            or status.get("place_id")
            or (status.get("game") or {}).get("place_id", "")
            or ""
        ).strip()
        if place_id:
            return place_id
        try:
            return str(
                self._cfg("game_place_id", "") or ""
            ).strip()
        except Exception:
            return ""

    def _presence_status(self) -> _PresenceStatus:
        running, total, in_game, place_id = False, 0, 0, ""
        try:
            status = (
                self._status_getter()
                if self._status_getter
                else {}
            ) or {}
            running = bool(status.get("running"))
            total = _num(status.get("total_accounts"))
            in_game = _num(
                status.get("in_game")
                or status.get("in_game_count")
            )
            place_id = self._presence_place_id(status)
        except Exception:
            "intentional: ignore exception"
        return _PresenceStatus(
            running=running,
            total=total,
            in_game=in_game,
            place_id=place_id,
        )

    def _ensure_ipc(
        self,
        client_id: str,
    ) -> tuple[_DiscordIpc, bool]:
        with self._lock:
            needs_new_client = (
                self._ipc is None
                or getattr(self._ipc, "client_id", "") != client_id
            )
            ipc = self._ipc
        if needs_new_client:
            if ipc is not None:
                try:
                    ipc.close()
                except Exception:
                    "intentional: ignore exception"
            ipc = _DiscordIpc(client_id)
            ipc.connect(timeout=5.0)
            with self._lock:
                self._ipc = ipc
        assert ipc is not None
        return ipc, needs_new_client

    def _push_activity_if_changed(
        self,
        ipc: _DiscordIpc,
        activity: dict[str, Any],
        needs_new_client: bool,
    ) -> None:
        with self._lock:
            last = self._state.get("last_activity") or {}
        now_ts = time.time()
        changed = (
            needs_new_client
            or activity.get("details") != last.get("details")
            or activity.get("state") != last.get("state")
            or (now_ts - (self._last_push_at or 0.0)) >= 15.0
        )
        if changed:
            ipc.set_activity(activity, os.getpid())
            self._last_push_at = now_ts

    def _mark_connected(self, activity: dict[str, Any]) -> None:
        with self._lock:
            self._state["connected"] = True
            self._state["last_update_at"] = time.time()
            self._state["last_error"] = ""
            self._state["last_activity"] = activity

    def _handle_loop_error(self, exc: Exception, failures: int) -> int:
        failures += 1
        with self._lock:
            self._state["connected"] = False
            self._state["last_error"] = str(exc)[:300]
            try:
                if self._ipc is not None:
                    try:
                        self._ipc.close()
                    except Exception:
                        "intentional: ignore exception"
            finally:
                self._ipc = None
        if failures <= 2:
            _flog_kv(
                "DISCORD",
                "presence_update_failed",
                "warning",
                error=str(exc)[:200],
            )
        return failures

    def _loop(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                config = self._read_loop_config()
                with self._lock:
                    self._state["enabled"] = config.enabled
                    self._state["client_id"] = config.client_id
                if not config.enabled or not config.client_id:
                    self._teardown_presence(config)
                    self._stop.wait(_DEFAULT_UPDATE_INTERVAL)
                    continue
                status = self._presence_status()
                activity = self._build_activity(
                    status.running,
                    status.total,
                    status.in_game,
                    status.place_id,
                    config.show_accounts,
                    config.show_game,
                )
                ipc, needs_new_client = self._ensure_ipc(
                    config.client_id,
                )
                self._push_activity_if_changed(
                    ipc,
                    activity,
                    needs_new_client,
                )
                self._mark_connected(activity)
                failures = 0
            except Exception as exc:
                failures = self._handle_loop_error(exc, failures)
            self._stop.wait(_DEFAULT_UPDATE_INTERVAL)


DISCORD_PRESENCE = DiscordPresenceService()
