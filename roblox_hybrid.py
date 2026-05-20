from __future__ import annotations

import atexit
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from app_paths import EXECUTABLE_PATH, IS_COMPILED
from account_hybrid import ACCOUNT_STORE, decrypt_cookie
from services.captcha_guard import CAPTCHA_BLOCK_REASON, CAPTCHA_REASON, captcha_detail, is_captcha_text
from domain.roblox_private_servers import (
    _safe_hash,
    build_owned_private_server_link,
    build_place_launcher_url,
    build_roblox_player_uri,
    ensure_owned_private_server as _ensure_owned_private_server,
    fetch_private_server_metadata,
    game_name_for_universe,
    list_my_private_servers,
    list_private_servers_for_place,
    parse_launch_destination_from_cmdline,
    parse_vip_access_code_html,
    parse_vip_components,
    parse_vip_link,
    private_servers_enabled_for_universe,
    resolve_vip_access_code,
    universe_id_for_place,
)


USER_AGENT = "CronusLauncherHybrid/1.0"
_MULTI_ROBLOX_LOCK = threading.RLock()
_MULTI_ROBLOX_HANDLES: List[Tuple[str, int]] = []
_MULTI_ROBLOX_HELPER: Optional[subprocess.Popen] = None
_MULTI_ROBLOX_STATE = "stopped"
_MULTI_ROBLOX_DETAIL = ""
_MULTI_ROBLOX_LAST_FAILURE = ""
_MULTI_ROBLOX_STARTED_AT = 0.0
_MULTI_ROBLOX_HANDLE_NAMES: List[str] = []
_MULTI_ROBLOX_GUARD_MODE = "mutex"
ROBLOX_HOME = "https://www.roblox.com/"
AUTH_BASE = "https://auth.roblox.com/"
USERS_BASE = "https://users.roblox.com/"


class RobloxHTTP:
    def __init__(self, cookie: str = ""):
        self.cookie = str(cookie or "").strip()
        self.csrf_token = ""

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": ROBLOX_HOME,
        }
        if self.cookie:
            headers["Cookie"] = f".ROBLOSECURITY={self.cookie}"
        if self.csrf_token:
            headers["X-CSRF-TOKEN"] = self.csrf_token
        if extra:
            headers.update(extra)
        return headers

    def request(
        self,
        url: str,
        method: str = "GET",
        data: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 12.0,
        retry_csrf: bool = True,
    ) -> Tuple[int, str, Dict[str, str]]:
        body = None
        req_headers = self._headers(headers)
        if data is not None:
            if isinstance(data, (dict, list)):
                body = json.dumps(data).encode("utf-8")
                req_headers.setdefault("Content-Type", "application/json")
            elif isinstance(data, bytes):
                body = data
            else:
                body = str(data).encode("utf-8")
                req_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        req = urllib.request.Request(url, data=body, method=method.upper(), headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace"), dict(resp.headers.items())
        except urllib.error.HTTPError as exc:
            response_headers = dict(exc.headers.items())
            token = response_headers.get("x-csrf-token") or response_headers.get("X-CSRF-TOKEN")
            if token and retry_csrf and method.upper() not in {"GET", "HEAD"}:
                self.csrf_token = token
                return self.request(url, method, data, headers, timeout, retry_csrf=False)
            try:
                body_text = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body_text = str(exc)
            return exc.code, body_text, response_headers

    def get_csrf(self) -> Tuple[bool, str]:
        status, body, headers = self.request(
            AUTH_BASE + "v1/authentication-ticket/",
            method="POST",
            headers={"Content-Type": "application/json"},
            retry_csrf=False,
        )
        token = headers.get("x-csrf-token") or headers.get("X-CSRF-TOKEN")
        if token:
            self.csrf_token = token
            return True, token
        challenge_detail = captcha_detail(status, body, headers)
        if challenge_detail:
            return False, challenge_detail
        return False, f"csrf failed ({status}) {body[:180]}"

    def get_auth_ticket(self) -> Tuple[bool, str]:
        if not self.csrf_token:
            ok, detail = self.get_csrf()
            if not ok:
                return False, detail
        status, body, headers = self.request(
            AUTH_BASE + "v1/authentication-ticket/",
            method="POST",
            headers={"Content-Type": "application/json", "X-CSRF-TOKEN": self.csrf_token},
            retry_csrf=True,
        )
        ticket = headers.get("rbx-authentication-ticket") or headers.get("Rbx-Authentication-Ticket")
        if ticket:
            return True, ticket
        challenge_detail = captcha_detail(status, body, headers)
        if challenge_detail:
            return False, challenge_detail
        return False, f"auth ticket failed ({status}) {body[:180]}"

    def authenticated_user(self) -> Tuple[bool, Dict[str, Any], str]:
        status, body, headers = self.request(USERS_BASE + "v1/users/authenticated", method="GET")
        if status == 200:
            try:
                data = json.loads(body)
            except Exception:
                data = {}
            return True, data, "ok"
        challenge_detail = captcha_detail(status, body, headers)
        if challenge_detail:
            return False, {}, challenge_detail
        return False, {}, f"cookie validation failed ({status}) {body[:180]}"

    def csrf_post(self, url: str, data: Optional[Any] = None, method: str = "POST") -> Tuple[bool, Dict[str, Any], str, Dict[str, str]]:
        if not self.csrf_token:
            ok, detail = self.get_csrf()
            if not ok:
                return False, {}, detail, {}
        status, body, headers = self.request(url, method=method, data=data, retry_csrf=True)
        parsed: Dict[str, Any] = {}
        try:
            parsed = json.loads(body) if body else {}
        except Exception:
            parsed = {"raw": body}
        if 200 <= status < 300:
            return True, parsed, "ok", headers
        challenge_detail = captcha_detail(status, body, headers)
        if challenge_detail:
            return False, parsed, challenge_detail, headers
        detail = ""
        try:
            detail = parsed.get("errors", [{}])[0].get("message", "")
        except Exception:
            detail = ""
        return False, parsed, detail or f"HTTP {status}: {body[:220]}", headers


def ensure_owned_private_server(
    client: RobloxHTTP,
    username: str,
    owner_user_id: str,
    place_id: str,
    name_template: str = "",
    free_only: bool = True,
    known_servers: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return _ensure_owned_private_server(
        client,
        username,
        owner_user_id,
        place_id,
        name_template=name_template,
        free_only=free_only,
        known_servers=known_servers,
        universe_lookup=universe_id_for_place,
        list_my_private_servers_fn=list_my_private_servers,
        list_private_servers_for_place_fn=list_private_servers_for_place,
        fetch_private_server_metadata_fn=fetch_private_server_metadata,
        private_servers_enabled_for_universe_fn=private_servers_enabled_for_universe,
        game_name_for_universe_fn=game_name_for_universe,
    )


def _merge_owned_private_server(records: List[Dict[str, Any]], server: Dict[str, Any]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    incoming_id = str(server.get("private_server_id") or "").strip()
    incoming_key = (
        str(server.get("owner_user_id") or ""),
        str(server.get("place_id") or ""),
        str(server.get("universe_id") or ""),
    )
    replaced = False
    for old in records or []:
        old_id = str(old.get("private_server_id") or "").strip()
        old_key = (
            str(old.get("owner_user_id") or ""),
            str(old.get("place_id") or ""),
            str(old.get("universe_id") or ""),
        )
        if (incoming_id and old_id == incoming_id) or (incoming_key[0] and old_key == incoming_key):
            combined = dict(old)
            combined.update(dict(server))
            if str(server.get("status") or "").lower() == "error":
                for key in ("private_server_id", "link", "join_code", "access_code", "source", "name"):
                    if combined.get(key) in (None, "") and old.get(key) not in (None, ""):
                        combined[key] = old.get(key)
            merged.append(combined)
            replaced = True
        else:
            merged.append(dict(old))
    if not replaced:
        merged.append(dict(server))
    return merged


def validate_cookie_details(cookie: str) -> Tuple[bool, str, str, Dict[str, Any]]:
    ok, data, detail = RobloxHTTP(cookie).authenticated_user()
    if ok:
        username = str(data.get("name") or data.get("displayName") or "")
        return True, username, "ok", {"username": username, "user_id": str(data.get("id") or "")}
    return False, "", detail, {}


def validate_record_cookie_identity(record: Dict[str, Any], cookie: str, update_store: bool = True) -> Dict[str, Any]:
    username = str((record or {}).get("username") or "").strip()
    ok, cookie_username, detail, meta = validate_cookie_details(cookie)
    if not ok:
        if update_store and username:
            if is_captcha_text(detail):
                ACCOUNT_STORE.update_record(
                    username,
                    {"manual_status": CAPTCHA_BLOCK_REASON, "import_status": CAPTCHA_REASON},
                )
            else:
                ACCOUNT_STORE.update_record(username, {"cookie_mismatch": True, "import_status": "cookie_invalid"})
        if is_captcha_text(detail):
            return {
                "ok": False,
                "fatal": True,
                "captcha_required": True,
                "msg": CAPTCHA_BLOCK_REASON,
                "detail": detail,
                "cookie_username": "",
                "cookie_user_id": "",
            }
        return {"ok": False, "msg": detail, "cookie_username": "", "cookie_user_id": ""}
    cookie_user_id = str(meta.get("user_id") or "")
    mismatch = bool(username and cookie_username and username.lower() != cookie_username.lower())
    updates = {
        "cookie_username": cookie_username,
        "cookie_user_id": cookie_user_id,
        "cookie_mismatch": mismatch,
        "import_status": "cookie_mismatch" if mismatch else "",
    }
    if update_store and username:
        ACCOUNT_STORE.update_record(username, updates)
    if mismatch:
        return {
            "ok": False,
            "msg": f"Cookie belongs to {cookie_username}, not {username}. Reimport the correct .ROBLOSECURITY for this account.",
            "cookie_username": cookie_username,
            "cookie_user_id": cookie_user_id,
            "cookie_mismatch": True,
        }
    return {"ok": True, "msg": "ok", "cookie_username": cookie_username, "cookie_user_id": cookie_user_id, "cookie_mismatch": False}


def _multi_roblox_log(event: str, severity: str = "info", **fields: Any) -> None:
    try:
        from core import flog_kv

        flog_kv("MULTI_ROBLOX", event, severity, **fields)
    except Exception:
        pass


def _read_guard_ready_line(proc: subprocess.Popen, timeout: float) -> Tuple[str, str]:
    box: Dict[str, str] = {}

    def _reader() -> None:
        try:
            if proc.stdout is None:
                box["line"] = ""
                return
            box["line"] = str(proc.stdout.readline() or "").strip()
        except Exception as exc:
            box["error"] = str(exc)

    thread = threading.Thread(target=_reader, daemon=True, name="MultiRobloxGuardReadyReader")
    thread.start()
    thread.join(max(0.5, float(timeout or 5.0)))
    if thread.is_alive():
        return "", "ready timeout"
    return box.get("line", ""), box.get("error", "")


def _parse_multi_roblox_ready_line(ready_line: str) -> Tuple[str, List[str], bool, bool]:
    detail = str(ready_line or "").replace("multi_roblox_guard_ready", "", 1).strip()
    handle_part = detail.split(" pid=", 1)[0].strip()
    handle_names = [name for name in handle_part.split(",") if name]
    has_mutex = any("ROBLOX_singletonMutex" in name for name in handle_names)
    has_event = any("ROBLOX_singletonEvent" in name for name in handle_names)
    return detail, handle_names, has_mutex, has_event


def _terminate_multi_roblox_helper_locked(reason: str = "release") -> None:
    global _MULTI_ROBLOX_HELPER
    proc = _MULTI_ROBLOX_HELPER
    _MULTI_ROBLOX_HELPER = None
    if not proc:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3.0)
    except Exception as exc:
        _multi_roblox_log("guard_stop_error", "warning", pid=getattr(proc, "pid", ""), reason=reason, error=str(exc))


def record_multi_roblox_guard_failure(detail: str) -> None:
    global _MULTI_ROBLOX_STATE, _MULTI_ROBLOX_LAST_FAILURE, _MULTI_ROBLOX_DETAIL
    message = str(detail or "Multi Roblox guard failed").strip()
    with _MULTI_ROBLOX_LOCK:
        _MULTI_ROBLOX_STATE = "failed"
        _MULTI_ROBLOX_LAST_FAILURE = message
        _MULTI_ROBLOX_DETAIL = message
    _multi_roblox_log("guard_failed", "error", detail=message)


def multi_roblox_guard_status() -> Dict[str, Any]:
    global _MULTI_ROBLOX_STATE, _MULTI_ROBLOX_LAST_FAILURE, _MULTI_ROBLOX_DETAIL
    with _MULTI_ROBLOX_LOCK:
        proc = _MULTI_ROBLOX_HELPER
        pid = int(getattr(proc, "pid", 0) or 0) if proc else 0
        state = _MULTI_ROBLOX_STATE
        detail = _MULTI_ROBLOX_DETAIL
        if proc:
            rc = proc.poll()
            if rc is not None:
                if state not in {"stopped", "failed"}:
                    state = "failed"
                    detail = f"guard helper exited rc={rc}"
                    _MULTI_ROBLOX_STATE = state
                    _MULTI_ROBLOX_DETAIL = detail
                    _MULTI_ROBLOX_LAST_FAILURE = detail
                pid = 0
        return {
            "state": state,
            "pid": pid,
            "detail": detail,
            "last_failure": _MULTI_ROBLOX_LAST_FAILURE,
            "started_at": _MULTI_ROBLOX_STARTED_AT,
            "handle_names": list(_MULTI_ROBLOX_HANDLE_NAMES),
        }


def ensure_multi_roblox_guard(timeout: float = 6.0) -> Tuple[bool, str]:
    global _MULTI_ROBLOX_HELPER, _MULTI_ROBLOX_STATE, _MULTI_ROBLOX_DETAIL
    global _MULTI_ROBLOX_LAST_FAILURE, _MULTI_ROBLOX_STARTED_AT, _MULTI_ROBLOX_HANDLE_NAMES
    with _MULTI_ROBLOX_LOCK:
        if _MULTI_ROBLOX_HELPER and _MULTI_ROBLOX_HELPER.poll() is None and _MULTI_ROBLOX_STATE == "ready":
            return True, _MULTI_ROBLOX_DETAIL
        if _MULTI_ROBLOX_HELPER and _MULTI_ROBLOX_HELPER.poll() is None:
            _terminate_multi_roblox_helper_locked("restart_not_ready")
        elif _MULTI_ROBLOX_HELPER and _MULTI_ROBLOX_HELPER.poll() is not None:
            rc = _MULTI_ROBLOX_HELPER.poll()
            _MULTI_ROBLOX_LAST_FAILURE = f"guard helper exited rc={rc}"
            _MULTI_ROBLOX_HELPER = None

        if IS_COMPILED:
            guard_path = EXECUTABLE_PATH or sys.executable or "CronusLauncher.exe"
            cmd = [guard_path, "--multi-roblox-guard", _MULTI_ROBLOX_GUARD_MODE, "--parent-pid", str(os.getpid())]
        else:
            guard_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "multi_roblox_guard.py")
            cmd = [
                sys.executable or "python",
                guard_path,
                _MULTI_ROBLOX_GUARD_MODE,
                "--parent-pid",
                str(os.getpid()),
            ]
        try:
            _MULTI_ROBLOX_STATE = "starting"
            _MULTI_ROBLOX_DETAIL = "starting guard helper"
            _MULTI_ROBLOX_HANDLE_NAMES = []
            _MULTI_ROBLOX_HELPER = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            _MULTI_ROBLOX_STARTED_AT = time.time()
            _multi_roblox_log("guard_started", pid=_MULTI_ROBLOX_HELPER.pid, cmd=os.path.basename(guard_path))
            ready_line, read_error = _read_guard_ready_line(_MULTI_ROBLOX_HELPER, timeout)
            if read_error:
                raise RuntimeError(read_error)
            if _MULTI_ROBLOX_HELPER.poll() is not None:
                raise RuntimeError(f"guard helper exited before ready rc={_MULTI_ROBLOX_HELPER.poll()} {ready_line}".strip())
            if not ready_line.startswith("multi_roblox_guard_ready"):
                raise RuntimeError(ready_line or "guard helper did not report ready")
            detail, handle_names, has_mutex, has_event = _parse_multi_roblox_ready_line(ready_line)
            if not has_mutex:
                raise RuntimeError(f"guard helper missing Roblox singleton mutex: {ready_line}")
            if has_event:
                detail = f"{detail} unexpected_event_handle=present".strip()
                _multi_roblox_log(
                    "guard_event_handle_present",
                    "warning",
                    pid=_MULTI_ROBLOX_HELPER.pid,
                    detail=detail,
                )
            _MULTI_ROBLOX_HANDLE_NAMES = handle_names
            _MULTI_ROBLOX_STATE = "ready"
            _MULTI_ROBLOX_DETAIL = detail
            _MULTI_ROBLOX_LAST_FAILURE = ""
            _multi_roblox_log("guard_ready", pid=_MULTI_ROBLOX_HELPER.pid, detail=detail)
            return True, detail
        except Exception as exc:
            failure = str(exc)
            _MULTI_ROBLOX_LAST_FAILURE = failure
            _MULTI_ROBLOX_DETAIL = failure
            _MULTI_ROBLOX_STATE = "failed"
            _terminate_multi_roblox_helper_locked("startup_failed")
            _multi_roblox_log("guard_failed", "error", detail=failure)
            return False, failure


def release_multi_roblox_guard() -> None:
    global _MULTI_ROBLOX_HANDLES, _MULTI_ROBLOX_STATE, _MULTI_ROBLOX_DETAIL, _MULTI_ROBLOX_HANDLE_NAMES
    with _MULTI_ROBLOX_LOCK:
        helper_pid = int(getattr(_MULTI_ROBLOX_HELPER, "pid", 0) or 0) if _MULTI_ROBLOX_HELPER else 0
        _terminate_multi_roblox_helper_locked("release")
        try:
            if _MULTI_ROBLOX_HANDLES:
                from multi_roblox_guard import close_handles

                close_handles(_MULTI_ROBLOX_HANDLES)
        finally:
            _MULTI_ROBLOX_HANDLES = []
            _MULTI_ROBLOX_HANDLE_NAMES = []
            _MULTI_ROBLOX_STATE = "stopped"
            _MULTI_ROBLOX_DETAIL = ""
        if helper_pid:
            _multi_roblox_log("guard_stopped", pid=helper_pid)


atexit.register(release_multi_roblox_guard)


class HybridLauncher:
    @staticmethod
    def _tracker_from_cmdline(cmdline: str) -> str:
        text = str(cmdline or "")
        for pattern in (
            r"browsertrackerid[:=](\d+)",
            r"browserTrackerId=(\d+)",
            r"browsertrackerid%3a(\d+)",
            r"browserTrackerId%3D(\d+)",
            r"\-b\s+(\d+)",
        ):
            match = re.search(pattern, text, flags=re.I)
            if match:
                return match.group(1)
        return ""

    @classmethod
    def duplicate_pids_for_tracker(cls, browser_tracker_id: str) -> List[int]:
        browser_tracker_id = str(browser_tracker_id or "").strip()
        if not browser_tracker_id:
            return []
        try:
            from services.process_service import ProcessManager
        except Exception:
            return []
        pids: List[int] = []
        for item in ProcessManager.list_live_game_processes():
            if cls._tracker_from_cmdline(str(item.get("cmdline") or "")) == browser_tracker_id:
                pid = int(item.get("pid") or 0)
                if pid:
                    pids.append(pid)
        return pids

    @classmethod
    def kill_duplicate_instances(cls, browser_tracker_id: str, graceful: bool = True) -> Dict[str, Any]:
        try:
            from services.process_service import ProcessManager
        except Exception as exc:
            return {"ok": False, "killed": [], "msg": str(exc)}
        killed: List[int] = []
        for pid in cls.duplicate_pids_for_tracker(browser_tracker_id):
            try:
                if ProcessManager.kill_pid(pid):
                    killed.append(pid)
            except Exception:
                continue
        return {"ok": True, "killed": killed, "count": len(killed)}

    @classmethod
    def launch_record(cls, record: Dict[str, Any], target: Optional[Dict[str, Any]] = None, multi_roblox: bool = True) -> Dict[str, Any]:
        data = dict(record or {})
        target = dict(target or {})
        username = str(data.get("username") or "").strip()
        if username:
            try:
                for stored in ACCOUNT_STORE.read_records(include_cookies=False):
                    if str(stored.get("username") or "").strip().lower() != username.lower():
                        continue
                    for key in ("owned_private_servers", "place_id", "job_id", "browser_tracker_id", "global_vip_link"):
                        if not data.get(key) and stored.get(key):
                            data[key] = stored.get(key)
                    stored_links = stored.get("vip_links") if isinstance(stored.get("vip_links"), list) else []
                    data_links = data.get("vip_links") if isinstance(data.get("vip_links"), list) else []
                    merged_links = []
                    for link in list(data_links or []) + list(stored_links or []):
                        text = str(link or "").strip()
                        if text and text not in merged_links:
                            merged_links.append(text)
                    if merged_links:
                        data["vip_links"] = merged_links
                    break
            except Exception:
                pass
        cookie = data.get("cookie") or ""
        if not cookie and data.get("encrypted_cookie"):
            cookie = decrypt_cookie(str(data.get("encrypted_cookie") or ""))
        if not cookie:
            return {"ok": False, "msg": "No .ROBLOSECURITY cookie for account"}
        identity = validate_record_cookie_identity(data, cookie, update_store=True)
        if not identity.get("ok"):
            return {"ok": False, "fatal": True, "msg": identity.get("msg", "cookie identity mismatch"), **identity}
        browser_tracker_id = str(target.get("browser_tracker_id") or data.get("browser_tracker_id") or "").strip()
        if not browser_tracker_id:
            browser_tracker_id = str(secrets.randbelow(75_000) + 100_000) + str(secrets.randbelow(800_000) + 100_000)
        guard_ok = False
        guard_detail = ""
        if multi_roblox:
            guard_ok, guard_detail = ensure_multi_roblox_guard()
            if not guard_ok:
                return {"ok": False, "fatal": True, "msg": f"Multi Roblox guard failed: {guard_detail}"}
            close_result = cls.kill_duplicate_instances(browser_tracker_id, graceful=True)
        else:
            release_multi_roblox_guard()
            from process_net import ProcessManager

            killed_count = ProcessManager.kill_all_roblox_clients(wait_seconds=2.5)
            close_result = {"ok": True, "killed": [], "count": int(killed_count), "all_instances_closed": int(killed_count)}
        place_id = str(target.get("place_id") or data.get("place_id") or "").strip()
        job_id = str(target.get("job_id") or data.get("job_id") or "").strip()
        explicit_vip_link = str(target.get("vip_link") or target.get("vip_url") or "").strip()
        links = target.get("vip_links") or data.get("vip_links") or []
        if isinstance(links, str):
            links = [line.strip() for line in links.splitlines() if line.strip()]
        if not isinstance(links, list):
            links = []
        global_vip_link = str(target.get("global_vip_link") or data.get("global_vip_link") or "").strip()
        vip_link = explicit_vip_link
        vip_resolved = False
        vip_resolution: Dict[str, Any] = {}
        if job_id.startswith("http") and not vip_link:
            vip_link = job_id
            job_id = ""
        client = RobloxHTTP(cookie)
        private_server_meta: Dict[str, Any] = {}
        auto_private_enabled = bool(
            target.get(
                "auto_create_private_server_enabled",
                data.get("auto_create_private_server_enabled", False),
            )
        )
        if auto_private_enabled:
            link_candidates = [str(vip_link or "").strip()] + [str(link or "").strip() for link in links] + [global_vip_link]
            for candidate in link_candidates:
                if not place_id:
                    parsed_place, _link_code = parse_vip_link(candidate)
                    if parsed_place:
                        place_id = parsed_place
                        break
            if not place_id:
                return {
                    "ok": False,
                    "fatal": True,
                    "mode": "vip",
                    "vip_resolved": False,
                    "msg": "Auto Create Private Server requires Place ID or a VIP link with Place ID",
                }
            free_only = bool(target.get("auto_create_private_server_free_only", data.get("auto_create_private_server_free_only", True)))
            known_private_servers = list(data.get("owned_private_servers") or [])
            for candidate_link in link_candidates:
                components = parse_vip_components(candidate_link)
                candidate_place = str(components.get("place_id") or "").strip()
                candidate_link_code = str(components.get("link_code") or "").strip()
                candidate_access_code = str(components.get("access_code") or "").strip()
                if candidate_place and candidate_place != str(place_id):
                    continue
                if not (candidate_link_code or candidate_access_code):
                    continue
                known_private_servers.append(
                    {
                        "owner_user_id": str(identity.get("cookie_user_id") or data.get("cookie_user_id") or ""),
                        "place_id": str(place_id),
                        "link": candidate_link,
                        "join_code": candidate_link_code,
                        "access_code": candidate_access_code,
                    }
                )
            private_result = ensure_owned_private_server(
                client,
                username=username or str(identity.get("cookie_username") or ""),
                owner_user_id=str(identity.get("cookie_user_id") or data.get("cookie_user_id") or ""),
                place_id=place_id,
                free_only=free_only,
                known_servers=known_private_servers,
            )
            if not private_result.get("ok"):
                ACCOUNT_STORE.update_record(
                    username,
                    {
                        "owned_private_servers": _merge_owned_private_server(
                            list(data.get("owned_private_servers") or []),
                            {
                                "owner_user_id": str(identity.get("cookie_user_id") or data.get("cookie_user_id") or ""),
                                "place_id": place_id,
                                "universe_id": str(private_result.get("universe_id") or ""),
                                "status": "error",
                                "error": str(private_result.get("msg") or "private server creation failed"),
                                "synced_at": time.time(),
                            },
                        )
                    },
                )
                return {
                    "ok": False,
                    "fatal": True,
                    "mode": "vip",
                    "vip_resolved": False,
                    "msg": str(private_result.get("msg") or "Private server setup failed"),
                    "auto_private_server": True,
                }
            private_server_meta = dict(private_result)
            vip_link = str(private_server_meta.get("link") or "").strip()
            vip_resolution = {
                "ok": True,
                "place_id": str(private_server_meta.get("place_id") or place_id),
                "access_code": str(private_server_meta.get("access_code") or ""),
                "link_code": str(private_server_meta.get("join_code") or private_server_meta.get("access_code") or ""),
                "source": f"owned_private_server:{private_server_meta.get('source') or 'unknown'}",
            }
            if not vip_link:
                vip_link = build_owned_private_server_link(place_id, private_server_meta)
            if not vip_resolution.get("access_code") and vip_link:
                vip_resolution = resolve_vip_access_code(cookie, vip_link)
                if not vip_resolution.get("ok"):
                    return {
                        "ok": False,
                        "fatal": True,
                        "msg": str(vip_resolution.get("msg") or "Owned private server invite resolve failed"),
                        "mode": "vip",
                        "vip_resolved": False,
                        "auto_private_server": True,
                    }
            vip_resolved = True
            place_id = place_id or str(vip_resolution.get("place_id") or "")
            owned_servers = _merge_owned_private_server(list(data.get("owned_private_servers") or []), private_server_meta)
            updated_vip_links = list(data.get("vip_links") or [])
            if vip_link:
                components = parse_vip_components(vip_link)
                if (components.get("link_code") or components.get("access_code")) and vip_link not in updated_vip_links:
                    updated_vip_links.insert(0, vip_link)
            ACCOUNT_STORE.update_record(
                username,
                {
                    "owned_private_servers": owned_servers,
                    "vip_links": updated_vip_links,
                    "place_id": place_id or data.get("place_id", ""),
                },
            )
        elif not vip_link:
            if isinstance(links, list) and links:
                vip_link = str(links[0] or "").strip()
            if not vip_link:
                vip_link = global_vip_link
        if vip_link:
            if not vip_resolution.get("ok"):
                vip_resolution = resolve_vip_access_code(cookie, vip_link)
                if not vip_resolution.get("ok"):
                    return {"ok": False, "fatal": True, "msg": str(vip_resolution.get("msg") or "VIP invite resolve failed"), "mode": "vip", "vip_resolved": False}
                vip_resolved = True
            place_id = place_id or str(vip_resolution.get("place_id") or "")
        follow_user_id = str(target.get("follow_user_id") or target.get("follow_user") or "").strip()
        if not place_id and not vip_link and not job_id and not follow_user_id:
            return {"ok": False, "msg": "PlaceId, JobId, VIP link, or follow user is required"}
        ok, ticket = client.get_auth_ticket()
        if not ok:
            return {"ok": False, "msg": ticket}
        launcher_url, mode, attempted_vip = build_place_launcher_url(
            place_id=place_id,
            job_id=job_id,
            vip_link=vip_link,
            follow_user_id=follow_user_id,
            browser_tracker_id=browser_tracker_id,
            vip_access_code=str(vip_resolution.get("access_code") or ""),
            vip_link_code=str(vip_resolution.get("link_code") or ""),
        )
        uri = build_roblox_player_uri(ticket, launcher_url, browser_tracker_id)
        try:
            os.startfile(uri)  # type: ignore[attr-defined]
        except Exception:
            subprocess.Popen(
                f'start "" "{uri}"',
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        ACCOUNT_STORE.update_record(
            str(data.get("username") or ""),
            {
                "browser_tracker_id": browser_tracker_id,
                "last_use": time.time(),
                "place_id": place_id or data.get("place_id", ""),
                "job_id": job_id or data.get("job_id", ""),
            },
        )
        return {
            "ok": True,
            "mode": mode,
            "browser_tracker_id": browser_tracker_id,
            "closed_duplicates": close_result.get("killed", []),
            "closed_instances": close_result.get("count", 0),
            "multi_roblox_guard": {"ok": guard_ok, "detail": guard_detail} if multi_roblox else {"ok": False, "detail": "disabled"},
            "cookie_username": identity.get("cookie_username", ""),
            "cookie_user_id": identity.get("cookie_user_id", ""),
            "vip_resolved": vip_resolved,
            "vip_access_code_hash": _safe_hash(str(vip_resolution.get("access_code") or "")),
            "vip_link_code_hash": _safe_hash(str(vip_resolution.get("link_code") or "")),
            "launch_uri_preview": re.sub(r"(gameinfo:)[^+]+", r"\1[REDACTED]", uri),
            "attempted_vip": attempted_vip,
            "auto_private_server": bool(auto_private_enabled),
            "owned_private_server_id": str(private_server_meta.get("private_server_id") or ""),
            "private_server_source": str(private_server_meta.get("source") or ""),
            "private_server_owner_user_id": str(private_server_meta.get("owner_user_id") or ""),
            "private_server_place_id": str(private_server_meta.get("place_id") or ""),
            "private_server_universe_id": str(private_server_meta.get("universe_id") or ""),
            "msg": "Roblox launch requested",
        }


def validate_cookie(cookie: str) -> Tuple[bool, str, str]:
    ok, username, detail, _meta = validate_cookie_details(cookie)
    return ok, username, detail
