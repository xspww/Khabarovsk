from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

from core import (
    Account,
    AccountState,
    EventBus,
    StateManager,
    cookie_identity_block_reason,
    flog,
    flog_kv,
)
from infra import net_tls
from runtime.account_worker_disconnects import handle_disconnect_checks
from runtime.maintenance_performance import _apply_cpu_limiter_for_bound_process
from runtime.recovery_support import RECOVERY_REASON_MESSAGES, _set_account_cookie_block, compute_backoff
from runtime.supervisor_runtime import SupervisorRuntime
from services.auth_gate import evaluate_account_auth_gate, mark_account_auth_quarantined
from services.captcha_guard import (
    CAPTCHA_BLOCK_REASON,
    CAPTCHA_REASON,
    captcha_detail,
    is_captcha_text,
    set_account_captcha_hold,
)
from services.process_service import ProcessManager, ProcessService
from services.verify_guard import (
    LOCKED_BLOCK_REASON,
    LOCKED_REASON,
    RECOVERY_BLOCK_REASON,
    RECOVERY_REASON,
    is_locked_text,
    is_recovery_text,
    set_account_locked_hold,
    set_account_recovery_hold,
)

if TYPE_CHECKING:
    from runtime.recovery_engine import RecoveryEngine


def _is_banned_detail(detail: str) -> bool:
    """True when the validation detail reads as a ban."""
    try:
        from services.ban_guard import is_banned_text as _is_banned_text
    except Exception:
        return False
    try:
        return bool(_is_banned_text(detail))
    except Exception:
        return False


def _resolve_moderation_locked(worker: Any, acc: Any, detail: str) -> bool:
    """Disambiguate a bare "User is moderated" answer: lock or ban.

    Applies the locked hold and notifies the runtime owner when the probe
    confirms a lock, then returns True so the caller stops the worker.
    """
    try:
        from services.roblox_identity import moderated_only_detail as _mod_only
        from services.roblox_identity import resolve_moderated_dead_cookie as _resolve_mod

        mod_hit = bool(_mod_only(detail))
    except Exception:
        return False
    if not mod_hit:
        return False
    try:
        mv, md, _mn, _mu = _resolve_mod(acc.cookie, acc.username)
    except Exception:
        mv, md = None, ""
    if mv != "locked":
        return False
    set_account_locked_hold(acc, md or detail, source="cookie_validation", runtime_writer=worker.state_mgr)
    flog_kv("LOCKED", "detected", "warning", account=acc.display_name, detail=detail)
    worker.runtime_owner.handle_runtime_signal(
        acc,
        "fatal",
        LOCKED_REASON,
        payload={"reason_msg": LOCKED_BLOCK_REASON, "detail": detail},
    )
    return True


def _apply_banned_hold(worker: Any, acc: Any, detail: str) -> None:
    """Apply the banned hold (with a safe local fallback) and stop the worker."""
    try:
        from services.ban_guard import BANNED_BLOCK_REASON as _BANNED_MSG
        from services.ban_guard import set_account_banned_hold as _set_hold
    except Exception:
        _BANNED_MSG = "Account banned (Roblox). Unmark to allow rejoin."
        _set_hold = None
    hold_ok = False
    if _set_hold is not None:
        try:
            _set_hold(acc, detail, source="cookie_validation", runtime_writer=worker.state_mgr)
            hold_ok = True
        except Exception:
            hold_ok = False
    if not hold_ok:
        try:
            with acc._lock:
                acc.session_checked = True
                acc.session_valid = False
                acc.manual_status = _BANNED_MSG
                acc.last_error = detail or _BANNED_MSG
                acc.last_crash_reason = "banned"
                acc.last_recovery_reason = "banned"
                acc.recovery_scheduled_at = 0.0
        except Exception:
            "intentional: ignore exception"
    flog_kv("BANNED", "detected", "warning", account=acc.display_name, detail=detail)
    worker.runtime_owner.handle_runtime_signal(
        acc,
        "fatal",
        "banned",
        payload={"reason_msg": _BANNED_MSG, "detail": detail or _BANNED_MSG},
    )


def _rebind_validation(reconciliation: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    validation = reconciliation.get("validation") or {}
    return validation, validation.get("pid")


def _log_rebind_rejected(
    acc: Any,
    reconciliation: dict[str, Any],
    pid: Any,
    reason: str,
) -> None:
    validation = reconciliation.get("validation") or {}
    flog_kv(
        "WORKER",
        "rebind_rejected",
        "warning",
        account=acc.display_name,
        pid=pid,
        reason=reason,
        action=str(reconciliation.get("action") or ""),
        confidence=f"{float(validation.get('confidence') or 0.0):.1f}",
        confidence_level=reconciliation.get("confidence_level", ""),
        reject=reconciliation.get("reason", ""),
    )


def _log_rebind_validation_rejected(
    acc: Any,
    pid: Any,
    reason: str,
    bind_result: dict[str, Any],
) -> None:
    flog_kv(
        "WORKER",
        "rebind_validation_rejected",
        "warning",
        account=acc.display_name,
        pid=pid,
        reason=reason,
        reject=bind_result.get("reason", ""),
    )


def _log_rebind_success(
    acc: Any,
    old_pid: Any,
    pid: Any,
    reason: str,
    validation: dict[str, Any],
) -> None:
    if pid != old_pid:
        flog(
            f"[WORKER] {acc.display_name} rebound live game PID {old_pid} -> {pid} "
            f"({reason})"
        )
        return
    flog_kv(
        "WORKER",
        "rebind_refreshed",
        account=acc.display_name,
        pid=pid,
        reason=reason,
        confidence=f"{float(validation.get('confidence') or 0.0):.1f}",
    )


def _missing_presence(assessment: dict[str, Any], include_pids: bool = False) -> dict[str, Any]:
    presence = assessment.get("presence")
    if presence:
        return presence
    if include_pids:
        return {
            "pids": [],
            "visible_windows": 0,
            "max_ram_mb": 0.0,
            "max_cpu": 0.0,
        }
    return {
        "visible_windows": 0,
        "max_ram_mb": 0.0,
        "max_cpu": 0.0,
    }


def _hold_reason(reconciliation: dict[str, Any]) -> str:
    return str(reconciliation.get("reason") or "presence_hold")


def _missing_window(acc: Any, now: float) -> tuple[float, Any]:
    with acc._lock:
        if not acc.pid_missing_since:
            acc.pid_missing_since = now
        return now - acc.pid_missing_since, acc.pid


def _observed_identity(acc: Any) -> tuple[Any, int, str, str, str, float]:
    with acc._lock:
        return (
            acc.pid,
            acc.runtime_generation,
            acc.session_id,
            acc.launch_nonce,
            acc.rejoin_transaction_id,
            float(getattr(acc, "teleport_suppress_until", 0.0) or 0.0),
        )


def _signal_identity(acc: Any) -> tuple[int, str, str, str]:
    with acc._lock:
        return (
            acc.runtime_generation,
            acc.session_id,
            acc.launch_nonce,
            acc.rejoin_transaction_id,
        )


def _missing_extra(
    pid_was: Any,
    grace_period: float,
    presence: dict[str, Any],
) -> str:
    extra = f"PID={pid_was}" if pid_was else "PID=<none>"
    extra += (
        f" grace={grace_period:.1f}s"
        f" windows={presence['visible_windows']}"
        f" ram={float(presence['max_ram_mb'] or 0.0):.1f}MB"
        f" cpu={float(presence['max_cpu'] or 0.0):.2f}%"
    )
    return extra


def _presence_pids(presence: dict[str, Any]) -> list[int]:
    pids: list[int] = []
    for item in presence.get("pids", []) or []:
        try:
            pid = int(item)
        except Exception:
            continue
        if pid:
            pids.append(pid)
    return pids


def _other_pids(presence: dict[str, Any], pid_was: Any) -> list[int]:
    return [pid for pid in _presence_pids(presence) if pid != int(pid_was)]


def _multi_launch_timing(acc: Any) -> tuple[float, float]:
    with acc._lock:
        launch_age = time.time() - float(acc.last_launch_at or 0.0)
        missing_since = float(acc.pid_missing_since or 0.0)
    return launch_age, missing_since


def _multi_timing_ok(
    launch_age: float,
    newest_created: float,
    missing_since: float,
    window: float,
    overlap_window: float,
    grace_period: float,
) -> bool:
    if launch_age > window and newest_created < (time.time() - window):
        return False
    if newest_created < (missing_since - overlap_window):
        return False
    if newest_created > (missing_since + max(5.0, grace_period)):
        return False
    return True


def _moderation_probe(kind: str, value: str) -> tuple[Any, str]:
    try:
        from services.roblox_moderation import (
            fetch_moderation_status,
            fetch_user_ban_status,
        )

        if kind == "cookie":
            banned, detail, _payload = fetch_moderation_status(value.strip())
            return banned, detail
        return fetch_user_ban_status(value)
    except Exception:
        return None, ""


def _banned_message() -> str:
    try:
        from services.ban_guard import BANNED_BLOCK_REASON

        return BANNED_BLOCK_REASON
    except Exception:
        return "Account banned (Roblox). Unmark to allow rejoin."


def _http_moderation_detail(code: int, body: str, cookie: str) -> str:
    try:
        lowered_body = f"HTTP {code} {body}".lower()
        if code != 403 or not ("moderated" in lowered_body or "banned" in lowered_body):
            return ""
        banned, detail = _moderation_probe("cookie", cookie)
        if banned is True and detail:
            return detail
    except Exception:
        "intentional: ignore exception"
    return ""


def _invalid_cookie_result(code: int, body: str) -> tuple[bool, str, str, bool]:
    transient = code >= 500 or code == 429
    suffix = body[:180].replace("\r", " ").replace("\n", " ") if body else ""
    detail = f"HTTP {code} {'(transient)' if transient else '(cookie expired or invalid)'}"
    if suffix:
        detail += f" {suffix}"
    return False, "", detail, transient


def _http_error_result(
    error: urllib.error.HTTPError,
    cookie: str,
) -> tuple[bool, str, str, bool]:
    headers = dict(error.headers.items()) if error.headers else {}
    try:
        body = error.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""
    captcha = captcha_detail(error.code, body, headers)
    if captcha:
        return False, "", captcha, False
    moderation = _http_moderation_detail(error.code, body, cookie)
    if moderation:
        return False, "", moderation, False
    if _is_banned_detail(f"HTTP {error.code} {body}"):
        return False, "", body.strip()[:180] or _banned_message(), False
    return _invalid_cookie_result(error.code, body)


def _authenticated_result(
    username: str,
    user_id: str,
    cookie: str,
) -> tuple[bool, str, str, bool]:
    if not username:
        return False, "", "no username in response", True
    mod_banned, mod_detail = _moderation_probe("cookie", cookie)
    if mod_banned is True and mod_detail:
        return False, username, mod_detail, False
    if user_id:
        banned, ban_detail = _moderation_probe("user", user_id)
        if banned is True:
            return False, username, ban_detail or _banned_message(), False
    return True, username, "", False


def _authenticated_identity(req: urllib.request.Request) -> tuple[str, str]:
    with net_tls.urlopen(req, timeout=8) as resp:
        data = __import__("json").loads(resp.read())
    username = data.get("name", "")
    user_id = str(data.get("id") or "")
    return username, user_id


class AccountWorker(threading.Thread):
    """
    AccountWorker observes process health.
    It no longer decides how recovery should happen.
    """

    REASON_MESSAGES = RECOVERY_REASON_MESSAGES

    def __init__(
        self,
        acc: Account,
        state_mgr: StateManager,
        bus: EventBus,
        cfg: dict,
        recovery: RecoveryEngine,
        stop: threading.Event,
        supervisor: SupervisorRuntime | None = None,
        accounts: list[Account] | None = None,
    ):
        super().__init__(daemon=True, name=f"Worker-{acc.username}")
        self.acc = acc
        self.state_mgr = state_mgr
        self.bus = bus
        self.cfg = cfg
        self.recovery = recovery
        self.runtime_owner = getattr(recovery, "runtime_orchestrator", recovery)
        # NOTE: must not be named `self._stop` — threading.Thread uses
        # `_stop()` internally (join/_wait_for_tstate_lock on Python <=3.13),
        # so shadowing it with an Event breaks join() with
        # "TypeError: 'Event' object is not callable".
        self._stop_event = stop
        self._supervisor = supervisor
        self._accounts = accounts or [acc]
        self._wake = threading.Event()
        self._not_responding_since: float | None = None
        self._connection_error_since: float | None = None
        self._last_missing_hold_log = 0.0

    def wake(self):
        self._wake.set()

    def update_config(self, cfg: dict) -> None:
        self.cfg = cfg
    def report_fault(
        self,
        reason_key: str,
        extra: str = "",
        expected_runtime_generation: int | None = None,
        expected_session_id: str = "",
        expected_launch_nonce: str = "",
        expected_transaction_id: str = "",
    ):
        msg = self.REASON_MESSAGES.get(reason_key, reason_key)
        if extra:
            msg += f" [{extra}]"
        flog(f"[WORKER] {self.acc.display_name} fault: {msg}")
        if self._supervisor:
            self._supervisor.emit(
                "AccountSupervisor",
                "FAULT_SIGNAL",
                account=self.acc,
                severity="warning",
                reason=reason_key,
                payload={"detail": msg},
            )
        self.runtime_owner.handle_runtime_signal(
            self.acc,
            "fault",
            reason_key,
            payload={"detail": msg, "reason_msg": msg},
            expected_runtime_generation=expected_runtime_generation,
            expected_session_id=expected_session_id,
            expected_launch_nonce=expected_launch_nonce,
            expected_transaction_id=expected_transaction_id,
        )
        self._wake.set()

    def _rebind_quarantine(self) -> float:
        window = float(self.cfg.get("launch_verify_window", 25) or 25)
        return max(15.0, min(30.0, window))

    def _bind_rebound_process(
        self,
        acc: Account,
        validation: dict[str, Any],
        pid: Any,
        reason: str,
        runtime_generation: int,
    ) -> dict[str, Any]:
        return ProcessService.bind_account_process(
            acc,
            pid,
            self.state_mgr,
            reason=reason,
            expected_identity=str(validation.get("identity") or ""),
            launched_after=acc.last_launch_at,
            process_name=str(validation.get("name") or "") or "RobloxPlayerBeta.exe",
            min_ram_mb=20.0,
            expected_runtime_generation=runtime_generation,
        )

    def _rebind_live_game_process(self, reason: str) -> bool:
        acc = self.acc
        reconciliation = ProcessManager.staged_orphan_reconcile(
            acc,
            launched_after=acc.last_launch_at,
            quarantine_seconds=self._rebind_quarantine(),
        )
        validation, pid = _rebind_validation(reconciliation)
        if not pid:
            return False
        if reconciliation.get("action") != "auto_bind":
            _log_rebind_rejected(acc, reconciliation, pid, reason)
            return False
        with acc._lock:
            old_pid = acc.pid
            runtime_generation = acc.runtime_generation
        bind_result = self._bind_rebound_process(acc, validation, pid, reason, runtime_generation)
        if not bind_result.get("ok"):
            self.state_mgr.set_binding_status(
                acc,
                "rebind_rejected",
                reason=bind_result.get("reason", reason),
            )
            _log_rebind_validation_rejected(acc, pid, reason, bind_result)
            return False
        _log_rebind_success(acc, old_pid, pid, reason, validation)
        _apply_cpu_limiter_for_bound_process(self._accounts, self.cfg, reason, acc)
        return True

    def _safe_adopt_visible_process(self, reason: str) -> bool:
        acc = self.acc
        with acc._lock:
            runtime_generation = acc.runtime_generation
        result = ProcessService.safe_adopt_visible_process(
            acc,
            self.state_mgr,
            accounts=self._accounts,
            reason=reason,
            expected_runtime_generation=runtime_generation,
        )
        if result.get("ok"):
            flog_kv(
                "WORKER",
                "visible_process_adopted",
                account=acc.display_name,
                pid=result.get("pid"),
                reason=reason,
                runtime_generation=runtime_generation,
            )
            _apply_cpu_limiter_for_bound_process(self._accounts, self.cfg, reason, acc)
            return True
        if result.get("reason") not in {"no_visible_candidate", "desired_state_not_in_game"}:
            flog_kv(
                "WORKER",
                "visible_process_adopt_rejected",
                "warning",
                account=acc.display_name,
                pid=result.get("pid") or "",
                reason=reason,
                reject=result.get("reason", ""),
                runtime_generation=runtime_generation,
            )
        return False

    def _reconcile_presence(self, acc: Account) -> tuple[dict[str, Any], dict[str, Any]]:
        reconciliation = ProcessManager.staged_orphan_reconcile(
            acc,
            launched_after=acc.last_launch_at,
            quarantine_seconds=self._rebind_quarantine(),
        )
        validation = reconciliation.get("validation") or {}
        return reconciliation, validation

    def _record_reconcile_signal(self, acc: Account, validation: dict[str, Any]) -> None:
        with acc._lock:
            acc.last_signal_confidence = float(validation.get("confidence") or 0.0)
            acc.last_reconcile_at = time.time()

    def _assess_missing_bound_process(self, source: str) -> dict[str, object]:
        acc = self.acc
        now = time.time()
        grace_period = max(4.0, min(float(self.cfg.get("crash_timeout", 30)), 10.0))

        if self._rebind_live_game_process(source):
            return {"status": "rebound", "grace_period": grace_period}

        if self._safe_adopt_visible_process(f"{source}_visible_adopt"):
            return {"status": "rebound", "grace_period": grace_period, "adopted": True}

        reconciliation, validation = self._reconcile_presence(acc)
        presence = ProcessManager.summarize_game_presence(launched_after=acc.last_launch_at)
        self._record_reconcile_signal(acc, validation)
        if reconciliation.get("action") == "auto_bind" and self._rebind_live_game_process(
            f"{source}_presence"
        ):
            return {
                "status": "rebound",
                "presence": presence,
                "validation": validation,
                "grace_period": grace_period,
            }

        missing_for, pid_was = _missing_window(acc, now)

        has_multi_signal = reconciliation.get("action") == "quarantine"
        if self._looks_like_multi_roblox_guard_failure(
            pid_was,
            presence,
            missing_for,
            grace_period,
        ):
            return {
                "status": "multi_roblox_guard_failed",
                "presence": presence,
                "validation": validation,
                "missing_for": missing_for,
                "grace_period": grace_period,
            }

        if missing_for < grace_period or has_multi_signal:
            return {
                "status": "hold",
                "reason": _hold_reason(reconciliation),
                "presence": presence,
                "validation": validation,
                "missing_for": missing_for,
                "grace_period": grace_period,
            }

        return {
            "status": "dead",
            "presence": presence,
            "validation": validation,
            "missing_for": missing_for,
            "grace_period": grace_period,
        }

    def _multi_guard_enabled(self) -> bool:
        return bool(self.cfg.get("multi_roblox_enabled", False)) and not bool(
            self.cfg.get("rt_rotation_enabled", False)
        )

    def _guard_window(self, grace_period: float) -> float:
        try:
            return max(
                grace_period,
                float(self.cfg.get("multi_roblox_guard_failure_window", 180) or 180),
            )
        except Exception:
            return 180.0

    def _overlap_window(self, grace_period: float, window: float) -> float:
        try:
            overlap_window = float(
                self.cfg.get(
                    "multi_roblox_guard_failure_overlap_seconds",
                    grace_period,
                )
                or grace_period
            )
        except Exception:
            overlap_window = grace_period
        return max(1.0, min(overlap_window, window))

    def _looks_like_multi_roblox_guard_failure(
        self,
        pid_was: int | None,
        presence: dict[str, Any],
        missing_for: float,
        grace_period: float,
    ) -> bool:
        if not self._multi_guard_enabled():
            return False
        if not pid_was or missing_for < grace_period:
            return False
        window = self._guard_window(grace_period)
        overlap_window = self._overlap_window(grace_period, window)
        launch_age, missing_since = _multi_launch_timing(self.acc)
        newest_created = float(presence.get("newest_created") or 0.0)
        if not newest_created or not missing_since:
            return False
        if not self._has_active_multi_roblox_launch_overlap():
            return False
        if not _multi_timing_ok(
            launch_age,
            newest_created,
            missing_since,
            window,
            overlap_window,
            grace_period,
        ):
            return False
        return bool(_other_pids(presence, pid_was))

    def _has_active_multi_roblox_launch_overlap(self) -> bool:
        accounts = list(getattr(self, "_accounts", []) or [])
        if not accounts:
            return True
        active_launch_states = {AccountState.QUEUED, AccountState.LAUNCHING, AccountState.VERIFY}
        for other in accounts:
            if other is self.acc:
                continue
            try:
                with other._lock:
                    if other.desired_state == AccountState.IN_GAME and other.state in active_launch_states:
                        return True
            except Exception:
                continue
        return False

    def _log_missing_hold(
        self,
        acc: Account,
        assessment: dict[str, Any],
        now: float,
    ) -> None:
        presence = _missing_presence(assessment, include_pids=True)
        missing_for = float(assessment.get("missing_for") or 0.0)
        if now - self._last_missing_hold_log < 5.0:
            return
        flog(
            f"[WORKER] {acc.display_name} bound PID missing but game signals remain "
            f"(missing={missing_for:.1f}s pids={presence['pids']} "
            f"windows={presence['visible_windows']} ram={presence['max_ram_mb']:.1f}MB "
            f"cpu={presence['max_cpu']:.2f}%)"
        )
        self._last_missing_hold_log = now

    def _guard_session(
        self,
        acc: Account,
        observed: tuple[Any, int, str, str, str, float],
        source: str,
    ) -> bool:
        pid_was, runtime_generation, session_id, launch_nonce, transaction_id, _teleport = (
            observed
        )
        with acc._lock:
            if not self.recovery._runtime_state.guard_session_identity(
                acc,
                expected_generation=runtime_generation,
                expected_session_id=session_id,
                expected_launch_nonce=launch_nonce,
                expected_transaction_id=transaction_id,
                reason=f"missing_bound_process:{source}",
            ):
                flog_kv(
                    "WORKER",
                    "stale_worker_signal_rejected",
                    "warning",
                    account=acc.display_name,
                    source=source,
                    pid=pid_was or "",
                    expected_runtime_generation=runtime_generation,
                    current_runtime_generation=acc.runtime_generation,
                    expected_session_id=session_id,
                    current_session_id=acc.session_id,
                    expected_transaction_id=transaction_id,
                    current_transaction_id=acc.rejoin_transaction_id,
                )
                return False
        return True

    def _clear_dead_binding(self, acc: Account, pid_was: Any, source: str) -> None:
        if pid_was:
            ProcessService.evict_pid_cache(
                pid_was,
                reason=f"missing_bound_process:{source}",
                account=acc,
            )
            self.state_mgr.clear_process_binding(
                acc,
                reason="missing_bound_process_dead",
                increment_generation=True,
            )
            return
        with acc._lock:
            acc.pid_missing_since = 0.0

    def _handle_guard_failure(
        self,
        acc: Account,
        extra: str,
        signal_identity: tuple[int, str, str, str],
    ) -> None:
        msg = self.REASON_MESSAGES["multi_roblox_guard_failed"]
        try:
            from services.roblox_multibox import record_multi_roblox_guard_failure

            record_multi_roblox_guard_failure(f"{acc.display_name}: {extra}")
        except Exception:
            "intentional: ignore exception"
        with acc._lock:
            acc.manual_status = msg
            acc.last_error = f"{msg} [{extra}]"
        flog_kv(
            "MULTI_ROBLOX",
            "guard_runtime_failure_detected",
            "error",
            account=acc.display_name,
            detail=extra,
        )
        self.runtime_owner.handle_runtime_signal(
            acc,
            "fatal",
            "multi_roblox_guard_failed",
            payload={"detail": f"{msg} [{extra}]", "reason_msg": msg},
            expected_runtime_generation=signal_identity[0],
            expected_session_id=signal_identity[1],
            expected_launch_nonce=signal_identity[2],
            expected_transaction_id=signal_identity[3],
        )
        self._wake.set()

    def handle_missing_bound_process(self, source: str) -> str:
        acc = self.acc
        observed = _observed_identity(acc)
        (
            pid_was,
            _runtime_generation,
            _session_id,
            _launch_nonce,
            _transaction_id,
            teleport_until,
        ) = observed
        if teleport_until and time.time() < teleport_until:
            # Teleport handoff: old client exited, queued new client still
            # starting. Not a crash — let it bind instead of racing a
            # fresh recovery launch against it (that race rejoins twice).
            return "teleporting"
        assessment = self._assess_missing_bound_process(source)
        status = str(assessment.get("status") or "")
        if status == "rebound":
            return status

        if status == "hold":
            self._log_missing_hold(acc, assessment, time.time())
            return status

        presence = _missing_presence(assessment)
        grace_period = float(assessment.get("grace_period") or 0.0)
        guard_failed = status == "multi_roblox_guard_failed"
        if not self._guard_session(acc, observed, source):
            return "stale"
        self._clear_dead_binding(acc, pid_was, source)
        signal_identity = _signal_identity(acc)
        extra = _missing_extra(pid_was, grace_period, presence)
        if guard_failed:
            self._handle_guard_failure(acc, extra, signal_identity)
            return status
        self.report_fault(
            "pid_dead",
            extra,
            expected_runtime_generation=signal_identity[0],
            expected_session_id=signal_identity[1],
            expected_launch_nonce=signal_identity[2],
            expected_transaction_id=signal_identity[3],
        )
        return status

    def _skip_finished(self, acc: Account) -> bool:
        try:
            from domain.account_model import is_account_finished

            if is_account_finished(acc):
                flog(f"[WORKER] {acc.display_name} is Finished - skipping launch")
                return True
        except Exception:
            "intentional: ignore exception"
        return False

    def _auth_gate_blocked(self, acc: Account) -> bool:
        auth_gate = evaluate_account_auth_gate(acc)
        if not auth_gate.blocked:
            return False
        mark_account_auth_quarantined(
            acc,
            auth_gate,
            source="worker_preflight",
            runtime_writer=self.state_mgr,
        )
        reason_key = auth_gate.reason_key
        reason_msg = auth_gate.reason
        if auth_gate.reason_key == CAPTCHA_REASON:
            flog_kv(
                "CAPTCHA",
                "account_hold",
                "warning",
                account=acc.display_name,
                detail=auth_gate.reason,
            )
        else:
            flog(
                f"[WORKER] {acc.display_name} launch blocked: {auth_gate.reason}",
                "warning",
            )
        self.runtime_owner.handle_runtime_signal(
            acc,
            "fatal",
            reason_key,
            payload={"reason_msg": reason_msg, "detail": auth_gate.reason},
        )
        return True

    def _wait_for_network(self) -> bool:
        while not self.recovery._net.is_online() and not self._stop_event.is_set():
            self._wake.wait(timeout=2.0)
            self._wake.clear()
        return not self._stop_event.is_set()

    def _accept_cookie(self, acc: Account, username: str) -> bool:
        mismatch_reason = cookie_identity_block_reason(
            acc.username,
            username,
            bool(username and username.lower() != acc.username.lower()),
        )
        if mismatch_reason:
            _set_account_cookie_block(acc, mismatch_reason, cookie_username=username)
            flog(
                f"[WORKER] {acc.display_name} launch blocked: {mismatch_reason}",
                "warning",
            )
            self.runtime_owner.handle_runtime_signal(
                acc,
                "fatal",
                "cookie_mismatch",
                payload={"reason_msg": mismatch_reason, "detail": mismatch_reason},
            )
            return False
        with acc._lock:
            acc.cookie_username = str(username or acc.cookie_username or "")
            acc.cookie_mismatch = False
            acc.session_valid = True
            acc.session_checked = True
            acc.session_wait_started_at = 0.0
        if username and username != acc.username:
            flog(f"[WORKER] {acc.display_name} cookie validated as '{username}'")
        flog(f"[WORKER] {acc.display_name} cookie valid ({username})")
        self.runtime_owner.request_evaluate(acc, trigger="cookie_validated")
        return True

    def _record_cookie_failure(self, acc: Account, transient: bool) -> None:
        with acc._lock:
            acc.session_checked = True
            acc.session_valid = False
            acc.last_crash_reason = (
                "cookie_check_transient" if transient else "cookie_invalid"
            )

    def _retry_cookie_validation(self, acc: Account, attempt: int, detail: str) -> None:
        delay = compute_backoff(attempt, base=3, cap=30)
        flog(
            f"[WORKER] {acc.display_name} cookie validation transient error "
            f"-> retry in {delay:.1f}s: {detail}",
            "warning",
        )
        self.runtime_owner.request_evaluate(acc, trigger="cookie_validation_retry")
        self._wake.wait(timeout=delay)
        self._wake.clear()

    def _handle_cookie_failure(self, acc: Account, detail: str) -> None:
        if _is_banned_detail(detail):
            if _resolve_moderation_locked(self, acc, detail):
                return
            _apply_banned_hold(self, acc, detail)
            return

        if is_locked_text(detail):
            set_account_locked_hold(
                acc,
                detail,
                source="cookie_validation",
                runtime_writer=self.state_mgr,
            )
            flog_kv("LOCKED", "detected", "warning", account=acc.display_name, detail=detail)
            self.runtime_owner.handle_runtime_signal(
                acc,
                "fatal",
                LOCKED_REASON,
                payload={"reason_msg": LOCKED_BLOCK_REASON, "detail": detail},
            )
            return

        if is_recovery_text(detail):
            set_account_recovery_hold(
                acc,
                detail,
                source="cookie_validation",
                runtime_writer=self.state_mgr,
            )
            flog_kv("RECOVERY", "detected", "warning", account=acc.display_name, detail=detail)
            self.runtime_owner.handle_runtime_signal(
                acc,
                "fatal",
                RECOVERY_REASON,
                payload={"reason_msg": RECOVERY_BLOCK_REASON, "detail": detail},
            )
            return

        if is_captcha_text(detail):
            set_account_captcha_hold(
                acc,
                detail,
                source="cookie_validation",
                runtime_writer=self.state_mgr,
            )
            flog_kv("CAPTCHA", "detected", "warning", account=acc.display_name, detail=detail)
            self.runtime_owner.handle_runtime_signal(
                acc,
                "fatal",
                CAPTCHA_REASON,
                payload={"reason_msg": CAPTCHA_BLOCK_REASON, "detail": detail},
            )
            return

        flog(
            f"[WORKER] {acc.display_name} cookie invalid -> FAILED: {detail}",
            "warning",
        )
        self.runtime_owner.handle_runtime_signal(
            acc,
            "fatal",
            "cookie_invalid",
            payload={
                "reason_msg": self.REASON_MESSAGES["cookie_invalid"],
                "detail": detail,
            },
        )

    def _validate_cookie_loop(self, acc: Account) -> bool:
        validate_attempt = 0
        while not self._stop_event.is_set():
            if not self._wait_for_network():
                return False

            ok, username, detail, transient = self._validate_cookie(acc.cookie)
            if ok:
                return self._accept_cookie(acc, username)

            self._record_cookie_failure(acc, transient)

            if transient:
                validate_attempt += 1
                self._retry_cookie_validation(acc, validate_attempt, detail)
                continue

            self._handle_cookie_failure(acc, detail)
            return False
        return True

    def _mark_cookie_not_required(self, acc: Account) -> None:
        with acc._lock:
            acc.session_valid = True
            acc.session_checked = True
        flog(f"[WORKER] {acc.display_name} no cookie - skipping validation")

    def _initial_probe(self, acc: Account) -> bool:
        if acc.state == AccountState.IN_GAME:
            return False
        if not (
            self._rebind_live_game_process("initial_probe")
            or self._safe_adopt_visible_process("initial_probe_visible_adopt")
        ):
            return False
        with acc._lock:
            runtime_generation = acc.runtime_generation
            session_id = acc.session_id
            launch_nonce = acc.launch_nonce
            transaction_id = acc.rejoin_transaction_id
        self.runtime_owner.handle_runtime_signal(
            acc,
            "launch_success",
            "initial_probe",
            payload={"trigger": "initial_probe", "count_rejoin": False},
            expected_runtime_generation=runtime_generation,
            expected_session_id=session_id,
            expected_launch_nonce=launch_nonce,
            expected_transaction_id=transaction_id,
        )
        self._wake.wait(timeout=1.0)
        self._wake.clear()
        return True

    def _account_inactive(self, acc: Account) -> bool:
        try:
            from domain.account_model import is_account_finished as _is_finished
            from domain.account_model import is_account_locked as _is_locked_mid
            from domain.account_model import is_account_recovery as _is_recovery_mid
            from services.ban_guard import is_account_banned as _is_banned_mid
            from services.ban_guard import is_account_suspended as _is_suspended_mid
            from services.captcha_guard import is_account_captcha_required as _is_captcha_mid

            return bool(
                _is_finished(acc)
                or _is_banned_mid(acc)
                or _is_suspended_mid(acc)
                or _is_locked_mid(acc)
                or _is_recovery_mid(acc)
                or _is_captcha_mid(acc)
            )
        except Exception:
            try:
                from domain.account_model import is_account_finished as _is_finished2

                return bool(_is_finished2(acc))
            except Exception:
                return False

    def _idle(self, timeout: float) -> None:
        self._wake.wait(timeout=timeout)
        self._wake.clear()

    def _check_missing_process(self, acc: Account, crash_to: Any) -> bool:
        if acc.pid and ProcessManager.is_bound_game_alive(
            acc.pid,
            owner_key=acc._config_username,
            expected_identity=acc.bound_process_identity,
        ):
            with acc._lock:
                acc.pid_missing_since = 0.0
            return False
        status = self.handle_missing_bound_process("bound_pid_missing")
        if status == "rebound":
            self._idle(1.0)
            return True
        if status == "hold":
            self._idle(min(float(crash_to), 5.0))
            return True
        return True

    def _wait_timeout(self, acc: Account, crash_to: Any) -> float:
        wait_timeout = float(crash_to)
        if not (
            acc.pid
            and acc.in_game_since
            and self.cfg.get("connection_error_rejoin", True)
            and self.cfg.get("popup_disconnected_enabled", True)
        ):
            return wait_timeout
        return min(
            wait_timeout,
            max(1.0, float(self.cfg.get("popup_scan_interval_seconds", 2.0) or 2.0)),
        )

    def _monitor_loop(self, acc: Account) -> None:
        crash_to = self.cfg.get("crash_timeout", 30)
        while not self._stop_event.is_set():
            if self._account_inactive(acc):
                # Marked Finished/Banned/Suspended/Locked/Recovery/Captcha mid-run:
                # sit idle, no rejoin, no actions.
                self._idle(2.0)
                continue
            if acc.state != AccountState.IN_GAME:
                self._idle(2.0)
                continue
            if self._check_missing_process(acc, crash_to):
                continue
            if acc.pid and acc.in_game_since:
                runtime = time.time() - (acc.in_game_since or time.time())
                if handle_disconnect_checks(self, acc, runtime):
                    continue
            self._idle(self._wait_timeout(acc, crash_to))

    def run(self):
        acc = self.acc
        flog(f"[WORKER] {acc.display_name} started")

        if self._skip_finished(acc):
            return
        if self._auth_gate_blocked(acc):
            return

        if acc.cookie:
            if not self._validate_cookie_loop(acc):
                return
        else:
            self._mark_cookie_not_required(acc)

        self._initial_probe(acc)
        self.runtime_owner.request_evaluate(acc, trigger="initial_boot")
        self._monitor_loop(acc)

        flog(f"[WORKER] {acc.display_name} stopped")

    @staticmethod
    def _validate_cookie(cookie: str):
        try:
            req = urllib.request.Request(
                "https://users.roblox.com/v1/users/authenticated",
                headers={
                    "Cookie": f".ROBLOSECURITY={cookie.strip()}",
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/json",
                },
            )
            username, user_id = _authenticated_identity(req)
            return _authenticated_result(username, user_id, cookie)
        except urllib.error.HTTPError as e:
            return _http_error_result(e, cookie)
        except Exception as e:
            return False, "", str(e), True
