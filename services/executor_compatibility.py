from __future__ import annotations

import json
import threading
import time
import urllib.request
from collections.abc import Callable, Iterable
from typing import Any

from infra import net_tls
from services._util import _cfg

WEAO_BASE = "https://weao.xyz/api"
WEAO_USER_AGENT = "WEAO-3PService"
# Official Roblox source for the latest WindowsPlayer version.
# WEAO is only used for executor status (status/exploits), never for
# deciding which Roblox client to download/install.
OFFICIAL_ROBLOX_VERSION_URL = "https://clientsettingscdn.roblox.com/v2/client-version/WindowsPlayer"
OFFICIAL_USER_AGENT = "CronusLauncher/RT"
ALLOWED_EXECUTORS = ("Volt", "Potassium", "Real", "Madium")


def _executor_row(item: dict[str, Any], canonical: str, latest: str) -> dict[str, Any]:
    supported = (
        str(item.get("rbxversion") or "").strip().lower() == latest.lower()
        and bool(item.get("updateStatus") is True)
    )
    return {
        "name": canonical,
        "version": str(item.get("version") or ""),
        "rbxversion": str(item.get("rbxversion") or ""),
        "update_status": bool(item.get("updateStatus") is True),
        "supported": supported,
        "updated_date": str(item.get("updatedDate") or ""),
    }


class ExecutorCompatibilityService:
    """Poll executor status (WEAO) + official Roblox version for compatibility decisions."""

    def __init__(self, cfg_mgr: Any, *, on_transition: Callable[[str, dict[str, Any]], None] | None = None, get_installed_versions: Callable[[], Iterable[str]] | None = None, logger: Callable[..., Any] | None = None):
        self.cfg_mgr = cfg_mgr
        self.on_transition = on_transition or (lambda _event, _payload: None)
        self.logger = logger or (lambda *_args, **_kwargs: None)
        self.get_installed_versions = get_installed_versions or (list)
        self._lock = threading.RLock()
        self._status: dict[str, Any] = {"state": "disabled", "executors": [], "latest_version": "", "error": ""}
        self._last_signature = ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def _lua_enabled(self) -> bool:
        try:
            data = self.cfg_mgr.snapshot() if hasattr(self.cfg_mgr, "snapshot") else {}
        except Exception:
            data = {}
        if isinstance(data, dict) and "lua_enabled" in data:
            return bool(data.get("lua_enabled", True))
        return bool(_cfg(self.cfg_mgr, "lua_enabled", _cfg(self.cfg_mgr, "use_lua", True)))

    def enabled(self) -> bool:
        if not self._lua_enabled():
            return False
        return bool(_cfg(self.cfg_mgr, "executor_monitor_enabled", False) or _cfg(self.cfg_mgr, "roblox_auto_update_enabled", False))

    def selected_name(self) -> str:
        return str(_cfg(self.cfg_mgr, "executor_selected", "") or "").strip()

    def _get_json(self, path: str) -> Any:
        request = urllib.request.Request(
            f"{WEAO_BASE}/{path.lstrip('/')}",
            headers={"User-Agent": WEAO_USER_AGENT, "Accept": "application/json"},
        )
        with net_tls.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8", "replace"))

    def _fetch_official_roblox_version(self) -> str:
        request = urllib.request.Request(
            OFFICIAL_ROBLOX_VERSION_URL,
            headers={"User-Agent": OFFICIAL_USER_AGENT, "Accept": "application/json"},
        )
        with net_tls.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        version = str((data or {}).get("clientVersionUpload") or (data or {}).get("version") or "").strip()
        if not version:
            raise RuntimeError("Official Roblox version missing")
        return version

    def _disabled_payload(self, lua_disabled: bool = False) -> dict[str, Any]:
        if lua_disabled:
            return {
                "ok": True,
                "state": "disabled",
                "executors": [],
                "selected": self.selected_name(),
                "latest_version": "",
                "error": "Enable Use Lua to use Roblox Executor",
                "lua_disabled": True,
            }
        return {
            "ok": True,
            "state": "disabled",
            "executors": [],
            "latest_version": "",
            "error": "",
        }

    def _build_rows(self, raw: Any, latest: str) -> list[dict[str, Any]]:
        allowed = {name.lower(): name for name in ALLOWED_EXECUTORS}
        rows: list[dict[str, Any]] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            canonical = allowed.get(title.lower())
            if not canonical or str(item.get("platform") or "").lower() != "windows":
                continue
            rows.append(_executor_row(item, canonical, latest))
        return rows

    def _clear_stale_selection(
        self,
        selected: str,
        selected_row: dict[str, Any] | None,
    ) -> str:
        if selected and not selected_row:
            try:
                self.cfg_mgr.update({"executor_selected": ""})
                self.cfg_mgr.save()
            except Exception:
                "intentional: ignore exception"
            return ""
        return selected

    def _build_payload(
        self,
        latest: str,
        rows: list[dict[str, Any]],
        selected: str,
        selected_row: dict[str, Any] | None,
    ) -> dict[str, Any]:
        state = (
            "unknown"
            if not selected_row
            else ("compatible" if selected_row["supported"] else "incompatible")
        )
        installed_versions = [
            str(value or "").strip()
            for value in self.get_installed_versions()
            if str(value or "").strip()
        ]
        needs_update = bool(
            latest
            and (
                not installed_versions
                or any(
                    value.lower() != latest.lower()
                    for value in installed_versions
                )
            )
        )
        return {
            "ok": True,
            "state": state,
            "executors": rows,
            "selected": selected,
            "selected_status": selected_row,
            "latest_version": latest,
            "installed_versions": installed_versions,
            "needs_update": needs_update,
            "error": "",
            "checked_at": time.time(),
        }

    def _error_payload(self, exc: Exception) -> dict[str, Any]:
        return {
            "ok": False,
            "state": "api_error",
            "executors": [],
            "selected": self.selected_name(),
            "latest_version": "",
            "error": str(exc),
            "checked_at": time.time(),
        }

    def _apply_transition(self, payload: dict[str, Any]) -> None:
        with self._lock:
            previous = dict(self._status)
            self._status = payload
        previous_state = str(previous.get("state") or "")
        previous_selected = previous.get("selected_status")
        previous_version = (
            str(previous_selected.get("version") or "")
            if isinstance(previous_selected, dict)
            else ""
        )
        current_selected = payload.get("selected_status")
        current_version = (
            str(current_selected.get("version") or "")
            if isinstance(current_selected, dict)
            else ""
        )
        payload["became_compatible"] = (
            previous_state == "incompatible" and payload.get("state") == "compatible"
        )
        payload["executor_version_changed"] = bool(
            previous_version and current_version and previous_version != current_version
        )
        signature = json.dumps(
            {
                key: payload.get(key)
                for key in (
                    "state",
                    "selected",
                    "latest_version",
                    "installed_versions",
                    "needs_update",
                    "error",
                    "executor_version_changed",
                )
            },
            sort_keys=True,
        )
        if signature != self._last_signature:
            self._last_signature = signature
            self.on_transition(str(payload.get("state") or "unknown"), dict(payload))

    def refresh(self) -> dict[str, Any]:
        if not self._lua_enabled():
            payload = self._disabled_payload(True)
            with self._lock:
                self._status = payload
            return dict(payload)
        if not self.enabled():
            payload = self._disabled_payload(False)
            with self._lock:
                self._status = payload
            return payload
        try:
            # Official-only: latest Roblox version comes from Roblox CDN,
            # not from WEAO. WEAO is only used below for executor rbxversion/status.
            latest = self._fetch_official_roblox_version()
            rows = self._build_rows(self._get_json("status/exploits"), latest)
            selected = self.selected_name()
            selected_row = next(
                (row for row in rows if row["name"].lower() == selected.lower()),
                None,
            )
            selected = self._clear_stale_selection(selected, selected_row)
            payload = self._build_payload(latest, rows, selected, selected_row)
        except Exception as exc:
            payload = self._error_payload(exc)
        self._apply_transition(payload)
        return dict(payload)

    def status(self) -> dict[str, Any]:
        with self._lock:
            cached = dict(self._status)
        if not self._lua_enabled() and cached.get("state") != "disabled":
            return {
                "ok": True,
                "state": "disabled",
                "executors": [],
                "selected": self.selected_name(),
                "latest_version": "",
                "error": "Enable Use Lua to use Roblox Executor",
                "lua_disabled": True,
            }
        return cached

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ExecutorCompatibility", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            interval = max(30, int(_cfg(self.cfg_mgr, "executor_check_interval_seconds", 30) or 30))
            self._stop.wait(interval)
