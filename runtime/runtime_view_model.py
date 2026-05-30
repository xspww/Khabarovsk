from __future__ import annotations

import time
import threading
from typing import Any, Dict, List

from account_hybrid import redact_secret
from core import AccountState, account_launch_block_reason, flog_kv
from services.network_monitor import NET_ONLINE
from services.process_service import ProcessManager
from services.resource_monitor import get_rt_monitor
from services.captcha_guard import CAPTCHA_BLOCK_REASON, CAPTCHA_LABEL, is_account_captcha_required
from runtime.account_worker import AccountWorker
from runtime.account_selection import runtime_account_filter_reason
from runtime.runtime_health import account_health_flags, build_runtime_health
from runtime.runtime_truth import TRUTH_SUSPECT, build_account_truth
from runtime.lua_liveness_policy import LUA_WAITING_STATUS, account_lua_online, lua_liveness_required


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


def _important_runtime_event(event: Dict[str, Any]) -> bool:
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

    def _process_status(self, acc: Any, pid: int | None, identity: str) -> Dict[str, Any]:
        if not pid:
            return {"alive": False, "validation": {}, "not_responding": False}
        ttl = self._process_cache_ttl()
        key = (int(pid), str(acc._config_username), str(identity or ""), str(getattr(acc, "browser_tracker_id", "") or ""))
        now = time.time()
        if ttl > 0:
            lock = getattr(self._farm, "_dashboard_process_cache_lock", None)
            if lock is None:
                lock = threading.Lock()
                setattr(self._farm, "_dashboard_process_cache_lock", lock)
            cache = getattr(self._farm, "_dashboard_process_cache", None)
            if cache is None:
                cache = {}
                setattr(self._farm, "_dashboard_process_cache", cache)
            with lock:
                cached = cache.get(key)
                if cached and now - float(cached[0] or 0.0) < ttl:
                    return dict(cached[1])
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
            "not_responding": bool(alive and ProcessManager.is_not_responding(pid)),
        }
        if ttl > 0:
            with lock:
                cache[key] = (now, dict(result))
                if len(cache) > 128:
                    stale_keys = sorted(cache, key=lambda item: float(cache[item][0] or 0.0))[:32]
                    for stale_key in stale_keys:
                        cache.pop(stale_key, None)
        return result

    def _log_suspect_transition(self, acc: Any, truth: Any) -> None:
        account_id = str(getattr(acc, "_config_username", getattr(acc, "username", "")) or "")
        key = account_id.lower()
        state_cache = getattr(self._farm, "_last_runtime_truth_state", None)
        if state_cache is None:
            state_cache = {}
            setattr(self._farm, "_last_runtime_truth_state", state_cache)
        previous = state_cache.get(key)
        state_cache[key] = truth.truth_state
        if truth.truth_state != TRUTH_SUSPECT:
            if previous == TRUTH_SUSPECT:
                flog_kv(
                    "RUNTIME",
                    "suspect_process_check",
                    "warning",
                    account=getattr(acc, "display_name", account_id) or account_id,
                    account_id=account_id,
                    pid=truth.pid or getattr(acc, "pid", "") or "",
                    confidence=round(float(truth.confidence or 0.0), 1),
                    final=True,
                    reasons=",".join(list(truth.reasons or [])[:4]),
                )
            return
        if previous == TRUTH_SUSPECT:
            return
        flog_kv(
            "RUNTIME",
            "suspect_process_check",
            "warning",
            account=getattr(acc, "display_name", account_id) or account_id,
            account_id=account_id,
            pid=truth.pid or getattr(acc, "pid", "") or "",
            confidence=round(float(truth.confidence or 0.0), 1),
            final=False,
            reasons=",".join(list(truth.reasons or [])[:4]),
        )

    def build_status(self) -> dict:
        farm = self._farm
        from core import STATE_META

        include_diagnostics = self._include_diagnostics
        uptime = int(time.time() - farm.start_ts) if farm.start_ts else 0
        h, r = divmod(uptime, 3600)
        m, s = divmod(r, 60)
        mon = get_rt_monitor() if include_diagnostics else None
        accounts_data = []
        try:
            from roblox_hybrid import multi_roblox_guard_status

            multi_guard = multi_roblox_guard_status()
        except Exception as exc:
            multi_guard = {"state": "unknown", "pid": 0, "detail": str(exc), "last_failure": str(exc), "handle_names": []}
        global_command = farm.command_inflight("global")
        if include_diagnostics:
            try:
                recent_runtime_events = [
                    event
                    for event in farm._runtime_store.list_recent_events(limit=100)
                    if _important_runtime_event(event)
                ]
            except Exception as e:
                flog_kv("RUNTIME", "recent_events_failed", "warning", error=e)
                recent_runtime_events = []
        else:
            recent_runtime_events = []
        events_by_account: Dict[str, List[Dict[str, Any]]] = {}
        for event in recent_runtime_events:
            events_by_account.setdefault(str(event.get("account", "") or ""), []).append(event)

        any_command_inflight = farm._command_tracker.any_inflight()
        cfg_snapshot = farm.cfg_mgr.snapshot()
        lua_required = lua_liveness_required(cfg_snapshot)
        lua_timeout = float(cfg_snapshot.get("heartbeat_timeout", 60) or 60)
        queue_snapshot = farm._queue.snapshot() if farm._queue else {
            "size": 0,
            "pending": 0,
            "busy": False,
            "closed": not farm.running,
            "stale_rejections": 0,
            "oldest_age_seconds": 0,
            "entries": [],
        }
        queue_entries_by_account = {
            str(item.get("account") or ""): item
            for item in (queue_snapshot.get("entries") or [])
            if isinstance(item, dict)
        }

        for acc in farm._accounts:
            queue_entry = queue_entries_by_account.get(acc._config_username) or {}
            runtime_snapshot = farm._runtime_state.snapshot(acc)
            snapshot_pid = runtime_snapshot.get("pid")
            try:
                snapshot_pid = int(snapshot_pid) if snapshot_pid else None
            except (TypeError, ValueError):
                snapshot_pid = None
            snapshot_identity = str(runtime_snapshot.get("process_identity") or "")
            snapshot_public = str(runtime_snapshot.get("public_state") or getattr(getattr(acc, "state", None), "name", "IDLE"))
            display_state = AccountState.__members__.get(snapshot_public, AccountState.IDLE)
            lua_online = account_lua_online(acc, timeout=lua_timeout) if lua_required else False
            process_status = self._process_status(acc, snapshot_pid, snapshot_identity)
            pid_alive = bool(process_status.get("alive"))
            process_validation = dict(process_status.get("validation") or {})
            window_count = int(process_validation.get("windows") or 0)
            if display_state == AccountState.IN_GAME and snapshot_pid and not pid_alive:
                display_state = AccountState.CRASH
            if lua_required and not lua_online and display_state == AccountState.IN_GAME:
                display_state = AccountState.VERIFY
            display_public_state = display_state.name if lua_required and not lua_online and snapshot_public == "IN_GAME" else snapshot_public

            meta = STATE_META.get(display_state, {"label": display_state.name, "color": "#6b7280"})
            cpu = mon.get_cpu(snapshot_pid) if (include_diagnostics and mon and snapshot_pid and pid_alive) else 0.0
            mem = mon.get_ram(snapshot_pid) if (include_diagnostics and mon and snapshot_pid and pid_alive) else 0.0
            is_nr = bool(display_state == AccountState.IN_GAME and process_status.get("not_responding"))
            vip_tracker_status = []
            if include_diagnostics and acc._vip_tracker:
                try:
                    vip_tracker_status = [
                        {**item, "link": redact_secret(item.get("link", ""))}
                        for item in acc._vip_tracker.status()
                        if isinstance(item, dict)
                    ]
                except Exception:
                    pass
            account_command = farm.command_inflight(f"account:{acc._config_username}")
            recovery_step, recovery_step_index, recovery_step_started_at = farm._recovery_step_for_account(acc, display_state)
            state_label = meta["label"]
            state_color = meta["color"]
            active_runtime_action = bool(acc.recovery_inflight or account_command)
            if recovery_step == "Rejoining" and (active_runtime_action or display_state != AccountState.IN_GAME):
                state_label = "Rejoining"
                state_color = "#a1a1aa"
            elif recovery_step == "Disconnected" and display_state != AccountState.IN_GAME:
                state_label = "Disconnected"
                state_color = "#f97316"
            elif display_state == AccountState.IN_GAME and pid_alive:
                state_label = "In Game"
                state_color = meta["color"]
            if lua_required and not lua_online and (
                str(acc.recovery_status or "") == LUA_WAITING_STATUS
                or (display_state == AccountState.VERIFY and bool(snapshot_pid and pid_alive))
            ):
                state_label = "Waiting For Lua"
                state_color = "#38bdf8"
            cooldown_until = float(acc.cooldown_until or 0.0)
            cooldown_left = max(0, int(cooldown_until - time.time()))
            captcha_required = is_account_captcha_required(acc)
            if captcha_required:
                state_label = CAPTCHA_LABEL
                state_color = "#f0c76f"
            blocked_reason = account_launch_block_reason(acc) or runtime_account_filter_reason(acc, cfg_snapshot)
            if captcha_required:
                blocked_reason = CAPTCHA_BLOCK_REASON
            if not blocked_reason and acc.last_crash_reason == "cookie_mismatch":
                blocked_reason = acc.manual_status or acc.last_error or AccountWorker.REASON_MESSAGES.get("cookie_mismatch", "cookie_mismatch")
            if not blocked_reason and acc.last_crash_reason == "multi_roblox_guard_failed":
                blocked_reason = acc.manual_status or acc.last_error or AccountWorker.REASON_MESSAGES.get("multi_roblox_guard_failed", "multi_roblox_guard_failed")
            launchable = not bool(blocked_reason)
            reported_liveness = acc.liveness_state or ""
            reported_liveness_score = round(float(acc.liveness_score or 0.0), 1)
            if not pid_alive:
                reported_liveness = "unbound" if snapshot_pid else "unknown"
                reported_liveness_score = 0.0
            account_payload = {
                "username": acc.username,
                "account_id": acc._config_username,
                "display": acc.display_name,
                "state": display_state.name,
                "public_state": display_public_state,
                "desired_state": runtime_snapshot.get("desired_public_state", acc.desired_state.name),
                "state_label": state_label,
                "state_color": state_color,
                "queue": dict(queue_entry),
                "queue_position": int(queue_entry.get("queue_position") or 0),
                "queue_reason": str(queue_entry.get("reason") or ""),
                "queue_ready": bool(queue_entry.get("ready", False)),
                "queue_due_in_seconds": float(queue_entry.get("due_in_seconds") or 0.0),
                "queue_age_seconds": float(queue_entry.get("age_seconds") or 0.0),
                "queue_score": float(queue_entry.get("score") or 0.0),
                "lua_required": lua_required,
                "lua_online": bool(lua_online),
                "lua_last_event": getattr(acc, "lua_last_event", "") or "",
                "lua_last_event_at": float(getattr(acc, "lua_last_event_at", 0.0) or 0.0),
                "lua_in_game_at": float(getattr(acc, "lua_in_game_at", 0.0) or 0.0),
                "description": acc.description,
                "manual_status": acc.manual_status,
                "finished_at": float(acc.finished_at or 0.0),
                "launchable": launchable,
                "blocked_reason": blocked_reason,
                "captcha_required": bool(captcha_required),
                "cookie_username": acc.cookie_username,
                "cookie_user_id": acc.cookie_user_id,
                "user_id": getattr(acc, "user_id", "") or acc.cookie_user_id,
                "cookie_mismatch": bool(acc.cookie_mismatch),
                "pid": snapshot_pid if pid_alive else None,
                "process_alive": pid_alive,
                "process_name": runtime_snapshot.get("process_name", acc.bound_process_name),
                "process_identity": snapshot_identity,
                "process_owner": ProcessManager.get_pid_owner(snapshot_pid) if (include_diagnostics and snapshot_pid) else "",
                "server_type": acc.server_type.value if acc.server_type else "UNKNOWN",
                "active_vip": redact_secret(acc.active_vip),
                "observed_server_type": runtime_snapshot.get("observed_server_type", acc.observed_server_type or ""),
                "observed_is_vip": (runtime_snapshot.get("observed_server_type", acc.observed_server_type or "").upper() == "VIP"),
                "observed_private_server_id": runtime_snapshot.get("observed_private_server_id", acc.observed_private_server_id or ""),
                "observed_private_server_owner_id": runtime_snapshot.get("observed_private_server_owner_id", acc.observed_private_server_owner_id or ""),
                "observed_place_id": runtime_snapshot.get("observed_place_id", acc.observed_place_id or ""),
                "observed_job_id": runtime_snapshot.get("observed_job_id", acc.observed_job_id or ""),
                "observed_universe_id": runtime_snapshot.get("observed_universe_id", acc.observed_universe_id or ""),
                "observed_server_at": float(runtime_snapshot.get("observed_server_at", acc.observed_server_at or 0.0) or 0.0),
                "uptime": acc.uptime_str,
                "retry": acc.retry_count,
                "retry_count": acc.retry_count,
                "crash": acc.crash_count,
                "crash_count": acc.crash_count,
                "fail": acc.fail_count,
                "fail_count": acc.fail_count,
                "recovery_budget_count": len(acc.recovery_budget_attempts or []),
                "cpu": cpu,
                "mem_mb": mem,
                "is_vip": acc.is_vip,
                "session_valid": acc.session_valid,
                "last_crash_reason": acc.last_crash_reason,
                "last_state_reason": acc.last_state_reason,
                "last_state_change_at": float(acc.last_state_change_at or 0.0),
                "last_pid_change_at": float(acc.last_pid_change_at or 0.0),
                "vip_tracker": vip_tracker_status,
                "not_responding": is_nr,
                "cooldown_until": cooldown_until,
                "cooldown_left": cooldown_left,
                "pid_missing_for": max(0, int(time.time() - acc.pid_missing_since)) if acc.pid_missing_since else 0,
                "ownership_confidence": round(float(acc.ownership_confidence or 0.0), 1),
                "signal_confidence": round(float(acc.last_signal_confidence or 0.0), 1),
                "launch_strategy": acc.launch_strategy or "",
                "recovery_status": acc.recovery_status or "",
                "last_recovery_reason": acc.last_recovery_reason or "",
                "recovery_step": recovery_step,
                "recovery_step_index": recovery_step_index,
                "recovery_step_started_at": recovery_step_started_at,
                "watchdog_classification": acc.last_watchdog_classification or "",
                "liveness_state": reported_liveness,
                "liveness_score": reported_liveness_score,
                "process_binding_status": acc.process_binding_status or "",
                "binding_decision": acc.binding_decision or runtime_snapshot.get("binding_decision", ""),
                "process_binding_confidence": round(float(acc.process_binding_confidence or runtime_snapshot.get("process_binding_confidence", 0.0) or 0.0), 1),
                "process_proof_level": acc.process_proof_level or runtime_snapshot.get("process_proof_level", "untrusted"),
                "process_reject_reason": acc.process_reject_reason or runtime_snapshot.get("process_reject_reason", ""),
                "process_owner_claim": acc.process_owner_claim or runtime_snapshot.get("process_owner_claim", ""),
                "unmanaged_live_process_count": int(acc.unmanaged_live_process_count or runtime_snapshot.get("unmanaged_live_process_count", 0) or 0),
                "unmanaged_live_pids": list(acc.unmanaged_live_pids or runtime_snapshot.get("unmanaged_live_pids", []) or []),
                "adopt_candidate_pid": acc.adopt_candidate_pid or runtime_snapshot.get("adopt_candidate_pid"),
                "adopt_reject_reason": acc.adopt_reject_reason or runtime_snapshot.get("adopt_reject_reason", ""),
                "orphan_confidence": round(float(acc.orphan_confidence or 0.0), 1),
                "runtime_state": runtime_snapshot.get("runtime_state", ""),
                "runtime": runtime_snapshot,
                "runtime_generation": runtime_snapshot.get("runtime_generation", 0),
                "recovery_generation": runtime_snapshot.get("recovery_generation", acc.recovery_generation),
                "command_generation": runtime_snapshot.get("command_generation", acc.command_generation),
                "recovery_active": bool(runtime_snapshot.get("recovery_active", False)),
                "recovery_inflight": bool(runtime_snapshot.get("recovery_inflight", False)),
                "recovery_reason": runtime_snapshot.get("recovery_reason", acc.last_recovery_reason or ""),
                "bind_status": runtime_snapshot.get("bind_status", acc.process_binding_status or ""),
                "binding_status": runtime_snapshot.get("binding_status", acc.process_binding_status or ""),
                "last_heartbeat": runtime_snapshot.get("last_heartbeat", 0.0),
                "session_id": runtime_snapshot.get("session_id", ""),
                "launch_nonce": runtime_snapshot.get("launch_nonce", ""),
                "account_runtime_id": runtime_snapshot.get("account_runtime_id", ""),
                "rejoin_transaction_id": runtime_snapshot.get("rejoin_transaction_id", ""),
                "server_validation": runtime_snapshot.get("server_validation", acc.server_validation or ""),
                "destination_validation": runtime_snapshot.get("destination_validation", acc.destination_validation or acc.server_validation or ""),
                "scheduler_slot": runtime_snapshot.get("scheduler_slot", acc.scheduler_slot or ""),
                "supervisor_state": runtime_snapshot.get("supervisor_state", acc.supervisor_state or ""),
                "last_transaction_status": runtime_snapshot.get("last_transaction_status", acc.last_transaction_status or ""),
                "last_transaction_step": runtime_snapshot.get("last_transaction_step", acc.last_transaction_step or ""),
                "last_transaction_reason": runtime_snapshot.get("last_transaction_reason", acc.last_transaction_reason or ""),
                "last_transaction_started_at": runtime_snapshot.get("last_transaction_started_at", float(acc.last_transaction_started_at or acc.session_started_at or 0.0)),
                "last_transaction_completed_at": runtime_snapshot.get("last_transaction_completed_at", float(acc.last_transaction_completed_at or 0.0)),
                "last_transaction_failure_reason": runtime_snapshot.get("last_transaction_failure_reason", acc.last_transaction_failure_reason or ""),
                "session_started_at": float(acc.session_started_at or 0.0),
                "last_transaction_at": float(acc.last_transaction_at or 0.0),
                "launch_intent": dict(acc.launch_intent or {}),
                "launch_intent_summary": dict(acc.launch_intent_summary or runtime_snapshot.get("launch_intent_summary", {}) or {}),
                "recent_runtime_events": events_by_account.get(acc._config_username, [])[:20],
                "last_transition_at": runtime_snapshot.get("last_transition_at", 0.0),
                "last_transition_reason": runtime_snapshot.get("last_transition_reason", ""),
                "current_command": runtime_snapshot.get("current_command", ""),
                "command_inflight": account_command,
                "can_start": bool((not farm.running) and not any_command_inflight),
                "can_stop": bool(farm.running and not any_command_inflight),
                "can_rejoin": bool(farm.running and not any_command_inflight and display_state != AccountState.FAILED and not blocked_reason),
                "can_kill": bool(pid_alive and snapshot_pid and not any_command_inflight),
            }
            if not include_diagnostics:
                for key in (
                    "runtime",
                    "launch_intent",
                    "recent_runtime_events",
                    "vip_tracker",
                    "unmanaged_live_pids",
                    "multi_roblox_guard_handles",
                ):
                    account_payload.pop(key, None)
            runtime_truth = build_account_truth(
                acc,
                process_alive=pid_alive,
                window_count=window_count,
            )
            self._log_suspect_transition(acc, runtime_truth)
            account_payload["runtime_truth"] = runtime_truth.to_dict()
            account_payload["health_flags"] = account_health_flags(account_payload)
            accounts_data.append(account_payload)

        blocked_count = sum(1 for a in accounts_data if a.get("blocked_reason"))
        launchable_count = sum(1 for a in accounts_data if a.get("launchable", True))
        in_game_count = sum(1 for a in accounts_data if a.get("state") == "IN_GAME" and not a.get("blocked_reason"))
        failed_count = sum(1 for a in accounts_data if a.get("state") == "FAILED" or a.get("blocked_reason"))
        with farm._event_lock:
            total_rejoin = farm._total_rejoin
            total_crash = farm._total_crash
            event_log_count = len(farm._event_log)
            event_log = list(farm._event_log) if include_diagnostics else []
        with farm._status_lock:
            status_revision = int(farm._status_revision)
        scheduler_snapshot = {}
        if include_diagnostics and getattr(farm, "_runtime_scheduler", None):
            scheduler_snapshot = farm._runtime_scheduler.snapshot()
        runtime_health = (
            build_runtime_health(
                accounts_data,
                queue_snapshot,
                recent_runtime_events,
                scheduler_snapshot=scheduler_snapshot,
            )
            if include_diagnostics
            else {}
        )
        machine_supervisor = {}
        if include_diagnostics and getattr(farm, "_machine_supervisor", None):
            machine_supervisor = farm._machine_supervisor.snapshot().to_dict()
        recovery_storm = {}
        if include_diagnostics and getattr(getattr(farm, "_recovery", None), "_storm", None):
            recovery_storm = farm._recovery._storm.snapshot()
        payload = {
            "running": farm.running,
            "status_revision": status_revision,
            "status_updated_at": time.time(),
            "uptime": f"{h:02d}:{m:02d}:{s:02d}",
            "total_accounts": len(farm._accounts),
            "launchable_count": launchable_count,
            "blocked_count": blocked_count,
            "in_game": in_game_count,
            "crash": sum(1 for a in accounts_data if a.get("state") == "CRASH" and not a.get("blocked_reason")),
            "launching": sum(1 for a in accounts_data if a.get("state") in {"LAUNCHING", "VERIFY"} and not a.get("blocked_reason")),
            "queued": sum(1 for a in accounts_data if a.get("state") == "QUEUED" and not a.get("blocked_reason")),
            "failed": failed_count,
            "total_rejoin": total_rejoin,
            "total_crash": total_crash,
            "network_state": farm._net_mon.get_state() if farm._net_mon else NET_ONLINE,
            "runtime_state": "RUNNING" if farm.running else "STOPPED",
            "queue_duration_effective_seconds": farm._maintenance._queue_duration_seconds() if farm._maintenance else 0,
            "command_generation": farm._command_tracker.generation,
            "command_inflight": global_command,
            "multi_roblox_guard_state": multi_guard.get("state", "unknown"),
            "multi_roblox_guard_pid": multi_guard.get("pid", 0),
            "multi_roblox_guard_detail": multi_guard.get("detail", ""),
            "last_multi_roblox_failure": multi_guard.get("last_failure", ""),
            "multi_roblox_guard_handles": multi_guard.get("handle_names", []),
            "queue_snapshot": queue_snapshot,
            "scheduler_health": scheduler_snapshot,
            "machine_supervisor": machine_supervisor,
            "recovery_storm": recovery_storm,
            "runtime_health": runtime_health,
            "can_start": bool((not farm.running) and not any_command_inflight),
            "can_stop": bool(farm.running and not any_command_inflight),
            "accounts": accounts_data,
            "event_log_count": event_log_count,
            "recent_runtime_event_count": len(recent_runtime_events),
        }
        if include_diagnostics:
            payload.update({
                "event_log": event_log,
                "runtime_events": event_log,
                "recent_runtime_events": recent_runtime_events,
                "supervisor": farm._supervisor.snapshot(),
            })
        return payload
