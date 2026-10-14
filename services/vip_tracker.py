from __future__ import annotations

import random
import threading
import time

from core import flog


class VipTracker:
    BLACKLIST_DURATION = 600

    def __init__(self, links: list[str]):
        self._links     = list(links)
        self._scores:   dict[str, float]  = dict.fromkeys(links, 1.0)
        self._blacklist: dict[str, float] = {}
        self._lock      = threading.Lock()

    def pick(self) -> str | None:
        with self._lock:
            now = time.time()
            available = [lnk for lnk in self._links if self._blacklist.get(lnk, 0) < now]
            if not available:
                if self._blacklist:
                    available = [min(self._blacklist, key=self._blacklist.get)]
                else:
                    return None
            weights = [max(0.1, self._scores.get(lnk, 1.0)) for lnk in available]
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

    def status(self) -> list[dict]:
        with self._lock:
            now = time.time()
            return [
                {
                    "link":              lnk,
                    "score":             round(self._scores.get(lnk, 1.0), 2),
                    "blacklisted":       self._blacklist.get(lnk, 0) > now,
                    "blacklist_remaining": max(0, int(self._blacklist.get(lnk, 0) - now)),
                }
                for lnk in self._links
            ]


# ─────────────────────────────────────────────────────────────────────────────
#  PROCESS MANAGER
# ─────────────────────────────────────────────────────────────────────────────
ROBLOX_GAME_NAMES = {"robloxplayerbeta.exe"}
ROBLOX_NAMES = ROBLOX_GAME_NAMES | {"robloxplayer.exe", "roblox.exe"}

__all__ = ["VipTracker"]
