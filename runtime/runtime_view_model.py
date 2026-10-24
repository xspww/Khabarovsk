from __future__ import annotations

import threading
import time
from typing import Any

from core import (
    STATE_META,
    AccountState,
    account_launch_block_reason,
    flog_kv,
)
from runtime.account_selection import runtime_account_filter_reason
from runtime.account_worker import AccountWorker
from runtime.lua_liveness_policy import (
    LUA_WAITING_STATUS,
    account_lua_online,
    lua_liveness_required,
    lua_wait_timeout_seconds,
)
from runtime.runtime_health import account_health_flags, build_runtime_health
from runtime.runtime_truth import TRUTH_SUSPECT, build_account_truth
from services.account_hybrid import redact_secret
from services.ban_guard import (
    BANNED_BLOCK_REASON,
    BANNED_LABEL,
    SUSPENDED_BLOCK_REASON,
    SUSPENDED_LABEL,
    is_account_banned,
    is_account_suspended,
)
from services.captcha_guard import CAPTCHA_BLOCK_REASON, CAPTCHA_LABEL, is_account_captcha_required
from services.network_monitor import NET_ONLINE
from services.process_service import ProcessManager
from services.resource_monitor import get_rt_monitor
from services.verify_guard import (
    LOCKED_BLOCK_REASON,
    LOCKED_LABEL,
    RECOVERY_BLOCK_REASON,
    RECOVERY_LABEL,
    is_account_locked,
    is_account_recovery,
)
from version import app_display_version

IMPORTANT_RUNTIME_EVENTS = {
    "lua",
    "network",
    "error",
    "disconnect_detected",
    "process_lost",
    "network_lost",
    "network_restored",
    "captcha",
    "rejoin_requested",
    "runtime_rejoin_requested",
    "launch_success",
    "failed",
    "cooldown",
}


def _important_runtime_event(event: dict[str, Any]) -> bool:
    kind = str(event.get("event_type") or event.get("kind") or "").strip().lower()
    return kind in IMPORTANT_RUNTIME_EVENTS


class RuntimeViewModelBuilder:
    def __init__(self, farm: Any, include_diagnostics: bool = False):
        self._farm = farm
        self._include_diagnostics = bool(include_diagnostics)

    def _process_cache_ttl(self) -> float:
        if not bool(getattr(self._farm, "running", False)):
            return 0.0
        try:
            value = float(self._farm.cfg_mgr.get("dashboard_process_cache_ttl_seconds", 5.0) or 0.0)
        except Exception:
            value = 5.0
        if value <= 0:
            return 0.0
        return min(10.0, max(1.0, value))

    def _dashboard_cache(self) -> tuple[Any, dict]:
        lock = getattr(self._farm, "_dashboard_process_cache_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._farm._dashboard_process_cache_lock = lock
        cache = getattr(self._farm, "_dashboard_process_cache", None)
        if cache is None:
            cache = {}
            self._farm._dashboard_process_cache = cache
        return lock, cache

    def _process_cache_read(
        self, key: tuple, ttl: float, now: float
    ) -> dict[str, Any] | None:
        if ttl <= 0:
            return None
        lock, cache = self._dashboard_cache()
        with lock:
            cached = cache.get(key)
            if cached and now - float(cached[0] or 0.0) < ttl:
                return dict(cached[1])
        return None

    def _process_cache_write(
        self, key: tuple, result: dict[str, Any], ttl: float, now: float
    ) -> None:
        if ttl <= 0:
            return
        lock, cache = self._dashboard_cache()
        with lock:
            cache[key] = (now, dict(result))
            if len(cache) > 128:
                stale_keys = sorted(
                    cache, key=lambda item: float(cache[item][0] or 0.0)
                )[:32]
                for stale_key in stale_keys:
                    cache.pop(stale_key, None)

    def _process_status(self, acc: Any, pid: int | None, identity: str) -> dict[str, Any]:
        if not pid:
            return {"alive": False, "validation": {}, "not_responding": False}
        ttl = self._process_cache_ttl()
        identity_text = str(identity or "")
        tracker_text = str(getattr(acc, "browser_tracker_id", "") or "")
        key = (int(pid), str(acc._config_username), identity_text, tracker_text)
        now = time.time()
        cached = self._process_cache_read(key, ttl, now)
        if cached is not None:
            return cached
        try:
            validation = ProcessManager.validate_game_process(
                pid,
                owner_key=acc._config_username,
                expected_identity=identity,
                expected_browser_tracker_id=acc.browser_tracker_id,
                min_ram_mb=0.0,
            )
        except Exception:
            validation = {}
        alive = bool(validation.get("ok"))
        result = {
            "alive": alive,
            "validation": validation,
            "not_responding": bool(
                alive and ProcessManager.is_not_responding(pid)
            ),
        }
        self._process_cache_write(key, result, ttl, now)
        return result

    def _last_truth_cache(self) -> dict:
        state_cache = getattr(self._farm, "_last_runtime_truth_state", None)
        if state_cache is None:
            state_cache = {}
            self._farm._last_runtime_truth_state = state_cache
        return state_cache

    def _suspect_log_fields(self, acc: Any, truth: Any, account_id: str) -> dict:
        return {
            "account": getattr(acc, "display_name", account_id) or account_id,
            "account_id": account_id,
            "pid": truth.pid or getattr(acc, "pid", "") or "",
            "confidence": round(float(truth.confidence or 0.0), 1),
            "reasons": ",".join(list(truth.reasons or [])[:4]),
        }

    def _log_suspect(
        self, acc: Any, truth: Any, account_id: str, final: bool
    ) -> None:
        flog_kv(
            "RUNTIME",
            "suspect_process_check",
            "warning",
            final=final,
            **self._suspect_log_fields(acc, truth, account_id),
        )

    def _log_suspect_transition(self, acc: Any, truth: Any) -> None:
        account_id = str(
            getattr(acc, "_config_username", getattr(acc, "username", "")) or ""
        )
        key = account_id.lower()
        state_cache = self._last_truth_cache()
        previous = state_cache.get(key)
        state_cache[key] = truth.truth_state
        if truth.truth_state != TRUTH_SUSPECT:
            if previous == TRUTH_SUSPECT:
                self._log_suspect(acc, truth, account_id, final=True)
            return
        if previous == TRUTH_SUSPECT:
            return
        self._log_suspect(acc, truth, account_id, final=False)

    def build_status(self) -> dict:
        ctx = self._context()
        accounts_data = self._build_accounts(ctx)
        ctx["runtime_health"] = _runtime_health(ctx, accounts_data)
        payload = _status_payload(ctx, accounts_data)
        return _with_diagnostics(payload, ctx)

    def _recent_events(self, include_diagnostics: bool) -> list[dict[str, Any]]:
        if not include_diagnostics:
            return []
        try:
            return [
                event
                for event in self._farm._runtime_store.list_recent_events(limit=100)
                if _important_runtime_event(event)
            ]
        except Exception as e:
            flog_kv("RUNTIME", "recent_events_failed", "warning", error=e)
            return []

    def _diagnostic_snapshots(self) -> tuple[dict, dict, dict]:
        farm = self._farm
        include_diagnostics = self._include_diagnostics
        scheduler_snapshot: dict[str, Any] = {}
        if include_diagnostics and getattr(farm, "_runtime_scheduler", None):
            scheduler_snapshot = farm._runtime_scheduler.snapshot()
        machine_supervisor: dict[str, Any] = {}
        if include_diagnostics and getattr(farm, "_machine_supervisor", None):
            machine_supervisor = farm._machine_supervisor.snapshot().to_dict()
        recovery_storm: dict[str, Any] = {}
        if (
            include_diagnostics
            and getattr(getattr(farm, "_recovery", None), "_storm", None)
        ):
            recovery_storm = farm._recovery._storm.snapshot()
        return scheduler_snapshot, machine_supervisor, recovery_storm

    def _context(self) -> dict[str, Any]:
        farm = self._farm
        include_diagnostics = self._include_diagnostics
        uptime = int(time.time() - farm.start_ts) if farm.start_ts else 0
        h, r = divmod(uptime, 3600)
        m, s = divmod(r, 60)
        mon = get_rt_monitor() if include_diagnostics else None
        try:
            from services.roblox_multibox import multi_roblox_guard_status

            multi_guard = multi_roblox_guard_status()
        except Exception as exc:
            multi_guard = {
                "state": "unknown",
                "pid": 0,
                "detail": str(exc),
                "last_failure": str(exc),
                "handle_names": [],
            }
        global_command = farm.command_inflight("global")
        recent_runtime_events = self._recent_events(include_diagnostics)
        events_by_account: dict[str, list[dict[str, Any]]] = {}
        for event in recent_runtime_events:
            events_by_account.setdefault(
                str(event.get("account", "") or ""), []
            ).append(event)
        any_command_inflight = farm._command_tracker.any_inflight()
        cfg_snapshot = farm.cfg_mgr.snapshot()
        queue_snapshot = (
            farm._queue.snapshot()
            if farm._queue
            else {
                "size": 0,
                "pending": 0,
                "busy": False,
                "closed": not farm.running,
                "stale_rejections": 0,
                "oldest_age_seconds": 0,
                "entries": [],
            }
        )
        queue_entries_by_account = {
            str(item.get("account") or ""): item
            for item in (queue_snapshot.get("entries") or [])
            if isinstance(item, dict)
        }
        with farm._event_lock:
            total_rejoin = farm._total_rejoin
            total_crash = farm._total_crash
            event_log_count = len(farm._event_log)
            event_log = list(farm._event_log) if include_diagnostics else []
        with farm._status_lock:
            status_revision = int(farm._status_revision)
        scheduler_snapshot, machine_supervisor, recovery_storm = (
            self._diagnostic_snapshots()
        )
        return {
            "farm": farm,
            "include_diagnostics": include_diagnostics,
            "mon": mon,
            "multi_guard": multi_guard,
            "global_command": global_command,
            "recent_runtime_events": recent_runtime_events,
            "events_by_account": events_by_account,
            "any_command_inflight": any_command_inflight,
            "cfg_snapshot": cfg_snapshot,
            "lua_required": lua_liveness_required(cfg_snapshot),
            "lua_timeout": lua_wait_timeout_seconds(cfg_snapshot),
            "queue_snapshot": queue_snapshot,
            "queue_entries_by_account": queue_entries_by_account,
            "uptime_text": f"{h:02d}:{m:02d}:{s:02d}",
            "status_revision": status_revision,
            "total_rejoin": total_rejoin,
            "total_crash": total_crash,
            "event_log_count": event_log_count,
            "event_log": event_log,
            "scheduler_snapshot": scheduler_snapshot,
            "machine_supervisor": machine_supervisor,
            "recovery_storm": recovery_storm,
        }

    def _build_accounts(self, ctx: dict[str, Any]) -> list[dict[str, Any]]:
        return [self._account_payload(ctx, acc) for acc in self._farm._accounts]

    def _account_payload(
        self, ctx: dict[str, Any], acc: Any
    ) -> dict[str, Any]:
        row = self._account_row(ctx, acc)
        payload = _account_dict(ctx, row)
        if not ctx["include_diagnostics"]:
            for key in (
                "runtime",
                "launch_intent",
                "recent_runtime_events",
                "vip_tracker",
                "unmanaged_live_pids",
                "multi_roblox_guard_handles",
            ):
                payload.pop(key, None)
        runtime_truth = build_account_truth(
            acc,
            process_alive=row["pid_alive"],
            window_count=row["window_count"],
        )
        self._log_suspect_transition(acc, runtime_truth)
        payload["runtime_truth"] = runtime_truth.to_dict()
        payload["health_flags"] = account_health_flags(payload)
        return payload

    def _account_row(
        self, ctx: dict[str, Any], acc: Any
    ) -> dict[str, Any]:
        queue_entry = (
            ctx["queue_entries_by_account"].get(acc._config_username) or {}
        )
        runtime_snapshot = self._farm._runtime_state.snapshot(acc)
        snapshot_pid = _optional_int(runtime_snapshot.get("pid"))
        snapshot_identity = str(runtime_snapshot.get("process_identity") or "")
        snapshot_public = str(
            runtime_snapshot.get("public_state")
            or getattr(getattr(acc, "state", None), "name", "IDLE")
        )
        lua_online = (
            account_lua_online(acc, timeout=ctx["lua_timeout"])
            if ctx["lua_required"]
            else False
        )
        process_status = self._process_status(acc, snapshot_pid, snapshot_identity)
        pid_alive = bool(process_status.get("alive"))
        window_count = _process_windows(process_status)
        display_state = _display_state(
            snapshot_public,
            snapshot_pid,
            pid_alive,
            ctx["lua_required"],
            lua_online,
        )
        display_public_state = _display_public_state(
            display_state, ctx["lua_required"], lua_online, snapshot_public
        )
        meta = STATE_META.get(
            display_state, {"label": display_state.name, "color": "#6b7280"}
        )
        cpu, mem = _sample_process(
            ctx["mon"], snapshot_pid, pid_alive, ctx["include_diagnostics"]
        )
        is_nr = bool(
            display_state == AccountState.IN_GAME
            and process_status.get("not_responding")
        )
        vip_tracker_status = _vip_tracker_status(ctx["include_diagnostics"], acc)
        account_command = self._farm.command_inflight(
            f"account:{acc._config_username}"
        )
        recovery_step, recovery_step_index, recovery_step_started_at = (
            self._farm._recovery_step_for_account(acc, display_state)
        )
        active_runtime_action = bool(acc.recovery_inflight or account_command)
        waiting_patch = _waiting_patch(acc)
        game_closed = _game_closed(acc)
        network_lost = (
            display_state == AccountState.NETWORK_LOST
            or _network_recovery(acc)
        )
        label, color = _label_for_state(
            display_state,
            pid_alive,
            meta,
            waiting_patch,
            game_closed,
            network_lost,
            recovery_step,
            active_runtime_action,
        )
        label, color = _waiting_lua_label(
            label,
            color,
            display_state,
            snapshot_pid,
            pid_alive,
            ctx["lua_required"],
            lua_online,
            acc.recovery_status,
        )
        moderation = _account_moderation(acc, ctx["cfg_snapshot"])
        status = _hold_status_label(
            moderation["banned_required"],
            moderation["suspended_required"],
            moderation["locked_required"],
            moderation["recovery_required"],
            moderation["captcha_required"],
        )
        if status is not None:
            label, color = status
        cooldown_until = float(acc.cooldown_until or 0.0)
        liveness_state, liveness_score = _liveness_values(
            acc, pid_alive, snapshot_pid
        )
        return {
            "acc": acc,
            "queue_entry": queue_entry,
            "runtime_snapshot": runtime_snapshot,
            "snapshot_pid": snapshot_pid,
            "snapshot_identity": snapshot_identity,
            "snapshot_public": snapshot_public,
            "display_state": display_state,
            "display_public_state": display_public_state,
            "lua_required": ctx["lua_required"],
            "lua_online": lua_online,
            "process_status": process_status,
            "pid_alive": pid_alive,
            "window_count": window_count,
            "meta": meta,
            "cpu": cpu,
            "mem": mem,
            "is_nr": is_nr,
            "vip_tracker_status": vip_tracker_status,
            "account_command": account_command,
            "recovery_step": recovery_step,
            "recovery_step_index": recovery_step_index,
            "recovery_step_started_at": recovery_step_started_at,
            "state_label": label,
            "state_color": color,
            "cooldown_until": cooldown_until,
            "cooldown_left": max(0, int(cooldown_until - time.time())),
            "reported_liveness": liveness_state,
            "reported_liveness_score": liveness_score,
            **moderation,
        }


def _account_dict(ctx: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    acc = row["acc"]
    runtime_snapshot = row["runtime_snapshot"]
    payload: dict[str, Any] = {}
    payload.update(_core_fields(row))
    payload.update(_process_fields(row, ctx["include_diagnostics"]))
    payload.update(_stat_fields(row))
    payload.update(_recovery_row_fields(row))
    payload.update(_binding_fields(acc, runtime_snapshot))
    payload.update(_orphan_fields(acc, runtime_snapshot))
    payload.update(_runtime_fields(acc, runtime_snapshot))
    payload.update(_validation_fields(acc, runtime_snapshot))
    payload.update(_transaction_fields(acc, runtime_snapshot))
    payload.update(_session_times(acc))
    payload.update(_intent_fields(row, ctx))
    payload.update(_transition_fields(acc, runtime_snapshot, row))
    payload.update(_command_fields(ctx, row))
    return payload


def _core_fields(row: dict[str, Any]) -> dict[str, Any]:
    acc = row["acc"]
    return {
        "username": acc.username,
        "account_id": acc._config_username,
        "display": acc.display_name,
        "state": row["display_state"].name,
        "public_state": row["display_public_state"],
        "desired_state": row["runtime_snapshot"].get(
            "desired_public_state", acc.desired_state.name
        ),
        "state_label": row["state_label"],
        "state_color": row["state_color"],
        **_queue_fields(row["queue_entry"]),
        "lua_required": row["lua_required"],
        "lua_online": bool(row["lua_online"]),
        "lua_last_event": getattr(acc, "lua_last_event", "") or "",
        "lua_last_event_at": float(
            getattr(acc, "lua_last_event_at", 0.0) or 0.0
        ),
        "lua_in_game_at": float(getattr(acc, "lua_in_game_at", 0.0) or 0.0),
        "description": acc.description,
        "manual_status": acc.manual_status,
        "finished_at": float(acc.finished_at or 0.0),
        "launchable": row["launchable"],
        "blocked_reason": row["blocked_reason"],
        "captcha_required": bool(row["captcha_required"]),
        "locked_required": bool(row["locked_required"]),
        "recovery_required": bool(row["recovery_required"]),
        "locked": bool(row["locked_required"]),
        "account_recovery": bool(row["recovery_required"]),
        "banned": bool(row["permanent_ban_required"]),
        "suspended": bool(row["suspended_required"]),
        "import_status": getattr(acc, "import_status", ""),
        "cookie_username": acc.cookie_username,
        "cookie_user_id": acc.cookie_user_id,
        "user_id": getattr(acc, "user_id", "") or acc.cookie_user_id,
        "cookie_mismatch": bool(acc.cookie_mismatch),
    }


def _queue_fields(queue_entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "queue": dict(queue_entry),
        "queue_position": int(queue_entry.get("queue_position") or 0),
        "queue_reason": str(queue_entry.get("reason") or ""),
        "queue_ready": bool(queue_entry.get("ready", False)),
        "queue_due_in_seconds": float(
            queue_entry.get("due_in_seconds") or 0.0
        ),
        "queue_age_seconds": float(queue_entry.get("age_seconds") or 0.0),
        "queue_score": float(queue_entry.get("score") or 0.0),
    }


def _process_fields(
    row: dict[str, Any], include_diagnostics: bool
) -> dict[str, Any]:
    acc = row["acc"]
    runtime_snapshot = row["runtime_snapshot"]
    pid = row["snapshot_pid"]
    return {
        "pid": pid if row["pid_alive"] else None,
        "process_alive": row["pid_alive"],
        "process_name": runtime_snapshot.get(
            "process_name", acc.bound_process_name
        ),
        "process_identity": row["snapshot_identity"],
        "process_owner": (
            ProcessManager.get_pid_owner(pid)
            if (include_diagnostics and pid)
            else ""
        ),
        "server_type": acc.server_type.value if acc.server_type else "UNKNOWN",
        "active_vip": redact_secret(acc.active_vip),
        **_observed_fields(acc, runtime_snapshot),
    }


def _observed_fields(acc: Any, runtime_snapshot: dict) -> dict[str, Any]:
    observed_type = runtime_snapshot.get(
        "observed_server_type", acc.observed_server_type or ""
    )
    return {
        "observed_server_type": observed_type,
        "observed_is_vip": observed_type.upper() == "VIP",
        "observed_private_server_id": runtime_snapshot.get(
            "observed_private_server_id", acc.observed_private_server_id or ""
        ),
        "observed_private_server_owner_id": runtime_snapshot.get(
            "observed_private_server_owner_id",
            acc.observed_private_server_owner_id or "",
        ),
        "observed_place_id": runtime_snapshot.get(
            "observed_place_id", acc.observed_place_id or ""
        ),
        "observed_job_id": runtime_snapshot.get(
            "observed_job_id", acc.observed_job_id or ""
        ),
        "observed_universe_id": runtime_snapshot.get(
            "observed_universe_id", acc.observed_universe_id or ""
        ),
        "observed_server_at": float(
            runtime_snapshot.get(
                "observed_server_at", acc.observed_server_at or 0.0
            )
            or 0.0
        ),
    }


def _stat_fields(row: dict[str, Any]) -> dict[str, Any]:
    acc = row["acc"]
    return {
        "uptime": acc.uptime_str,
        "retry": acc.retry_count,
        "retry_count": acc.retry_count,
        "crash": acc.crash_count,
        "crash_count": acc.crash_count,
        "fail": acc.fail_count,
        "fail_count": acc.fail_count,
        "recovery_budget_count": len(acc.recovery_budget_attempts or []),
        "cpu": row["cpu"],
        "mem_mb": row["mem"],
        "is_vip": acc.is_vip,
        "session_valid": acc.session_valid,
        "last_crash_reason": acc.last_crash_reason,
        "last_state_reason": acc.last_state_reason,
        "last_state_change_at": float(acc.last_state_change_at or 0.0),
        "last_pid_change_at": float(acc.last_pid_change_at or 0.0),
        "vip_tracker": row["vip_tracker_status"],
        "not_responding": row["is_nr"],
        "cooldown_until": row["cooldown_until"],
        "cooldown_left": row["cooldown_left"],
        "pid_missing_for": (
            max(0, int(time.time() - acc.pid_missing_since))
            if acc.pid_missing_since
            else 0
        ),
        "ownership_confidence": round(
            float(acc.ownership_confidence or 0.0), 1
        ),
        "signal_confidence": round(
            float(acc.last_signal_confidence or 0.0), 1
        ),
        **_text_fields(acc),
    }


def _text_fields(acc: Any) -> dict[str, str]:
    return {
        "launch_strategy": acc.launch_strategy or "",
        "recovery_status": acc.recovery_status or "",
        "last_recovery_reason": acc.last_recovery_reason or "",
    }


def _recovery_row_fields(row: dict[str, Any]) -> dict[str, Any]:
    acc = row["acc"]
    return {
        "recovery_step": row["recovery_step"],
        "recovery_step_index": row["recovery_step_index"],
        "recovery_step_started_at": row["recovery_step_started_at"],
        "watchdog_classification": acc.last_watchdog_classification or "",
        "liveness_state": row["reported_liveness"],
        "liveness_score": row["reported_liveness_score"],
    }


def _binding_fields(acc: Any, runtime_snapshot: dict) -> dict[str, Any]:
    return {
        "process_binding_status": acc.process_binding_status or "",
        "binding_decision": acc.binding_decision
        or runtime_snapshot.get("binding_decision", ""),
        "process_binding_confidence": round(
            float(
                acc.process_binding_confidence
                or runtime_snapshot.get("process_binding_confidence", 0.0)
                or 0.0
            ),
            1,
        ),
        "process_proof_level": acc.process_proof_level
        or runtime_snapshot.get("process_proof_level", "untrusted"),
        "process_reject_reason": acc.process_reject_reason
        or runtime_snapshot.get("process_reject_reason", ""),
        "process_owner_claim": acc.process_owner_claim
        or runtime_snapshot.get("process_owner_claim", ""),
    }


def _orphan_fields(acc: Any, runtime_snapshot: dict) -> dict[str, Any]:
    return {
        "unmanaged_live_process_count": int(
            acc.unmanaged_live_process_count
            or runtime_snapshot.get("unmanaged_live_process_count", 0)
            or 0
        ),
        "unmanaged_live_pids": list(
            acc.unmanaged_live_pids
            or runtime_snapshot.get("unmanaged_live_pids", [])
            or []
        ),
        "adopt_candidate_pid": acc.adopt_candidate_pid
        or runtime_snapshot.get("adopt_candidate_pid"),
        "adopt_reject_reason": acc.adopt_reject_reason
        or runtime_snapshot.get("adopt_reject_reason", ""),
        "orphan_confidence": round(float(acc.orphan_confidence or 0.0), 1),
    }


def _runtime_fields(acc: Any, runtime_snapshot: dict) -> dict[str, Any]:
    return {
        "runtime_state": runtime_snapshot.get("runtime_state", ""),
        "runtime": runtime_snapshot,
        "runtime_generation": runtime_snapshot.get("runtime_generation", 0),
        "recovery_generation": runtime_snapshot.get(
            "recovery_generation", acc.recovery_generation
        ),
        "command_generation": runtime_snapshot.get(
            "command_generation", acc.command_generation
        ),
        "recovery_active": bool(runtime_snapshot.get("recovery_active", False)),
        "recovery_inflight": bool(
            runtime_snapshot.get("recovery_inflight", False)
        ),
        "recovery_reason": runtime_snapshot.get(
            "recovery_reason", acc.last_recovery_reason or ""
        ),
        "bind_status": runtime_snapshot.get(
            "bind_status", acc.process_binding_status or ""
        ),
        "binding_status": runtime_snapshot.get(
            "binding_status", acc.process_binding_status or ""
        ),
        "last_heartbeat": runtime_snapshot.get("last_heartbeat", 0.0),
        "session_id": runtime_snapshot.get("session_id", ""),
        "launch_nonce": runtime_snapshot.get("launch_nonce", ""),
        "account_runtime_id": runtime_snapshot.get("account_runtime_id", ""),
        "rejoin_transaction_id": runtime_snapshot.get(
            "rejoin_transaction_id", ""
        ),
    }


def _validation_fields(acc: Any, runtime_snapshot: dict) -> dict[str, Any]:
    return {
        "server_validation": runtime_snapshot.get(
            "server_validation", acc.server_validation or ""
        ),
        "destination_validation": runtime_snapshot.get(
            "destination_validation",
            acc.destination_validation or acc.server_validation or "",
        ),
        "scheduler_slot": runtime_snapshot.get(
            "scheduler_slot", acc.scheduler_slot or ""
        ),
        "supervisor_state": runtime_snapshot.get(
            "supervisor_state", acc.supervisor_state or ""
        ),
    }


def _transaction_fields(acc: Any, runtime_snapshot: dict) -> dict[str, Any]:
    return {
        "last_transaction_status": runtime_snapshot.get(
            "last_transaction_status", acc.last_transaction_status or ""
        ),
        "last_transaction_step": runtime_snapshot.get(
            "last_transaction_step", acc.last_transaction_step or ""
        ),
        "last_transaction_reason": runtime_snapshot.get(
            "last_transaction_reason", acc.last_transaction_reason or ""
        ),
        "last_transaction_started_at": runtime_snapshot.get(
            "last_transaction_started_at",
            float(acc.last_transaction_started_at or acc.session_started_at or 0.0),
        ),
        "last_transaction_completed_at": runtime_snapshot.get(
            "last_transaction_completed_at",
            float(acc.last_transaction_completed_at or 0.0),
        ),
        "last_transaction_failure_reason": runtime_snapshot.get(
            "last_transaction_failure_reason",
            acc.last_transaction_failure_reason or "",
        ),
    }


def _session_times(acc: Any) -> dict[str, float]:
    return {
        "session_started_at": float(acc.session_started_at or 0.0),
        "last_transaction_at": float(acc.last_transaction_at or 0.0),
    }


def _intent_fields(
    row: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    acc = row["acc"]
    runtime_snapshot = row["runtime_snapshot"]
    return {
        "launch_intent": dict(acc.launch_intent or {}),
        "launch_intent_summary": dict(
            acc.launch_intent_summary
            or runtime_snapshot.get("launch_intent_summary", {})
            or {}
        ),
        "recent_runtime_events": ctx["events_by_account"].get(
            acc._config_username, []
        )[:20],
    }


def _transition_fields(
    acc: Any, runtime_snapshot: dict, row: dict[str, Any]
) -> dict[str, Any]:
    return {
        "last_transition_at": runtime_snapshot.get("last_transition_at", 0.0),
        "last_transition_reason": runtime_snapshot.get(
            "last_transition_reason", ""
        ),
        "current_command": runtime_snapshot.get("current_command", ""),
        "command_inflight": row["account_command"],
    }


def _command_fields(ctx: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    farm = ctx["farm"]
    any_command_inflight = ctx["any_command_inflight"]
    display_state = row["display_state"]
    return {
        "can_start": bool((not farm.running) and not any_command_inflight),
        "can_stop": bool(farm.running and not any_command_inflight),
        "can_rejoin": bool(
            farm.running
            and not any_command_inflight
            and display_state != AccountState.FAILED
            and not row["blocked_reason"]
        ),
        "can_kill": bool(
            row["pid_alive"]
            and row["snapshot_pid"]
            and not any_command_inflight
        ),
    }


def _account_moderation(acc: Any, cfg_snapshot: dict) -> dict[str, Any]:
    banned = is_account_banned(acc)
    suspended = is_account_suspended(acc)
    locked = is_account_locked(acc)
    recovery = is_account_recovery(acc)
    captcha = is_account_captcha_required(acc) and not locked and not recovery
    blocked_reason = account_launch_block_reason(
        acc
    ) or runtime_account_filter_reason(acc, cfg_snapshot)
    blocked_reason = _hold_blocked_reason(
        blocked_reason, banned, suspended, locked, recovery, captcha
    )
    blocked_reason = _crash_blocked_reason(acc) or blocked_reason
    return {
        "banned_required": banned,
        "suspended_required": suspended,
        "permanent_ban_required": banned and not suspended,
        "locked_required": locked,
        "recovery_required": recovery,
        "captcha_required": captcha,
        "blocked_reason": blocked_reason,
        "launchable": not bool(blocked_reason),
    }


def _hold_blocked_reason(
    blocked_reason: str,
    banned: bool,
    suspended: bool,
    locked: bool,
    recovery: bool,
    captcha: bool,
) -> str:
    if suspended:
        return SUSPENDED_BLOCK_REASON
    if banned:
        return BANNED_BLOCK_REASON
    if locked:
        return LOCKED_BLOCK_REASON
    if recovery:
        return RECOVERY_BLOCK_REASON
    if captcha:
        return CAPTCHA_BLOCK_REASON
    return blocked_reason


def _crash_blocked_reason(acc: Any) -> str:
    if acc.last_crash_reason == "cookie_mismatch":
        return (
            acc.manual_status
            or acc.last_error
            or AccountWorker.REASON_MESSAGES.get(
                "cookie_mismatch", "cookie_mismatch"
            )
        )
    if acc.last_crash_reason == "multi_roblox_guard_failed":
        return (
            acc.manual_status
            or acc.last_error
            or AccountWorker.REASON_MESSAGES.get(
                "multi_roblox_guard_failed", "multi_roblox_guard_failed"
            )
        )
    return ""


def _hold_status_label(
    banned: bool,
    suspended: bool,
    locked: bool,
    recovery: bool,
    captcha: bool,
) -> tuple[str, str] | None:
    if suspended:
        return SUSPENDED_LABEL, "#f97316"
    if banned:
        return BANNED_LABEL, "#ef4444"
    if locked:
        return LOCKED_LABEL, "#fcd34d"
    if recovery:
        return RECOVERY_LABEL, "#fcd34d"
    if captcha:
        return CAPTCHA_LABEL, "#f0c76f"
    return None


def _label_for_state(
    display_state: AccountState,
    pid_alive: bool,
    meta: dict[str, Any],
    waiting_patch: bool,
    game_closed: bool,
    network_lost: bool,
    recovery_step: str,
    active_runtime_action: bool,
) -> tuple[str, str]:
    hold = _hold_label(
        display_state,
        pid_alive,
        waiting_patch,
        game_closed,
        network_lost,
    )
    if hold is not None:
        return hold
    return _recovery_label(
        display_state, pid_alive, recovery_step, active_runtime_action, meta
    )


def _hold_label(
    display_state: AccountState,
    pid_alive: bool,
    waiting_patch: bool,
    game_closed: bool,
    network_lost: bool,
) -> tuple[str, str] | None:
    if waiting_patch and not (
        display_state == AccountState.IN_GAME and pid_alive
    ):
        return "Wait for exploit patch", "#f0c76f"
    # A live network loss must outrank "Game Close": the account cannot
    # rejoin until the network is back.
    if network_lost and display_state != AccountState.IN_GAME:
        return "Disconnected", "#f97316"
    if game_closed and display_state != AccountState.IN_GAME:
        return "Game Close", "#fcd34d"
    return None


def _recovery_label(
    display_state: AccountState,
    pid_alive: bool,
    recovery_step: str,
    active_runtime_action: bool,
    meta: dict[str, Any],
) -> tuple[str, str]:
    in_game = display_state == AccountState.IN_GAME
    if recovery_step == "Rejoining" and (active_runtime_action or not in_game):
        return "Rejoining", "#a1a1aa"
    if recovery_step == "Disconnected" and not in_game:
        return "Disconnected", "#f97316"
    if in_game and pid_alive:
        return "In Game", meta["color"]
    return meta["label"], meta["color"]


def _waiting_lua_label(
    label: str,
    color: str,
    display_state: AccountState,
    snapshot_pid: int | None,
    pid_alive: bool,
    lua_required: bool,
    lua_online: bool,
    recovery_status: str,
) -> tuple[str, str]:
    if not lua_required or lua_online:
        return label, color
    if recovery_status == LUA_WAITING_STATUS or (
        display_state == AccountState.VERIFY and bool(snapshot_pid and pid_alive)
    ):
        return "Waiting For Lua", "#38bdf8"
    return label, color


def _waiting_patch(acc: Any) -> bool:
    return (
        str(acc.recovery_status or "").strip().lower()
        == "wait_exploit_patch"
        or "wait_exploit_patch"
        in str(acc.last_recovery_reason or "").lower()
    )


def _network_recovery(acc: Any) -> bool:
    """True while the account is inside a network-loss recovery.

    Network loss is a live condition, while ``last_crash_reason`` is only
    history: ``mark_network_lost`` updates the recovery status/reason but
    leaves the crash text untouched. Callers must let the network state
    outrank any historical crash classification.
    """
    if str(acc.recovery_status or "").strip().lower() == "network_lost":
        return True
    reason = str(acc.last_recovery_reason or "").lower()
    return "network_drop" in reason or "network_lost" in reason


def _game_closed(acc: Any) -> bool:
    if _network_recovery(acc):
        return False
    # Only an active recovery marker may keep the account on "Game Close".
    # last_crash_reason is historical and can still say game_unavailable
    # long after the map came back.
    return (
        str(acc.recovery_status or "").strip().lower() == "game_unavailable"
        or "game_unavailable" in str(acc.last_recovery_reason or "").lower()
    )


def _liveness_values(
    acc: Any, pid_alive: bool, snapshot_pid: int | None
) -> tuple[str, float]:
    if not pid_alive:
        return ("unbound" if snapshot_pid else "unknown"), 0.0
    return (
        acc.liveness_state or "",
        round(float(acc.liveness_score or 0.0), 1),
    )


def _sample_process(
    mon: Any,
    snapshot_pid: int | None,
    pid_alive: bool,
    include_diagnostics: bool,
) -> tuple[float, float]:
    if not include_diagnostics or not mon or not snapshot_pid or not pid_alive:
        return 0.0, 0.0
    return mon.get_cpu(snapshot_pid), mon.get_ram(snapshot_pid)


def _process_windows(process_status: dict) -> int:
    validation = dict(process_status.get("validation") or {})
    return int(validation.get("windows") or 0)


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _display_state(
    snapshot_public: str,
    snapshot_pid: int | None,
    pid_alive: bool,
    lua_required: bool,
    lua_online: bool,
) -> AccountState:
    display_state = AccountState.__members__.get(
        snapshot_public, AccountState.IDLE
    )
    if (
        display_state == AccountState.IN_GAME
        and snapshot_pid
        and not pid_alive
    ):
        display_state = AccountState.CRASH
    if (
        lua_required
        and not lua_online
        and display_state == AccountState.IN_GAME
    ):
        display_state = AccountState.VERIFY
    return display_state


def _display_public_state(
    display_state: AccountState,
    lua_required: bool,
    lua_online: bool,
    snapshot_public: str,
) -> str:
    if (
        lua_required
        and not lua_online
        and snapshot_public == "IN_GAME"
    ):
        return display_state.name
    return snapshot_public


def _vip_tracker_status(
    include_diagnostics: bool, acc: Any
) -> list[dict[str, Any]]:
    if not include_diagnostics or not acc._vip_tracker:
        return []
    try:
        return [
            {**item, "link": redact_secret(item.get("link", ""))}
            for item in acc._vip_tracker.status()
            if isinstance(item, dict)
        ]
    except Exception:
        return []


def _account_counts(accounts_data: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "blocked_count": sum(
            1 for a in accounts_data if a.get("blocked_reason")
        ),
        "launchable_count": sum(
            1 for a in accounts_data if a.get("launchable", True)
        ),
        "in_game_count": sum(
            1
            for a in accounts_data
            if a.get("state") == "IN_GAME" and not a.get("blocked_reason")
        ),
        "failed_count": sum(
            1
            for a in accounts_data
            if a.get("state") == "FAILED" or a.get("blocked_reason")
        ),
        "crash_count": sum(
            1
            for a in accounts_data
            if a.get("state") == "CRASH" and not a.get("blocked_reason")
        ),
        "launching_count": sum(
            1
            for a in accounts_data
            if a.get("state") in {"LAUNCHING", "VERIFY"}
            and not a.get("blocked_reason")
        ),
        "queued_count": sum(
            1
            for a in accounts_data
            if a.get("state") == "QUEUED" and not a.get("blocked_reason")
        ),
    }


def _farm_status_fields(ctx: dict[str, Any]) -> dict[str, Any]:
    farm = ctx["farm"]
    return {
        "running": farm.running,
        "status_revision": ctx["status_revision"],
        "status_updated_at": time.time(),
        "executor_hold": bool(
            getattr(farm, "is_executor_hold", lambda: False)()
        ),
        "executor_hold_reason": str(
            (getattr(farm, "get_executor_hold", dict)() or {}).get("reason")
            or ""
        ),
        "executor_hold_detail": str(
            (getattr(farm, "get_executor_hold", dict)() or {}).get("detail")
            or ""
        ),
        "app_version": app_display_version(),
        "uptime": ctx["uptime_text"],
        "total_accounts": len(farm._accounts),
    }


def _status_payload(
    ctx: dict[str, Any], accounts_data: list[dict[str, Any]]
) -> dict[str, Any]:
    farm = ctx["farm"]
    counts = _account_counts(accounts_data)
    multi_guard = ctx["multi_guard"]
    payload = _farm_status_fields(ctx)
    payload.update(
        {
            "launchable_count": counts["launchable_count"],
            "blocked_count": counts["blocked_count"],
            "in_game": counts["in_game_count"],
            "crash": counts["crash_count"],
            "launching": counts["launching_count"],
            "queued": counts["queued_count"],
            "failed": counts["failed_count"],
            "total_rejoin": ctx["total_rejoin"],
            "total_crash": ctx["total_crash"],
            "network_state": (
                farm._net_mon.get_state() if farm._net_mon else NET_ONLINE
            ),
            "runtime_state": "RUNNING" if farm.running else "STOPPED",
            "command_generation": farm._command_tracker.generation,
            "command_inflight": ctx["global_command"],
            "multi_roblox_guard_state": multi_guard.get("state", "unknown"),
            "multi_roblox_guard_pid": multi_guard.get("pid", 0),
            "multi_roblox_guard_detail": multi_guard.get("detail", ""),
            "last_multi_roblox_failure": multi_guard.get("last_failure", ""),
            "multi_roblox_guard_handles": multi_guard.get(
                "handle_names", []
            ),
            "queue_snapshot": ctx["queue_snapshot"],
            "scheduler_health": ctx["scheduler_snapshot"],
            "machine_supervisor": ctx["machine_supervisor"],
            "recovery_storm": ctx["recovery_storm"],
            "runtime_health": ctx["runtime_health"],
            "can_start": bool(
                (not farm.running) and not ctx["any_command_inflight"]
            ),
            "can_stop": bool(
                farm.running and not ctx["any_command_inflight"]
            ),
            "accounts": accounts_data,
            "event_log_count": ctx["event_log_count"],
            "recent_runtime_event_count": len(ctx["recent_runtime_events"]),
        }
    )
    return payload


def _runtime_health(
    ctx: dict[str, Any], accounts_data: list[dict[str, Any]]
) -> dict:
    if not ctx["include_diagnostics"]:
        return {}
    return build_runtime_health(
        accounts_data,
        ctx["queue_snapshot"],
        ctx["recent_runtime_events"],
        scheduler_snapshot=ctx["scheduler_snapshot"],
    )


def _with_diagnostics(
    payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    if not ctx["include_diagnostics"]:
        return payload
    payload.update(
        {
            "event_log": ctx["event_log"],
            "runtime_events": ctx["event_log"],
            "recent_runtime_events": ctx["recent_runtime_events"],
            "supervisor": ctx["farm"]._supervisor.snapshot(),
        }
    )
    return payload
