from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import sqlite3
import time
from ctypes import wintypes
from typing import Any

from core import flog
from infra.app_paths import USER_DATA_ROOT
from services.cookie_artifact_ledger import CookieArtifactLedger


class IsolationManager:
    BASE_DIR = os.path.join(USER_DATA_ROOT, "instances")
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

        with open(local_state_path, encoding="utf-8") as f:
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

    @staticmethod
    def _read_json_object(
        path: str,
        encoding: str | None = None,
    ) -> dict[str, Any]:
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding=encoding) as f:
                return json.load(f)
        except Exception:
            return {}

    @classmethod
    def _write_json_target(
        cls,
        path: str,
        cookie: str,
        username: str,
        ledger: CookieArtifactLedger,
        encoding: str | None = None,
    ) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = cls._read_json_object(path, encoding)
        data[".ROBLOSECURITY"] = cookie
        with open(path, "w", encoding=encoding) as f:
            json.dump(data, f, indent=2)
        ledger.record_json_cookie(username, path, cookie)

    @classmethod
    def _inject_instance_json(
        cls,
        username: str,
        cookie: str,
        ledger: CookieArtifactLedger,
        written: list[str],
    ) -> None:
        instance = cls.setup(username)
        json_targets = [
            os.path.join(
                instance,
                "Roblox",
                "LocalStorage",
                "RobloxLocalStorage.json",
            ),
        ]
        for path in json_targets:
            try:
                cls._write_json_target(path, cookie, username, ledger)
                written.append(f"json:{path}")
            except Exception as e:
                flog(f"[ISO] json inject error: {e}", "warning")

    @classmethod
    def _inject_default_json(
        cls,
        local_appdata: str,
        username: str,
        cookie: str,
        ledger: CookieArtifactLedger,
        written: list[str],
    ) -> None:
        default_path = os.path.join(
            local_appdata,
            "Roblox",
            "LocalStorage",
            "RobloxLocalStorage.json",
        )
        try:
            cls._write_json_target(
                default_path,
                cookie,
                username,
                ledger,
            )
            written.append(f"localappdata:{default_path}")
        except Exception as e:
            flog(f"[ISO] localappdata inject error: {e}", "warning")

    @classmethod
    def _inject_store_json(
        cls,
        local_appdata: str,
        username: str,
        cookie: str,
        ledger: CookieArtifactLedger,
        written: list[str],
    ) -> None:
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
                cls._write_json_target(
                    store_path,
                    cookie,
                    username,
                    ledger,
                    encoding="utf-8",
                )
                written.append(f"store:{store_path}")
            except Exception as e:
                flog(
                    f"[ISO] store localstorage inject error: {e}",
                    "warning",
                )

    @staticmethod
    def _replace_webview_cookie(
        conn: sqlite3.Connection,
        encrypted_cookie: bytes,
        now_utc: int,
        expires_utc: int,
    ) -> None:
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

    @classmethod
    def _inject_webview2(
        cls,
        local_appdata: str,
        username: str,
        cookie: str,
        ledger: CookieArtifactLedger,
    ) -> str | None:
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
            os.makedirs(
                os.path.dirname(webview_cookie_db),
                exist_ok=True,
            )
            encrypted_cookie = cls._encrypt_webview2_cookie(
                cookie,
                local_state_path,
            )
            now_utc = cls._chrome_utc_now()
            expires_utc = now_utc + (400 * 24 * 60 * 60 * 1_000_000)
            conn = sqlite3.connect(webview_cookie_db, timeout=5)
            try:
                cls._replace_webview_cookie(
                    conn,
                    encrypted_cookie,
                    now_utc,
                    expires_utc,
                )
            finally:
                conn.close()
            ledger.record_artifact(
                username,
                "webview2",
                webview_cookie_db,
                cookie,
            )
            return webview_cookie_db
        except Exception as e:
            flog(f"[ISO] webview2 cookie inject error: {e}", "warning")
            return None

    @classmethod
    def _inject_local_appdata_targets(
        cls,
        username: str,
        cookie: str,
        ledger: CookieArtifactLedger,
        written: list[str],
    ) -> None:
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        if not local_appdata:
            return
        cls._inject_default_json(
            local_appdata,
            username,
            cookie,
            ledger,
            written,
        )
        cls._inject_store_json(
            local_appdata,
            username,
            cookie,
            ledger,
            written,
        )
        webview_target = cls._inject_webview2(
            local_appdata,
            username,
            cookie,
            ledger,
        )
        if webview_target:
            written.append(f"webview2:{webview_target}")

    @classmethod
    def _inject_registry(
        cls,
        username: str,
        cookie: str,
        ledger: CookieArtifactLedger,
    ) -> str | None:
        try:
            import winreg
        except ImportError:
            flog(
                "[ISO] winreg not available — skip registry inject",
                "warning",
            )
            return None
        except Exception as e:
            flog(f"[ISO] registry inject error: {e}", "warning")
            return None
        try:
            key_path = (
                r"Software\ROBLOX Corporation\Environments"
                r"\www.roblox.com\Global"
            )
            try:
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    key_path,
                    0,
                    winreg.KEY_SET_VALUE,
                )
            except FileNotFoundError:
                key = winreg.CreateKey(
                    winreg.HKEY_CURRENT_USER,
                    key_path,
                )
            with key:
                winreg.SetValueEx(
                    key,
                    ".ROBLOSECURITY",
                    0,
                    winreg.REG_SZ,
                    cookie,
                )
            ledger.record_artifact(
                username,
                "registry",
                (
                    r"HKCU\Software\ROBLOX Corporation\Environments"
                    r"\www.roblox.com\Global"
                ),
                cookie,
            )
            return r"registry:HKCU\...\www.roblox.com\Global"
        except Exception as e:
            flog(f"[ISO] registry inject error: {e}", "warning")
            return None

    @classmethod
    def inject_cookie(cls, username: str, cookie: str) -> tuple[bool, str]:
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
        ledger = CookieArtifactLedger()
        cls._inject_instance_json(username, cookie, ledger, written)
        cls._inject_local_appdata_targets(
            username,
            cookie,
            ledger,
            written,
        )
        registry_target = cls._inject_registry(
            username,
            cookie,
            ledger,
        )
        if registry_target:
            written.append(registry_target)

        if written:
            flog(
                f"[ISO] Cookie injected for {username}: "
                f"{len(written)} targets"
            )
            return True, f"Injected to {len(written)} targets"
        return False, "ไม่สามารถ inject cookie ได้"


# ─────────────────────────────────────────────────────────────────────────────
#  VIP ROTATION TRACKER
# ─────────────────────────────────────────────────────────────────────────────

__all__ = ["IsolationManager"]
