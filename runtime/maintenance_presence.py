from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from core import Account, flog_kv


def _account_presence_user_id(acc: Account) -> str:
    return str(getattr(acc, "user_id", "") or getattr(acc, "cookie_user_id", "") or "").strip()


class MaintenancePresenceMixin:
    def _presence_disconnect_reason(
        self,
        acc: Account,
        now: float,
        in_game_for: float,
        loading_grace: float,
    ) -> Tuple[str, Dict[str, Any]]:
        return "", {}

    def _reset_presence_mismatch(self, acc: Account, reason: str = "") -> None:
        with acc._lock:
            had_mismatch = bool(acc.presence_mismatch_since)
            acc.presence_mismatch_since = 0.0
            acc.presence_mismatch_status = ""
            acc.presence_mismatch_reason = ""
            acc.presence_rejoin_pending_clear = False
            acc.presence_rejoin_suppressed_until = 0.0
        if had_mismatch:
            flog_kv(
                "PRESENCE",
                "presence_assist_disabled_clear",
                account=acc.display_name,
                reason=reason or "presence_assist_disabled",
            )

    def _handle_presence_disconnect_assist(
        self,
        acc: Account,
        worker: Optional[Any],
        now: float,
        pid: int,
        in_game_for: float,
        loading_grace: float,
        allow_rejoin: bool = True,
    ) -> bool:
        self._reset_presence_mismatch(acc, "presence_assist_disabled")
        return False
