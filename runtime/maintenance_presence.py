from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

from core import Account, flog_kv
from services.presence_service import PRESENCE_SERVICE


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
        if not bool(self._cfg.get("presence_api_enabled", False)):
            return "", {}
        if not bool(self._cfg.get("presence_assist_rejoin_enabled", True)):
            return "", {}
        if not bool(self._cfg.get("connection_error_rejoin", True)):
            return "", {}
        uid = _account_presence_user_id(acc)
        if not uid:
            return "", {}
        poll_interval = float(self._cfg.get("presence_poll_interval_seconds", 30) or 30)
        cache_ttl = float(self._cfg.get("presence_cache_ttl_seconds", 30) or 30)
        hold = max(1.0, float(self._cfg.get("connection_error_hold_time", 3) or 3))
        launch_grace = max(20.0, min(float(loading_grace or 90.0), poll_interval + hold))
        if in_game_for < launch_grace:
            return "", {}
        result = PRESENCE_SERVICE.refresh(
            [uid],
            enabled=True,
            poll_interval=poll_interval,
            cache_ttl=cache_ttl,
            force=False,
        )
        presence = (result.get("presences") or {}).get(uid) or PRESENCE_SERVICE.get_cached(uid)
        if not presence:
            return "", {}
        try:
            presence_type = int(presence.get("presence_type") if presence.get("presence_type") is not None else -1)
        except Exception:
            presence_type = -1
        fetched_at = float(presence.get("presence_fetched_at") or 0.0)
        presence_age = float(presence.get("presence_age_seconds") if presence.get("presence_age_seconds") is not None else max(0.0, now - fetched_at))
        if fetched_at and presence_age > max(cache_ttl + poll_interval + 5.0, 45.0):
            return "", presence

        with acc._lock:
            expected_places = {
                str(acc.place_id or "").strip(),
                str((acc.launch_intent or {}).get("place_id") or "").strip(),
                str((acc.launch_intent_summary or {}).get("place_id") or "").strip(),
            }
        expected_places.discard("")

        if presence_type == 2:
            observed_places = {
                str(presence.get("presence_place_id") or "").strip(),
                str(presence.get("presence_root_place_id") or "").strip(),
            }
            observed_places.discard("")
            if not observed_places:
                return "", presence
            if expected_places and not observed_places.intersection(expected_places):
                return "presence_place_mismatch", presence
            return "", presence
        if presence_type in {0, 1, 3, 4}:
            return f"presence_not_ingame:{presence.get('presence_type_name') or presence_type}", presence
        return "", presence

    def _reset_presence_mismatch(self, acc: Account, reason: str = "") -> None:
        with acc._lock:
            had_mismatch = bool(acc.presence_mismatch_since)
            acc.presence_mismatch_since = 0.0
            acc.presence_mismatch_status = ""
            acc.presence_mismatch_reason = ""
        if had_mismatch:
            flog_kv("PRESENCE", "presence_disconnect_cleared", account=acc.display_name, reason=reason or "presence_recovered")

    def _handle_presence_disconnect_assist(
        self,
        acc: Account,
        worker: Optional[AccountWorker],
        now: float,
        pid: int,
        in_game_for: float,
        loading_grace: float,
        allow_rejoin: bool = True,
    ) -> bool:
        reason, presence = self._presence_disconnect_reason(acc, now, in_game_for, loading_grace)
        if not reason:
            try:
                presence_type = int(presence.get("presence_type") if presence.get("presence_type") is not None else -1) if presence else -1
            except Exception:
                presence_type = -1
            if presence_type == 2:
                with acc._lock:
                    acc.presence_rejoin_pending_clear = False
                    acc.presence_rejoin_suppressed_until = 0.0
            self._reset_presence_mismatch(acc, "presence_ingame_or_unavailable")
            return False
        if not allow_rejoin:
            with acc._lock:
                if not acc.presence_mismatch_since:
                    acc.presence_mismatch_since = now
                acc.presence_mismatch_status = str(presence.get("presence_type_name") or "")
                acc.presence_mismatch_reason = reason
                acc.last_watchdog_classification = "presence_mismatch_observed"
                acc.last_activity_reason = f"presence_observed:{reason}"
            flog_kv(
                "PRESENCE",
                "presence_mismatch_observed",
                "warning",
                account=acc.display_name,
                pid=pid,
                reason=reason,
                presence_type=presence.get("presence_type_name", ""),
                last_location=presence.get("presence_last_location", ""),
                action="hold_local_process_alive",
            )
            return False
        hold = max(1.0, float(self._cfg.get("connection_error_hold_time", 3) or 3))
        default_cooldown = max(10.0, hold * 2.0)
        rejoin_cooldown = max(5.0, float(self._cfg.get("presence_rejoin_cooldown_seconds", default_cooldown) or default_cooldown))
        with acc._lock:
            suppressed_until = float(getattr(acc, "presence_rejoin_suppressed_until", 0.0) or 0.0)
            if suppressed_until > now:
                acc.presence_mismatch_since = acc.presence_mismatch_since or now
                acc.presence_mismatch_status = str(presence.get("presence_type_name") or "")
                acc.presence_mismatch_reason = reason
                acc.liveness_state = "reconnecting"
                acc.last_watchdog_classification = "presence_disconnect_suppressed"
                acc.last_activity_reason = f"presence_suppressed:{reason}"
                remaining = suppressed_until - now
                flog_kv(
                    "PRESENCE",
                    "presence_disconnect_suppressed",
                    "warning",
                    account=acc.display_name,
                    pid=pid,
                    reason=reason,
                    presence_type=presence.get("presence_type_name", ""),
                    last_location=presence.get("presence_last_location", ""),
                    remaining=f"{remaining:.1f}",
                )
                return False
            if not acc.presence_mismatch_since:
                acc.presence_mismatch_since = now
                acc.presence_mismatch_status = str(presence.get("presence_type_name") or "")
                acc.presence_mismatch_reason = reason
                acc.liveness_state = "reconnecting"
                acc.last_watchdog_classification = "presence_not_ingame_hold"
                acc.last_activity_reason = f"presence:{reason}"
                mismatch_for = 0.0
            else:
                mismatch_for = now - float(acc.presence_mismatch_since or now)
            runtime_generation = acc.runtime_generation
            session_id = acc.session_id
            launch_nonce = acc.launch_nonce
            transaction_id = acc.rejoin_transaction_id
        if mismatch_for < hold:
            flog_kv(
                "PRESENCE",
                "presence_disconnect_hold",
                "warning",
                account=acc.display_name,
                pid=pid,
                reason=reason,
                hold=f"{hold:.1f}",
                mismatch_for=f"{mismatch_for:.1f}",
                presence_type=presence.get("presence_type_name", ""),
                last_location=presence.get("presence_last_location", ""),
            )
            return False

        with acc._lock:
            acc.presence_mismatch_since = 0.0
            acc.presence_mismatch_status = str(presence.get("presence_type_name") or "")
            acc.presence_mismatch_reason = reason
            acc.last_presence_rejoin_at = now
            acc.presence_rejoin_suppressed_until = now + rejoin_cooldown
            acc.presence_rejoin_pending_clear = True
            acc.liveness_state = "presence_disconnected"
            acc.last_watchdog_classification = "presence_disconnected"
            acc.last_activity_reason = f"presence:{reason}"
        flog_kv(
            "PRESENCE",
            "presence_disconnect_rejoin_signal",
            "warning",
            account=acc.display_name,
            pid=pid,
            reason=reason,
            presence_type=presence.get("presence_type_name", ""),
            last_location=presence.get("presence_last_location", ""),
            mismatch_for=f"{mismatch_for:.1f}",
            runtime_generation=runtime_generation,
            session_id=session_id,
            transaction_id=transaction_id,
        )
        if self._supervisor:
            self._supervisor.emit(
                "WatchdogSupervisor",
                "PRESENCE_DISCONNECT",
                account=acc,
                severity="warning",
                reason="connection_error",
                payload={
                    "presence_reason": reason,
                    "presence_type": presence.get("presence_type_name", ""),
                    "last_location": presence.get("presence_last_location", ""),
                    "mismatch_for": mismatch_for,
                },
            )
        if worker:
            worker.report_fault(
                "connection_error",
                f"Presence API no longer reports InGame ({reason}, location={presence.get('presence_last_location', '')})",
                expected_runtime_generation=runtime_generation,
                expected_session_id=session_id,
                expected_launch_nonce=launch_nonce,
                expected_transaction_id=transaction_id,
            )
        return True
