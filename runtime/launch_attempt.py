from __future__ import annotations

import re
import threading
import time
from typing import Any, NamedTuple

from core import Account, AccountState, EventName, flog, flog_kv
from domain.session_identity import build_launch_intent
from runtime.maintenance_performance import _apply_cpu_limiter_for_bound_process
from services.process_service import ProcessManager, ProcessService


class _LaunchPrep(NamedTuple):
    launch_guard: dict[str, Any]
    warmup_delay: float
    before_pids: Any
    launch_ts: float
    attempted_vip: str


class _VerifyConfig(NamedTuple):
    verify_window: Any
    quick_bind_poll: float
    visible_probe_interval: float


class _VerifyContext(NamedTuple):
    controller: Any
    acc: Account
    stop: threading.Event
    prep: _LaunchPrep
    config: _VerifyConfig
    quick_bind_deadline: float


def _redact_launch_detail(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    text = re.sub(
        r"([?&](?:privateServerLinkCode|linkCode|code|accessCode|reservedServerAccessCode)=)[^&\s]+",
        r"\1<redacted>",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _configure_process_manager(controller: Any) -> bool:
    cfg = controller._cfg
    multi_roblox = bool(cfg.get("multi_roblox_enabled", True))
    ProcessManager.MULTI_ROBLOX_ENABLED = multi_roblox
    ProcessManager.GLOBAL_VIP_LINK = str(
        cfg.get("game_private_server_url", "") or ""
    ).strip()
    ProcessManager.AUTO_CREATE_PRIVATE_SERVER_ENABLED = bool(
        cfg.get("auto_create_private_server_enabled", False)
    )
    ProcessManager.AUTO_CREATE_PRIVATE_SERVER_FREE_ONLY = bool(
        cfg.get("auto_create_private_server_free_only", True)
    )
    ProcessManager.SHARED_PLACE_ID = str(
        cfg.get("game_place_id", "") or ""
    ).strip()
    try:
        from domain.games import normalize_game_mode

        ProcessManager.GAME_MODE = normalize_game_mode(
            cfg.get("game_mode", "shared")
        )
    except Exception:
        ProcessManager.GAME_MODE = "shared"
    try:
        from domain.games import ensure_games_migrated

        snap = dict(controller._cfg or {})
        ProcessManager.GAMES_SNAPSHOT = ensure_games_migrated(snap)
    except Exception:
        "intentional: ignore exception"
    return multi_roblox


def _prepare_direct_launch(multi_roblox: bool) -> None:
    protected_pids = (
        [
            int(item.get("pid"))
            for item in ProcessManager.list_live_game_processes()
            if item.get("pid")
        ]
        if multi_roblox
        else []
    )
    killed = ProcessService.kill_all_roblox_clients(
        wait_seconds=4.0,
        exclude_pids=protected_pids,
        reason="prepare_direct_launch",
    )
    if protected_pids:
        flog(
            "[LAUNCH] Preserving live Roblox game PID(s) during "
            f"relaunch: {sorted(set(protected_pids))}"
        )
    if killed:
        flog(
            f"[LAUNCH] Cleared {killed} existing Roblox client "
            "process(es) before relaunch"
        )
    time.sleep(1.2)


def _inject_cookie_for_direct_launch(
    acc: Account,
) -> tuple[bool, str]:
    if not acc.cookie:
        return False, "no cookie available"
    from services.cookie_service import IsolationManager

    return IsolationManager.inject_cookie(
        acc.username,
        acc.cookie,
    )


def _inject_shared_cookie(
    acc: Account,
    skip_shared_cookie_inject: bool,
) -> None:
    if skip_shared_cookie_inject:
        flog(
            f"[LAUNCH] Skipping shared cookie injection for "
            f"{acc.display_name} because Multi Roblox auth-ticket "
            "launch is enabled"
        )
        return
    ok, detail = _inject_cookie_for_direct_launch(acc)
    if not ok:
        flog(
            f"[LAUNCH] Cookie inject warning for "
            f"{acc.display_name}: {detail}",
            "warning",
        )
    else:
        flog(
            f"[LAUNCH] Cookie injected for "
            f"{acc.display_name}: {detail}"
        )


def _kill_stale_pid(
    controller: Any,
    acc: Account,
    launch_guard: dict[str, Any],
) -> None:
    stale_pid = acc.pid
    kill_result = ProcessService.safe_kill_bound_process(
        acc,
        controller._state_mgr,
        reason="prelaunch_stale_pid",
        expected_runtime_generation=launch_guard[
            "runtime_generation"
        ],
    )
    if not kill_result.get("killed"):
        flog_kv(
            "LAUNCH",
            "stale_pid_not_killed",
            "warning",
            account=acc.display_name,
            pid=stale_pid,
            reason=kill_result.get(
                "reason",
                "identity_or_owner_not_verified",
            ),
        )
    time.sleep(1.0)


def _handle_launch_failure(
    acc: Account,
    controller: Any,
    launch_guard: dict[str, Any],
    safe_detail: str,
    attempted_vip: str,
) -> None:
    flog(
        f"[LAUNCH] Failed for {acc.display_name}: {safe_detail}",
        "warning",
    )
    if controller._transaction_update(
        acc,
        status="failed",
        step="launch_failed",
        reason=str(safe_detail or "launch_failed"),
        server_validation="launch_failed",
        expected=launch_guard,
    ):
        controller._bus.emit(
            EventName.LAUNCH_FAILED,
            account=acc,
            reason=safe_detail,
        )
    if attempted_vip and acc._vip_tracker:
        try:
            from runtime.recovery_policy import (
                is_game_unavailable_detail as _is_game_down,
            )

            game_down = bool(_is_game_down(safe_detail))
        except Exception:
            game_down = False
        # Game closed for update: the VIP link is not at fault.
        # Blacklisting it would delay the rejoin when the map reopens.
        if not game_down:
            acc._vip_tracker.mark_crash(attempted_vip)


def _record_launch_intent(
    controller: Any,
    acc: Account,
    launch_guard: dict[str, Any],
) -> None:
    with acc._lock:
        controller._runtime_state.update_launch_intent(
            acc,
            build_launch_intent(acc, reason="launch_sent"),
            reason="launch_sent",
            expected_generation=launch_guard[
                "runtime_generation"
            ],
        )


def _launch_guard(acc: Account) -> dict[str, Any]:
    with acc._lock:
        return {
            "runtime_generation": acc.runtime_generation,
            "session_id": acc.session_id,
            "launch_nonce": acc.launch_nonce,
            "transaction_id": acc.rejoin_transaction_id,
        }


def _warmup_delay(controller: Any) -> float:
    return max(
        0.0,
        float(
            controller._cfg.get(
                "login_warmup_delay",
                6,
            )
            or 0
        ),
    )


def _restart_context(acc: Account) -> tuple[str, bool]:
    restart_reasons = {
        "watchdog_timeout",
        "loading_freeze",
        "teleport_timeout",
        "render_freeze",
    }
    with acc._lock:
        last_recovery_reason = str(
            acc.last_recovery_reason
            or acc.last_crash_reason
            or ""
        )
    return (
        last_recovery_reason,
        last_recovery_reason not in restart_reasons,
    )


def _skip_shared_cookie(acc: Account, multi_roblox: bool) -> bool:
    return bool(multi_roblox and acc.cookie)


def _launch_ts(acc: Account) -> float:
    return acc.last_launch_at or time.time()


def _verify_config(controller: Any) -> _VerifyConfig:
    verify_window = controller._cfg.get(
        "launch_verify_window",
        25,
    )
    try:
        quick_bind_poll = max(
            0.5,
            min(
                1.5,
                float(
                    controller._cfg.get(
                        "launch_quick_bind_poll_seconds",
                        0.75,
                    )
                    or 0.75
                ),
            ),
        )
    except Exception:
        quick_bind_poll = 0.75
    try:
        visible_probe_interval = max(
            1.5,
            min(
                4.0,
                float(
                    controller._cfg.get(
                        "launch_visible_probe_interval_seconds",
                        2.0,
                    )
                    or 2.0
                ),
            ),
        )
    except Exception:
        visible_probe_interval = 2.0
    return _VerifyConfig(
        verify_window=verify_window,
        quick_bind_poll=quick_bind_poll,
        visible_probe_interval=visible_probe_interval,
    )


def _cleanup_extra(
    acc: Account,
    before_pids: Any,
    launch_ts: float,
    reason: str,
    keep_pids: list[Any],
) -> int:
    return ProcessService.cleanup_extra_launch_processes(
        before_pids,
        keep_pids=keep_pids,
        launched_after=launch_ts,
        reason=reason,
        account=acc,
    )


def _bind_existing(
    ctx: _VerifyContext,
    reason: str,
    cleanup_reason: str,
) -> bool:
    if not ctx.controller._try_bind_any_live_game(
        ctx.acc,
        reason,
        launched_after=ctx.prep.launch_ts,
        expected_runtime_generation=ctx.prep.launch_guard[
            "runtime_generation"
        ],
    ):
        return False
    _cleanup_extra(
        ctx.acc,
        ctx.prep.before_pids,
        ctx.prep.launch_ts,
        cleanup_reason,
        [ctx.acc.pid] if ctx.acc.pid else [],
    )
    return True


def _adopt_visible(
    controller: Any,
    acc: Account,
    reason: str,
    launch_guard: dict[str, Any],
) -> dict[str, Any]:
    adopt = controller._safe_adopt_visible(
        acc,
        reason,
        expected_runtime_generation=launch_guard[
            "runtime_generation"
        ],
    )
    if adopt.get("ok"):
        controller._transaction_update(
            acc,
            status="process_bound",
            step="adopted_existing_window",
            reason="adopted_existing_window",
            server_validation=(
                "process_verified_destination_pending"
            ),
            expected=launch_guard,
        )
    return adopt


def _quick_bind(ctx: _VerifyContext) -> bool | None:
    next_visible_probe_at = time.time()
    while (
        not ctx.stop.is_set()
        and time.time() < ctx.quick_bind_deadline
    ):
        if _bind_existing(
            ctx,
            "post_launch_existing",
            "post_launch_existing_cleanup",
        ):
            return True
        now = time.time()
        if now >= next_visible_probe_at:
            next_visible_probe_at = (
                now + ctx.config.visible_probe_interval
            )
            presence = ctx.controller._visible_process_presence()
        else:
            presence = {}
        if int(presence.get("visible_count") or 0) == 1:
            adopt = _adopt_visible(
                ctx.controller,
                ctx.acc,
                "post_launch_visible_adopt",
                ctx.prep.launch_guard,
            )
            if adopt.get("ok"):
                return True
        ctx.stop.wait(timeout=ctx.config.quick_bind_poll)
    return None


def _handle_no_pid(ctx: _VerifyContext) -> bool:
    presence = ctx.controller._visible_process_presence()
    if presence.get("visible_count"):
        adopt = _adopt_visible(
            ctx.controller,
            ctx.acc,
            "verify_fallback_visible_adopt",
            ctx.prep.launch_guard,
        )
        if adopt.get("ok"):
            return True
    if _bind_existing(
        ctx,
        "verify_fallback",
        "verify_fallback_cleanup",
    ):
        return True
    flog(
        f"[LAUNCH] PID not detected for {ctx.acc.display_name} "
        f"within {ctx.config.verify_window}s",
        "warning",
    )
    if ctx.prep.attempted_vip and ctx.acc._vip_tracker:
        ctx.acc._vip_tracker.mark_crash(
            ctx.prep.attempted_vip
        )
    if ctx.controller._transaction_update(
        ctx.acc,
        status="failed",
        step="failed",
        reason="PID not detected",
        server_validation="unverified_no_pid",
        expected=ctx.prep.launch_guard,
    ):
        ctx.controller._bus.emit(
            EventName.LAUNCH_FAILED,
            account=ctx.acc,
            reason="PID not detected",
        )
    return False


def _validate_pid(
    ctx: _VerifyContext,
    pid: Any,
    reason: str,
) -> dict[str, Any]:
    return ProcessManager.validate_binding(
        ctx.acc,
        pid,
        reason=reason,
        launched_after=ctx.prep.launch_ts
        - max(0.0, ctx.prep.warmup_delay + 2.0),
        min_ram_mb=20.0,
        log_success=False,
        log_failure=False,
    )


def _is_transient_visible(
    pid_validation: dict[str, Any],
    presence: dict[str, Any],
) -> bool:
    return bool(
        pid_validation.get("ok")
        and int(pid_validation.get("windows") or 0) <= 0
        and presence.get("visible_count")
    )


def _is_no_window(pid_validation: dict[str, Any]) -> bool:
    return bool(
        pid_validation.get("ok")
        and int(pid_validation.get("windows") or 0) <= 0
    )


def _transient_reject_with_visible(
    ctx: _VerifyContext,
    pid: Any,
    presence: dict[str, Any],
) -> bool:
    flog_kv(
        "LAUNCH",
        "transient_launch_pid_rejected",
        "warning",
        account=ctx.acc.display_name,
        pid=pid,
        visible_pids=",".join(
            str(item)
            for item in presence.get("visible_pids", [])
        ),
        reason="no_window_with_visible_preserved_process",
    )
    with ctx.acc._lock:
        ctx.acc.process_reject_reason = (
            "transient_launch_pid_rejected"
        )
        ctx.acc.adopt_reject_reason = ""
        ctx.acc.sync_runtime(
            "transient_launch_pid_rejected"
        )
    adopt = _adopt_visible(
        ctx.controller,
        ctx.acc,
        "transient_pid_visible_adopt",
        ctx.prep.launch_guard,
    )
    if adopt.get("ok"):
        return True
    reason = (
        "visible process adopt failed: "
        f"{adopt.get('reason', 'unknown')}"
    )
    if ctx.controller._transaction_update(
        ctx.acc,
        status="failed",
        step="failed",
        reason=reason,
        server_validation="visible_process_unowned",
        expected=ctx.prep.launch_guard,
    ):
        ctx.controller._bus.emit(
            EventName.LAUNCH_FAILED,
            account=ctx.acc,
            reason=reason,
        )
    return False


def _settle_no_window(
    ctx: _VerifyContext,
    pid: Any,
    pid_validation: dict[str, Any],
) -> dict[str, Any]:
    settle_deadline = time.time() + min(
        3.0,
        max(1.0, float(ctx.config.verify_window) * 0.12),
    )
    stable_validation = dict(pid_validation)
    while (
        not ctx.stop.is_set()
        and time.time() < settle_deadline
    ):
        time.sleep(0.5)
        stable_validation = _validate_pid(
            ctx,
            pid,
            "post_launch_no_window_settle",
        )
        if not stable_validation.get("ok"):
            break
        stable_windows = int(
            stable_validation.get("windows") or 0
        )
        stable_ram = float(
            stable_validation.get("rss_mb")
            or stable_validation.get("ram_mb")
            or 0.0
        )
        if stable_windows > 0 or stable_ram >= 100.0:
            pid_validation = stable_validation
            break
    return pid_validation


def _window_or_runtime_missing(
    pid_validation: dict[str, Any],
) -> bool:
    final_windows = int(
        pid_validation.get("windows") or 0
    )
    final_ram = float(
        pid_validation.get("rss_mb")
        or pid_validation.get("ram_mb")
        or 0.0
    )
    return final_windows <= 0 and final_ram < 100.0


def _reject_no_window(
    ctx: _VerifyContext,
    pid: Any,
    pid_validation: dict[str, Any],
) -> bool:
    final_ram = float(
        pid_validation.get("rss_mb")
        or pid_validation.get("ram_mb")
        or 0.0
    )
    flog_kv(
        "LAUNCH",
        "transient_launch_pid_rejected",
        "warning",
        account=ctx.acc.display_name,
        pid=pid,
        reason="no_window_or_stable_runtime_after_settle",
        ram=f"{final_ram:.1f}",
    )
    with ctx.acc._lock:
        ctx.acc.process_reject_reason = (
            "transient_launch_pid_rejected_no_window"
        )
        ctx.acc.sync_runtime(
            "transient_launch_pid_rejected_no_window"
        )
    if ctx.controller._transaction_update(
        ctx.acc,
        status="failed",
        step="failed",
        reason=(
            "transient launch PID rejected: "
            "no window or stable runtime"
        ),
        server_validation="transient_launch_pid_rejected",
        expected=ctx.prep.launch_guard,
    ):
        ctx.controller._bus.emit(
            EventName.LAUNCH_FAILED,
            account=ctx.acc,
            reason="transient launch PID rejected",
        )
    return False


def _handle_pid_validation(
    ctx: _VerifyContext,
    pid: Any,
) -> bool | None:
    presence = ctx.controller._visible_process_presence(
        exclude_pids=[pid],
    )
    pid_validation = _validate_pid(
        ctx,
        pid,
        "post_launch_detected_precheck",
    )
    if _is_transient_visible(pid_validation, presence):
        return _transient_reject_with_visible(
            ctx,
            pid,
            presence,
        )
    if _is_no_window(pid_validation):
        settled = _settle_no_window(
            ctx,
            pid,
            pid_validation,
        )
        if _window_or_runtime_missing(settled):
            return _reject_no_window(ctx, pid, settled)
    return None


def _bind_and_finish(ctx: _VerifyContext, pid: Any) -> bool:
    bind_result = ProcessService.bind_account_process(
        ctx.acc,
        pid,
        ctx.controller._state_mgr,
        reason="post_launch_detected",
        launched_after=ctx.prep.launch_ts
        - max(0.0, ctx.prep.warmup_delay + 2.0),
        min_ram_mb=20.0,
        expected_runtime_generation=ctx.prep.launch_guard[
            "runtime_generation"
        ],
    )
    validation = bind_result.get("validation") or {}
    if not bind_result.get("ok"):
        flog_kv(
            "LAUNCH",
            "detected_pid_rejected",
            "warning",
            account=ctx.acc.display_name,
            pid=pid,
            reject=validation.get("reason", ""),
        )
        if ctx.controller._transaction_update(
            ctx.acc,
            status="failed",
            step="failed",
            reason=f"PID rejected: {validation.get('reason', '')}",
            server_validation="process_rejected",
            expected=ctx.prep.launch_guard,
        ):
            ctx.controller._bus.emit(
                EventName.LAUNCH_FAILED,
                account=ctx.acc,
                reason=(
                    "PID rejected: "
                    f"{validation.get('reason', '')}"
                ),
            )
        return False
    flog_kv(
        "LAUNCH",
        "pid_bound",
        account=ctx.acc.display_name,
        pid=pid,
        confidence=validation.get("confidence", 0.0),
    )
    ctx.controller._transaction_update(
        ctx.acc,
        status="process_bound",
        step="process_bound",
        reason="post_launch_detected",
        server_validation="process_verified_destination_pending",
        expected=ctx.prep.launch_guard,
    )
    if ctx.controller._runtime_store:
        ctx.controller._runtime_store.record_process_binding(
            ctx.acc._config_username,
            pid,
            ctx.acc.bound_process_identity,
            ctx.acc.session_id,
            ctx.acc.rejoin_transaction_id,
            "verified",
            "post_launch_detected",
        )
    _apply_cpu_limiter_for_bound_process(
        ctx.controller._accounts,
        ctx.controller._cfg,
        "post_launch_detected",
        ctx.acc,
    )
    extra_killed = _cleanup_extra(
        ctx.acc,
        ctx.prep.before_pids,
        ctx.prep.launch_ts,
        "post_launch_detected_cleanup",
        [pid],
    )
    if extra_killed:
        flog(
            f"[LAUNCH] Cleaned {extra_killed} leftover Roblox "
            f"process(es) after bind for {ctx.acc.display_name}"
        )
    if ctx.prep.attempted_vip and ctx.acc._vip_tracker:
        ctx.acc._vip_tracker.mark_success(
            ctx.prep.attempted_vip
        )
    ctx.controller._bus.emit(
        EventName.LAUNCH_SUCCESS,
        account=ctx.acc,
        pid=pid,
    )
    return True


class LaunchAttempt:
    def __init__(self, controller: Any, account: Account, stop: threading.Event):
        self.controller = controller
        self.account = account
        self.stop = stop

    def _prepare_locked(self) -> _LaunchPrep | bool:
        controller = self.controller
        acc = self.account
        stop = self.stop
        launch_guard = _launch_guard(acc)
        multi_roblox = _configure_process_manager(controller)
        warmup_delay = _warmup_delay(controller)
        last_recovery_reason, allow_existing_reuse = (
            _restart_context(acc)
        )
        existing_pid = (
            acc.pid
            if ProcessManager.is_bound_game_alive(
                acc.pid,
                owner_key=acc._config_username,
                expected_identity=acc.bound_process_identity,
                expected_browser_tracker_id=(
                    acc.browser_tracker_id
                ),
            )
            else None
        )
        existing_name = acc.bound_process_name
        if existing_pid:
            if allow_existing_reuse:
                return controller._bind_live_game(
                    acc,
                    existing_pid,
                    existing_name,
                    "prelaunch_probe",
                    expected_runtime_generation=launch_guard[
                        "runtime_generation"
                    ],
                )
            flog_kv(
                "LAUNCH",
                "verified_kill_deferred",
                "warning",
                account=acc.display_name,
                pid=existing_pid,
                reason=last_recovery_reason,
                runtime_generation=launch_guard[
                    "runtime_generation"
                ],
                session_id=launch_guard["session_id"],
                transaction_id=launch_guard[
                    "transaction_id"
                ],
            )
        ProcessManager.LOGIN_WARMUP_DELAY = warmup_delay
        skip_shared_cookie_inject = _skip_shared_cookie(
            acc,
            multi_roblox,
        )
        _prepare_direct_launch(multi_roblox)
        _inject_shared_cookie(
            acc,
            skip_shared_cookie_inject,
        )
        controller._limiter.wait(stop)
        if stop.is_set():
            return False
        before_pids = ProcessManager.snapshot_pids()
        if acc.pid:
            _kill_stale_pid(controller, acc, launch_guard)
        ok, detail, attempted_vip = ProcessManager.launch(acc)
        safe_detail = _redact_launch_detail(detail)
        if not ok:
            _handle_launch_failure(
                acc,
                controller,
                launch_guard,
                safe_detail,
                attempted_vip,
            )
            return False
        flog(
            f"[LAUNCH] Sent for {acc.display_name} "
            f"({safe_detail[:80]})"
        )
        _record_launch_intent(
            controller,
            acc,
            launch_guard,
        )
        if not controller._transaction_update(
            acc,
            status="launching",
            step="launch_sent",
            reason="launch_sent",
            server_validation="intent_recorded",
            expected=launch_guard,
        ):
            return False
        controller._state_mgr.transition(
            acc,
            AccountState.VERIFY,
            reason="launch_sent",
            expected_generation=launch_guard[
                "runtime_generation"
            ],
        )
        launch_ts = _launch_ts(acc)
        return _LaunchPrep(
            launch_guard=launch_guard,
            warmup_delay=warmup_delay,
            before_pids=before_pids,
            launch_ts=launch_ts,
            attempted_vip=attempted_vip,
        )

    def _verify(self, prep: _LaunchPrep) -> bool:
        controller = self.controller
        acc = self.account
        stop = self.stop
        config = _verify_config(controller)
        quick_bind_deadline = time.time() + min(
            6.0,
            max(1.0, float(config.verify_window) / 3.0),
        )
        ctx = _VerifyContext(
            controller=controller,
            acc=acc,
            stop=stop,
            prep=prep,
            config=config,
            quick_bind_deadline=quick_bind_deadline,
        )
        bound = _quick_bind(ctx)
        if bound is not None:
            return bound
        pid = ProcessManager.detect_new_pid(
            prep.before_pids,
            timeout=config.verify_window,
            launched_after=prep.launch_ts,
            created_after_slack=prep.warmup_delay + 2.0,
            expected_browser_tracker_id=(
                acc.browser_tracker_id
            ),
        )
        if stop.is_set():
            return False
        if not pid:
            return _handle_no_pid(ctx)
        result = _handle_pid_validation(ctx, pid)
        if result is not None:
            return result
        return _bind_and_finish(ctx, pid)

    def run(self) -> bool:
        with self.controller._lock:
            prep = self._prepare_locked()
            if isinstance(prep, bool):
                return prep
        return self._verify(prep)
