from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

from app_paths import APP_DATA_DIR


COOKIE_ARTIFACT_LEDGER_FILE = os.path.join(APP_DATA_DIR, "cronus_cookie_artifacts.json")
COOKIE_ARTIFACT_LEDGER_SCHEMA = "cronus.cookie_artifacts.v1"
COOKIE_STORAGE_KEY = ".ROBLOSECURITY"
MAX_COOKIE_ARTIFACTS = 1000

_LEDGER_LOCK = threading.RLock()


def _flog(message: str, level: str = "info") -> None:
    try:
        from core import flog

        flog(message, level)
    except Exception:
        pass


def cookie_hash(cookie: str) -> str:
    return "sha256:" + hashlib.sha256(str(cookie or "").encode("utf-8")).hexdigest()


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(os.path.expandvars(str(path or "")))))


class CookieArtifactLedger:
    def __init__(self, path: str = COOKIE_ARTIFACT_LEDGER_FILE, max_artifacts: int = MAX_COOKIE_ARTIFACTS):
        self.path = path
        self.max_artifacts = max(1, int(max_artifacts or MAX_COOKIE_ARTIFACTS))

    def _read_artifacts(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:
            _flog(f"[COOKIE_LEDGER] load error: {exc}", "warning")
            return []
        if isinstance(payload, dict):
            artifacts = payload.get("artifacts", [])
        else:
            artifacts = payload
        if not isinstance(artifacts, list):
            return []
        return [item for item in artifacts if isinstance(item, dict)]

    def _write_artifacts(self, artifacts: List[Dict[str, Any]]) -> bool:
        parent = os.path.dirname(os.path.abspath(self.path))
        tmp_path = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
        payload = {
            "schema": COOKIE_ARTIFACT_LEDGER_SCHEMA,
            "updated_at": time.time(),
            "artifacts": artifacts[-self.max_artifacts :],
        }
        try:
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
            return True
        except Exception as exc:
            _flog(f"[COOKIE_LEDGER] save error: {exc}", "warning")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            return False

    @staticmethod
    def _target_key(artifact: Dict[str, Any]) -> tuple:
        return (
            str(artifact.get("target_type") or "").strip().lower(),
            _norm_path(str(artifact.get("path") or "")),
            str(artifact.get("key") or COOKIE_STORAGE_KEY).strip(),
        )

    def record_artifact(
        self,
        account: str,
        target_type: str,
        path: str,
        cookie: str,
        key: str = COOKIE_STORAGE_KEY,
    ) -> bool:
        account = str(account or "").strip()
        target_type = str(target_type or "").strip().lower()
        path = _norm_path(path)
        key = str(key or COOKIE_STORAGE_KEY).strip()
        cookie = str(cookie or "").strip()
        if not account or not target_type or not path or not cookie:
            return False

        artifact = {
            "account": account,
            "target_type": target_type,
            "path": path,
            "key": key,
            "cookie_hash": cookie_hash(cookie),
            "written_at": time.time(),
        }
        target_key = self._target_key(artifact)
        with _LEDGER_LOCK:
            artifacts = [
                item
                for item in self._read_artifacts()
                if self._target_key(item) != target_key
            ]
            artifacts.append(artifact)
            return self._write_artifacts(artifacts)

    def record_json_cookie(self, account: str, path: str, cookie: str) -> bool:
        return self.record_artifact(account, "json", path, cookie, COOKIE_STORAGE_KEY)

    def _write_json_artifact(self, path: str, payload: Dict[str, Any]) -> None:
        tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)

    def scrub_json_cookie_artifacts(self, account: Optional[str] = None) -> Dict[str, int]:
        account_key = str(account or "").strip().lower()
        summary = {"scrubbed": 0, "skipped": 0, "missing": 0}

        with _LEDGER_LOCK:
            artifacts = self._read_artifacts()
            remaining: List[Dict[str, Any]] = []
            for artifact in artifacts:
                if str(artifact.get("target_type") or "").strip().lower() != "json":
                    remaining.append(artifact)
                    continue
                artifact_account = str(artifact.get("account") or "").strip().lower()
                if account_key and artifact_account != account_key:
                    remaining.append(artifact)
                    continue

                path = str(artifact.get("path") or "").strip()
                key = str(artifact.get("key") or COOKIE_STORAGE_KEY).strip()
                expected_hash = str(artifact.get("cookie_hash") or "").strip()
                if not path or not os.path.exists(path):
                    summary["missing"] += 1
                    continue

                try:
                    with open(path, "r", encoding="utf-8") as handle:
                        payload = json.load(handle)
                except Exception as exc:
                    _flog(f"[COOKIE_LEDGER] scrub read error ({path}): {exc}", "warning")
                    summary["skipped"] += 1
                    remaining.append(artifact)
                    continue

                if not isinstance(payload, dict) or key not in payload:
                    summary["missing"] += 1
                    continue

                current_cookie = str(payload.get(key) or "")
                if cookie_hash(current_cookie) != expected_hash:
                    summary["skipped"] += 1
                    remaining.append(artifact)
                    continue

                cleaned = dict(payload)
                cleaned.pop(key, None)
                try:
                    self._write_json_artifact(path, cleaned)
                except Exception as exc:
                    _flog(f"[COOKIE_LEDGER] scrub write error ({path}): {exc}", "warning")
                    summary["skipped"] += 1
                    remaining.append(artifact)
                    continue
                summary["scrubbed"] += 1

            self._write_artifacts(remaining)
        return summary


__all__ = [
    "COOKIE_ARTIFACT_LEDGER_FILE",
    "COOKIE_STORAGE_KEY",
    "CookieArtifactLedger",
    "cookie_hash",
]
