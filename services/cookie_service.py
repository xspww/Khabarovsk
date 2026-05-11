from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import sqlite3
import time
from ctypes import wintypes
from typing import Tuple

from core import flog

class IsolationManager:
    BASE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "roboguard_rt1_instances")
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

__all__ = ["IsolationManager"]
