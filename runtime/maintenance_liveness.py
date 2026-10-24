from __future__ import annotations

import time
from typing import Any, NamedTuple

from core import Account, AccountState, flog, flog_kv
from runtime.lua_liveness_policy import LUA_WAITING_STATUS, lua_liveness_required, lua_wait_timeout_seconds
from runtime.maintenance_captcha import (
    clear_captcha_suspicion,
    handle_watchdog_captcha,
)
from runtime.maintenance_lua_timeout import emit_lua_wait_timeout_recovery, handle_in_game_lua_wait_timeout
from runtime.maintenance_performance import _apply_cpu_limiter_for_bound_process
from runtime.maintenance_verify import clear_verify_suspicion, handle_watchdog_verify
from runtime.maintenance_watchdog_actions import (
    handle_disconnect_dialog_rejoin,
    handle_frozen_recovery_signal,
    handle_memory_pressure_rejoin,
    log_memory_pressure_hold,
)
from services.captcha_guard import CAPTCHA_REASON
from services.process_service import ProcessManager, ProcessService
from services.verify_guard import LOCKED_REASON, RECOVERY_REASON

_UNSTABLE_STATES = frozenset({"suspect_frozen", "frozen", "reconnecting", "teleporting"})
_LOADING_STATES = frozenset({"loading", "reconnecting", "teleporting"})
_ALIVE_STATES = frozenset({"alive", "idle"})
_DIALOG_REJOIN_ACTIONS = frozenset({"rejoin", "conditional_rejoin"})
_FAST_HOLD_ERROR_CODES = frozenset({"267", "268", "273", "277", "279"})


def _first_time(*values: Any) -> float:
    for value in values:
        if value:
            return float(value)
    return 0.0


def _as_float(value: Any) -> float:
    return float(value or 0.0)


def _as_int(value: Any) -> int:
    return int(value or 0)


def _as_text(value: Any, default: str = "") -> str:
    return str(value or default)


def _is_lua_waiting(state: AccountState, recovery_status: Any, lua_required_now: bool) -> bool:
    return bool(
        state == AccountState.VERIFY
        and lua_required_now
        and str(recovery_status or "") == LUA_WAITING_STATUS
    )


class _JoiningSnapshot(NamedTuple):
    state: AccountState
    age: float
    pid: int | None
    identity: str
    browser_tracker_id: str
    runtime_generation: int
    session_id: str
    launch_nonce: str
    transaction_id: str
    recovery_status: str


class _FailedLiveSnapshot(NamedTuple):
    state: AccountState
    desired: AccountState
    last_launch_at: float | None
    pid: int | None
    runtime_generation: int
    identity: str


class _WatchdogConfig(NamedTuple):
    hold_sec: float
    activity_timeout: float
    loading_grace: float
    cpu_low: float
    startup_grace: float
    popup_scan_interval: float
    popup_scan_max_parallel: int
    popup_enabled: bool
    memory_guard_enabled: bool
    memory_guard_mb: float
    memory_guard_hold: float
    connection_hold: float


class _WatchdogContext(NamedTuple):
    now: float
    config: _WatchdogConfig
    net_online: bool
    lua_required_now: bool


class _WatchSnapshot(NamedTuple):
    state: AccountState
    lua_waiting: bool
    pid: int | None
    previous_cpu: float
    previous_ram: float
    in_game_for: float
    last_activity: float
    recovery_inflight: bool
    old_state: str


class _PopupFlags(NamedTuple):
    popup_periodic_allowed: bool
    lua_waiting_popup_allowed: bool
    inspect_ui: bool


class _LivenessEvent(NamedTuple):
    state: str
    score: float
    validation: dict[str, Any]
    reason_key: str
    dialog: dict[str, Any]
    log_evidence: dict[str, Any]
    cpu: float
    ram: float
    windows: int


class _PressureState(NamedTuple):
    hold: dict[str, Any] | None
    rejoin: dict[str, Any] | None


class _LoadingOutcome(NamedTuple):
    dialog_rejoin: dict[str, Any] | None
    skip: bool


def _joining_snapshot(acc: Account, now: float) -> _JoiningSnapshot:
    with acc._lock:
        return _JoiningSnapshot(
            state=acc.state,
            age=now - _first_time(acc.last_state_change_at, now),
            pid=acc.pid,
            identity=acc.bound_process_identity,
            browser_tracker_id=acc.browser_tracker_id,
            runtime_generation=acc.runtime_generation,
            session_id=acc.session_id,
            launch_nonce=acc.launch_nonce,
            transaction_id=acc.rejoin_transaction_id,
            recovery_status=str(acc.recovery_status or ""),
        )


def _failed_live_snapshot(acc: Account) -> _FailedLiveSnapshot:
    with acc._lock:
        return _FailedLiveSnapshot(
            state=acc.state,
            desired=acc.desired_state,
            last_launch_at=acc.last_launch_at,
            pid=acc.pid,
            runtime_generation=acc.runtime_generation,
            identity=acc.bound_process_identity,
        )


def _lua_wait_timed_out(snapshot: _JoiningSnapshot, lua_required_now: bool, lua_timeout: float) -> bool:
    return bool(
        snapshot.state == AccountState.VERIFY
        and lua_required_now
        and snapshot.recovery_status == LUA_WAITING_STATUS
        and snapshot.age >= lua_timeout
    )


def _lua_wait_still_pending(snapshot: _JoiningSnapshot, lua_required_now: bool) -> bool:
    return bool(
        snapshot.state == AccountState.VERIFY
        and lua_required_now
        and snapshot.recovery_status == LUA_WAITING_STATUS
    )


def _bound_game_alive(acc: Account, snapshot: _JoiningSnapshot) -> bool:
    return bool(
        snapshot.pid
        and ProcessManager.is_bound_game_alive(
            snapshot.pid,
            owner_key=acc._config_username,
            expected_identity=snapshot.identity,
            expected_browser_tracker_id=snapshot.browser_tracker_id,
        )
    )


def _log_stale_joining(acc: Account, snapshot: _JoiningSnapshot) -> None:
    flog_kv(
        "MAINT",
        "stale_joining_recovery",
        "warning",
        account=acc.display_name,
        state=snapshot.state.name,
        age=f"{snapshot.age:.1f}",
        pid=snapshot.pid or "",
    )


def _log_stale_queue(acc: Account, snapshot: _JoiningSnapshot) -> None:
    flog_kv(
        "MAINT",
        "stale_queue_recovery",
        "warning",
        account=acc.display_name,
        age=f"{snapshot.age:.1f}",
    )


def _apply_failed_live_rebind(acc: Account) -> None:
    with acc._lock:
        acc.pid_missing_since = 0.0
        acc.liveness_state = "alive"
        acc.liveness_score = max(float(acc.liveness_score or 0.0), 6.0)
        acc.last_watchdog_classification = "alive"
        acc.last_activity_at = time.time()
        acc.last_activity_reason = "failed_live_session_rebind"


def _popup_cursor(value: Any, count: int) -> int:
    try:
        cursor = int(value or 0)
    except Exception:
        cursor = 0
    return cursor % count


def _popup_last_batch(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _popup_parallel_count(max_parallel: Any) -> int:
    try:
        return max(1, int(max_parallel or 1))
    except Exception:
        return 1


def _cfg_float(cfg: dict, key: str, default: float, minimum: float | None = None) -> float:
    try:
        value = float(cfg.get(key, default) or default)
    except Exception:
        value = default
    if minimum is None:
        return value
    return max(minimum, value)


def _cfg_int(cfg: dict, key: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(float(cfg.get(key, default) or default))
    except Exception:
        value = default
    return max(minimum, value)


def _watchdog_config(cfg: dict) -> _WatchdogConfig:
    hold_sec = _cfg_float(cfg, "watchdog_hold_time", 60.0, 5.0)
    return _WatchdogConfig(
        hold_sec=hold_sec,
        activity_timeout=_cfg_float(cfg, "watchdog_activity_timeout", 180.0, hold_sec),
        loading_grace=_cfg_float(cfg, "watchdog_loading_grace", 90.0, 30.0),
        cpu_low=_cfg_float(cfg, "watchdog_cpu_low", 0.9),
        startup_grace=_cfg_float(cfg, "popup_startup_grace_seconds", 8.0, 0.0),
        popup_scan_interval=_cfg_float(cfg, "popup_scan_interval_seconds", 30.0, 5.0),
        popup_scan_max_parallel=_cfg_int(cfg, "popup_scan_max_parallel", 2, 1),
        popup_enabled=bool(cfg.get("popup_disconnected_enabled", True)),
        memory_guard_enabled=bool(cfg.get("roblox_memory_guard_enabled", True)),
        memory_guard_mb=_cfg_float(cfg, "roblox_memory_guard_mb", 6144.0, 512.0),
        memory_guard_hold=_cfg_float(cfg, "roblox_memory_guard_hold_seconds", 30.0, 5.0),
        connection_hold=_cfg_float(cfg, "connection_error_hold_time", 3.0, 1.0),
    )


def _account_watch_snapshot(acc: Account, now: float, lua_required_now: bool) -> _WatchSnapshot | None:
    with acc._lock:
        state = acc.state
        lua_waiting = _is_lua_waiting(state, acc.recovery_status, lua_required_now)
        if state != AccountState.IN_GAME and not lua_waiting:
            acc.liveness_suspect_since = 0.0
            return None
        if lua_waiting:
            started = _first_time(acc.last_state_change_at, acc.last_launch_at, now)
            last_activity = _first_time(acc.last_activity_at, acc.last_state_change_at, acc.last_launch_at, now)
        else:
            started = _first_time(acc.in_game_since, now)
            last_activity = _first_time(acc.last_activity_at, acc.in_game_since, now)
        return _WatchSnapshot(
            state=state,
            lua_waiting=lua_waiting,
            pid=acc.pid,
            previous_cpu=acc.last_activity_cpu,
            previous_ram=acc.last_activity_ram_mb,
            in_game_for=now - started,
            last_activity=last_activity,
            recovery_inflight=acc.recovery_inflight,
            old_state=acc.liveness_state,
        )


def _popup_candidate_age(acc: Account, now: float, lua_required_now: bool) -> tuple[bool, float | None]:
    with acc._lock:
        state = acc.state
        if not acc.pid:
            return False, None
        lua_waiting = _is_lua_waiting(state, acc.recovery_status, lua_required_now)
        if state == AccountState.IN_GAME:
            started = _first_time(acc.in_game_since, now)
        elif lua_waiting:
            started = _first_time(acc.last_state_change_at, acc.last_launch_at, now)
        else:
            return False, None
    return lua_waiting, now - started


def _popup_candidates(accounts: list[Account], ctx: _WatchdogContext) -> tuple[list[str], list[str]]:
    lua_waiting: list[str] = []
    regular: list[str] = []
    for candidate in accounts:
        is_lua, age = _popup_candidate_age(candidate, ctx.now, ctx.lua_required_now)
        if age is None or age < ctx.config.startup_grace:
            continue
        if is_lua:
            lua_waiting.append(candidate._config_username)
        else:
            regular.append(candidate._config_username)
    return lua_waiting, regular


def _popup_scan_interval(cfg: dict, popup_interval: float, lua_waiting: list[str]) -> float:
    if not lua_waiting:
        return popup_interval
    try:
        lua_scan_interval = float(cfg.get("lua_captcha_scan_interval_seconds", 5.0) or 5.0)
    except Exception:
        lua_scan_interval = 5.0
    return min(popup_interval, max(2.0, lua_scan_interval))


def _watchdog_missing(worker: Any) -> None:
    if worker:
        worker.handle_missing_bound_process("maintenance_pid_missing")


def _watchdog_inspect_ui(popup_enabled: bool, popup_periodic_allowed: bool, old_state: str) -> bool:
    if not popup_enabled:
        return False
    return popup_periodic_allowed or old_state in _UNSTABLE_STATES


def _popup_flags(
    popup_enabled: bool,
    popup_key: str,
    popup_batch_keys: set[str],
    lua_waiting_scan_keys: set[str],
    old_state: str,
) -> _PopupFlags:
    periodic = bool(popup_enabled and popup_key in popup_batch_keys)
    lua_allowed = popup_key in lua_waiting_scan_keys
    return _PopupFlags(periodic, lua_allowed, _watchdog_inspect_ui(popup_enabled, periodic, old_state))


def _liveness_event(liveness: dict[str, Any]) -> _LivenessEvent:
    validation = liveness.get("validation") or {}
    return _LivenessEvent(
        state=_as_text(liveness.get("state"), "unknown"),
        score=_as_float(liveness.get("score")),
        validation=validation,
        reason_key=_as_text(liveness.get("reason_key"), "watchdog_timeout"),
        dialog=liveness.get("dialog") or {},
        log_evidence=liveness.get("log_evidence") or {},
        cpu=_as_float(validation.get("cpu")),
        ram=_as_float(validation.get("ram_mb")),
        windows=_as_int(validation.get("windows")),
    )


def _log_log_evidence(acc: Account, pid: int, log_evidence: dict[str, Any]) -> None:
    if log_evidence.get("matched"):
        flog_kv(
            "WATCHDOG",
            "roblox_log_evidence",
            account=acc.display_name,
            pid=pid,
            error_code=log_evidence.get("error_code", ""),
            confidence=log_evidence.get("confidence", 0.0),
            source=log_evidence.get("source", "roblox_log"),
        )


def _log_liveness_change(acc: Account, pid: int, old_state: str, event: _LivenessEvent) -> None:
    flog_kv(
        "WATCHDOG",
        "liveness_change",
        account=acc.display_name,
        pid=pid,
        old=old_state,
        new=event.state,
        score=f"{event.score:.1f}",
        cpu=f"{event.cpu:.2f}",
        ram=f"{event.ram:.1f}",
        windows=event.windows,
    )


def _apply_memory_pressure(
    acc: Account,
    ctx: _WatchdogContext,
    ram: float,
    recovery_inflight: bool,
) -> _PressureState:
    cfg = ctx.config
    if cfg.memory_guard_enabled and ram >= cfg.memory_guard_mb and not recovery_inflight:
        if acc.resource_pressure_reason != "process_memory_pressure" or not acc.resource_pressure_since:
            acc.resource_pressure_since = ctx.now
            memory_high_for = 0.0
        else:
            memory_high_for = max(0.0, ctx.now - acc.resource_pressure_since)
        acc.resource_pressure_reason = "process_memory_pressure"
        acc.liveness_state = "memory_pressure"
        acc.last_watchdog_classification = "memory_pressure"
        acc.last_activity_reason = f"resource:memory_pressure:{ram:.1f}MB"
        payload = {
            "runtime_generation": acc.runtime_generation,
            "session_id": acc.session_id,
            "launch_nonce": acc.launch_nonce,
            "transaction_id": acc.rejoin_transaction_id,
            "ram_mb": ram,
            "limit_mb": cfg.memory_guard_mb,
            "high_for": memory_high_for,
        }
        if memory_high_for >= cfg.memory_guard_hold:
            acc.resource_pressure_since = 0.0
            return _PressureState(None, payload)
        return _PressureState(payload, None)
    if not cfg.memory_guard_enabled or ram < cfg.memory_guard_mb:
        acc.resource_pressure_since = 0.0
        acc.resource_pressure_reason = ""
    return _PressureState(None, None)


def _update_liveness_locked(
    acc: Account,
    ctx: _WatchdogContext,
    snap: _WatchSnapshot,
    event: _LivenessEvent,
) -> _PressureState:
    if event.state != acc.liveness_state:
        _log_liveness_change(acc, snap.pid, acc.liveness_state, event)
    acc.liveness_state = event.state
    acc.liveness_score = event.score
    acc.last_watchdog_classification = event.state
    acc.last_activity_cpu = event.cpu
    acc.last_activity_ram_mb = event.ram
    return _apply_memory_pressure(acc, ctx, event.ram, snap.recovery_inflight)


def _should_clear_alive(pressure: _PressureState, event: _LivenessEvent) -> bool:
    if pressure.hold or pressure.rejoin:
        return False
    return event.state in _ALIVE_STATES and event.score >= 4.0


def _dialog_rejoin_ready(ctx: _WatchdogContext, event: _LivenessEvent) -> bool:
    dialog = event.dialog
    return bool(
        event.state == "reconnecting"
        and ctx.config.popup_enabled
        and dialog.get("matched")
        and dialog.get("recovery_allowed")
        and _as_text(dialog.get("action"), "rejoin") in _DIALOG_REJOIN_ACTIONS
    )


def _reconnect_hold(acc: Account, ctx: _WatchdogContext, dialog: dict[str, Any]) -> tuple[float, float]:
    error_code = _as_text(dialog.get("error_code"))
    dialog_hold = 1.0 if error_code in _FAST_HOLD_ERROR_CODES else ctx.config.connection_hold
    if not acc.liveness_suspect_since:
        acc.liveness_suspect_since = ctx.now
        return 0.0, dialog_hold
    return ctx.now - acc.liveness_suspect_since, dialog_hold


def _dialog_rejoin_payload(acc: Account, event: _LivenessEvent, reconnecting_for: float) -> dict[str, Any]:
    dialog = event.dialog
    return {
        "runtime_generation": acc.runtime_generation,
        "session_id": acc.session_id,
        "launch_nonce": acc.launch_nonce,
        "transaction_id": acc.rejoin_transaction_id,
        "reason_key": event.reason_key or _as_text(dialog.get("reason_key"), "connection_error"),
        "detail": _as_text(dialog.get("detail")),
        "error_code": _as_text(dialog.get("error_code")),
        "action": _as_text(dialog.get("action"), "rejoin"),
        "popup_confidence": _as_float(dialog.get("popup_confidence", dialog.get("confidence", 0.0))),
        "disconnect_category": _as_text(dialog.get("disconnect_category")),
        "visual_disconnect": bool(dialog.get("visual_disconnect", False)),
        "evidence_source": _as_text(dialog.get("evidence_source")),
        "visual_evidence_source": _as_text(dialog.get("visual_evidence_source")),
        "reconnecting_for": reconnecting_for,
    }


def _update_suspect(acc: Account, now: float) -> float:
    if not acc.liveness_suspect_since:
        acc.liveness_suspect_since = now
        return 0.0
    return now - acc.liveness_suspect_since


class MaintenanceLivenessMixin:
    def _reconcile_duplicate_pid_claims(self):
        owners: dict[int, list[Account]] = {}
        for acc in self._accounts:
            if acc.pid:
                owners.setdefault(int(acc.pid), []).append(acc)
        for pid, accounts in owners.items():
            if len(accounts) <= 1:
                continue
            ordered = sorted(
                accounts,
                key=lambda item: (float(item.last_reconcile_at or 0.0), float(item.last_pid_change_at or 0.0)),
                reverse=True,
            )
            keeper = ordered[0]
            flog(
                f"[MAINT] duplicate PID claim detected for {pid}: "
                f"{', '.join(a.display_name for a in ordered)} -> keep {keeper.display_name}",
                "warning",
            )
            for acc in ordered[1:]:
                if acc.pid == pid:
                    self._state_mgr.clear_process_binding(acc, reason="duplicate_pid_claim")
                worker = self._workers.get(acc._config_username)
                if worker:
                    worker.wake()

    def _recover_stale_joining_states(self):
        now = time.time()
        verify_window = max(15.0, float(self._cfg.get("launch_verify_window", 25) or 25) + 10.0)
        queue_window = max(30.0, float(self._cfg.get("queue_timeout", 90) or 90))
        lua_required_now = lua_liveness_required(self._cfg)
        lua_timeout = lua_wait_timeout_seconds(self._cfg)
        for acc in self._accounts:
            snapshot = _joining_snapshot(acc, now)
            if snapshot.state in (AccountState.LAUNCHING, AccountState.VERIFY):
                self._recover_stale_verify(acc, snapshot, verify_window, lua_required_now, lua_timeout)
            elif snapshot.state == AccountState.QUEUED and snapshot.age >= queue_window:
                _log_stale_queue(acc, snapshot)
                self._runtime_evaluate(acc, trigger="queue_timeout")
            elif snapshot.state == AccountState.IN_GAME and handle_in_game_lua_wait_timeout(self, acc, self._cfg, now):
                continue

    def _recover_stale_verify(
        self,
        acc: Account,
        snapshot: _JoiningSnapshot,
        verify_window: float,
        lua_required_now: bool,
        lua_timeout: float,
    ) -> None:
        lua_wait_timed_out = _lua_wait_timed_out(snapshot, lua_required_now, lua_timeout)
        if snapshot.age < verify_window and not lua_wait_timed_out:
            return
        pid_live = _bound_game_alive(acc, snapshot)
        if pid_live and lua_wait_timed_out:
            self._emit_lua_wait_timeout(acc, snapshot, lua_timeout)
            return
        if pid_live and _lua_wait_still_pending(snapshot, lua_required_now):
            return
        if pid_live:
            self._signal_launch_success(acc, snapshot, "stale_joining_recovered")
            return
        _log_stale_joining(acc, snapshot)
        self._run_launch_failure(acc, snapshot)

    def _emit_lua_wait_timeout(self, acc: Account, snapshot: _JoiningSnapshot, lua_timeout: float) -> None:
        emit_lua_wait_timeout_recovery(
            self,
            acc,
            age=snapshot.age,
            timeout=lua_timeout,
            state_name=snapshot.state.name,
            runtime_generation=snapshot.runtime_generation,
            session_id=snapshot.session_id,
            launch_nonce=snapshot.launch_nonce,
            transaction_id=snapshot.transaction_id,
            pid=snapshot.pid,
            log_event="lua_wait_timeout_recovery",
            captcha_probe="lua_wait_timeout",
        )

    def _signal_launch_success(self, acc: Account, snapshot: _JoiningSnapshot, trigger: str) -> None:
        self._runtime_signal(
            acc,
            "launch_success",
            trigger,
            payload={"trigger": trigger, "count_rejoin": False},
            expected_runtime_generation=snapshot.runtime_generation,
            expected_session_id=snapshot.session_id,
            expected_launch_nonce=snapshot.launch_nonce,
            expected_transaction_id=snapshot.transaction_id,
        )

    def _run_launch_failure(self, acc: Account, snapshot: _JoiningSnapshot) -> None:
        self._runtime_signal(
            acc,
            "launch_failure",
            "launch_verify_timeout",
            payload={"detail": "launch_verify_timeout"},
            expected_runtime_generation=snapshot.runtime_generation,
            expected_session_id=snapshot.session_id,
            expected_launch_nonce=snapshot.launch_nonce,
            expected_transaction_id=snapshot.transaction_id,
        )

    def _recover_failed_live_sessions(self):
        for acc in self._accounts:
            snapshot = _failed_live_snapshot(acc)
            if snapshot.state != AccountState.FAILED or snapshot.desired != AccountState.IN_GAME:
                continue
            if not self._failed_live_rebind_allowed(acc, snapshot):
                continue
            self._rebind_failed_live(acc, snapshot)

    def _failed_live_rebind_allowed(self, acc: Account, snapshot: _FailedLiveSnapshot) -> bool:
        if not snapshot.pid or not snapshot.identity:
            live = ProcessManager.list_live_game_processes(launched_after=snapshot.last_launch_at)
            if live:
                flog_kv(
                    "MAINT",
                    "failed_live_rebind_skipped",
                    "warning",
                    account=acc.display_name,
                    candidates=len(live),
                    reason="missing_persisted_pid_identity",
                )
            return False
        if any(other is not acc and other.pid == snapshot.pid for other in self._accounts):
            flog_kv(
                "MAINT",
                "failed_live_rebind_skipped",
                "warning",
                account=acc.display_name,
                pid=snapshot.pid,
                reason="pid_claimed_by_other_account",
            )
            return False
        return True

    def _rebind_failed_live(self, acc: Account, snapshot: _FailedLiveSnapshot) -> None:
        current_pid = snapshot.pid
        bind_result = ProcessService.bind_account_process(
            acc,
            current_pid,
            self._state_mgr,
            reason="failed_live_session_rebind",
            expected_identity=snapshot.identity,
            launched_after=None,
            process_name=acc.bound_process_name or "RobloxPlayerBeta.exe",
            min_ram_mb=0.0,
            expected_runtime_generation=snapshot.runtime_generation,
        )
        if not bind_result.get("ok"):
            flog_kv(
                "MAINT",
                "failed_live_rebind_rejected",
                "warning",
                account=acc.display_name,
                pid=current_pid,
                reason=bind_result.get("reason", ""),
                previous_pid=current_pid or "",
            )
            return
        validation = bind_result.get("validation") or {}
        _apply_failed_live_rebind(acc)
        flog_kv(
            "MAINT",
            "failed_live_session_recovered",
            account=acc.display_name,
            pid=current_pid,
            previous_pid=current_pid or "",
            confidence=validation.get("confidence", 0.0),
        )
        _apply_cpu_limiter_for_bound_process(self._accounts, self._cfg, "failed_live_session_rebind", acc)
        self._signal_rebind_success(acc)

    def _signal_rebind_success(self, acc: Account) -> None:
        with acc._lock:
            generation = acc.runtime_generation
            session_id = acc.session_id
            launch_nonce = acc.launch_nonce
            transaction_id = acc.rejoin_transaction_id
        self._runtime_signal(
            acc,
            "launch_success",
            "failed_live_session_rebind",
            payload={"trigger": "failed_live_session_rebind", "count_rejoin": False},
            expected_runtime_generation=generation,
            expected_session_id=session_id,
            expected_launch_nonce=launch_nonce,
            expected_transaction_id=transaction_id,
        )

    def _popup_periodic_scan_batch(self, now: float, candidate_keys: list, interval: float, max_parallel: int) -> set:
        if not candidate_keys:
            self._popup_scan_cursor = 0
            return set()

        cursor = _popup_cursor(getattr(self, "_popup_scan_cursor", 0), len(candidate_keys))
        last_batch_at = _popup_last_batch(getattr(self, "_last_popup_batch_at", 0.0))
        if last_batch_at and (now - last_batch_at) < interval:
            return set()

        count = min(_popup_parallel_count(max_parallel), len(candidate_keys))
        selected = [candidate_keys[(cursor + offset) % len(candidate_keys)] for offset in range(count)]
        self._popup_scan_cursor = (cursor + count) % len(candidate_keys)
        self._last_popup_batch_at = now

        popup_scan_at = getattr(self, "_last_popup_scan_at", None)
        if popup_scan_at is None:
            popup_scan_at = {}
            self._last_popup_scan_at = popup_scan_at
        for key in selected:
            popup_scan_at[key] = now
        return set(selected)

    def _scan_liveness_watchdog(self):
        if not self._cfg.get("watchdog_enabled", True):
            return
        ctx = self._watchdog_context()
        popup_batch_keys, lua_waiting_scan_keys = self._watchdog_popup_batch(ctx)
        for acc in self._accounts:
            self._watchdog_one_account(acc, ctx, popup_batch_keys, lua_waiting_scan_keys)

    def _watchdog_context(self) -> _WatchdogContext:
        return _WatchdogContext(
            now=time.time(),
            config=_watchdog_config(self._cfg),
            net_online=self._recovery._net.is_online(),
            lua_required_now=lua_liveness_required(self._cfg),
        )

    def _watchdog_popup_batch(self, ctx: _WatchdogContext) -> tuple[set[str], set[str]]:
        popup_scan_at = getattr(self, "_last_popup_scan_at", None)
        if popup_scan_at is None:
            self._last_popup_scan_at = {}
        if not ctx.config.popup_enabled:
            return set(), set()
        lua_waiting, regular = _popup_candidates(self._accounts, ctx)
        scan_interval = _popup_scan_interval(self._cfg, ctx.config.popup_scan_interval, lua_waiting)
        batch = self._popup_periodic_scan_batch(
            ctx.now,
            lua_waiting + regular,
            scan_interval,
            ctx.config.popup_scan_max_parallel,
        )
        return batch, set(lua_waiting).intersection(batch)

    def _watchdog_one_account(
        self,
        acc: Account,
        ctx: _WatchdogContext,
        popup_batch_keys: set[str],
        lua_waiting_scan_keys: set[str],
    ) -> None:
        snap = _account_watch_snapshot(acc, ctx.now, ctx.lua_required_now)
        if snap is None:
            return
        worker = self._workers.get(acc._config_username)
        if not snap.pid:
            _watchdog_missing(worker)
            return
        if snap.in_game_for < ctx.config.startup_grace and not snap.recovery_inflight:
            return
        flags = _popup_flags(
            ctx.config.popup_enabled,
            acc._config_username,
            popup_batch_keys,
            lua_waiting_scan_keys,
            snap.old_state,
        )
        liveness = ProcessManager.assess_liveness(
            snap.pid,
            previous_cpu=snap.previous_cpu,
            previous_ram_mb=snap.previous_ram,
            net_online=ctx.net_online,
            recovery_inflight=snap.recovery_inflight,
            in_game_for=snap.in_game_for,
            loading_grace=ctx.config.loading_grace,
            cpu_threshold=ctx.config.cpu_low,
            inspect_ui=flags.inspect_ui,
            ui_sample_count=2 if flags.lua_waiting_popup_allowed else None,
        )
        event = _liveness_event(liveness)
        _log_log_evidence(acc, snap.pid, event.log_evidence)
        if flags.inspect_ui and self._watchdog_guard_popups(acc, snap.pid):
            return
        if event.state == "missing":
            _watchdog_missing(worker)
            return
        if snap.state != AccountState.IN_GAME:
            return
        self._watchdog_apply_liveness(acc, ctx, snap, event, worker)

    def _watchdog_guard_popups(self, acc: Account, pid: int) -> bool:
        try:
            from runtime.captcha_detector import inspect_captcha_window

            cap = inspect_captcha_window(pid)
        except Exception:
            cap = {"matched": False}
        if cap.get("matched") and _as_text(cap.get("reason_key")) == CAPTCHA_REASON:
            handle_watchdog_captcha(self, acc, pid, dict(cap))
            return True
        try:
            from runtime.verify_detector import inspect_verify_window

            ver = inspect_verify_window(pid)
        except Exception:
            ver = {"matched": False}
        if ver.get("matched") and _as_text(ver.get("reason_key")) in (LOCKED_REASON, RECOVERY_REASON):
            handle_watchdog_verify(self, acc, pid, dict(ver))
            return True
        clear_captcha_suspicion(acc)
        clear_verify_suspicion(acc)
        return False

    def _watchdog_apply_liveness(
        self,
        acc: Account,
        ctx: _WatchdogContext,
        snap: _WatchSnapshot,
        event: _LivenessEvent,
        worker: Any,
    ) -> None:
        inactive_for = max(0.0, ctx.now - snap.last_activity)
        outcome = _LoadingOutcome(None, False)
        with acc._lock:
            pressure = _update_liveness_locked(acc, ctx, snap, event)
            if _should_clear_alive(pressure, event):
                self._clear_disconnect_on_alive(acc, ctx, event)
                return
            if event.state in _LOADING_STATES:
                outcome = self._apply_loading_state(acc, ctx, snap, event)
                if outcome.skip:
                    return
            if snap.recovery_inflight:
                acc.liveness_suspect_since = 0.0
                return
            suspect_for = _update_suspect(acc, ctx.now)
        self._dispatch_watchdog(
            acc,
            ctx,
            snap,
            event,
            worker,
            inactive_for,
            suspect_for,
            pressure,
            outcome.dialog_rejoin,
        )

    def _clear_disconnect_on_alive(self, acc: Account, ctx: _WatchdogContext, event: _LivenessEvent) -> None:
        if acc.recovery_status in {"checking_disconnect", "disconnect_detected"} and not acc.recovery_inflight:
            self._set_recovery_status(
                acc,
                status="in_game",
                reason="liveness_alive_clear_disconnect_check",
                inflight=False,
            )
        acc.last_activity_at = ctx.now
        acc.last_activity_reason = f"liveness:{event.state}"
        acc.liveness_suspect_since = 0.0

    def _apply_loading_state(
        self,
        acc: Account,
        ctx: _WatchdogContext,
        snap: _WatchSnapshot,
        event: _LivenessEvent,
    ) -> _LoadingOutcome:
        if not _dialog_rejoin_ready(ctx, event):
            acc.liveness_suspect_since = 0.0
            self._mark_verified(acc, event.state)
            return _LoadingOutcome(None, True)
        reconnecting_for, dialog_hold = _reconnect_hold(acc, ctx, event.dialog)
        acc.last_watchdog_classification = "disconnect_dialog_hold"
        acc.last_activity_reason = f"dialog:{event.reason_key}"
        if reconnecting_for < dialog_hold or snap.recovery_inflight:
            self._set_disconnect_status(acc, event)
            self._mark_verified(acc, event.state)
            return _LoadingOutcome(None, True)
        payload = _dialog_rejoin_payload(acc, event, reconnecting_for)
        acc.liveness_suspect_since = 0.0
        acc.last_watchdog_classification = "disconnect_dialog_rejoin"
        acc.liveness_state = "reconnecting"
        self._mark_verified(acc, event.state)
        return _LoadingOutcome(payload, False)

    def _set_disconnect_status(self, acc: Account, event: _LivenessEvent) -> None:
        self._set_recovery_status(
            acc,
            status="disconnect_detected",
            reason=event.reason_key or _as_text(event.dialog.get("reason_key"), "connection_error"),
            inflight=False,
        )

    def _mark_verified(self, acc: Account, state: str) -> None:
        self._state_mgr.set_binding_status(acc, "verified", reason=f"liveness:{state}")

    def _dispatch_watchdog(
        self,
        acc: Account,
        ctx: _WatchdogContext,
        snap: _WatchSnapshot,
        event: _LivenessEvent,
        worker: Any,
        inactive_for: float,
        suspect_for: float,
        pressure: _PressureState,
        dialog_rejoin: dict[str, Any] | None,
    ) -> None:
        if pressure.rejoin:
            handle_memory_pressure_rejoin(self, acc, snap.pid, pressure.rejoin)
            return
        if pressure.hold:
            log_memory_pressure_hold(acc, snap.pid, pressure.hold, ctx.config.memory_guard_hold)
            return
        if dialog_rejoin:
            handle_disconnect_dialog_rejoin(self, acc, snap.pid, dialog_rejoin)
            return
        if inactive_for < ctx.config.activity_timeout or suspect_for < ctx.config.hold_sec:
            return
        handle_frozen_recovery_signal(
            self,
            acc,
            worker,
            snap.pid,
            event.reason_key,
            event.state,
            event.score,
            inactive_for,
            suspect_for,
            event.cpu,
            event.ram,
            event.windows,
        )
