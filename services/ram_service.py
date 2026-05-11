from __future__ import annotations

import json
import os
import random
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from core import Account, flog


def _parse_vip_link(vip_url: str) -> Tuple[str, str]:
    from process_net import ProcessManager

    return ProcessManager.parse_vip_link(vip_url)

class RAMManager:
    _accounts_cache_lock = threading.Lock()
    _accounts_cache: Tuple[float, List[dict], bool] = (0.0, [], False)
    _accounts_cache_ttl = 3.0

    @staticmethod
    def _base_url(cfg: dict) -> str:
        host = str(cfg.get("ram_host", "localhost")).strip() or "localhost"
        if host in {"127.0.0.1", "::1", "0.0.0.0"}:
            host = "localhost"
        port = int(cfg.get("ram_port", 7963) or 7963)
        return f"http://{host}:{port}"

    @staticmethod
    def _auth_params(cfg: dict) -> Dict[str, str]:
        password = str(cfg.get("ram_password", "") or "").strip()
        return {"Password": password} if password else {}

    @classmethod
    def _request_text(
        cls,
        cfg: dict,
        endpoint: str,
        params: Optional[Dict[str, object]] = None,
        timeout: float = 5.0,
    ) -> Tuple[bool, str]:
        query: Dict[str, object] = {}
        query.update(cls._auth_params(cfg))
        if params:
            query.update({k: v for k, v in params.items() if v not in (None, "")})
        url = f"{cls._base_url(cfg)}/{endpoint}"
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        try:
            req = urllib.request.Request(url, method="GET", headers={"User-Agent": "ArgusLauncher/RT"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return True, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                body = str(e)
            return False, body or f"HTTP {e.code}"
        except Exception as e:
            return False, str(e)

    @classmethod
    def ensure_running(cls, cfg: dict, wait_seconds: float = 12.0) -> Tuple[bool, str]:
        ok, msg = cls._request_text(cfg, "GetAccounts", timeout=2.0)
        if ok:
            return True, "RAM web API reachable"

        if not cfg.get("ram_auto_launch", True):
            return False, f"RAM API unavailable: {msg}"

        exe_path = str(cfg.get("ram_path", "") or "").strip()
        if not exe_path or not os.path.exists(exe_path):
            return False, f"RAM executable not found: {exe_path or '<empty>'}"

        try:
            subprocess.Popen([exe_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            return False, f"Failed to launch RAM: {e}"

        deadline = time.time() + max(1.0, wait_seconds)
        last_msg = msg
        while time.time() < deadline:
            time.sleep(1.0)
            ok, last_msg = cls._request_text(cfg, "GetAccounts", timeout=2.0)
            if ok:
                return True, "RAM launched and API reachable"
        return False, f"RAM launched but API did not come online: {last_msg}"

    @classmethod
    def get_accounts(
        cls,
        cfg: dict,
        include_cookies: bool = True,
        force_refresh: bool = False,
    ) -> Tuple[bool, object]:
        if not force_refresh:
            with cls._accounts_cache_lock:
                ts, cached_accounts, cached_include_cookies = cls._accounts_cache
                if (
                    cached_accounts
                    and (time.time() - ts) <= cls._accounts_cache_ttl
                    and (include_cookies or not cached_include_cookies)
                ):
                    return True, list(cached_accounts)

        ready, detail = cls.ensure_running(cfg)
        if not ready:
            return False, detail

        ok, body = cls._request_text(
            cfg,
            "GetAccountsJson",
            params={"IncludeCookies": "true" if include_cookies else "false"},
            timeout=8.0,
        )
        if ok:
            try:
                data = json.loads(body)
                if isinstance(data, list):
                    with cls._accounts_cache_lock:
                        cls._accounts_cache = (time.time(), list(data), include_cookies)
                    return True, data
                if isinstance(data, dict):
                    for key in ("accounts", "Accounts", "data"):
                        value = data.get(key)
                        if isinstance(value, list):
                            with cls._accounts_cache_lock:
                                cls._accounts_cache = (time.time(), list(value), include_cookies)
                            return True, value
            except Exception as e:
                flog(f"[RAM] GetAccountsJson parse error: {e}", "warning")

        ok, body = cls._request_text(cfg, "GetAccounts", timeout=5.0)
        if not ok:
            return False, body
        accounts = [item.strip() for item in body.split(",") if item.strip()]
        payload = [{"Username": username} for username in accounts]
        with cls._accounts_cache_lock:
            cls._accounts_cache = (time.time(), list(payload), False)
        return True, payload

    @staticmethod
    def _norm_name(value: object) -> str:
        return str(value or "").strip().lower()

    @staticmethod
    def _coerce_bool(value: object) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "y", "online", "running", "ingame", "in_game", "connected"}:
            return True
        if text in {"0", "false", "no", "n", "offline", "stopped", "disconnected"}:
            return False
        return None

    @classmethod
    def find_account_record(
        cls,
        acc: Account,
        cfg: dict,
        force_refresh: bool = False,
    ) -> Tuple[Optional[dict], str]:
        ok, payload = cls.get_accounts(cfg, include_cookies=False, force_refresh=force_refresh)
        if not ok:
            return None, str(payload)

        wanted = {
            cls._norm_name(acc.username),
            cls._norm_name(acc.display_name),
            cls._norm_name(acc.alias),
        }
        wanted.discard("")

        for item in payload:
            if not isinstance(item, dict):
                continue
            names = {
                cls._norm_name(item.get("Username")),
                cls._norm_name(item.get("username")),
                cls._norm_name(item.get("Alias")),
                cls._norm_name(item.get("alias")),
                cls._norm_name(item.get("Account")),
            }
            names.discard("")
            if wanted & names:
                return item, "matched GetAccountsJson"
        return None, "account not found in RAM"

    @classmethod
    def get_cookie(cls, username: str, cfg: dict) -> Tuple[bool, str]:
        ready, detail = cls.ensure_running(cfg)
        if not ready:
            return False, detail

        ok, body = cls._request_text(
            cfg,
            "GetCookie",
            params={"Account": username},
            timeout=8.0,
        )
        if not ok:
            return False, body

        text = str(body or "").strip()
        if not text:
            return False, "empty cookie response"
        return True, text

    @classmethod
    def sync_account_profile(cls, acc: Account, cfg: dict) -> Tuple[bool, str]:
        record, detail = cls.find_account_record(acc, cfg, force_refresh=True)
        if not record:
            return False, detail

        fields = record.get("Fields")
        if not isinstance(fields, dict):
            fields = {}

        saved_place = str(
            record.get("PlaceId")
            or record.get("placeId")
            or fields.get("SavedPlaceId")
            or acc.place_id
            or ""
        ).strip()
        saved_job = str(
            record.get("JobId")
            or record.get("jobId")
            or fields.get("SavedJobId")
            or ""
        ).strip()

        changed: List[str] = []
        fresh_cookie = ""
        ok_cookie = False
        cookie_or_msg = ""
        if acc.username:
            ok_cookie, cookie_or_msg = cls.get_cookie(acc.username, cfg)
            if ok_cookie:
                fresh_cookie = str(cookie_or_msg or "").strip()

        with acc._lock:
            if saved_place and saved_place != acc.place_id:
                acc.place_id = saved_place
                changed.append("place_id")

            if saved_job.startswith("http") and saved_job not in acc.vip_links:
                acc.vip_links = [saved_job] + [link for link in acc.vip_links if link != saved_job]
                changed.append("vip_links")

            if fresh_cookie and fresh_cookie != acc.cookie:
                acc.cookie = fresh_cookie
                changed.append("cookie")
            elif not acc.cookie and ok_cookie and cookie_or_msg:
                acc.cookie = str(cookie_or_msg).strip()
                changed.append("cookie")

        if changed:
            return True, f"synced from RAM: {', '.join(changed)}"
        if not ok_cookie:
            return True, f"RAM profile already in sync (cookie unavailable: {cookie_or_msg})"
        return True, "RAM profile already in sync"

    @classmethod
    def resolve_account_online(
        cls,
        acc: Account,
        cfg: dict,
        force_refresh: bool = False,
    ) -> Tuple[Optional[bool], str, Optional[dict]]:
        record, detail = cls.find_account_record(acc, cfg, force_refresh=force_refresh)
        if not record:
            return None, detail, None

        verdict, online_detail = cls.resolve_record_online(record)
        return verdict, online_detail, record

    @classmethod
    def resolve_record_online(cls, record: dict) -> Tuple[Optional[bool], str]:
        if not isinstance(record, dict):
            return None, "invalid RAM record"

        fields = record.get("Fields")
        if not isinstance(fields, dict):
            fields = {}

        def pick(*keys: str) -> Any:
            for key in keys:
                if key in record:
                    return record.get(key)
            for key in keys:
                if key in fields:
                    return fields.get(key)
            return None

        for keys, label in (
            (("IsRunning", "Running", "running"), "running"),
            (("IsOnline", "Online", "online"), "online"),
            (("InGame", "IsInGame", "inGame", "ingame"), "in_game"),
            (("Connected", "IsConnected", "connected"), "connected"),
        ):
            verdict = cls._coerce_bool(pick(*keys))
            if verdict is not None:
                return verdict, f"RAM {label}={verdict}"

        presence = pick("PresenceType", "presenceType", "Presence", "presence")
        try:
            if presence is not None and str(presence).strip() != "":
                presence_num = int(str(presence).strip())
                return presence_num > 0, f"RAM presence={presence_num}"
        except Exception:
            pass

        tracker = pick("BrowserTrackerId", "BrowserTrackerID", "browserTrackerId", "browserTrackerID")
        try:
            if tracker is not None and str(tracker).strip() != "":
                tracker_num = int(str(tracker).strip())
                if tracker_num > 0:
                    return True, f"RAM browserTrackerId={tracker_num}"
        except Exception:
            pass

        for key in ("JobId", "jobId", "GameId", "gameId", "CurrentGameId", "PlaceId", "LastPlaceId"):
            value = pick(key)
            if str(value or "").strip():
                return True, f"RAM {key} present"

        return None, "RAM record found but no online hint"

    @classmethod
    def launch_account(cls, acc: Account, cfg: dict) -> Tuple[bool, str]:
        ready, detail = cls.ensure_running(cfg)
        if not ready:
            return False, detail

        place_target = (acc.active_vip or "").strip()
        if not place_target:
            if acc.vip_links and getattr(acc, "_vip_tracker", None):
                place_target = str(acc._vip_tracker.pick() or "").strip()
            elif acc.vip_links:
                place_target = str(random.choice(acc.vip_links)).strip()
        if not place_target:
            place_target = str(acc.place_id or "").strip()
        if not place_target:
            return False, "No PlaceId or VIP link configured"

        variants: List[Tuple[Dict[str, object], str]] = []
        if place_target.startswith("http"):
            place_id, link_code = _parse_vip_link(place_target)
            if not place_id:
                place_id = str(acc.place_id or "").strip()
            if not place_id:
                return False, "VIP link missing PlaceId and account has no fallback PlaceId"
            base = {"Account": acc.username, "PlaceId": place_id}
            if link_code:
                variants.append(
                    (
                        {**base, "JobId": link_code, "JoinVip": "true"},
                        f"PlaceId={place_id} JobId=<linkCode> JoinVip=true",
                    )
                )
                variants.append(
                    (
                        {**base, "JobId": link_code},
                        f"PlaceId={place_id} JobId=<linkCode>",
                    )
                )
                variants.append(
                    (
                        {**base, "JobId": place_target, "JoinVip": "true"},
                        f"PlaceId={place_id} JobId=<shareUrl> JoinVip=true",
                    )
                )
            variants.append((base, f"PlaceId={place_id}"))
        else:
            variants.append(({"Account": acc.username, "PlaceId": place_target}, f"PlaceId={place_target}"))

        last_error = "RAM launch failed"
        for params, launch_summary in variants:
            ok, body = cls._request_text(cfg, "LaunchAccount", params=params, timeout=10.0)
            if ok:
                acc.active_vip = place_target if place_target.startswith("http") else ""
                with cls._accounts_cache_lock:
                    cls._accounts_cache = (0.0, [], False)
                return True, body.strip() or f"Launched via RAM ({launch_summary})"
            last_error = (body.strip() if isinstance(body, str) else str(body)).strip() or last_error
            flog(f"[RAM] LaunchAccount variant failed for {acc.display_name}: {launch_summary} -> {last_error}", "warning")

        return False, last_error


# ─────────────────────────────────────────────────────────────────────────────
#  ISOLATION MANAGER  (Cookie fix)
# ─────────────────────────────────────────────────────────────────────────────

__all__ = ["RAMManager"]
