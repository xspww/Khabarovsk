from __future__ import annotations

import time
from typing import Any, Dict, List

from core import flog_kv


class MaintenanceModerationMixin:
    """RT moderation monitor — Suspended / Banned while farming.

    Tick job (default every 30s) picks the oldest-due accounts (default
    batch 2) whose last check is older than
    ``moderation_check_interval_seconds`` (default 900s = 15 min) and calls
    ``roblox_hybrid.validate_cookie_details`` which sees both:

    * temporary suspension via ``usermoderation/v1/not-approved``
    * permanent ban via public profile ``isBanned``

    On hit: kill bound game + mark FAILED (no rejoin) with a distinct
    Suspended vs Banned label. On clean after a previous mark (suspension
    expired): clear the hold so the account rejoins automatically.
    Transient (429/5xx/network) never marks — retry with short backoff.
    """

    def _moderation_enabled(self) -> bool:
        try:
            return bool(self._cfg.get("moderation_monitor_enabled", True))
        except Exception:
            return True

    def _moderation_interval(self) -> float:
        try:
            value = float(self._cfg.get("moderation_check_interval_seconds", 900) or 900)
        except Exception:
            value = 900.0
        return max(300.0, min(3600.0, value))

    def _moderation_batch(self) -> int:
        try:
            value = int(float(self._cfg.get("moderation_check_batch_size", 2) or 2))
        except Exception:
            value = 2
        return max(1, min(5, value))

    def _moderation_state(self) -> Dict[str, float]:
        store = getattr(self, "_moderation_last_check", None)
        if not isinstance(store, dict):
            store = {}
            self._moderation_last_check = store
        return store

    def request_moderation_check(self, username: str) -> bool:
        """Prioritise one account for the next moderation tick (e.g. kick 267)."""
        key = str(username or "").strip().lower()
        if not key:
            return False
        try:
            self._moderation_state().pop(key, None)
            return True
        except Exception:
            return False

    def _run_moderation(self, job: Any) -> None:
        if not self._moderation_enabled():
            return
        try:
            accounts = list(self._accounts or [])
        except Exception:
            return
        if not accounts:
            return
        try:
            net = getattr(getattr(self, "_recovery", None), "_net", None)
            if net is not None and hasattr(net, "is_online") and not net.is_online():
                return
        except Exception:
            pass
        now = time.time()
        interval = self._moderation_interval()
        batch = self._moderation_batch()
        last = self._moderation_state()
        candidates: List[Any] = []
        for acc in accounts:
            try:
                cookie = str(getattr(acc, "cookie", "") or "").strip()
            except Exception:
                continue
            if not cookie:
                continue
            try:
                from services.ban_guard import is_account_permanently_banned

                if is_account_permanently_banned(acc):
                    continue
            except Exception:
                pass
            key = str(getattr(acc, "_config_username", "") or getattr(acc, "username", "") or "").strip().lower()
            if not key:
                continue
            if now - float(last.get(key, 0.0) or 0.0) < interval:
                continue
            candidates.append(acc)
        if not candidates:
            return
        candidates.sort(key=lambda a: float(last.get(str(getattr(a, "_config_username", "") or getattr(a, "username", "") or "").strip().lower(), 0.0) or 0.0))
        for acc in candidates[:batch]:
            try:
                self._check_moderation_account(acc, source="periodic")
            except Exception as exc:
                try:
                    flog_kv("MODERATION", "check_failed", "warning", account=getattr(acc, "display_name", "?"), error=str(exc))
                except Exception:
                    pass

    def _check_moderation_account(self, acc: Any, source: str = "periodic") -> bool:
        key = str(getattr(acc, "_config_username", "") or getattr(acc, "username", "") or "").strip().lower()
        now = time.time()
        try:
            cookie = str(getattr(acc, "cookie", "") or "").strip()
        except Exception:
            return False
        if not cookie:
            return False
        try:
            from roblox_hybrid import validate_cookie_details
            from services.ban_guard import is_suspended_text, is_banned_text
        except Exception as exc:
            flog_kv("MODERATION", "unavailable", "warning", account=getattr(acc, "display_name", "?"), error=str(exc))
            return False
        try:
            ok, _cookie_username, detail, meta = validate_cookie_details(cookie)
        except Exception as exc:
            self._moderation_state()[key] = now - self._moderation_interval() + 300.0
            flog_kv("MODERATION", "transient", "warning", account=getattr(acc, "display_name", "?"), error=str(exc))
            return False
        meta = meta or {}
        detail_s = str(detail or "")
        # Transient public-profile check (429/5xx) reports ok=True with
        # ban_check=unknown — never treat that as clean, or a previous
        # Suspended/Banned mark would be cleared by a rate-limit.
        if ok and str(meta.get("ban_check") or "").lower() == "unknown":
            self._moderation_state()[key] = now - self._moderation_interval() + 300.0
            return False
        is_suspended = bool(meta.get("is_suspended") or meta.get("suspended")) or bool(is_suspended_text(detail_s))
        is_banned = (bool(meta.get("is_banned") or meta.get("banned")) or bool(is_banned_text(detail_s))) and not is_suspended
        if ok:
            self._moderation_state()[key] = now
            try:
                from services.ban_guard import is_account_banned, is_account_suspended, clear_account_banned_hold

                if is_account_banned(acc) or is_account_suspended(acc):
                    writer = getattr(self, "_runtime_state", None) or getattr(self, "_state_mgr", None)
                    clear_account_banned_hold(acc, runtime_writer=writer)
                    try:
                        from account_hybrid import audit_event

                        audit_event("moderation_cleared", username=getattr(acc, "_config_username", "") or getattr(acc, "username", ""), ok=True, source=source)
                    except Exception:
                        pass
                    flog_kv("MODERATION", "cleared", account=getattr(acc, "display_name", "?"), source=source)
                    try:
                        self._runtime_evaluate(acc, trigger="moderation_cleared")
                    except Exception:
                        pass
                    worker = (getattr(self, "_workers", None) or {}).get(getattr(acc, "_config_username", ""))
                    if worker is not None:
                        try:
                            worker.wake()
                        except Exception:
                            pass
            except Exception:
                pass
            return False
        if is_suspended or is_banned:
            self._moderation_state()[key] = now
            return self._apply_moderation_hold(acc, detail_s, suspended=is_suspended, source=source)
        lowered = detail_s.lower()
        if "transient" in lowered or "http 429" in lowered or "http 5" in lowered:
            self._moderation_state()[key] = now - self._moderation_interval() + 300.0
            return False
        if "captcha" in lowered or "challenge" in lowered:
            self._moderation_state()[key] = now
            return False
        try:
            from roblox_hybrid import resolve_ban_for_dead_cookie as _resolve_dead

            username_hint = str(getattr(acc, "username", "") or getattr(acc, "_config_username", "") or "")
            _b, _name, _uid = _resolve_dead(cookie, username_hint)
        except Exception:
            _b = None
        if _b is True:
            self._moderation_state()[key] = now
            return self._apply_moderation_hold(acc, "Account banned (Roblox isBanned=true)", suspended=False, source="dead_cookie_fallback")
        self._moderation_state()[key] = now
        return False

    def _apply_moderation_hold(self, acc: Any, detail: str, suspended: bool, source: str) -> bool:
        try:
            from services.ban_guard import (
                BANNED_BLOCK_REASON,
                BANNED_REASON,
                SUSPENDED_BLOCK_REASON,
                SUSPENDED_REASON,
                is_account_banned,
                is_account_permanently_banned,
                is_account_suspended,
                set_account_banned_hold,
            )
        except Exception:
            return False
        if not suspended and (is_account_banned(acc) or is_account_suspended(acc)):
            return True
        if suspended and is_account_suspended(acc):
            return True
        try:
            if suspended and is_account_permanently_banned(acc):
                return True
        except Exception:
            pass
        reason_key = SUSPENDED_REASON if suspended else BANNED_REASON
        block_msg = SUSPENDED_BLOCK_REASON if suspended else BANNED_BLOCK_REASON
        label = "suspended" if suspended else "banned"
        writer = getattr(self, "_runtime_state", None) or getattr(self, "_state_mgr", None)
        try:
            set_account_banned_hold(acc, detail or block_msg, source=f"moderation_{source}", runtime_writer=writer)
        except Exception as exc:
            flog_kv("MODERATION", "hold_failed", "warning", account=getattr(acc, "display_name", "?"), error=str(exc))
            return False
        try:
            from account_hybrid import audit_event

            audit_event(f"moderation_{label}", username=getattr(acc, "_config_username", "") or getattr(acc, "username", ""), ok=False, reason=detail or block_msg, source=source)
        except Exception:
            pass
        try:
            with acc._lock:
                pid = acc.pid
                runtime_generation = acc.runtime_generation
        except Exception:
            pid = None
            runtime_generation = 0
        if pid:
            try:
                from services.process_service import ProcessService

                state_mgr = getattr(self, "_state_mgr", None)
                ProcessService.safe_kill_bound_process(acc, state_mgr, reason=f"moderation_{label}", expected_runtime_generation=runtime_generation)
            except Exception as exc:
                flog_kv("MODERATION", "kill_failed", "warning", account=getattr(acc, "display_name", "?"), pid=pid or "", error=str(exc))
        try:
            recovery = getattr(self, "_recovery", None)
            if recovery is not None and hasattr(recovery, "fail_account"):
                recovery.fail_account(acc, reason_key, block_msg)
        except Exception:
            pass
        flog_kv("MODERATION", f"detected_{label}", "error", account=getattr(acc, "display_name", "?"), detail=(detail or block_msg)[:220], source=source)
        return True
