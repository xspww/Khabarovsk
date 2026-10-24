"""Browser-like HTTP client for Roblox endpoints + version-pinned launch URIs."""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from typing import Any

from infra import net_tls
from services.captcha_guard import captcha_detail
from services.roblox_install_manager import ROBLOX_EXE, _roblox_roots

USER_AGENT = "CronusLauncherHybrid/1.0"
# Public (no-cookie) Roblox lookups get empty/filtered responses with a
# non-browser UA, so these use browser-like headers instead.
PUBLIC_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Referer": "https://www.roblox.com/",
}


ROBLOX_HOME = "https://www.roblox.com/"
AUTH_BASE = "https://auth.roblox.com/"
USERS_BASE = "https://users.roblox.com/"
USERMODERATION_BASE = "https://usermoderation.roblox.com/"
# The not-approved endpoint rejects non-browser callers, so it needs a full UA.
MODERATION_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Referer": "https://www.roblox.com/",
}

class _SelectedRobloxVersion:
    def __init__(self) -> None:
        self.value = ""

    def set(self, version: str = "") -> None:
        self.value = str(version or "").strip()

    def get(self) -> str:
        return self.value


_SELECTED_ROBLOX_VERSION = _SelectedRobloxVersion()


def set_selected_roblox_version(version: str = "") -> None:
    """Pin launches to a specific installed official Roblox version ('' = OS default)."""
    _SELECTED_ROBLOX_VERSION.set(version)


def _roblox_version_exe(version: str) -> str:
    name = str(version or "").strip()
    if not name:
        return ""
    target_l = name.lower()
    for root in _roblox_roots():
        versions_dir = os.path.join(str(root), "Versions")
        if not os.path.isdir(versions_dir):
            continue
        try:
            entries = os.listdir(versions_dir)
        except OSError:
            continue
        for entry in entries:
            if entry.lower() != target_l:
                continue
            exe = os.path.join(versions_dir, entry, ROBLOX_EXE)
            if os.path.isfile(exe):
                return exe
    return ""


def open_roblox_uri(uri: str) -> None:
    """Open a Roblox URI, pinned to the selected version when one is set."""
    exe = _roblox_version_exe(_SELECTED_ROBLOX_VERSION.get())
    if exe:
        try:
            subprocess.Popen([exe, uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except Exception:
            "intentional: ignore exception"
    try:
        os.startfile(uri)
    except Exception:
        subprocess.Popen(
            f'start "" "{uri}"',
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


def _encode_body(data: Any, headers: dict[str, str]) -> bytes | None:
    """Encode a request body and set the matching Content-Type when needed."""
    if data is None:
        return None
    if isinstance(data, (dict, list)):
        headers.setdefault("Content-Type", "application/json")
        return json.dumps(data).encode("utf-8")
    if isinstance(data, bytes):
        return data
    headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    return str(data).encode("utf-8")


def _csrf_header(headers: dict[str, str]) -> str:
    return headers.get("x-csrf-token") or headers.get("X-CSRF-TOKEN") or ""


def _csrf_retry_allowed(token: str, retry_csrf: bool, method: str) -> bool:
    return bool(token and retry_csrf and method.upper() not in {"GET", "HEAD"})


def _error_body_text(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:
        return str(exc)


class RobloxHTTP:
    def __init__(self, cookie: str = ""):
        self.cookie = str(cookie or "").strip()
        self.csrf_token = ""

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
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
        data: Any | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 12.0,
        retry_csrf: bool = True,
    ) -> tuple[int, str, dict[str, str]]:
        req_headers = self._headers(headers)
        body = _encode_body(data, req_headers)
        req = urllib.request.Request(url, data=body, method=method.upper(), headers=req_headers)
        try:
            with net_tls.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace"), dict(resp.headers.items())
        except urllib.error.HTTPError as exc:
            response_headers = dict(exc.headers.items())
            token = _csrf_header(response_headers)
            if _csrf_retry_allowed(token, retry_csrf, method):
                self.csrf_token = token
                return self.request(url, method, data, headers, timeout, retry_csrf=False)
            return exc.code, _error_body_text(exc), response_headers

    def get_csrf(self) -> tuple[bool, str]:
        status, body, headers = self.request(
            AUTH_BASE + "v1/authentication-ticket/",
            method="POST",
            headers={"Content-Type": "application/json"},
            retry_csrf=False,
        )
        token = _csrf_header(headers)
        if token:
            self.csrf_token = token
            return True, token
        challenge_detail = captcha_detail(status, body, headers)
        if challenge_detail:
            return False, challenge_detail
        return False, f"csrf failed ({status}) {body[:180]}"

    def get_auth_ticket(self) -> tuple[bool, str]:
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

    def authenticated_user(self) -> tuple[bool, dict[str, Any], str]:
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

    def csrf_post(self, url: str, data: Any | None = None, method: str = "POST") -> tuple[bool, dict[str, Any], str, dict[str, str]]:
        if not self.csrf_token:
            ok, detail = self.get_csrf()
            if not ok:
                return False, {}, detail, {}
        status, body, headers = self.request(url, method=method, data=data, retry_csrf=True)
        parsed: dict[str, Any] = {}
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
