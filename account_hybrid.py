from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import secrets
import time
import webbrowser
from ctypes import wintypes
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app_paths import APP_DATA_DIR


ACCOUNT_DATA_FILE = os.path.join(APP_DATA_DIR, "AccountData.json")
ACCOUNT_AUDIT_FILE = os.path.join(APP_DATA_DIR, "account_tools_audit.jsonl")
PENDING_IMPORT_FILE = os.path.join(APP_DATA_DIR, "account_import_pending.json")

COOKIE_RE = re.compile(
    r"(_\|WARNING:-DO-NOT-SHARE-THIS\.[^\s'\"<>]+|_\|WARNING:[^\s'\"<>]+)",
    re.IGNORECASE,
)
LINK_CODE_RE = re.compile(r"((?:privateServerLinkCode|linkCode|accessCode)=)[^&\s]+", re.I)

ROBOGUARD_ENTROPY = b"RoboGuard Hybrid AccountData v1"
RAM_ENTROPY = bytes(
    [
        0x52,
        0x4F,
        0x42,
        0x4C,
        0x4F,
        0x58,
        0x20,
        0x41,
        0x43,
        0x43,
        0x4F,
        0x55,
        0x4E,
        0x54,
        0x20,
        0x4D,
        0x41,
        0x4E,
        0x41,
        0x47,
        0x45,
        0x52,
        0x20,
        0x7C,
        0x20,
        0x3A,
        0x29,
        0x20,
        0x7C,
        0x20,
        0x42,
        0x52,
        0x4F,
        0x55,
        0x47,
        0x48,
        0x54,
        0x20,
        0x54,
        0x4F,
        0x20,
        0x59,
        0x4F,
        0x55,
        0x20,
        0x42,
        0x55,
        0x59,
        0x20,
        0x69,
        0x63,
        0x33,
        0x77,
        0x30,
        0x6C,
        0x66,
    ]
)


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _make_blob(data: bytes) -> Tuple[DATA_BLOB, Any]:
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def dpapi_protect(data: bytes, entropy: bytes = ROBOGUARD_ENTROPY) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI is only available on Windows")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, in_buffer = _make_blob(data)
    entropy_blob, entropy_buffer = _make_blob(entropy or b"")
    out_blob = DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    keepalive = (in_buffer, entropy_buffer)
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)
        _ = keepalive


def dpapi_unprotect(data: bytes, entropy: bytes = ROBOGUARD_ENTROPY) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI is only available on Windows")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, in_buffer = _make_blob(data)
    entropy_blob, entropy_buffer = _make_blob(entropy or b"")
    out_blob = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    keepalive = (in_buffer, entropy_buffer)
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)
        _ = keepalive


def encrypt_cookie(cookie: str) -> str:
    cookie = str(cookie or "").strip()
    if not cookie:
        return ""
    protected = dpapi_protect(cookie.encode("utf-8"), ROBOGUARD_ENTROPY)
    return "dpapi:v1:" + base64.b64encode(protected).decode("ascii")


def decrypt_cookie(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if value.startswith("dpapi:v1:"):
        raw = base64.b64decode(value.split(":", 2)[2].encode("ascii"))
        return dpapi_unprotect(raw, ROBOGUARD_ENTROPY).decode("utf-8", errors="replace")
    if value.startswith("_|WARNING:"):
        return value
    return ""


def redact_secret(value: Any) -> Any:
    text = str(value or "")
    text = COOKIE_RE.sub("[ROBLOX_COOKIE_REDACTED]", text)
    text = LINK_CODE_RE.sub(r"\1[REDACTED]", text)
    return text


def _short_hash(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _normalize_owned_private_servers(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    clean: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        server = {
            "private_server_id": str(
                item.get("private_server_id")
                or item.get("privateServerId")
                or item.get("vipServerId")
                or item.get("id")
                or ""
            ).strip(),
            "owner_user_id": str(item.get("owner_user_id") or item.get("ownerId") or item.get("ownerUserId") or "").strip(),
            "place_id": str(item.get("place_id") or item.get("placeId") or "").strip(),
            "universe_id": str(item.get("universe_id") or item.get("universeId") or "").strip(),
            "name": str(item.get("name") or "").strip(),
            "active": bool(item.get("active", True)),
            "link": str(item.get("link") or item.get("join_link") or item.get("joinLink") or "").strip(),
            "join_code": str(item.get("join_code") or item.get("joinCode") or item.get("privateServerLinkCode") or "").strip(),
            "access_code": str(item.get("access_code") or item.get("accessCode") or "").strip(),
            "source": str(item.get("source") or "").strip(),
            "status": str(item.get("status") or "").strip(),
            "error": str(item.get("error") or "").strip(),
            "synced_at": float(item.get("synced_at") or item.get("syncedAt") or 0.0),
        }
        if (
            server["private_server_id"]
            or server["link"]
            or server["join_code"]
            or server["access_code"]
            or server["status"]
            or server["error"]
        ):
            clean.append(server)
    return clean


def _public_owned_private_servers(value: Any) -> List[Dict[str, Any]]:
    public: List[Dict[str, Any]] = []
    for item in _normalize_owned_private_servers(value):
        link = str(item.get("link") or "")
        join_code = str(item.get("join_code") or "")
        access_code = str(item.get("access_code") or "")
        public.append(
            {
                "private_server_id": item.get("private_server_id", ""),
                "owner_user_id": item.get("owner_user_id", ""),
                "place_id": item.get("place_id", ""),
                "universe_id": item.get("universe_id", ""),
                "name": item.get("name", ""),
                "active": bool(item.get("active", False)),
                "source": item.get("source", ""),
                "status": item.get("status", ""),
                "error": item.get("error", ""),
                "synced_at": item.get("synced_at", 0.0),
                "link_present": bool(link),
                "join_code_present": bool(join_code),
                "access_code_present": bool(access_code),
                "link_hash": _short_hash(link),
                "join_code_hash": _short_hash(join_code),
                "access_code_hash": _short_hash(access_code),
            }
        )
    return public


def _redact_vip_links_for_api(value: Any) -> Tuple[List[str], List[str]]:
    if isinstance(value, str):
        links = [line.strip() for line in value.splitlines() if line.strip()]
    elif isinstance(value, list):
        links = [str(link).strip() for link in value if str(link).strip()]
    else:
        links = []
    return [redact_secret(link) for link in links], [_short_hash(link) for link in links]


def _merge_redacted_vip_links(new_value: Any, old_value: Any) -> Any:
    if isinstance(new_value, str):
        new_links = [line.strip() for line in new_value.splitlines() if line.strip()]
    elif isinstance(new_value, list):
        new_links = [str(link).strip() for link in new_value if str(link).strip()]
    else:
        return new_value
    old_links = [str(link).strip() for link in (old_value or []) if str(link).strip()] if isinstance(old_value, list) else []
    if not any("[REDACTED]" in link for link in new_links):
        return new_value
    if not old_links:
        return [link for link in new_links if "[REDACTED]" not in link]
    merged: List[str] = []
    for idx, link in enumerate(new_links):
        if "[REDACTED]" in link and idx < len(old_links):
            merged.append(old_links[idx])
        elif "[REDACTED]" not in link:
            merged.append(link)
    if not merged and old_links:
        return old_links
    return merged


def parse_cookie_line(line: str) -> Tuple[str, str]:
    text = str(line or "").strip()
    if not text:
        return "", ""
    match = COOKIE_RE.search(text)
    if match:
        before = text[: match.start()].strip(": \t")
        username = before.split(":", 1)[0].strip() if before else ""
        return username, match.group(1).strip()
    parts = text.split(":")
    if len(parts) >= 3 and parts[2].strip().startswith("_|WARNING:"):
        return parts[0].strip(), ":".join(parts[2:]).strip()
    if text.startswith("_|WARNING:"):
        return "", text
    return "", ""


def parse_userpass_line(line: str) -> Tuple[str, str]:
    text = str(line or "").strip()
    if not text:
        return "", ""
    parts = text.split(":", 1)
    username = parts[0].strip()
    password = parts[1].strip() if len(parts) > 1 else ""
    return username, password


class AccountDataStore:
    def __init__(self, path: str = ACCOUNT_DATA_FILE):
        self.path = path

    def _read_bytes(self) -> bytes:
        if not os.path.exists(self.path):
            return b""
        with open(self.path, "rb") as f:
            return f.read()

    @staticmethod
    def _json_from_bytes(data: bytes) -> Any:
        return json.loads(data.decode("utf-8-sig"))

    @classmethod
    def decode_account_file_bytes(cls, data: bytes) -> List[Dict[str, Any]]:
        if not data:
            return []
        stripped = data.lstrip()
        if stripped.startswith(b"{") or stripped.startswith(b"["):
            raw = cls._json_from_bytes(data)
        else:
            last_error: Optional[Exception] = None
            raw = None
            for entropy in (ROBOGUARD_ENTROPY, RAM_ENTROPY, b""):
                try:
                    raw = cls._json_from_bytes(dpapi_unprotect(data, entropy))
                    break
                except Exception as exc:
                    last_error = exc
            if raw is None:
                raise ValueError(f"Unsupported or password-locked AccountData.json: {last_error}")
        if isinstance(raw, dict):
            accounts = raw.get("accounts", raw.get("Accounts", []))
        else:
            accounts = raw
        if not isinstance(accounts, list):
            raise ValueError("AccountData must contain an account array")
        return [cls.normalize_record(item) for item in accounts if isinstance(item, dict)]

    def read_records(self, include_cookies: bool = False) -> List[Dict[str, Any]]:
        records = self.decode_account_file_bytes(self._read_bytes())
        if include_cookies:
            for record in records:
                record["cookie"] = self.get_cookie_from_record(record)
        return records

    def write_records(self, records: Iterable[Dict[str, Any]]) -> None:
        clean = [self.normalize_record(item) for item in records if isinstance(item, dict)]
        payload = {
            "schema": "roboguard.accountdata.v1",
            "updated_at": time.time(),
            "accounts": clean,
        }
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = f"{self.path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.path)

    @staticmethod
    def get_cookie_from_record(record: Dict[str, Any]) -> str:
        encrypted = str(record.get("encrypted_cookie") or record.get("EncryptedCookie") or "").strip()
        if encrypted:
            return decrypt_cookie(encrypted)
        cookie = str(record.get("cookie") or record.get("SecurityToken") or record.get("securityToken") or "").strip()
        if cookie.startswith("_|WARNING:"):
            return cookie
        return ""

    @classmethod
    def normalize_record(cls, item: Dict[str, Any]) -> Dict[str, Any]:
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else item.get("Fields")
        if not isinstance(fields, dict):
            fields = {}

        def pick(*keys: str) -> Any:
            for key in keys:
                value = item.get(key)
                if value not in (None, ""):
                    return value
            for key in keys:
                value = fields.get(key)
                if value not in (None, ""):
                    return value
            return ""

        username = str(pick("username", "Username", "Account", "account")).strip()
        cookie = cls.get_cookie_from_record(item)
        encrypted_cookie = str(item.get("encrypted_cookie") or item.get("EncryptedCookie") or "").strip()
        if cookie and not encrypted_cookie:
            encrypted_cookie = encrypt_cookie(cookie)

        vip_links = item.get("vip_links", item.get("VipLinks", item.get("vipLinks", [])))
        if isinstance(vip_links, str):
            vip_links = [line.strip() for line in vip_links.splitlines() if line.strip()]
        if not isinstance(vip_links, list):
            vip_links = []
        owned_private_servers = _normalize_owned_private_servers(
            item.get("owned_private_servers", item.get("ownedPrivateServers", item.get("private_servers", [])))
        )

        job_id = str(pick("job_id", "JobId", "jobId", "SavedJobId")).strip()
        if job_id.startswith("http") and job_id not in vip_links:
            vip_links.append(job_id)

        browser_tracker_id = str(
            pick("browser_tracker_id", "BrowserTrackerID", "BrowserTrackerId", "browserTrackerId")
        ).strip()
        if not browser_tracker_id:
            browser_tracker_id = str(secrets.randbelow(75_000) + 100_000) + str(secrets.randbelow(800_000) + 100_000)

        normalized_fields = {str(k): str(v) for k, v in fields.items() if v not in (None, "")}
        def pick_bool(*keys: str) -> bool:
            value = pick(*keys)
            if isinstance(value, bool):
                return value
            return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}

        return {
            "username": username,
            "user_id": str(pick("user_id", "UserID", "userId")).strip(),
            "alias": str(pick("alias", "Alias")).strip(),
            "group": str(pick("group", "Group") or "Default").strip() or "Default",
            "description": str(pick("description", "Description")).strip(),
            "cookie_username": str(pick("cookie_username", "CookieUsername", "cookieUsername")).strip(),
            "cookie_user_id": str(pick("cookie_user_id", "CookieUserID", "cookieUserId")).strip(),
            "cookie_mismatch": pick_bool("cookie_mismatch", "CookieMismatch", "cookieMismatch"),
            "encrypted_cookie": encrypted_cookie,
            "place_id": str(pick("place_id", "PlaceId", "placeId", "SavedPlaceId")).strip(),
            "job_id": job_id,
            "vip_links": [str(link).strip() for link in vip_links if str(link).strip()],
            "owned_private_servers": owned_private_servers,
            "browser_tracker_id": browser_tracker_id,
            "last_use": float(item.get("last_use") or item.get("LastUse") or 0.0),
            "last_cookie_refresh": float(item.get("last_cookie_refresh") or item.get("LastAttemptedRefresh") or 0.0),
            "priority": int(float(item.get("priority") or item.get("Priority") or 50)),
            "manual_status": str(item.get("manual_status") or "").strip(),
            "finished_at": float(item.get("finished_at") or 0.0),
            "fields": normalized_fields,
            "import_status": str(item.get("import_status") or "").strip(),
        }

    @classmethod
    def to_api_record(cls, record: Dict[str, Any], include_cookie: bool = False) -> Dict[str, Any]:
        out = dict(cls.normalize_record(record))
        cookie = cls.get_cookie_from_record(record)
        out["cookie_present"] = bool(cookie)
        redacted_vip_links, vip_link_hashes = _redact_vip_links_for_api(out.get("vip_links", []))
        out["vip_links"] = redacted_vip_links
        out["vip_link_hashes"] = vip_link_hashes
        out["vip_links_count"] = len(vip_link_hashes)
        out["owned_private_servers"] = _public_owned_private_servers(out.get("owned_private_servers", []))
        out.pop("encrypted_cookie", None)
        if include_cookie:
            out["cookie"] = cookie
        return out

    @classmethod
    def to_roboguard_account(cls, record: Dict[str, Any]) -> Dict[str, Any]:
        normalized = cls.normalize_record(record)
        return {
            "username": normalized["username"],
            "user_id": normalized["user_id"],
            "priority": normalized["priority"],
            "place_id": normalized["place_id"],
            "vip_links": list(normalized["vip_links"]),
            "owned_private_servers": list(normalized.get("owned_private_servers") or []),
            "alias": normalized["alias"],
            "cookie": cls.get_cookie_from_record(normalized),
            "browser_tracker_id": normalized["browser_tracker_id"],
            "cookie_username": normalized["cookie_username"],
            "cookie_user_id": normalized["cookie_user_id"],
            "cookie_mismatch": normalized["cookie_mismatch"],
            "description": normalized["description"],
            "manual_status": normalized["manual_status"],
            "finished_at": normalized["finished_at"],
        }

    def to_roboguard_accounts(self) -> List[Dict[str, Any]]:
        return [self.to_roboguard_account(record) for record in self.read_records(include_cookies=True)]

    def ensure_from_legacy(self, legacy_accounts: Iterable[Dict[str, Any]]) -> None:
        if os.path.exists(self.path) and self.read_records():
            return
        records = [self.normalize_record(item) for item in legacy_accounts if isinstance(item, dict)]
        if records:
            self.write_records(records)

    def replace_from_roboguard_payload(self, payload: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        existing = {
            str(record.get("username") or "").strip().lower(): record
            for record in self.read_records(include_cookies=False)
            if str(record.get("username") or "").strip()
        }
        records: List[Dict[str, Any]] = []
        for item in payload:
            data = dict(item)
            username = str(data.get("username") or data.get("Username") or "").strip()
            old = existing.get(username.lower(), {})
            if "cookie" not in data and old:
                data["encrypted_cookie"] = old.get("encrypted_cookie", "")
            if not data.get("browser_tracker_id") and old:
                data["browser_tracker_id"] = old.get("browser_tracker_id", "")
            if not data.get("fields") and old:
                data["fields"] = old.get("fields", {})
            if old and "vip_links" in data:
                data["vip_links"] = _merge_redacted_vip_links(data.get("vip_links"), old.get("vip_links", []))
            records.append(self.normalize_record(data))
        self.write_records(records)
        return records

    def upsert_records(self, new_records: Iterable[Dict[str, Any]]) -> Tuple[int, List[Dict[str, Any]]]:
        merged = {
            str(record.get("username") or "").strip().lower(): record
            for record in self.read_records(include_cookies=False)
            if str(record.get("username") or "").strip()
        }
        changed = 0
        for raw in new_records:
            record = self.normalize_record(raw)
            key = str(record.get("username") or "").strip().lower()
            if not key:
                continue
            old = merged.get(key, {})
            if not record.get("encrypted_cookie") and old.get("encrypted_cookie"):
                record["encrypted_cookie"] = old["encrypted_cookie"]
            merged[key] = {**old, **record}
            changed += 1
        records = list(merged.values())
        self.write_records(records)
        return changed, records

    def import_cookie_lines(self, lines: Iterable[str], validator=None) -> Dict[str, Any]:
        records: List[Dict[str, Any]] = []
        errors: List[str] = []
        for line in lines:
            username, cookie = parse_cookie_line(line)
            if not cookie:
                continue
            cookie_username = ""
            cookie_user_id = ""
            cookie_mismatch = False
            if validator:
                try:
                    validation = validator(cookie)
                    ok, validated, detail = validation[:3]
                    if len(validation) >= 4 and isinstance(validation[3], dict):
                        cookie_user_id = str(validation[3].get("user_id") or "")
                        cookie_username = str(validation[3].get("username") or validated or "")
                    else:
                        cookie_username = str(validated or "")
                    if ok:
                        if not username:
                            username = str(validated or "")
                        elif cookie_username and username.strip().lower() != cookie_username.strip().lower():
                            cookie_mismatch = True
                            errors.append(f"cookie belongs to {cookie_username}, not {username}")
                    else:
                        errors.append(detail)
                        continue
                except Exception as exc:
                    errors.append(str(exc))
                    continue
            if not username:
                errors.append("cookie line missing username")
                continue
            records.append(
                {
                    "username": username,
                    "cookie": cookie,
                    "cookie_username": cookie_username,
                    "cookie_user_id": cookie_user_id,
                    "cookie_mismatch": cookie_mismatch,
                    "import_status": "cookie_mismatch" if cookie_mismatch else "",
                }
            )
        changed, merged = self.upsert_records(records)
        return {"ok": True, "imported": changed, "errors": errors, "count": len(merged)}

    def import_userpass_lines(self, lines: Iterable[str], open_browser: bool = True) -> Dict[str, Any]:
        pending = self.load_pending_imports()
        added = 0
        for line in lines:
            username, password = parse_userpass_line(line)
            if not username:
                continue
            pending_id = secrets.token_hex(8)
            pending.append(
                {
                    "id": pending_id,
                    "username": username,
                    "password_present": bool(password),
                    "created_at": time.time(),
                    "status": "browser_login_required",
                }
            )
            added += 1
        self.write_pending_imports(pending)
        if added and open_browser:
            try:
                webbrowser.open("https://www.roblox.com/login", new=2)
            except Exception:
                pass
        return {
            "ok": True,
            "pending": added,
            "msg": "Browser login opened; paste/import .ROBLOSECURITY after Roblox login completes.",
        }

    @staticmethod
    def load_pending_imports() -> List[Dict[str, Any]]:
        if not os.path.exists(PENDING_IMPORT_FILE):
            return []
        try:
            with open(PENDING_IMPORT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    @staticmethod
    def write_pending_imports(items: List[Dict[str, Any]]) -> None:
        tmp_path = f"{PENDING_IMPORT_FILE}.{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, PENDING_IMPORT_FILE)

    def update_record(self, username: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        records = self.read_records(include_cookies=False)
        key = str(username or "").strip().lower()
        updated: Optional[Dict[str, Any]] = None
        for idx, record in enumerate(records):
            if str(record.get("username") or "").strip().lower() == key:
                records[idx] = self.normalize_record({**record, **updates})
                updated = records[idx]
                break
        if updated is not None:
            self.write_records(records)
        return updated


def audit_event(action: str, username: str = "", ok: bool = True, **fields: Any) -> None:
    event = {
        "ts": round(time.time(), 3),
        "action": str(action or ""),
        "username": str(username or ""),
        "ok": bool(ok),
    }
    for key, value in fields.items():
        event[str(key)] = redact_secret(value)
    with open(ACCOUNT_AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


ACCOUNT_STORE = AccountDataStore()
