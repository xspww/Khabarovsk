from __future__ import annotations

import time
from typing import Any, Dict, List

from account_hybrid import redact_secret
from core import AccountState, account_launch_block_reason, flog_kv
from services.network_monitor import NET_ONLINE
from services.process_service import ProcessManager
from services.ram_service import RAMManager
from services.resource_monitor import get_rt_monitor
from services.captcha_guard import CAPTCHA_BLOCK_REASON, CAPTCHA_LABEL, is_account_captcha_required
from runtime.account_worker import AccountWorker
from runtime.runtime_health import account_health_flags, build_runtime_health


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
    def __init__(self, farm: Any):
        self._farm = farm

    def build_status(self) -> dict:
        farm = self._farm
        from core import STATE_META

        uptime = int(time.time() - farm.start_ts) if farm.start_ts else 0
        h, r = divmod(uptime, 3600)
        m, s = divmod(r, 60)
        mon = get_rt_monitor()
        accounts_data = []
        cfg = farm.cfg_mgr.snapshot()
        try:
            from roblox_hybrid import multi_roblox_guard_status

            multi_guard = multi_roblox_guard_status()
        except Exception as exc:
            multi_guard = {"state": "unknown", "pid": 0, "detail": str(exc), "last_failure": str(exc), "handle_names": []}
        ram_enabled = bool(cfg.get("use_ram_account_manager", False))
        ram_records: Dict[str, dict] = {}
        global_command = farm.command_inflight("global")
        try:
            recent_runtime_events = [
                event
                for event in farm._runtime_store.list_recent_events(limit=100)
                if _important_runtime_event(event)
            ]
        except Exception as e:
            flog_kv("RUNTIME", "recent_events_failed", "warning", error=e)
            recent_runtime_events = []
        events_by_account: Dict[str, List[Dict[str, Any]]] = {}
        for event in recent_runtime_events:
            events_by_account.setdefault(str(event.get("account", "") or ""), []).append(event)

        if ram_enabled:
            ok, payload = RAMManager.get_accounts(cfg, include_cookies=False, force_refresh=False)
            if ok and isinstance(payload, list):
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    for key in ("Username", "username", "Alias", "alias", "Account"):
                        value = str(item.get(key, "") or "").strip().lower()
                        if value:
                            ram_records[value] = item

        any_command_inflight = farm._command_tracker.any_inflight()
        queue_snapshot = farm._queue.snapshot() if farm._queue else {
            "size": 0,
            "pending": 0,
            "busy": False,
            "closed": not farm.running,
            "stale_rejections": 0,
            "oldest_age_seconds": 0,
            "entries": [],
        }

        for acc in farm._accounts:
            runtime_snapshot = farm._runtime_state.snapshot(acc)
            snapshot_pid = runtime_snapshot.get("pid")
            try:
                snapshot_pid = int(snapshot_pid) if snapshot_pid else None
            except (TypeError, ValueError):
                snapshot_pid = None
            snapshot_identity = str(runtime_snapshot.get("process_identity") or "")
            snapshot_public = str(runtime_snapshot.get("public_state") or getattr(getattr(acc, "state", None), "name", "IDLE"))
            display_state = AccountState.__members__.get(snapshot_public, AccountState.IDLE)
            pid_alive = bool(snapshot_pid and ProcessManager.is_bound_game_alive(
                snapshot_pid,
                owner_key=acc._config_username,
                expected_identity=snapshot_identity,
            ))
            if display_state == AccountState.IN_GAME and snapshot_pid and not pid_alive:
                display_state = AccountState.CRASH

            meta = STATE_META.get(display_state, {"label": display_state.name, "color": "#6b7280"})
            cpu = mon.get_cpu(snapshot_pid) if (snapshot_pid and pid_alive) else 0.0
            mem = mon.get_ram(snapshot_pid) if (snapshot_pid and pid_alive) else 0.0
            is_nr = bool(snapshot_pid and pid_alive and display_state == AccountState.IN_GAME and ProcessManager.is_not_responding(snapshot_pid))
            ram_online = None
            ram_detail = ""

            if ram_enabled:
                names = [
                    str(acc.username or "").strip().lower(),
                    str(acc.display_name or "").strip().lower(),
                    str(acc.alias or "").strip().lower(),
                ]
                record = None
                for name in names:
                    if name and name in ram_records:
                        record = ram_records[name]
                        break
                if record:
                    ram_online, ram_detail = RAMManager.resolve_record_online(record)

            vip_tracker_status = []
            if acc._vip_tracker:
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
            cooldown_until = float(acc.cooldown_until or 0.0)
            cooldown_left = max(0, int(cooldown_until - time.time()))
            captcha_required = is_account_captcha_required(acc)
            if captcha_required:
                state_label = CAPTCHA_LABEL
                state_color = "#f0c76f"
            blocked_reason = account_launch_block_reason(acc)
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
            roblox_presence: Dict[str, Any] = {}
            presence_age = None
            presence_type_name = ""

            account_payload = {
                "username": acc.username,
                "account_id": acc._config_username,
                "display": acc.display_name,
                "state": display_state.name,
                "public_state": snapshot_public,
                "desired_state": runtime_snapshot.get("desired_public_state", acc.desired_state.name),
                "state_label": state_label,
                "state_color": state_color,
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
                "process_owner": ProcessManager.get_pid_owner(snapshot_pid) if snapshot_pid else "",
                "server_type": acc.server_type.value if acc.server_type else "UNKNOWN",
                "active_vip": redact_secret(acc.active_vip),
                "uptime": acc.uptime_str,
                "retry": acc.retry_count,
                "retry_count": acc.retry_count,
                "crash": acc.crash_count,
                "crash_count": acc.crash_count,
                "fail": acc.fail_count,
                "fail_count": acc.fail_count,
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
                "ram_online": ram_online,
                "ram_detail": ram_detail,
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
                "presence_type": roblox_presence.get("presence_type"),
                "presence_type_name": presence_type_name,
                "presence_place_id": roblox_presence.get("presence_place_id", ""),
                "presence_root_place_id": roblox_presence.get("presence_root_place_id", ""),
                "presence_universe_id": roblox_presence.get("presence_universe_id", ""),
                "presence_game_id_present": bool(roblox_presence.get("presence_game_id_present", False)),
                "presence_last_location": roblox_presence.get("presence_last_location", ""),
                "presence_age_seconds": presence_age,
                "presence_limited": bool(roblox_presence.get("presence_limited", False)),
                "presence_disconnect_for": 0.0,
                "presence_disconnect_reason": "",
                "process_binding_status": acc.process_binding_status or "",
                "binding_decision": acc.binding_decision or runtime_snapshot.get("binding_decision", ""),
                "process_binding_confidence": round(float(acc.process_binding_confidence or runtime_snapshot.get("process_binding_confidence", 0.0) or 0.0), 1),
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
                "can_rejoin": bool(farm.running and not any_command_inflight and display_state != AccountState.FAILED and not captcha_required),
                "can_kill": bool(pid_alive and snapshot_pid and not any_command_inflight),
            }
            account_payload["health_flags"] = account_health_flags(account_payload)
            accounts_data.append(account_payload)

        states = [a["state"] for a in accounts_data]
        blocked_count = sum(1 for a in accounts_data if a.get("blocked_reason"))
        launchable_count = sum(1 for a in accounts_data if a.get("launchable", True))
        with farm._event_lock:
            total_rejoin = farm._total_rejoin
            total_crash = farm._total_crash
            event_log = list(farm._event_log)
        with farm._status_lock:
            status_revision = int(farm._status_revision)
        runtime_health = build_runtime_health(accounts_data, queue_snapshot, recent_runtime_events)
        return {
            "running": farm.running,
            "status_revision": status_revision,
            "status_updated_at": time.time(),
            "uptime": f"{h:02d}:{m:02d}:{s:02d}",
            "total_accounts": len(farm._accounts),
            "launchable_count": launchable_count,
            "blocked_count": blocked_count,
            "in_game": states.count("IN_GAME"),
            "crash": states.count("CRASH"),
            "launching": states.count("LAUNCHING") + states.count("VERIFY"),
            "queued": states.count("QUEUED"),
            "failed": states.count("FAILED"),
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
            "presence_api": {
                "enabled": False,
                "poll_interval_seconds": int(cfg.get("presence_poll_interval_seconds", 30) or 30),
                "cache_ttl_seconds": int(cfg.get("presence_cache_ttl_seconds", 30) or 30),
                "assist_rejoin_enabled": False,
                "ok": False,
                "msg": "presence_assist_disabled",
            },
            "queue_snapshot": queue_snapshot,
            "runtime_health": runtime_health,
            "can_start": bool((not farm.running) and not any_command_inflight),
            "can_stop": bool(farm.running and not any_command_inflight),
            "accounts": accounts_data,
            "event_log": event_log,
            "runtime_events": event_log,
            "recent_runtime_events": recent_runtime_events,
            "supervisor": farm._supervisor.snapshot(),
        }
