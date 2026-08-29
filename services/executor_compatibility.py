from __future__ import annotations

import json
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, Iterable, Optional


WEAO_BASE = "https://weao.xyz/api"
WEAO_USER_AGENT = "WEAO-3PService"
ALLOWED_EXECUTORS = ("Volt", "Potassium", "Real", "Madium")


class ExecutorCompatibilityService:
    """Poll WEAO and coordinate Roblox-version compatibility decisions."""

    def __init__(self, cfg_mgr: Any, *, on_transition: Optional[Callable[[str, Dict[str, Any]], None]] = None, get_installed_versions: Optional[Callable[[], Iterable[str]]] = None, logger: Optional[Callable[..., Any]] = None):
        self.cfg_mgr = cfg_mgr
        self.on_transition = on_transition or (lambda _event, _payload: None)
        self.logger = logger or (lambda *_args, **_kwargs: None)
        self.get_installed_versions = get_installed_versions or (lambda: [])
        self._lock = threading.RLock()
        self._status: Dict[str, Any] = {"state": "disabled", "executors": [], "latest_version": "", "error": ""}
        self._last_signature = ""
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            return self.cfg_mgr.get(key, default)
        except Exception:
            return default

    def enabled(self) -> bool:
        return bool(self._cfg("executor_monitor_enabled", False) or self._cfg("roblox_auto_update_enabled", False))

    def selected_name(self) -> str:
        return str(self._cfg("executor_selected", "") or "").strip()

    def _get_json(self, path: str) -> Any:
        request = urllib.request.Request(
            f"{WEAO_BASE}/{path.lstrip('/')}",
            headers={"User-Agent": WEAO_USER_AGENT, "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8", "replace"))

    def refresh(self) -> Dict[str, Any]:
        if not self.enabled():
            payload = {"ok": True, "state": "disabled", "executors": [], "latest_version": "", "error": ""}
            with self._lock:
                self._status = payload
            return payload
        try:
            versions = self._get_json("versions/current")
            latest = str((versions or {}).get("Windows") or "").strip()
            raw = self._get_json("status/exploits")
            allowed = {name.lower(): name for name in ALLOWED_EXECUTORS}
            rows = []
            for item in raw if isinstance(raw, list) else []:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                canonical = allowed.get(title.lower())
                if not canonical or str(item.get("platform") or "").lower() != "windows":
                    continue
                supported = str(item.get("rbxversion") or "").strip().lower() == latest.lower() and bool(item.get("updateStatus") is True)
                rows.append({
                    "name": canonical,
                    "version": str(item.get("version") or ""),
                    "rbxversion": str(item.get("rbxversion") or ""),
                    "update_status": bool(item.get("updateStatus") is True),
                    "supported": supported,
                    "updated_date": str(item.get("updatedDate") or ""),
                })
            selected = self.selected_name()
            selected_row = next((row for row in rows if row["name"].lower() == selected.lower()), None)
            if selected and not selected_row:
                try:
                    self.cfg_mgr.update({"executor_selected": ""})
                    self.cfg_mgr.save()
                except Exception:
                    pass
                selected = ""
            state = "unknown" if not selected_row else ("compatible" if selected_row["supported"] else "incompatible")
            installed_versions = [str(value or "").strip() for value in self.get_installed_versions() if str(value or "").strip()]
            payload = {"ok": True, "state": state, "executors": rows, "selected": selected, "selected_status": selected_row, "latest_version": latest, "installed_versions": installed_versions, "needs_update": bool(latest and any(value.lower() != latest.lower() for value in installed_versions)), "error": "", "checked_at": time.time()}
        except Exception as exc:
            payload = {"ok": False, "state": "api_error", "executors": [], "selected": self.selected_name(), "latest_version": "", "error": str(exc), "checked_at": time.time()}
        with self._lock:
            previous = dict(self._status)
            self._status = payload
        previous_state = str(previous.get("state") or "")
        previous_version = str(previous.get("selected_status", {}).get("version") or "") if isinstance(previous.get("selected_status"), dict) else ""
        current_version = str(payload.get("selected_status", {}).get("version") or "") if isinstance(payload.get("selected_status"), dict) else ""
        payload["became_compatible"] = previous_state == "incompatible" and payload.get("state") == "compatible"
        payload["executor_version_changed"] = bool(previous_version and current_version and previous_version != current_version)
        signature = json.dumps({key: payload.get(key) for key in ("state", "selected", "latest_version", "installed_versions", "needs_update", "error", "executor_version_changed")}, sort_keys=True)
        if signature != self._last_signature:
            self._last_signature = signature
            self.on_transition(str(payload.get("state") or "unknown"), dict(payload))
        return dict(payload)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._status)

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
            interval = max(30, int(self._cfg("executor_check_interval_seconds", 30) or 30))
            self._stop.wait(interval)
