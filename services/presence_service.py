from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Tuple


PRESENCE_ENDPOINT = "https://presence.roblox.com/v1/presence/users"
PRESENCE_TYPE_NAMES = {
    0: "Offline",
    1: "Online",
    2: "InGame",
    3: "InStudio",
    4: "Invisible",
}


def normalize_presence(item: Dict[str, Any], fetched_at: float | None = None) -> Dict[str, Any]:
    ts = time.time() if fetched_at is None else float(fetched_at)
    user_id = str(item.get("userId") or item.get("user_id") or "").strip()
    try:
        presence_type = int(item.get("userPresenceType") if item.get("userPresenceType") is not None else -1)
    except Exception:
        presence_type = -1
    return {
        "user_id": user_id,
        "presence_type": presence_type,
        "presence_type_name": PRESENCE_TYPE_NAMES.get(presence_type, "Unknown"),
        "presence_place_id": str(item.get("placeId") or ""),
        "presence_root_place_id": str(item.get("rootPlaceId") or ""),
        "presence_universe_id": str(item.get("universeId") or ""),
        "presence_game_id": str(item.get("gameId") or ""),
        "presence_game_id_present": bool(item.get("gameId")),
        "presence_last_location": str(item.get("lastLocation") or ""),
        "presence_last_online": str(item.get("lastOnline") or ""),
        "presence_fetched_at": ts,
        "presence_limited": presence_type == 2 and not (item.get("placeId") or item.get("rootPlaceId") or item.get("gameId")),
    }


class RobloxPresenceService:
    def __init__(self):
        self._lock = threading.RLock()
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._last_poll_at = 0.0
        self._last_error = ""
        self._last_status_code = 0
        self._backoff_until = 0.0
        self._last_requested = 0
        self._last_skipped = 0

    @staticmethod
    def normalize_user_ids(user_ids: Iterable[Any]) -> Tuple[List[str], int]:
        out: List[str] = []
        seen = set()
        skipped = 0
        for value in user_ids:
            text = str(value or "").strip()
            if not text or not text.isdigit():
                skipped += 1
                continue
            if text in seen:
                continue
            seen.add(text)
            out.append(text)
        return out, skipped

    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            return {
                "cached_users": len(self._cache),
                "last_poll_at": self._last_poll_at,
                "last_poll_age_seconds": round(max(0.0, now - self._last_poll_at), 1) if self._last_poll_at else None,
                "last_error": self._last_error,
                "last_status_code": self._last_status_code,
                "backoff_until": self._backoff_until,
                "backoff_seconds": round(max(0.0, self._backoff_until - now), 1),
                "rate_limited": self._backoff_until > now,
                "last_requested": self._last_requested,
                "last_skipped": self._last_skipped,
            }

    def get_cached(self, user_id: Any) -> Dict[str, Any]:
        key = str(user_id or "").strip()
        if not key:
            return {}
        with self._lock:
            item = dict(self._cache.get(key) or {})
        if item:
            item["presence_age_seconds"] = round(max(0.0, time.time() - float(item.get("presence_fetched_at") or 0.0)), 1)
        return item

    def refresh(
        self,
        user_ids: Iterable[Any],
        *,
        enabled: bool = True,
        poll_interval: float = 30.0,
        cache_ttl: float = 30.0,
        force: bool = False,
    ) -> Dict[str, Any]:
        ids, skipped = self.normalize_user_ids(user_ids)
        now = time.time()
        with self._lock:
            self._last_skipped = skipped
            if not enabled:
                return {"ok": True, "enabled": False, "presences": {}, "skipped": skipped, **self.snapshot()}
            if self._backoff_until > now:
                return {
                    "ok": False,
                    "enabled": True,
                    "presences": self._filtered_cache(ids),
                    "skipped": skipped,
                    "msg": "presence API is in backoff",
                    **self.snapshot(),
                }
            if not ids:
                return {"ok": True, "enabled": True, "presences": {}, "skipped": skipped, **self.snapshot()}
            stale = [
                uid for uid in ids
                if not self._cache.get(uid)
                or (now - float(self._cache[uid].get("presence_fetched_at") or 0.0)) > float(cache_ttl or 30.0)
            ]
            if not force and (not stale or (now - self._last_poll_at) < float(poll_interval or 30.0)):
                return {"ok": True, "enabled": True, "presences": self._filtered_cache(ids), "skipped": skipped, **self.snapshot()}

        requested = ids[:100]
        try:
            payload = json.dumps({"userIds": [int(uid) for uid in requested]}).encode("utf-8")
            req = urllib.request.Request(
                PRESENCE_ENDPOINT,
                data=payload,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "ArgusLauncher/Presence",
                },
            )
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                status_code = int(resp.status)
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            status_code = int(getattr(exc, "code", 0) or 0)
            with self._lock:
                self._last_poll_at = now
                self._last_status_code = status_code
                self._last_error = f"HTTP {status_code}"
                self._last_requested = len(requested)
                if status_code == 429:
                    self._backoff_until = now + 60.0
            return {"ok": False, "enabled": True, "presences": self.get_many(ids), "skipped": skipped, "msg": f"HTTP {status_code}", **self.snapshot()}
        except Exception as exc:
            with self._lock:
                self._last_poll_at = now
                self._last_status_code = 0
                self._last_error = str(exc)
                self._last_requested = len(requested)
            return {"ok": False, "enabled": True, "presences": self.get_many(ids), "skipped": skipped, "msg": str(exc), **self.snapshot()}

        try:
            data = json.loads(body)
            items = data.get("userPresences") if isinstance(data, dict) else []
            if not isinstance(items, list):
                items = []
        except Exception as exc:
            with self._lock:
                self._last_poll_at = now
                self._last_status_code = status_code
                self._last_error = str(exc)
                self._last_requested = len(requested)
            return {"ok": False, "enabled": True, "presences": self.get_many(ids), "skipped": skipped, "msg": str(exc), **self.snapshot()}

        fetched_at = time.time()
        with self._lock:
            for item in items:
                if isinstance(item, dict):
                    normalized = normalize_presence(item, fetched_at=fetched_at)
                    uid = normalized.get("user_id")
                    if uid:
                        self._cache[uid] = normalized
            self._last_poll_at = fetched_at
            self._last_status_code = status_code
            self._last_error = ""
            self._last_requested = len(requested)
            self._backoff_until = 0.0
            presences = self._filtered_cache(ids)
        return {"ok": True, "enabled": True, "presences": presences, "skipped": skipped, **self.snapshot()}

    def _filtered_cache(self, ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        now = time.time()
        return {
            uid: {**item, "presence_age_seconds": round(max(0.0, now - float(item.get("presence_fetched_at") or 0.0)), 1)}
            for uid in ids
            for item in [self._cache.get(str(uid))]
            if item
        }

    def get_many(self, ids: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
        normalized, _skipped = self.normalize_user_ids(ids)
        with self._lock:
            return self._filtered_cache(normalized)


PRESENCE_SERVICE = RobloxPresenceService()
