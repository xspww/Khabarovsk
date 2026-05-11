from __future__ import annotations

import random
import threading
import time
from typing import Dict, List, Optional

from core import flog

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

__all__ = ["VipTracker"]
