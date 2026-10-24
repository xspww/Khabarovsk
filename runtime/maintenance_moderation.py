from __future__ import annotations

import time
from typing import Any, NamedTuple

from core import flog_kv


class _ModerationValidation(NamedTuple):
    ok: bool
    detail: str
    meta: dict[str, Any]
    cookie: str
    is_banned_text: Any
    is_suspended_text: Any


def _account_key(acc: Any) -> str:
    return str(
        getattr(acc, "_config_username", "")
        or getattr(acc, "username", "")
        or ""
    ).strip().lower()


def _moderation_flags(
    meta: dict[str, Any],
    detail: str,
    is_suspended_text: Any,
    is_banned_text: Any,
) -> tuple[bool, bool]:
    is_suspended = bool(
        meta.get("is_suspended") or meta.get("suspended")
    ) or bool(is_suspended_text(detail))
    is_banned = (
        bool(meta.get("is_banned") or meta.get("banned"))
        or bool(is_banned_text(detail))
    ) and not is_suspended
    return is_suspended, is_banned


def _moderation_already_held(
    acc: Any,
    suspended: bool,
    is_account_banned: Any,
    is_account_suspended: Any,
    is_account_permanently_banned: Any,
) -> bool:
    if not suspended and (
        is_account_banned(acc) or is_account_suspended(acc)
    ):
        return True
    if suspended and is_account_suspended(acc):
        return True
    try:
        if suspended and is_account_permanently_banned(acc):
            return True
    except Exception:
        "intentional: ignore exception"
    return False


def _moderation_detail_phase(detail: str) -> str:
    lowered = detail.lower()
    if "transient" in lowered or "http 429" in lowered or "http 5" in lowered:
        return "transient"
    if "captcha" in lowered or "challenge" in lowered:
        return "captcha"
    return "clean"


def _moderation_sort_key(acc: Any, last: dict[str, float]) -> float:
    return float(last.get(_account_key(acc), 0.0) or 0.0)


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

    def _moderation_state(self) -> dict[str, float]:
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

    def _moderation_online(self) -> bool:
        try:
            net = getattr(getattr(self, "_recovery", None), "_net", None)
            if (
                net is not None
                and hasattr(net, "is_online")
                and not net.is_online()
            ):
                return False
        except Exception:
            "intentional: ignore exception"
        return True

    def _moderation_cookie(self, acc: Any) -> str | None:
        try:
            return str(getattr(acc, "cookie", "") or "").strip()
        except Exception:
            return None

    def _moderation_candidate(
        self,
        acc: Any,
        last: dict[str, float],
        now: float,
        interval: float,
    ) -> bool:
        cookie = self._moderation_cookie(acc)
        if not cookie:
            return False
        try:
            from services.ban_guard import is_account_permanently_banned

            if is_account_permanently_banned(acc):
                return False
        except Exception:
            "intentional: ignore exception"
        key = _account_key(acc)
        if not key:
            return False
        if now - float(last.get(key, 0.0) or 0.0) < interval:
            return False
        return True

    def _run_moderation_check(self, acc: Any) -> None:
        try:
            self._check_moderation_account(acc, source="periodic")
        except Exception as exc:
            try:
                flog_kv(
                    "MODERATION",
                    "check_failed",
                    "warning",
                    account=getattr(acc, "display_name", "?"),
                    error=str(exc),
                )
            except Exception:
                "intentional: ignore exception"

    def _run_moderation(self, job: Any) -> None:
        if not self._moderation_enabled():
            return
        try:
            accounts = list(self._accounts or [])
        except Exception:
            return
        if not accounts:
            return
        if not self._moderation_online():
            return
        now = time.time()
        interval = self._moderation_interval()
        batch = self._moderation_batch()
        last = self._moderation_state()
        candidates = [
            acc
            for acc in accounts
            if self._moderation_candidate(acc, last, now, interval)
        ]
        if not candidates:
            return
        candidates.sort(key=lambda a: _moderation_sort_key(a, last))
        for acc in candidates[:batch]:
            self._run_moderation_check(acc)

    def _moderation_validation(
        self,
        acc: Any,
        key: str,
        now: float,
    ) -> _ModerationValidation | None:
        cookie = self._moderation_cookie(acc)
        if not cookie:
            return None
        try:
            from services.ban_guard import is_banned_text, is_suspended_text
            from services.roblox_validation import validate_cookie_details
        except Exception as exc:
            flog_kv(
                "MODERATION",
                "unavailable",
                "warning",
                account=getattr(acc, "display_name", "?"),
                error=str(exc),
            )
            return None
        try:
            ok, _cookie_username, detail, meta = validate_cookie_details(cookie)
        except Exception as exc:
            self._moderation_state()[key] = (
                now - self._moderation_interval() + 300.0
            )
            flog_kv(
                "MODERATION",
                "transient",
                "warning",
                account=getattr(acc, "display_name", "?"),
                error=str(exc),
            )
            return None
        meta = meta or {}
        detail_s = str(detail or "")
        # Transient public-profile check (429/5xx) reports ok=True with
        # ban_check=unknown — never treat that as clean, or a previous
        # Suspended/Banned mark would be cleared by a rate-limit.
        if ok and str(meta.get("ban_check") or "").lower() == "unknown":
            self._moderation_state()[key] = (
                now - self._moderation_interval() + 300.0
            )
            return None
        return _ModerationValidation(
            ok=ok,
            detail=detail_s,
            meta=meta,
            cookie=cookie,
            is_banned_text=is_banned_text,
            is_suspended_text=is_suspended_text,
        )

    def _check_moderation_account(
        self,
        acc: Any,
        source: str = "periodic",
    ) -> bool:
        key = _account_key(acc)
        now = time.time()
        validation = self._moderation_validation(acc, key, now)
        if validation is None:
            return False
        return self._moderation_result(acc, key, now, validation, source)

    def _moderation_result(
        self,
        acc: Any,
        key: str,
        now: float,
        validation: _ModerationValidation,
        source: str,
    ) -> bool:
        is_suspended, is_banned = _moderation_flags(
            validation.meta,
            validation.detail,
            validation.is_suspended_text,
            validation.is_banned_text,
        )
        if validation.ok:
            self._moderation_state()[key] = now
            self._clear_moderation_hold(acc, source)
            return False
        if is_suspended or is_banned:
            self._moderation_state()[key] = now
            return self._apply_moderation_hold(
                acc,
                validation.detail,
                suspended=is_suspended,
                source=source,
            )
        phase = _moderation_detail_phase(validation.detail)
        if phase == "transient":
            self._moderation_state()[key] = (
                now - self._moderation_interval() + 300.0
            )
            return False
        if phase == "captcha":
            self._moderation_state()[key] = now
            return False
        if self._moderation_dead_cookie_ban(acc, validation.cookie):
            self._moderation_state()[key] = now
            return self._apply_moderation_hold(
                acc,
                "Account banned (Roblox isBanned=true)",
                suspended=False,
                source="dead_cookie_fallback",
            )
        self._moderation_state()[key] = now
        return False

    def _moderation_writer(self) -> Any:
        return getattr(self, "_runtime_state", None) or getattr(
            self,
            "_state_mgr",
            None,
        )

    def _moderation_audit_cleared(self, acc: Any, source: str) -> None:
        try:
            from services.account_hybrid import audit_event

            audit_event(
                "moderation_cleared",
                username=getattr(acc, "_config_username", "")
                or getattr(acc, "username", ""),
                ok=True,
                source=source,
            )
        except Exception:
            "intentional: ignore exception"

    def _moderation_wake_worker(self, acc: Any) -> None:
        worker = (getattr(self, "_workers", None) or {}).get(
            getattr(acc, "_config_username", "")
        )
        if worker is not None:
            try:
                worker.wake()
            except Exception:
                "intentional: ignore exception"

    def _clear_moderation_hold(self, acc: Any, source: str) -> None:
        try:
            from services.ban_guard import (
                clear_account_banned_hold,
                is_account_banned,
                is_account_suspended,
            )

            if is_account_banned(acc) or is_account_suspended(acc):
                writer = self._moderation_writer()
                clear_account_banned_hold(acc, runtime_writer=writer)
                self._moderation_audit_cleared(acc, source)
                flog_kv(
                    "MODERATION",
                    "cleared",
                    account=getattr(acc, "display_name", "?"),
                    source=source,
                )
                try:
                    self._runtime_evaluate(
                        acc,
                        trigger="moderation_cleared",
                    )
                except Exception:
                    "intentional: ignore exception"
                self._moderation_wake_worker(acc)
        except Exception:
            "intentional: ignore exception"

    def _moderation_audit_hold(
        self,
        acc: Any,
        label: str,
        reason: str,
        source: str,
    ) -> None:
        try:
            from services.account_hybrid import audit_event

            audit_event(
                f"moderation_{label}",
                username=getattr(acc, "_config_username", "")
                or getattr(acc, "username", ""),
                ok=False,
                reason=reason,
                source=source,
            )
        except Exception:
            "intentional: ignore exception"

    def _moderation_kill_bound_process(self, acc: Any, label: str) -> None:
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
                ProcessService.safe_kill_bound_process(
                    acc,
                    state_mgr,
                    reason=f"moderation_{label}",
                    expected_runtime_generation=runtime_generation,
                )
            except Exception as exc:
                flog_kv(
                    "MODERATION",
                    "kill_failed",
                    "warning",
                    account=getattr(acc, "display_name", "?"),
                    pid=pid or "",
                    error=str(exc),
                )

    def _moderation_fail_recovery(
        self,
        acc: Any,
        reason_key: str,
        block_msg: str,
    ) -> None:
        try:
            recovery = getattr(self, "_recovery", None)
            if recovery is not None and hasattr(recovery, "fail_account"):
                recovery.fail_account(acc, reason_key, block_msg)
        except Exception:
            "intentional: ignore exception"

    def _moderation_dead_cookie_ban(self, acc: Any, cookie: str) -> bool:
        try:
            from services.roblox_identity import (
                resolve_ban_for_dead_cookie as _resolve_dead,
            )

            username_hint = str(
                getattr(acc, "username", "")
                or getattr(acc, "_config_username", "")
                or ""
            )
            _b, _name, _uid = _resolve_dead(cookie, username_hint)
        except Exception:
            _b = None
        return _b is True

    def _apply_moderation_hold(
        self,
        acc: Any,
        detail: str,
        suspended: bool,
        source: str,
    ) -> bool:
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
        if _moderation_already_held(
            acc,
            suspended,
            is_account_banned,
            is_account_suspended,
            is_account_permanently_banned,
        ):
            return True
        if suspended:
            reason_key = SUSPENDED_REASON
            block_msg = SUSPENDED_BLOCK_REASON
            label = "suspended"
        else:
            reason_key = BANNED_REASON
            block_msg = BANNED_BLOCK_REASON
            label = "banned"
        writer = self._moderation_writer()
        try:
            set_account_banned_hold(
                acc,
                detail or block_msg,
                source=f"moderation_{source}",
                runtime_writer=writer,
            )
        except Exception as exc:
            flog_kv(
                "MODERATION",
                "hold_failed",
                "warning",
                account=getattr(acc, "display_name", "?"),
                error=str(exc),
            )
            return False
        self._moderation_audit_hold(
            acc,
            label,
            detail or block_msg,
            source,
        )
        self._moderation_kill_bound_process(acc, label)
        self._moderation_fail_recovery(acc, reason_key, block_msg)
        flog_kv(
            "MODERATION",
            f"detected_{label}",
            "error",
            account=getattr(acc, "display_name", "?"),
            detail=(detail or block_msg)[:220],
            source=source,
        )
        return True
