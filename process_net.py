from __future__ import annotations

import ctypes
import base64
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import random
import math
import getpass
from ctypes import wintypes
from typing import Any, Dict, List, Optional, Tuple

from app_paths import resource_path
from services.window_control import (
    arrange_windows,
    minimize_windows,
    primary_monitor_work_area,
    resize_windows,
    restore_window_styles,
)
from runtime.popup_detector import DEFAULT_POPUP_OBSERVER, classify_texts, is_inspection_held
from runtime.runtime_state_manager import RuntimeStateManager

from core import Account, EventBus, ServerType, flog, flog_kv, account_launch_block_reason

_RUNTIME_STATE = RuntimeStateManager(logger=flog_kv)


# ─────────────────────────────────────────────────────────────────────────────
#  REALTIME RESOURCE MONITOR
# ─────────────────────────────────────────────────────────────────────────────
class RealtimeResourceMonitor:
    """
    Background thread อัปเดต CPU% และ RAM MB ของทุก Roblox PID ที่ลงทะเบียนไว้
    ทุก 1 วินาที (ไม่ block API thread)

    การใช้งาน:
      monitor = RealtimeResourceMonitor()
      monitor.start()
      monitor.register(pid)          # เพิ่ม PID ที่ต้องการติดตาม
      monitor.unregister(pid)        # ลบ PID ออก
      cpu, ram = monitor.get(pid)    # ดึงค่าล่าสุด (non-blocking)
    """
    INTERVAL = 1.0  # วินาที

    def __init__(self):
        self._lock    = threading.Lock()
        self._pids:   Dict[int, object]  = {}  # pid → psutil.Process
        self._cpu:    Dict[int, float]   = {}  # pid → cpu%
        self._ram:    Dict[int, float]   = {}  # pid → MB
        self._stop    = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._logical_cpus = max(1, int(os.cpu_count() or 1))

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="RT-ResourceMonitor"
            )
            self._thread.start()

    def stop(self):
        with self._lock:
            thread = self._thread
            if not thread:
                return
            self._stop.set()
        thread.join(timeout=self.INTERVAL + 1.0)
        with self._lock:
            if self._thread is thread:
                self._thread = None

    def register(self, pid: int):
        if pid is None:
            return
        try:
            import psutil
            with self._lock:
                if pid not in self._pids:
                    p = psutil.Process(pid)
                    p.cpu_percent(interval=None)  # prime — ค่าแรกเสมอ 0
                    self._pids[pid] = p
                    self._cpu[pid]  = 0.0
                    self._ram[pid]  = 0.0
                    flog(f"[RT_MON] register PID {pid}")
        except Exception as e:
            flog(f"[RT_MON] register error PID {pid}: {e}", "warning")

    def unregister(self, pid: int):
        if pid is None:
            return
        with self._lock:
            self._pids.pop(pid, None)
            self._cpu.pop(pid, None)
            self._ram.pop(pid, None)
        flog(f"[RT_MON] unregister PID {pid}")

    def get(self, pid: int) -> Tuple[float, float]:
        """คืน (cpu%, ram_mb) ล่าสุด"""
        with self._lock:
            return self._cpu.get(pid, 0.0), self._ram.get(pid, 0.0)

    def get_cpu(self, pid: int) -> float:
        with self._lock:
            return self._cpu.get(pid, 0.0)

    def get_ram(self, pid: int) -> float:
        with self._lock:
            return self._ram.get(pid, 0.0)

    def _run(self):
        flog("[RT_MON] started")
        while not self._stop.wait(timeout=self.INTERVAL):
            with self._lock:
                pids = dict(self._pids)

            dead = []
            for pid, proc in pids.items():
                try:
                    # Normalize by logical CPU count so the reported value tracks
                    # Windows Task Manager's per-process CPU percentage more closely.
                    cpu_raw = proc.cpu_percent(interval=None)
                    cpu = max(0.0, cpu_raw / self._logical_cpus)
                    ram = proc.memory_info().rss / (1024 * 1024)
                    with self._lock:
                        self._cpu[pid] = round(cpu, 2)
                        self._ram[pid] = round(ram, 1)
                except Exception:
                    dead.append(pid)

            for pid in dead:
                self.unregister(pid)

        flog("[RT_MON] stopped")


# Singleton
_rt_monitor = RealtimeResourceMonitor()


def get_rt_monitor() -> RealtimeResourceMonitor:
    return _rt_monitor


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
            place_id, link_code = ProcessManager.parse_vip_link(place_target)
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
class IsolationManager:
    BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "roboguard_rt1_instances")
    STORE_PACKAGE_FOLDERS = (
        "ROBLOXCORPORATION.ROBLOX_55nm5eh3cm0pr",
    )

    @staticmethod
    def _chrome_utc_now() -> int:
        # Chromium/WebView2 stores timestamps as microseconds since 1601-01-01 UTC.
        return int((time.time() + 11644473600) * 1_000_000)

    @classmethod
    def _dpapi_unprotect(cls, encrypted_data: bytes) -> bytes:
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_byte)),
            ]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32

        buffer_in = ctypes.create_string_buffer(encrypted_data, len(encrypted_data))
        blob_in = DATA_BLOB(len(encrypted_data), ctypes.cast(buffer_in, ctypes.POINTER(ctypes.c_byte)))
        blob_out = DATA_BLOB()

        if not crypt32.CryptUnprotectData(
            ctypes.byref(blob_in),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(blob_out),
        ):
            raise ctypes.WinError()

        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            if blob_out.pbData:
                kernel32.LocalFree(blob_out.pbData)

    @classmethod
    def _encrypt_webview2_cookie(cls, cookie: str, local_state_path: str) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        with open(local_state_path, "r", encoding="utf-8") as f:
            local_state = json.load(f)

        enc_key_b64 = (
            local_state.get("os_crypt", {}).get("encrypted_key", "")
            if isinstance(local_state, dict)
            else ""
        )
        if not enc_key_b64:
            raise RuntimeError("os_crypt.encrypted_key missing")

        enc_key = base64.b64decode(enc_key_b64)
        if enc_key.startswith(b"DPAPI"):
            enc_key = enc_key[5:]
        if not enc_key:
            raise RuntimeError("encrypted_key empty after DPAPI prefix strip")

        master_key = cls._dpapi_unprotect(enc_key)
        nonce = os.urandom(12)
        encrypted = AESGCM(master_key).encrypt(nonce, cookie.encode("utf-8"), None)
        return b"v10" + nonce + encrypted

    @classmethod
    def get_instance_path(cls, username: str) -> str:
        safe = re.sub(r"[^\w\-]", "_", username)
        return os.path.join(cls.BASE_DIR, f"acc_{safe}")

    @classmethod
    def setup(cls, username: str) -> str:
        path = cls.get_instance_path(username)
        roblox_local = os.path.join(path, "Roblox", "LocalStorage")
        os.makedirs(roblox_local, exist_ok=True)
        flog(f"[ISO] Instance path for {username}: {path}")
        return path

    @classmethod
    def inject_cookie(cls, username: str, cookie: str) -> Tuple[bool, str]:
        """
        RT.1.0 FIX: inject cookie หลายจุด เพื่อให้ Roblox ทุก version อ่านได้

        1. เขียนไฟล์ RobloxLocalStorage.json (วิธีเดิม)
        2. เขียน Windows Registry HKCU (วิธีใหม่ — Roblox app อ่านจากนี้จริง)
        3. เขียน %LOCALAPPDATA%\\Roblox\\LocalStorage (default path)
        """
        if not cookie:
            return False, "cookie ว่าง"

        cookie = cookie.strip()
        written = []

        # ── 1. Per-instance LocalStorage JSON ─────────────────────────────
        instance = cls.setup(username)
        json_targets = [
            os.path.join(instance, "Roblox", "LocalStorage", "RobloxLocalStorage.json"),
        ]
        for path in json_targets:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                data = {}
                if os.path.exists(path):
                    try:
                        with open(path) as f:
                            data = json.load(f)
                    except Exception:
                        pass
                data[".ROBLOSECURITY"] = cookie
                with open(path, "w") as f:
                    json.dump(data, f, indent=2)
                written.append(f"json:{path}")
            except Exception as e:
                flog(f"[ISO] json inject error: {e}", "warning")

        # ── 2. Default %LOCALAPPDATA%\Roblox\LocalStorage ─────────────────
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        if local_appdata:
            default_path = os.path.join(
                local_appdata, "Roblox", "LocalStorage", "RobloxLocalStorage.json"
            )
            try:
                os.makedirs(os.path.dirname(default_path), exist_ok=True)
                data = {}
                if os.path.exists(default_path):
                    try:
                        with open(default_path) as f:
                            data = json.load(f)
                    except Exception:
                        pass
                data[".ROBLOSECURITY"] = cookie
                with open(default_path, "w") as f:
                    json.dump(data, f, indent=2)
                written.append(f"localappdata:{default_path}")
            except Exception as e:
                flog(f"[ISO] localappdata inject error: {e}", "warning")

        # ── 2b. Microsoft Store Roblox LocalStorage ───────────────────────
        if local_appdata:
            for package_name in cls.STORE_PACKAGE_FOLDERS:
                store_path = os.path.join(
                    local_appdata,
                    "Packages",
                    package_name,
                    "LocalState",
                    "LocalStorage",
                    "RobloxLocalStorage.json",
                )
                try:
                    os.makedirs(os.path.dirname(store_path), exist_ok=True)
                    data = {}
                    if os.path.exists(store_path):
                        try:
                            with open(store_path, encoding="utf-8") as f:
                                data = json.load(f)
                        except Exception:
                            pass
                    data[".ROBLOSECURITY"] = cookie
                    with open(store_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2)
                    written.append(f"store:{store_path}")
                except Exception as e:
                    flog(f"[ISO] store localstorage inject error: {e}", "warning")

        # ── 2c. Roblox WebView2 cookie store ───────────────────────────────
        if local_appdata:
            local_state_path = os.path.join(
                local_appdata,
                "Roblox",
                "UniversalApp",
                "WebView2",
                "EBWebView",
                "Local State",
            )
            webview_cookie_db = os.path.join(
                local_appdata,
                "Roblox",
                "UniversalApp",
                "WebView2",
                "EBWebView",
                "Default",
                "Network",
                "Cookies",
            )
            try:
                os.makedirs(os.path.dirname(webview_cookie_db), exist_ok=True)
                encrypted_cookie = cls._encrypt_webview2_cookie(cookie, local_state_path)
                now_utc = cls._chrome_utc_now()
                expires_utc = now_utc + (400 * 24 * 60 * 60 * 1_000_000)
                conn = sqlite3.connect(webview_cookie_db, timeout=5)
                try:
                    cur = conn.cursor()
                    cur.execute(
                        """
                        DELETE FROM cookies
                        WHERE host_key = ? AND name = ?
                        """,
                        (".roblox.com", ".ROBLOSECURITY"),
                    )
                    cur.execute(
                        """
                        INSERT INTO cookies (
                            creation_utc, host_key, top_frame_site_key, name, value,
                            encrypted_value, path, expires_utc, is_secure, is_httponly,
                            last_access_utc, has_expires, is_persistent, priority, samesite,
                            source_scheme, source_port, last_update_utc, source_type,
                            has_cross_site_ancestor, is_edgelegacycookie, browser_provenance
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            now_utc,
                            ".roblox.com",
                            "",
                            ".ROBLOSECURITY",
                            "",
                            sqlite3.Binary(encrypted_cookie),
                            "/",
                            expires_utc,
                            1,
                            1,
                            now_utc,
                            1,
                            1,
                            1,
                            -1,
                            2,
                            443,
                            now_utc,
                            1,
                            0,
                            0,
                            0,
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()
                written.append(f"webview2:{webview_cookie_db}")
            except Exception as e:
                flog(f"[ISO] webview2 cookie inject error: {e}", "warning")

        # ── 3. Windows Registry (HKCU\Software\ROBLOX Corporation\Environments\www.roblox.com) ──
        try:
            import winreg
            key_path = r"Software\ROBLOX Corporation\Environments\www.roblox.com\Global"
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path,
                                     0, winreg.KEY_SET_VALUE)
            except FileNotFoundError:
                key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
            with key:
                winreg.SetValueEx(key, ".ROBLOSECURITY", 0, winreg.REG_SZ, cookie)
            written.append("registry:HKCU\\...\\www.roblox.com\\Global")
        except ImportError:
            flog("[ISO] winreg not available — skip registry inject", "warning")
        except Exception as e:
            flog(f"[ISO] registry inject error: {e}", "warning")

        if written:
            flog(f"[ISO] Cookie injected for {username}: {len(written)} targets")
            return True, f"Injected to {len(written)} targets"
        return False, "ไม่สามารถ inject cookie ได้"


# ─────────────────────────────────────────────────────────────────────────────
#  VIP ROTATION TRACKER
# ─────────────────────────────────────────────────────────────────────────────
class VipTracker:
    BLACKLIST_DURATION = 600

    def __init__(self, links: List[str]):
        self._links     = list(links)
        self._scores:   Dict[str, float]  = {l: 1.0 for l in links}
        self._blacklist: Dict[str, float] = {}
        self._lock      = threading.Lock()

    def pick(self) -> Optional[str]:
        with self._lock:
            now = time.time()
            available = [l for l in self._links if self._blacklist.get(l, 0) < now]
            if not available:
                if self._blacklist:
                    available = [min(self._blacklist, key=self._blacklist.get)]
                else:
                    return None
            weights = [max(0.1, self._scores.get(l, 1.0)) for l in available]
            chosen = random.choices(available, weights=weights, k=1)[0]
            flog(f"[VIP_TRACKER] picked configured VIP link (score={self._scores.get(chosen,1):.1f})")
            return chosen

    def mark_success(self, link: str):
        with self._lock:
            self._scores[link] = min(self._scores.get(link, 1.0) + 0.5, 5.0)
            self._blacklist.pop(link, None)

    def mark_crash(self, link: str):
        with self._lock:
            self._scores[link] = max(self._scores.get(link, 1.0) - 1.0, 0.1)
            self._blacklist[link] = time.time() + self.BLACKLIST_DURATION

    def status(self) -> List[dict]:
        with self._lock:
            now = time.time()
            return [
                {
                    "link":              l,
                    "score":             round(self._scores.get(l, 1.0), 2),
                    "blacklisted":       self._blacklist.get(l, 0) > now,
                    "blacklist_remaining": max(0, int(self._blacklist.get(l, 0) - now)),
                }
                for l in self._links
            ]


# ─────────────────────────────────────────────────────────────────────────────
#  PROCESS MANAGER
# ─────────────────────────────────────────────────────────────────────────────
ROBLOX_GAME_NAMES = {"robloxplayerbeta.exe"}
ROBLOX_NAMES = ROBLOX_GAME_NAMES | {"robloxplayer.exe", "roblox.exe"}

class ProcessManager:
    LOGIN_WARMUP_URL = "roblox://navigation/home"
    LOGIN_WARMUP_DELAY = 3.0
    MULTI_ROBLOX_ENABLED = True
    GLOBAL_VIP_LINK = ""
    AUTO_CREATE_PRIVATE_SERVER_ENABLED = False
    AUTO_CREATE_PRIVATE_SERVER_FREE_ONLY = True
    _VISUAL_TEMPLATE_BASE_SIZE = (816, 638)
    _VISUAL_TITLE_BOX = (348, 215, 500, 250)
    _VISUAL_RECONNECT_BOX = (410, 400, 590, 438)
    CONNECTION_ERROR_KEYWORDS = (
        "connection error",
        "lost connection",
        "disconnected",
        "reconnect",
        "failed to connect",
        "teleport failed",
        "internet connection",
        "connection lost",
        "please check your internet connection",
        "lost connection to the game server",
    )
    REJOINABLE_DISCONNECT_CODES = {"277"}
    CONDITIONAL_REJOIN_DISCONNECT_CODES = {"273"}
    FATAL_DISCONNECT_CODES = {
        "267": "security_kick",
        "268": "unexpected_client_behavior",
    }
    _visual_template_cache: Dict[str, Any] = {}

    _process_cache: Dict[int, any] = {}
    _cache_lock = threading.Lock()
    _nr_cache: Dict[int, Tuple[float, bool]] = {}
    _nr_cache_ttl = 2.0
    _ownership_lock = threading.Lock()
    _pid_owner: Dict[int, str] = {}
    HIGH_CONFIDENCE = 75.0
    MEDIUM_CONFIDENCE = 45.0

    @classmethod
    def classify_disconnect_dialog_texts(cls, texts: List[str]) -> Dict[str, Any]:
        return classify_texts(texts)

    @staticmethod
    def _same_windows_user(process_user: str) -> bool:
        if not process_user:
            return True
        try:
            current = getpass.getuser().lower()
            user = str(process_user or "").replace("/", "\\").split("\\")[-1].lower()
            return bool(user and user == current)
        except Exception:
            return True

    @classmethod
    def get_process_identity(cls, pid: Optional[int]) -> str:
        if pid is None:
            return ""
        try:
            import psutil
            proc = psutil.Process(pid)
            created = float(proc.create_time() or 0.0)
            name = str(proc.name() or "").lower()
            exe = str(proc.exe() or "").lower()
            return f"{name}|{created:.6f}|{exe}"
        except Exception:
            return ""

    @classmethod
    def claim_pid_owner(cls, pid: Optional[int], owner_key: str):
        if not pid or not owner_key:
            return
        with cls._ownership_lock:
            cls._pid_owner[int(pid)] = str(owner_key)

    @classmethod
    def release_pid_owner(cls, pid: Optional[int], owner_key: Optional[str] = None):
        if not pid:
            return
        with cls._ownership_lock:
            current = cls._pid_owner.get(int(pid))
            if owner_key is None or current == owner_key:
                cls._pid_owner.pop(int(pid), None)

    @classmethod
    def get_pid_owner(cls, pid: Optional[int]) -> str:
        if not pid:
            return ""
        with cls._ownership_lock:
            return str(cls._pid_owner.get(int(pid)) or "")

    @classmethod
    def cleanup_stale_pid_claims(cls):
        with cls._ownership_lock:
            stale = [pid for pid in list(cls._pid_owner.keys()) if not cls.is_pid_alive(pid)]
            for pid in stale:
                cls._pid_owner.pop(pid, None)

    @staticmethod
    def parse_vip_link(vip_url: str) -> Tuple[str, str]:
        if not vip_url:
            return "", ""
        try:
            parsed = urllib.parse.urlparse(vip_url.strip())
            qs     = urllib.parse.parse_qs(parsed.query)
            m = re.search(r"/games/(\d+)", parsed.path)
            place_id = m.group(1) if m else qs.get("placeId", [""])[0]
            link_code = (
                qs.get("privateServerLinkCode", [""])[0] or
                qs.get("linkCode",              [""])[0] or
                qs.get("code",                  [""])[0]
            )
            if not place_id:
                flog("[VIP] Could not parse place_id from configured VIP link", "warning")
            if not link_code:
                flog("[VIP] No linkCode in configured VIP link", "warning")
            return place_id, link_code
        except Exception as e:
            flog(f"[VIP] parse error: {e}", "warning")
            return "", ""

    @staticmethod
    def build_launch_url(acc: Account) -> Tuple[str, ServerType, str]:
        use_public_fallback = bool(
            acc.place_id and
            acc.vip_links and
            int(acc.launch_fail_count or 0) >= 2
        )
        if acc.vip_links and hasattr(acc, '_vip_tracker') and acc._vip_tracker:
            vip_url = acc._vip_tracker.pick()
        elif acc.vip_links:
            vip_url = acc.active_vip or random.choice(acc.vip_links)
        else:
            vip_url = ""

        if vip_url and not use_public_fallback:
            place_id, link_code = ProcessManager.parse_vip_link(vip_url)
            if not place_id and link_code and acc.place_id:
                place_id = acc.place_id
            if place_id and link_code:
                url = (
                    f"roblox://experiences/start"
                    f"?placeId={place_id}"
                    f"&linkCode={link_code}"
                    f"&launchData="
                )
                acc.active_vip = vip_url
                return url, ServerType.VIP, vip_url
            elif place_id and not link_code:
                acc.active_vip = ""
                url = f"roblox://experiences/start?placeId={place_id}"
                return url, ServerType.PUBLIC, ""

        if use_public_fallback:
            acc.launch_strategy = "public_fallback"
        elif vip_url:
            acc.launch_strategy = "vip_preferred"
        else:
            acc.launch_strategy = "public_only"

        if acc.place_id:
            url = f"roblox://experiences/start?placeId={acc.place_id}"
            return url, ServerType.PUBLIC, ""

        return "", ServerType.UNKNOWN, ""

    @classmethod
    def _iter_roblox_processes(cls, game_only: bool = False):
        try:
            import psutil
            allowed_names = ROBLOX_GAME_NAMES if game_only else ROBLOX_NAMES
            for proc in psutil.process_iter(["pid", "name", "create_time", "status"]):
                name = str(proc.info.get("name") or "").lower()
                if name in allowed_names:
                    yield proc
        except ImportError:
            return

    @classmethod
    def _window_snapshot_for_pid(cls, pid: Optional[int]) -> Dict[str, Any]:
        snapshot = {"count": 0, "hwnd": 0, "responsive": False, "hung": False}
        if pid is None:
            return snapshot
        try:
            user32 = ctypes.windll.user32
            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

            def _enum_callback(hwnd, lparam):
                if not user32.IsWindowVisible(hwnd):
                    return True
                win_pid = ctypes.c_ulong(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
                if win_pid.value == pid:
                    snapshot["count"] += 1
                    if not snapshot["hwnd"]:
                        snapshot["hwnd"] = int(hwnd)
                    try:
                        if user32.IsHungAppWindow(hwnd):
                            snapshot["hung"] = True
                        else:
                            snapshot["responsive"] = True
                    except Exception:
                        snapshot["responsive"] = True
                return True

            user32.EnumWindows(WNDENUMPROC(_enum_callback), 0)
        except Exception:
            pass
        return snapshot

    @classmethod
    def _count_visible_windows_for_pid(cls, pid: Optional[int]) -> int:
        return int(cls._window_snapshot_for_pid(pid).get("count") or 0)

    @classmethod
    def _visible_roblox_windows(cls) -> List[Dict[str, Any]]:
        windows: List[Dict[str, Any]] = []
        try:
            import psutil
            proc_meta: Dict[int, Dict[str, Any]] = {}
            for proc in cls._iter_roblox_processes(game_only=True):
                try:
                    proc_meta[int(proc.pid)] = {
                        "created": float(proc.create_time() or 0.0),
                        "name": str(proc.name() or ""),
                    }
                except Exception:
                    continue

            if not proc_meta:
                return []

            user32 = ctypes.windll.user32
            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            def _enum_callback(hwnd, lparam):
                if not user32.IsWindowVisible(hwnd):
                    return True
                win_pid = ctypes.c_ulong(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
                pid = int(win_pid.value or 0)
                meta = proc_meta.get(pid)
                if not meta:
                    return True
                rect = RECT()
                if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    return True
                width = max(0, int(rect.right - rect.left))
                height = max(0, int(rect.bottom - rect.top))
                area = width * height
                if width < 60 or height < 45 or area <= 0:
                    return True
                windows.append({
                    "pid": pid,
                    "hwnd": int(hwnd),
                    "left": int(rect.left),
                    "top": int(rect.top),
                    "right": int(rect.right),
                    "bottom": int(rect.bottom),
                    "width": width,
                    "height": height,
                    "area": area,
                    "created": float(meta.get("created") or 0.0),
                    "name": str(meta.get("name") or ""),
                })
                return True

            user32.EnumWindows(WNDENUMPROC(_enum_callback), 0)
        except Exception as exc:
            flog_kv("WINDOW", "enumerate_roblox_windows_failed", "warning", error=str(exc))
            return []

        by_pid: Dict[int, Dict[str, Any]] = {}
        for item in windows:
            if is_inspection_held(int(item.get("pid") or 0)):
                continue
            current = by_pid.get(int(item["pid"]))
            if current is None or int(item.get("area") or 0) > int(current.get("area") or 0):
                by_pid[int(item["pid"])] = item
        return sorted(by_pid.values(), key=lambda item: (float(item.get("created") or 0.0), int(item.get("pid") or 0)))

    @classmethod
    def minimize_roblox_windows(cls) -> Dict[str, Any]:
        return minimize_windows(cls._visible_roblox_windows())

    @classmethod
    def resize_roblox_windows(cls, width: int, height: int, exclude_pids: Optional[List[int]] = None) -> Dict[str, Any]:
        excluded = {int(pid) for pid in (exclude_pids or []) if pid}
        windows = [item for item in cls._visible_roblox_windows() if int(item.get("pid") or 0) not in excluded]
        return resize_windows(windows, width, height)

    @classmethod
    def _primary_monitor_work_area(cls) -> Dict[str, int]:
        return primary_monitor_work_area()

    @classmethod
    def arrange_roblox_windows(
        cls,
        width: int,
        height: int,
        columns: int = 6,
        gap: int = 2,
        margin: int = 0,
        exclude_pids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        excluded = {int(pid) for pid in (exclude_pids or []) if pid}
        windows = [item for item in cls._visible_roblox_windows() if int(item.get("pid") or 0) not in excluded]
        return arrange_windows(windows, width, height, columns, gap, margin)

    @classmethod
    def restore_roblox_window_styles(cls) -> Dict[str, Any]:
        return restore_window_styles(cls._visible_roblox_windows())

    @classmethod
    def _inspect_roblox_process(cls, proc) -> Dict[str, Any]:
        proc_info = getattr(proc, "info", {}) or {}
        name = str(proc_info.get("name") or proc.name() or "")
        name_l = name.lower()
        status = str(proc_info.get("status") or proc.status() or "")
        created = float(proc_info.get("create_time") or proc.create_time() or 0.0)
        exe = ""
        cmdline = ""
        username = ""
        exe_accessible = False
        cmdline_accessible = False
        username_accessible = False
        try:
            exe = str(proc.exe() or "")
            exe_accessible = True
        except Exception:
            exe = ""
        try:
            cmdline = " ".join(str(part) for part in (proc.cmdline() or []))
            cmdline_accessible = True
        except Exception:
            cmdline = ""
        try:
            username = str(proc.username() or "")
            username_accessible = True
        except Exception:
            username = ""
        try:
            rss_mb = float(proc.memory_info().rss / (1024 * 1024))
        except Exception:
            rss_mb = 0.0
        window_snapshot = cls._window_snapshot_for_pid(proc.pid)
        windows = int(window_snapshot.get("count") or 0)
        cpu = float(_rt_monitor.get_cpu(proc.pid))
        identity = cls.get_process_identity(proc.pid)
        exe_name = os.path.basename(exe).lower() if exe else ""
        exe_l = exe.lower()
        valid_exe = (
            exe_accessible and
            exe_name in ROBLOX_GAME_NAMES and
            ("\\roblox\\" in exe_l or "\\versions\\" in exe_l or "\\windowsapps\\" in exe_l)
        )
        valid_cmdline = cmdline_accessible and ((not cmdline) or ("roblox" in cmdline.lower()))
        valid_user = username_accessible and cls._same_windows_user(username)
        return {
            "pid": int(proc.pid),
            "name": name,
            "name_l": name_l,
            "status": status,
            "created": created,
            "exe": exe,
            "exe_name": exe_name,
            "cmdline": cmdline,
            "username": username,
            "rss_mb": rss_mb,
            "windows": windows,
            "hwnd": int(window_snapshot.get("hwnd") or 0),
            "window_responsive": bool(window_snapshot.get("responsive")),
            "window_hung": bool(window_snapshot.get("hung")),
            "cpu": cpu,
            "identity": identity,
            "owner": cls.get_pid_owner(proc.pid),
            "valid_name": name_l in ROBLOX_GAME_NAMES,
            "valid_exe": valid_exe,
            "valid_cmdline": valid_cmdline,
            "valid_user": valid_user,
        }

    @classmethod
    def validate_game_process(
        cls,
        pid: Optional[int],
        owner_key: str = "",
        expected_identity: str = "",
        launched_after: Optional[float] = None,
        min_ram_mb: float = 20.0,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "ok": False,
            "pid": pid,
            "reason": "",
            "confidence": 0.0,
            "identity": "",
            "name": "",
            "created": 0.0,
            "windows": 0,
            "hwnd": 0,
            "cpu": 0.0,
            "ram_mb": 0.0,
            "owner": "",
        }
        if not pid:
            result["reason"] = "missing_pid"
            return result
        try:
            import psutil
            proc = psutil.Process(int(pid))
            info = cls._inspect_roblox_process(proc)
            result.update({
                "identity": info["identity"],
                "name": info["name"],
                "created": info["created"],
                "windows": info["windows"],
                "hwnd": info["hwnd"],
                "cpu": round(float(info["cpu"]), 2),
                "ram_mb": round(float(info["rss_mb"]), 1),
                "owner": info["owner"],
            })
            if str(info["status"]).lower() == "zombie":
                result["reason"] = "zombie"
                return result
            if not info["valid_name"]:
                result["reason"] = "invalid_name"
                return result
            if not info["valid_exe"]:
                result["reason"] = f"invalid_exe:{info['exe_name']}"
                return result
            if not info["valid_cmdline"]:
                result["reason"] = "invalid_cmdline"
                return result
            if not info["valid_user"]:
                result["reason"] = "owner_user_mismatch"
                return result
            if launched_after and info["created"] and info["created"] < (float(launched_after) - 3.0):
                result["reason"] = "created_before_launch"
                return result
            owner = str(info["owner"] or "")
            if owner_key and owner and owner != owner_key:
                result["reason"] = f"owner_mismatch:{owner}"
                return result
            if expected_identity and info["identity"] and info["identity"] != expected_identity:
                result["reason"] = "identity_mismatch"
                return result
            if float(info["rss_mb"] or 0.0) < min_ram_mb and int(info["windows"] or 0) <= 0:
                result["reason"] = "low_ram"
                return result

            confidence = 30.0
            if info["valid_exe"]:
                confidence += 15.0
            if owner_key and owner == owner_key:
                confidence += 25.0
            if info["valid_user"]:
                confidence += 8.0
            if expected_identity and info["identity"] == expected_identity:
                confidence += 35.0
            confidence += min(15.0, float(info["windows"]) * 7.0)
            confidence += min(12.0, float(info["rss_mb"]) / 120.0)
            confidence += min(10.0, float(info["cpu"]) * 2.0)
            result["ok"] = True
            result["reason"] = "ok"
            result["confidence"] = round(confidence, 1)
            result["confidence_level"] = cls.confidence_level(confidence)
            return result
        except ImportError:
            result["reason"] = "psutil_unavailable"
            return result
        except Exception as e:
            error_name = e.__class__.__name__.lower()
            if "nosuchprocess" in error_name:
                result["reason"] = "no_such_process"
            elif "accessdenied" in error_name:
                result["reason"] = "access_denied"
            else:
                result["reason"] = f"error:{e}"
            return result

    @classmethod
    def get_game_activity(cls, pid: Optional[int]) -> Dict[str, Any]:
        validation = cls.validate_game_process(pid, min_ram_mb=0.0)
        return {
            "alive": bool(validation.get("ok")),
            "windows": int(validation.get("windows") or 0),
            "cpu": float(validation.get("cpu") or 0.0),
            "ram_mb": float(validation.get("ram_mb") or 0.0),
            "reason": str(validation.get("reason") or ""),
        }

    @classmethod
    def get_pid_cmdline(cls, pid: Optional[int]) -> str:
        if not pid:
            return ""
        try:
            import psutil
            proc = psutil.Process(int(pid))
            return " ".join(str(part) for part in (proc.cmdline() or []))
        except Exception:
            return ""

    @classmethod
    def confidence_level(cls, confidence: float) -> str:
        value = float(confidence or 0.0)
        if value >= cls.HIGH_CONFIDENCE:
            return "HIGH_CONFIDENCE"
        if value >= cls.MEDIUM_CONFIDENCE:
            return "MEDIUM_CONFIDENCE"
        if value > 0:
            return "LOW_CONFIDENCE"
        return "UNTRUSTED"

    @classmethod
    def find_bound_game_process(
        cls,
        preferred_pid: Optional[int] = None,
        launched_after: Optional[float] = None,
        owner_key: str = "",
        expected_identity: str = "",
    ) -> Tuple[Optional[int], str]:
        try:
            import psutil
            candidates: List[Tuple[float, float, int, str]] = []
            cls.cleanup_stale_pid_claims()
            for proc in cls._iter_roblox_processes(game_only=True):
                try:
                    info = cls._inspect_roblox_process(proc)
                    pid = int(info["pid"])
                    if preferred_pid and pid == preferred_pid and expected_identity:
                        validation = cls.validate_game_process(
                            pid,
                            owner_key=owner_key,
                            expected_identity=expected_identity,
                            launched_after=launched_after,
                            min_ram_mb=20.0,
                        )
                        if not validation.get("ok"):
                            flog_kv(
                                "PROC",
                                "reject_preferred_pid",
                                "warning",
                                pid=pid,
                                reason=validation.get("reason", ""),
                            )
                            continue
                    if str(info["status"]).lower() == "zombie":
                        continue
                    created = float(info["created"] or 0.0)
                    if launched_after and created and created < (launched_after - 3.0):
                        continue
                    rss_mb = float(info["rss_mb"] or 0.0)
                    if rss_mb < 50 and int(info["windows"] or 0) <= 0:
                        continue
                    if not info["valid_name"] or not info["valid_exe"] or not info["valid_cmdline"] or not info["valid_user"]:
                        continue
                    window_count = int(info["windows"] or 0)
                    owner = str(info["owner"] or "")
                    if owner and owner_key and owner != owner_key:
                        continue
                    identity = str(info["identity"] or "")
                    score = (window_count * 100000.0) + rss_mb + created
                    if owner_key and owner == owner_key:
                        score += 500000.0
                    if preferred_pid and pid == preferred_pid:
                        score += 250000.0
                    if expected_identity and identity == expected_identity:
                        score += 400000.0
                    candidates.append((score, created, pid, str(info["name"] or "")))
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
                except Exception:
                    continue

            if preferred_pid:
                for _score, _created, pid, name in candidates:
                    if pid == preferred_pid:
                        return pid, name

            if not candidates:
                return None, ""

            candidates.sort(reverse=True)
            _score, _created, pid, name = candidates[0]
            return pid, name
        except ImportError:
            return None, ""

    @classmethod
    def summarize_game_presence(
        cls,
        launched_after: Optional[float] = None,
    ) -> Dict[str, Any]:
        summary: Dict[str, Any] = {
            "pids": [],
            "visible_windows": 0,
            "max_ram_mb": 0.0,
            "max_cpu": 0.0,
            "newest_created": 0.0,
        }
        try:
            import psutil
            for proc in cls._iter_roblox_processes(game_only=True):
                try:
                    info = cls._inspect_roblox_process(proc)
                    if not info["valid_name"] or not info["valid_exe"] or not info["valid_cmdline"] or not info["valid_user"]:
                        continue
                    created = float(info["created"] or 0.0)
                    if launched_after and created and created < (launched_after - 3.0):
                        continue
                    status = str(info["status"] or "")
                    if status == "zombie":
                        continue
                    rss_mb = float(info["rss_mb"] or 0.0)
                    if rss_mb < 20 and int(info["windows"] or 0) <= 0:
                        continue
                    pid = int(info["pid"])
                    summary["pids"].append(pid)
                    summary["visible_windows"] += int(info["windows"] or 0)
                    summary["max_ram_mb"] = max(float(summary["max_ram_mb"]), float(rss_mb))
                    summary["max_cpu"] = max(float(summary["max_cpu"]), float(info["cpu"] or 0.0))
                    summary["newest_created"] = max(float(summary["newest_created"] or 0.0), created)
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
                except Exception:
                    continue
        except ImportError:
            pass
        return summary

    @classmethod
    def list_live_game_processes(
        cls,
        launched_after: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        try:
            import psutil
            for proc in cls._iter_roblox_processes(game_only=True):
                try:
                    info = cls._inspect_roblox_process(proc)
                    if not info["valid_name"] or not info["valid_exe"] or not info["valid_cmdline"] or not info["valid_user"]:
                        continue
                    created = float(info["created"] or 0.0)
                    if launched_after and created and created < (launched_after - 3.0):
                        continue
                    status = str(info["status"] or "")
                    if status == "zombie":
                        continue
                    rss_mb = float(info["rss_mb"] or 0.0)
                    if rss_mb < 50 and int(info["windows"] or 0) <= 0:
                        continue
                    pid = int(info["pid"])
                    entries.append({
                        "pid": pid,
                        "name": str(info["name"] or ""),
                        "created": created,
                        "rss_mb": float(rss_mb),
                        "windows": int(info["windows"] or 0),
                        "hwnd": int(info["hwnd"] or 0),
                        "cpu": float(info["cpu"] or 0.0),
                        "identity": str(info["identity"] or ""),
                        "owner": str(info["owner"] or ""),
                        "exe": str(info["exe"] or ""),
                        "cmdline": str(info["cmdline"] or ""),
                        "username": str(info["username"] or ""),
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
                except Exception:
                    continue
        except ImportError:
            return entries

        entries.sort(
            key=lambda item: (
                int(item.get("windows", 0)),
                float(item.get("rss_mb", 0.0)),
                float(item.get("created", 0.0)),
            ),
            reverse=True,
        )
        return entries

    @classmethod
    def multi_signal_validate(
        cls,
        preferred_pid: Optional[int] = None,
        launched_after: Optional[float] = None,
        owner_key: str = "",
        expected_identity: str = "",
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "pid": None,
            "name": "",
            "identity": "",
            "confidence": 0.0,
            "confidence_level": "UNTRUSTED",
            "signals": {
                "pid_match": False,
                "identity_match": False,
                "created_after_launch": False,
                "windows": 0,
                "hwnd": 0,
                "cpu": 0.0,
                "ram_mb": 0.0,
                "owner_match": False,
                "candidates": [],
            },
        }
        best_score = -1.0
        for entry in cls.list_live_game_processes(launched_after=launched_after):
            owner = str(entry.get("owner") or "")
            if owner_key and owner and owner != owner_key:
                continue
            pid = int(entry.get("pid") or 0)
            windows = int(entry.get("windows") or 0)
            cpu = float(entry.get("cpu") or 0.0)
            ram_mb = float(entry.get("rss_mb") or 0.0)
            identity = str(entry.get("identity") or "")
            pid_match = bool(preferred_pid and pid == preferred_pid)
            identity_match = bool(expected_identity and identity == expected_identity)
            created_after_launch = bool(
                launched_after and float(entry.get("created") or 0.0) >= (float(launched_after) - 3.0)
            )
            if pid_match and expected_identity and not identity_match:
                result["signals"]["candidates"].append({
                    "pid": pid,
                    "owner": owner,
                    "identity_match": False,
                    "pid_match": True,
                    "created_after_launch": created_after_launch,
                    "windows": windows,
                    "hwnd": int(entry.get("hwnd") or 0),
                    "cpu": round(cpu, 2),
                    "ram_mb": round(ram_mb, 1),
                    "score": 0.0,
                    "rejected": "identity_mismatch",
                })
                continue
            owner_match = bool(owner_key and owner == owner_key)
            score = 0.0
            if pid_match:
                score += 35.0
            if identity_match:
                score += 35.0
            if owner_match:
                score += 20.0
            if created_after_launch:
                score += 12.0
            score += min(15.0, float(windows) * 7.0)
            score += min(12.0, ram_mb / 120.0)
            score += min(10.0, cpu * 2.0)
            if entry.get("exe"):
                score += 5.0
            if "roblox" in str(entry.get("cmdline") or "").lower():
                score += 3.0
            result["signals"]["candidates"].append({
                "pid": pid,
                "owner": owner,
                "identity_match": identity_match,
                "pid_match": pid_match,
                "created_after_launch": created_after_launch,
                "windows": windows,
                "hwnd": int(entry.get("hwnd") or 0),
                "cpu": round(cpu, 2),
                "ram_mb": round(ram_mb, 1),
                "score": round(score, 1),
            })
            if score > best_score:
                best_score = score
                result.update({
                    "pid": pid,
                    "name": str(entry.get("name") or ""),
                    "identity": identity,
                    "confidence": round(score, 1),
                    "confidence_level": cls.confidence_level(score),
                })
                result["signals"].update({
                    "pid_match": pid_match,
                    "identity_match": identity_match,
                    "created_after_launch": created_after_launch,
                    "windows": windows,
                    "hwnd": int(entry.get("hwnd") or 0),
                    "cpu": round(cpu, 2),
                    "ram_mb": round(ram_mb, 1),
                    "owner_match": owner_match,
                })
        return result

    @classmethod
    def staged_orphan_reconcile(
        cls,
        acc: Account,
        launched_after: Optional[float] = None,
        quarantine_seconds: float = 20.0,
    ) -> Dict[str, Any]:
        validation = cls.multi_signal_validate(
            preferred_pid=acc.pid,
            launched_after=launched_after,
            owner_key=acc._config_username,
            expected_identity=acc.bound_process_identity,
        )
        pid = int(validation.get("pid") or 0)
        confidence = float(validation.get("confidence") or 0.0)
        level = str(validation.get("confidence_level") or cls.confidence_level(confidence))
        signals = validation.get("signals") or {}
        now = time.time()
        result = {
            "action": "ignore",
            "pid": pid or None,
            "name": str(validation.get("name") or ""),
            "identity": str(validation.get("identity") or ""),
            "confidence": confidence,
            "confidence_level": level,
            "validation": validation,
            "reason": "",
        }
        if not pid:
            with acc._lock:
                _RUNTIME_STATE.set_binding_status(acc, "unbound", reason="orphan_reconcile_no_candidate")
                acc.orphan_confidence = 0.0
            result["reason"] = "no_candidate"
            return result

        trusted_owner = bool(signals.get("owner_match"))
        trusted_identity = bool(signals.get("identity_match"))
        trusted_restore = trusted_owner or trusted_identity
        if level == "HIGH_CONFIDENCE" and trusted_restore:
            with acc._lock:
                _RUNTIME_STATE.set_binding_status(acc, "verified", reason="orphan_reconcile_trusted_restore")
                acc.orphan_confidence = confidence
                acc.orphan_pid = None
                acc.orphan_identity = ""
                acc.orphan_observed_at = 0.0
                acc.orphan_verify_after = 0.0
            result["action"] = "auto_bind"
            result["reason"] = "trusted_restore"
            return result

        if level == "MEDIUM_CONFIDENCE":
            identity = str(validation.get("identity") or "")
            with acc._lock:
                same_orphan = acc.orphan_pid == pid and acc.orphan_identity == identity
                if not same_orphan:
                    acc.orphan_pid = pid
                    acc.orphan_identity = identity
                    acc.orphan_observed_at = now
                    acc.orphan_verify_after = now + max(5.0, float(quarantine_seconds or 20.0))
                acc.orphan_confidence = confidence
                _RUNTIME_STATE.set_binding_status(acc, "orphan_pending_verification", reason="orphan_reconcile_pending")
                verify_after = acc.orphan_verify_after
            result["action"] = "quarantine" if now < verify_after else "monitor_only"
            result["reason"] = "medium_confidence_pending" if now < verify_after else "medium_confidence_unowned"
            return result

        with acc._lock:
            acc.orphan_confidence = confidence
            _RUNTIME_STATE.set_binding_status(
                acc,
                "untrusted_orphan" if confidence > 0 else "unbound",
                reason="orphan_reconcile_low_confidence",
            )
        result["action"] = "monitor_only"
        result["reason"] = "low_confidence"
        return result

    @classmethod
    def assess_liveness(
        cls,
        pid: Optional[int],
        previous_cpu: float = 0.0,
        previous_ram_mb: float = 0.0,
        net_online: bool = True,
        recovery_inflight: bool = False,
        in_game_for: float = 0.0,
        loading_grace: float = 90.0,
        cpu_threshold: float = 0.9,
        ram_delta_threshold: float = 8.0,
        inspect_ui: bool = False,
    ) -> Dict[str, Any]:
        validation = cls.validate_game_process(pid, min_ram_mb=0.0)
        if not validation.get("ok"):
            return {
                "state": "missing",
                "score": 0.0,
                "reason_key": "process_crash",
                "validation": validation,
                "cpu_delta": 0.0,
                "ram_delta": 0.0,
                "dialog": {},
            }

        cpu = float(validation.get("cpu") or 0.0)
        ram = float(validation.get("ram_mb") or 0.0)
        windows = int(validation.get("windows") or 0)
        cpu_delta = abs(cpu - float(previous_cpu or 0.0))
        ram_delta = abs(ram - float(previous_ram_mb or 0.0))
        responsive = windows > 0 and not cls.is_not_responding(pid)

        score = 1.0
        if responsive:
            score += 3.0
        if cpu >= float(cpu_threshold or 0.9) or cpu_delta >= max(0.2, float(cpu_threshold or 0.9) / 2.0):
            score += 2.0
        if ram_delta >= max(1.0, float(ram_delta_threshold or 8.0)):
            score += 1.0
        if ram >= 90.0:
            score += 1.0
        if net_online:
            score += 1.0
        if recovery_inflight:
            score -= 1.0

        dialog: Dict[str, Any] = {}
        state = "alive"
        reason_key = ""
        if inspect_ui or (windows > 0 and score <= 4.0):
            dialog = cls.inspect_disconnect_dialog(
                pid,
                prepare=bool(inspect_ui),
                presence_mismatch=bool(inspect_ui),
                process_idle=score <= 4.0,
                sample_count=6 if inspect_ui else 2,
            )
            if dialog.get("matched") and dialog.get("recovery_allowed"):
                reason_key = str(dialog.get("reason_key") or "connection_error")
                if reason_key == "teleport_timeout":
                    state = "teleporting"
                elif reason_key in {"network_drop", "connection_error", "server_full"}:
                    state = "reconnecting"
                else:
                    state = "reconnecting"

        if not state or state == "alive":
            if in_game_for < max(30.0, float(loading_grace or 90.0)) and not responsive and score <= 4.0:
                state = "loading"
            elif score >= 5.0:
                state = "alive"
            elif score >= 3.0:
                state = "idle"
            else:
                state = "suspect_frozen"
                reason_key = "watchdog_timeout" if windows > 0 else "loading_freeze"

        return {
            "state": state,
            "score": round(max(0.0, score), 1),
            "reason_key": reason_key,
            "validation": validation,
            "cpu_delta": round(cpu_delta, 2),
            "ram_delta": round(ram_delta, 1),
            "dialog": dialog,
        }

    @classmethod
    def snapshot_pids(cls) -> set:
        try:
            import psutil
            return {
                p.pid
                for p in psutil.process_iter(["pid", "name"])
                if (p.info.get("name") or "").lower() in ROBLOX_NAMES
            }
        except ImportError:
            return set()

    @classmethod
    def kill_all_roblox_clients(
        cls,
        wait_seconds: float = 4.0,
        exclude_pids: Optional[List[int]] = None,
    ) -> int:
        killed = 0
        try:
            import psutil
            excluded = {int(pid) for pid in (exclude_pids or []) if pid}
            victims = []
            for p in psutil.process_iter(["pid", "name"]):
                if (p.info.get("name") or "").lower() in ROBLOX_NAMES:
                    if p.pid in excluded:
                        continue
                    victims.append(p)
            for proc in victims:
                try:
                    cls.evict_pid_cache(proc.pid)
                    proc.terminate()
                    killed += 1
                except Exception:
                    continue
            if victims:
                gone, alive = psutil.wait_procs(victims, timeout=max(0.5, wait_seconds))
                for proc in alive:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            return killed
        except ImportError:
            return 0
        except Exception as e:
            flog(f"[PROC] kill_all_roblox_clients error: {e}", "warning")
            return killed

    @classmethod
    def cleanup_extra_launch_processes(
        cls,
        before: set,
        keep_pids: Optional[List[int]] = None,
        launched_after: Optional[float] = None,
        wait_seconds: float = 2.0,
    ) -> int:
        killed = 0
        keep = {int(pid) for pid in (keep_pids or []) if pid}
        try:
            import psutil
            victims = []
            for proc in cls._iter_roblox_processes(game_only=True):
                try:
                    pid = int(proc.pid)
                    if pid in keep or pid in before:
                        continue
                    created = float(proc.info.get("create_time") or proc.create_time())
                    if launched_after and created and created < (launched_after - 3.0):
                        continue
                    victims.append(proc)
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
                except Exception:
                    continue

            for proc in victims:
                try:
                    flog(f"[PROC] cleaning leftover launch PID {proc.pid}")
                    cls.evict_pid_cache(proc.pid)
                    proc.terminate()
                    killed += 1
                except Exception:
                    continue

            if victims:
                gone, alive = psutil.wait_procs(victims, timeout=max(0.5, wait_seconds))
                for proc in alive:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        except ImportError:
            return 0
        except Exception as e:
            flog(f"[PROC] cleanup_extra_launch_processes error: {e}", "warning")
        return killed

    @classmethod
    def detect_new_pid(
        cls,
        before: set,
        timeout: float = 20.0,
        launched_after: Optional[float] = None,
        created_after_slack: float = 0.0,
    ) -> Optional[int]:
        try:
            deadline = time.time() + timeout
            first_seen: Dict[int, float] = {}
            settle_seconds = 2.0
            created_threshold = None
            if launched_after:
                created_threshold = float(launched_after) - max(0.0, float(created_after_slack or 0.0))
            while time.time() < deadline:
                now = time.time()
                live_new_pids = set()
                for entry in cls.list_live_game_processes(launched_after=None):
                    pid = int(entry.get("pid") or 0)
                    created = float(entry.get("created") or 0.0)
                    if not pid or pid in before:
                        continue
                    if created_threshold is not None and created and created < created_threshold:
                        continue
                    live_new_pids.add(pid)
                    first_seen.setdefault(pid, now)
                    if (now - first_seen[pid]) >= settle_seconds:
                        validation = cls.validate_game_process(pid, launched_after=None, min_ram_mb=20.0)
                        if not validation.get("ok"):
                            flog_kv(
                                "PROC",
                                "reject_detected_pid",
                                "warning",
                                pid=pid,
                                reason=validation.get("reason", ""),
                            )
                            continue
                        flog_kv(
                            "PROC",
                            "stable_pid_detected",
                            pid=pid,
                            name=entry.get("name") or "unknown",
                            confidence=validation.get("confidence", 0.0),
                        )
                        return pid
                first_seen = {pid: ts for pid, ts in first_seen.items() if pid in live_new_pids}
                time.sleep(0.5)
        except Exception:
            pass
        return None

    @classmethod
    def is_pid_alive(cls, pid: Optional[int]) -> bool:
        if pid is None:
            return False
        try:
            import psutil
            return psutil.pid_exists(pid) and psutil.Process(pid).status() != "zombie"
        except Exception:
            return False

    @classmethod
    def is_bound_game_alive(
        cls,
        pid: Optional[int],
        owner_key: str = "",
        expected_identity: str = "",
    ) -> bool:
        validation = cls.validate_game_process(
            pid,
            owner_key=owner_key,
            expected_identity=expected_identity,
            min_ram_mb=0.0,
        )
        return bool(validation.get("ok"))

    @classmethod
    def is_not_responding(cls, pid: Optional[int]) -> bool:
        """
        ตรวจจับ 'Not Responding' ผ่าน Windows IsHungAppWindow()
        เหมือน Task Manager ทุกประการ
        """
        if pid is None:
            return False
        with cls._cache_lock:
            cached = cls._nr_cache.get(pid)
            if cached and (time.time() - cached[0]) < cls._nr_cache_ttl:
                return cached[1]
        try:
            user32 = ctypes.windll.user32
            result = {"hung": False, "window_count": 0}
            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

            def _enum_callback(hwnd, lparam):
                if not user32.IsWindowVisible(hwnd):
                    return True
                win_pid = ctypes.c_ulong(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
                if win_pid.value == pid:
                    result["window_count"] += 1
                    if user32.IsHungAppWindow(hwnd):
                        result["hung"] = True
                        return False
                return True

            callback = WNDENUMPROC(_enum_callback)
            user32.EnumWindows(callback, 0)

            if result["window_count"] == 0:
                with cls._cache_lock:
                    cls._nr_cache[pid] = (time.time(), False)
                return False
            if result["hung"]:
                flog(f"[PROC] PID {pid} is NOT RESPONDING (Task Manager style)")
            with cls._cache_lock:
                cls._nr_cache[pid] = (time.time(), result["hung"])
            return result["hung"]

        except Exception as e:
            flog(f"[PROC] is_not_responding error for PID {pid}: {e}", "warning")
            return False

    @classmethod
    def inspect_disconnect_dialog(
        cls,
        pid: Optional[int],
        prepare: bool = False,
        presence_mismatch: bool = False,
        process_idle: bool = False,
        sample_count: Optional[int] = None,
    ) -> Dict[str, Any]:
        if pid is None:
            return {"matched": False, "action": "", "reason_key": "", "detail": "", "error_code": ""}
        try:
            return DEFAULT_POPUP_OBSERVER.inspect_pid(
                pid,
                prepare=prepare,
                presence_mismatch=presence_mismatch,
                process_idle=process_idle,
                sample_count=sample_count,
            )
        except Exception as e:
            flog(f"[PROC] inspect_disconnect_dialog error for PID {pid}: {e}", "warning")
            return cls._inspect_disconnect_dialog_visual(pid)

    @classmethod
    def detect_connection_error(cls, pid: Optional[int]) -> Tuple[bool, str]:
        info = cls.inspect_disconnect_dialog(pid)
        if not info.get("matched") or str(info.get("action") or "") not in {"rejoin", "conditional_rejoin"}:
            return False, ""
        return True, str(info.get("detail") or "")

    @classmethod
    def _template_path(cls, name: str) -> str:
        return resource_path("vision_templates", name)

    @classmethod
    def _load_visual_template(cls, name: str):
        cached = cls._visual_template_cache.get(name)
        if cached is not None:
            return cached
        try:
            from PIL import Image
            img = Image.open(cls._template_path(name)).convert("L")
            cls._visual_template_cache[name] = img
            return img
        except Exception:
            cls._visual_template_cache[name] = None
            return None

    @classmethod
    def _get_pid_window_rect(cls, pid: Optional[int]) -> Optional[Tuple[int, int, int, int]]:
        if pid is None:
            return None
        try:
            user32 = ctypes.windll.user32
            rects: List[Tuple[int, int, int, int, int]] = []
            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            def _enum_callback(hwnd, lparam):
                if not user32.IsWindowVisible(hwnd):
                    return True
                win_pid = ctypes.c_ulong(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
                if win_pid.value != pid:
                    return True
                rect = RECT()
                if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    return True
                width = max(0, int(rect.right - rect.left))
                height = max(0, int(rect.bottom - rect.top))
                area = width * height
                if width >= 300 and height >= 200 and area > 0:
                    rects.append((area, int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)))
                return True

            user32.EnumWindows(WNDENUMPROC(_enum_callback), 0)
            if not rects:
                return None
            rects.sort(reverse=True)
            _area, left, top, right, bottom = rects[0]
            return left, top, right, bottom
        except Exception:
            return None

    @classmethod
    def _capture_pid_window_image(cls, pid: Optional[int]):
        rect = cls._get_pid_window_rect(pid)
        if not rect:
            return None
        try:
            from PIL import Image
            left, top, right, bottom = rect
            width = max(0, int(right - left))
            height = max(0, int(bottom - top))
            if width <= 0 or height <= 0:
                return None
            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32

            hwnd = None
            try:
                hwnd = user32.WindowFromPoint(wintypes.POINT(left + 8, top + 8))
            except Exception:
                hwnd = None
            if not hwnd:
                hwnd = user32.GetForegroundWindow()

            target_hwnd = None
            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)
            def _enum_callback(win_hwnd, lparam):
                nonlocal target_hwnd
                if not user32.IsWindowVisible(win_hwnd):
                    return True
                win_pid = ctypes.c_ulong(0)
                user32.GetWindowThreadProcessId(win_hwnd, ctypes.byref(win_pid))
                if win_pid.value == pid:
                    target_hwnd = win_hwnd
                    return False
                return True
            user32.EnumWindows(WNDENUMPROC(_enum_callback), 0)
            if not target_hwnd:
                return None

            hwnd_dc = user32.GetWindowDC(target_hwnd)
            mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
            bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
            gdi32.SelectObject(mem_dc, bitmap)
            PW_RENDERFULLCONTENT = 0x00000002
            ok = user32.PrintWindow(target_hwnd, mem_dc, PW_RENDERFULLCONTENT)
            if not ok:
                user32.PrintWindow(target_hwnd, mem_dc, 0)

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize", wintypes.DWORD),
                    ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD),
                ]

            class BITMAPINFO(ctypes.Structure):
                _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = width
            bmi.bmiHeader.biHeight = -height
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0

            buf_len = width * height * 4
            buffer = ctypes.create_string_buffer(buf_len)
            gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(bmi), 0)
            image = Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1).convert("L")

            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(target_hwnd, hwnd_dc)
            return image
        except Exception:
            return None

    @staticmethod
    def _scaled_box(box: Tuple[int, int, int, int], size: Tuple[int, int]) -> Tuple[int, int, int, int]:
        base_w, base_h = ProcessManager._VISUAL_TEMPLATE_BASE_SIZE
        width, height = size
        sx = float(width) / float(base_w)
        sy = float(height) / float(base_h)
        left, top, right, bottom = box
        return (
            max(0, int(round(left * sx))),
            max(0, int(round(top * sy))),
            min(width, int(round(right * sx))),
            min(height, int(round(bottom * sy))),
        )

    @staticmethod
    def _rmsdiff(img_a, img_b) -> float:
        try:
            from PIL import ImageChops
            diff = ImageChops.difference(img_a, img_b)
            hist = diff.histogram()
            sq = sum((value * ((idx % 256) ** 2)) for idx, value in enumerate(hist))
            total = max(1, img_a.size[0] * img_a.size[1])
            return math.sqrt(float(sq) / float(total))
        except Exception:
            return 9999.0

    @classmethod
    def _inspect_disconnect_dialog_visual(cls, pid: Optional[int]) -> Dict[str, Any]:
        try:
            return DEFAULT_POPUP_OBSERVER.inspect_pid(pid, prepare=False, sample_count=2)
        except Exception as e:
            flog(f"[PROC] visual disconnect inspect error for PID {pid}: {e}", "warning")
        return {"matched": False, "action": "", "reason_key": "", "detail": "", "error_code": ""}

    @classmethod
    def get_pid_cpu(cls, pid: int, interval: float = 0.0) -> float:
        """ดึง CPU% จาก RealtimeResourceMonitor (realtime, non-blocking)"""
        return _rt_monitor.get_cpu(pid)

    @classmethod
    def get_pid_memory_mb(cls, pid: int) -> float:
        """ดึง RAM MB จาก RealtimeResourceMonitor (realtime, non-blocking)"""
        return _rt_monitor.get_ram(pid)

    @classmethod
    def evict_pid_cache(cls, pid: Optional[int]):
        if pid is not None:
            with cls._cache_lock:
                cls._process_cache.pop(pid, None)
                cls._nr_cache.pop(pid, None)
            cls.release_pid_owner(pid)
            _rt_monitor.unregister(pid)

    @classmethod
    def kill_pid(cls, pid: Optional[int]) -> bool:
        if not pid:
            return False
        cls.evict_pid_cache(pid)
        try:
            import psutil
            try:
                p = psutil.Process(pid)
                p.terminate()
                p.wait(timeout=3)
                return True
            except psutil.TimeoutExpired:
                p.kill()
                return True
            except psutil.NoSuchProcess:
                return True
            except (psutil.AccessDenied, psutil.ZombieProcess) as e:
                flog(f"[PROC] kill_pid access error for PID {pid}: {e}", "warning")
                return False
            except Exception as e:
                flog(f"[PROC] kill_pid error for PID {pid}: {e}", "warning")
                return False
        except ImportError:
            try:
                subprocess.call(
                    ["taskkill", "/F", "/PID", str(pid)],
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    stderr=subprocess.DEVNULL,
                )
                return True
            except Exception:
                return False

    @classmethod
    def launch(cls, acc: Account) -> Tuple[bool, str, str]:
        block_reason = account_launch_block_reason(acc)
        if block_reason:
            flog(f"[LAUNCH] Blocked for {acc.display_name}: {block_reason}", "warning")
            return False, block_reason, ""

        if str(getattr(acc, "cookie", "") or "").strip():
            try:
                from roblox_hybrid import HybridLauncher

                target_place = str(acc.place_id or "")
                active_vip = str(acc.active_vip or "")
                if target_place and active_vip:
                    active_place, _active_code = cls.parse_vip_link(active_vip)
                    if active_place and active_place != target_place:
                        active_vip = ""
                vip_links = list(acc.vip_links or [])
                if target_place:
                    vip_links = [
                        link for link in vip_links
                        if not cls.parse_vip_link(str(link or "").strip())[0]
                        or cls.parse_vip_link(str(link or "").strip())[0] == target_place
                    ]
                global_vip = cls.GLOBAL_VIP_LINK
                global_place = cls.parse_vip_link(global_vip)[0] if global_vip else ""
                if target_place and global_place and global_place != target_place:
                    global_vip = ""
                target = {
                    "place_id": target_place,
                    "vip_links": vip_links,
                    "vip_link": active_vip,
                    "global_vip_link": global_vip,
                    "browser_tracker_id": getattr(acc, "browser_tracker_id", ""),
                    "auto_create_private_server_enabled": bool(cls.AUTO_CREATE_PRIVATE_SERVER_ENABLED),
                    "auto_create_private_server_free_only": bool(cls.AUTO_CREATE_PRIVATE_SERVER_FREE_ONLY),
                }
                record = {
                    "username": acc.username,
                    "alias": acc.alias,
                    "cookie": acc.cookie,
                    "place_id": target_place,
                    "vip_links": vip_links,
                    "global_vip_link": global_vip,
                    "browser_tracker_id": getattr(acc, "browser_tracker_id", ""),
                    "auto_create_private_server_enabled": bool(cls.AUTO_CREATE_PRIVATE_SERVER_ENABLED),
                    "auto_create_private_server_free_only": bool(cls.AUTO_CREATE_PRIVATE_SERVER_FREE_ONLY),
                }
                result = HybridLauncher.launch_record(record, target=target, multi_roblox=bool(cls.MULTI_ROBLOX_ENABLED))
                if result.get("ok"):
                    acc.browser_tracker_id = str(result.get("browser_tracker_id") or getattr(acc, "browser_tracker_id", "") or "")
                    mode = str(result.get("mode") or "")
                    attempted_vip_hybrid = str(result.get("attempted_vip") or acc.active_vip or "")
                    if mode == "vip":
                        acc.server_type = ServerType.VIP
                        acc.active_vip = attempted_vip_hybrid
                    elif mode in {"job", "public"}:
                        acc.server_type = ServerType.PUBLIC
                        acc.active_vip = ""
                    acc.last_launch_at = time.time()
                    return True, str(result.get("msg") or "Launched via auth ticket"), attempted_vip_hybrid
                flog(f"[LAUNCH] Auth-ticket launch failed for {acc.display_name}: {result.get('msg')}", "warning")
                if result.get("fatal") or bool(cls.MULTI_ROBLOX_ENABLED):
                    return False, str(result.get("msg") or "Auth-ticket launch blocked"), ""
            except Exception as e:
                flog(f"[LAUNCH] Auth-ticket launch path errored for {acc.display_name}: {e}", "warning")
                if bool(cls.MULTI_ROBLOX_ENABLED):
                    return False, str(e), ""

        url, server_type, attempted_vip = cls.build_launch_url(acc)
        if not url:
            return False, "ไม่มี place_id หรือ VIP link ที่ถูกต้อง", ""

        acc.server_type = server_type
        safe_url = re.sub(
            r"([?&](?:privateServerLinkCode|linkCode|code|accessCode|reservedServerAccessCode)=)[^&\s]+",
            r"\1<redacted>",
            url,
            flags=re.IGNORECASE,
        )
        flog(f"[LAUNCH] {acc.display_name} → {safe_url[:120]}")

        try:
            os.startfile(cls.LOGIN_WARMUP_URL)
            flog(f"[LAUNCH] Warmup home for {acc.display_name}")
            time.sleep(cls.LOGIN_WARMUP_DELAY)
        except Exception as e:
            flog(f"[LAUNCH] warmup startfile failed: {e}", "warning")

        try:
            os.startfile(url)
            acc.last_launch_at = time.time()
            return True, url, attempted_vip
        except Exception as e:
            flog(f"[LAUNCH] os.startfile failed: {e} — trying subprocess fallback", "warning")
            try:
                subprocess.Popen(
                    f'start "" "{url}"',
                    shell=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    stderr=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                )
                acc.last_launch_at = time.time()
                return True, url, attempted_vip
            except Exception as e2:
                flog(f"[LAUNCH] all methods failed for {acc.display_name}: {e2}", "warning")
                return False, str(e2), attempted_vip


# ─────────────────────────────────────────────────────────────────────────────
#  NETWORK MONITOR
# ─────────────────────────────────────────────────────────────────────────────
NET_ONLINE   = "ONLINE"
NET_DEGRADED = "DEGRADED"
NET_OFFLINE  = "OFFLINE"

class NetworkState:
    ONLINE   = NET_ONLINE
    DEGRADED = NET_DEGRADED
    OFFLINE  = NET_OFFLINE

CHECK_TARGETS = [
    "http://connectivitycheck.gstatic.com/generate_204",
    "http://www.msftncsi.com/ncsi.txt",
]
CHECK_ROBLOX = "https://www.roblox.com"

class NetworkMonitor:
    def __init__(self, bus: EventBus, interval: int = 5, debounce: int = 3,
                 stop: Optional[threading.Event] = None):
        self._bus         = bus
        self._interval    = interval
        self._debounce    = debounce
        self._stop        = stop or threading.Event()
        self._state       = NET_ONLINE
        self._state_since = time.time()
        self._lock        = threading.Lock()
        self._online_ev   = threading.Event()
        self._online_ev.set()
        self._thread      = threading.Thread(target=self._run, daemon=True, name="NetworkMonitor")

    def start(self):
        if not self._thread.is_alive():
            self._thread.start()

    def join(self, timeout: Optional[float] = None):
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def get_state(self) -> str:
        with self._lock:
            return self._state

    def is_online(self) -> bool:
        return self.get_state() == NET_ONLINE

    def wait_until_online(self, timeout: Optional[float] = None) -> bool:
        return self._online_ev.wait(timeout=timeout)

    def _check(self) -> str:
        internet_ok = False
        for url in CHECK_TARGETS:
            if self._ping(url, timeout=3):
                internet_ok = True
                break
        if not internet_ok:
            return NET_OFFLINE
        roblox_ok = self._ping(CHECK_ROBLOX, timeout=4)
        return NET_ONLINE if roblox_ok else NET_DEGRADED

    @staticmethod
    def _ping(url: str, timeout: int = 3) -> bool:
        try:
            req = urllib.request.Request(url, method="GET",
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                r.read(64)
            return True
        except Exception:
            return False

    def _run(self):
        first_run = True
        pending_state: Optional[str] = None
        pending_since: float = 0.0

        while not self._stop.is_set():
            new_state = self._check()
            with self._lock:
                current = self._state

            if new_state != current:
                if first_run:
                    with self._lock:
                        self._state = new_state
                        self._state_since = time.time()
                        if new_state == NET_ONLINE:
                            self._online_ev.set()
                        else:
                            self._online_ev.clear()
                    pending_state = None
                elif pending_state != new_state:
                    pending_state = new_state
                    pending_since = time.time()
                elif time.time() - pending_since >= self._debounce:
                    with self._lock:
                        self._state = new_state
                        self._state_since = time.time()
                        if new_state == NET_ONLINE:
                            self._online_ev.set()
                        else:
                            self._online_ev.clear()
                    flog(f"[NET] {current} → {new_state}")
                    self._bus.emit("network_state_change", old=current, new=new_state)
                    pending_state = None
            else:
                pending_state = None
                if first_run:
                    flog(f"[NET] Initial state confirmed: {new_state}")

            first_run = False
            self._stop.wait(timeout=self._interval)

    def status_dict(self) -> dict:
        with self._lock:
            return {"state": self._state, "since": self._state_since}
