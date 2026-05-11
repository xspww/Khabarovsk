from __future__ import annotations

import json
import os
import shutil
import threading
import time
from typing import Any, Dict, List

from app_paths import APP_DATA_DIR
from domain.account_state import RuntimeState


def _flog(message: str, level: str = "info") -> None:
    from core import flog

    flog(message, level)


def _flog_kv(scope: str, name: str, level: str = "info", **fields: Any) -> None:
    from core import flog_kv

    flog_kv(scope, name, level, **fields)


CONFIG_FILE = os.path.join(APP_DATA_DIR, "roboguard_rt1_config.json")
COOKIE_STORE_FILE = os.path.join(APP_DATA_DIR, "roboguard_rt1_cookies.json")
ACCOUNTS_TEXT_FILE = os.path.join(APP_DATA_DIR, "roboguard_rt12_accounts.txt")
RUNTIME_TEXT_FILE = os.path.join(APP_DATA_DIR, "roboguard_rt12_runtime.txt")

DEFAULTS: Dict[str, Any] = {
    "auto_rejoin":              True,
    "rejoin_delay":             5,
    "max_retry":                10,
    "max_fail_count":           5,
    "crash_timeout":            30,
    "heartbeat_timeout":        60,
    "launch_verify_window":     25,
    "login_warmup_delay":       6,
    "anti_spam_window":         6,
    "launch_rate_interval":     6,
    "account_switch_cooldown":  10,
    "queue_delay_seconds":      15,
    "queue_duration_seconds":   15,
    "max_concurrent_accounts":  40,
    "game_private_server_url":  "",
    "game_place_id":            "",
    "auto_create_private_server_enabled": False,
    "auto_create_private_server_free_only": True,
    "auto_close_enabled":       False,
    "auto_close_minutes":       0,
    "auto_minimize_enabled":    False,
    "auto_minimize_seconds":    10,
    "not_responding_timeout":   30,
    "network_check_interval":   5,
    "network_debounce":         5,
    "periodic_reconcile_interval": 15,
    "queue_timeout":            90,
    "cooldown_after_crash":     5,
    "relaunch_loop_window":     45,
    "relaunch_loop_limit":      3,
    "launch_public_fallback_threshold": 2,
    "recovery_confidence_threshold": 45.0,
    "connection_error_rejoin":  True,
    "popup_disconnected_enabled": True,
    "popup_scan_interval_seconds": 30,
    "popup_scan_max_parallel": 2,
    "connection_error_hold_time": 3,
    "popup_startup_grace_seconds": 8,
    "popup_confidence_threshold": 1.0,
    "popup_sample_count": 6,
    "popup_sample_interval_seconds": 0.25,
    "recovery_dedupe_window_seconds": 3,
    "session_conflict_window_seconds": 90,
    "recovery_restore_window":  3600,
    "watchdog_activity_timeout": 180,
    "watchdog_loading_grace":   90,
    "event_bus_workers":        4,
    "event_bus_max_pending":    128,
    # ── Roblox Watchdog (ใหม่ RT.1.0) ──
    "watchdog_enabled":         True,
    "watchdog_cpu_low":         0.9,   # % CPU ต่ำกว่านี้ = ผิดปกติ
    "watchdog_ram_low":         90.0,  # MB RAM ต่ำกว่านี้ = ผิดปกติ
    "watchdog_hold_time":       60,    # วิ รอยืนยันก่อน kill+rejoin
    "fps_limiter_enabled":      False,
    "fps_limit":                240,
    "graphics_auto_enabled":    False,
    "graphics_low_enabled":     False,
    "graphics_quality_level":   1,
    "auto_process_priority_enabled": False,
    "process_priority":         "low",
    "cpu_limiter_enabled":      False,
    "cpu_limiter_mode":         "hard",
    "cpu_limiter_default_percent": 20,
    "cpu_limiter_apply_all":    True,
    "cpu_limiter_accounts":     {},
    "roblox_window_resize_enabled": False,
    "roblox_window_size_preset": "640x480",
    "roblox_window_width":      640,
    "roblox_window_height":     480,
    "roblox_window_resize_interval_seconds": 10,
    "roblox_window_arrange_enabled": False,
    "roblox_window_arrange_columns": 6,
    "roblox_window_arrange_gap": 2,
    "roblox_window_arrange_margin": 0,
    "presence_api_enabled":     False,
    "presence_poll_interval_seconds": 30,
    "presence_cache_ttl_seconds": 30,
    "presence_assist_rejoin_enabled": True,
    "presence_rejoin_cooldown_seconds": 10,
    "multi_roblox_enabled": True,
    "rt_rotation_enabled": False,
    "use_ram_account_manager":  False,
    "ram_launch_via_api":       True,
    "ram_auto_launch":          True,
    "ram_host":                 "localhost",
    "ram_port":                 7963,
    "ram_password":             "",
    "ram_path":                 os.path.join(
        os.path.expanduser("~"),
        "Documents",
        "acc",
        "Roblox Account Manager.exe",
    ),
    "accounts":                 [],
    "runtime_state":            {},
}

class ConfigManager:
    def __init__(self):
        self._cfg: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._io_lock = threading.RLock()
        self.load()

    def load(self):
        raw = self._read_text_json(CONFIG_FILE, {})

        # Migration from old config filenames/keys
        if "zombie_timeout" in raw and "not_responding_timeout" not in raw:
            raw["not_responding_timeout"] = raw["zombie_timeout"]
        if "auto_close_minutes" not in raw and "auto_close_seconds" in raw:
            try:
                seconds = max(0.0, float(raw.get("auto_close_seconds") or 0))
                raw["auto_close_minutes"] = int((seconds + 59) // 60) if seconds > 0 else 0
            except Exception:
                raw["auto_close_minutes"] = 0
        raw["use_ram_account_manager"] = False
        raw["ram_launch_via_api"] = False
        raw["ram_auto_launch"] = False

        with self._lock:
            self._cfg = {k: raw.get(k, v) for k, v in DEFAULTS.items()}

    def save(self):
        with self._lock:
            data = dict(self._cfg)
        data.pop("accounts", None)
        data.pop("runtime_state", None)
        data.setdefault("schema_version", 1)
        self._write_text_json(CONFIG_FILE, data)

    def get(self, key: str, default=None) -> Any:
        with self._lock:
            return self._cfg.get(key, default if default is not None else DEFAULTS.get(key))

    def update(self, updates: Dict[str, Any]):
        with self._lock:
            self._cfg.update(updates)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            snap = dict(self._cfg)
        snap["use_ram_account_manager"] = False
        snap["ram_launch_via_api"] = False
        snap["ram_auto_launch"] = False
        return snap

    def _read_text_json(self, path: str, fallback):
        if not os.path.exists(path):
            return fallback
        backup_path = f"{path}.bak"
        try:
            with self._io_lock:
                with open(path, "r", encoding="utf-8") as f:
                    body = f.read().strip()
                if not body:
                    return fallback
                return json.loads(body)
        except Exception as e:
            _flog(f"Text store load error ({path}): {e}", "warning")
            if os.path.exists(backup_path):
                try:
                    with self._io_lock:
                        with open(backup_path, "r", encoding="utf-8") as f:
                            body = f.read().strip()
                        if body:
                            recovered = json.loads(body)
                            _flog_kv("CONFIG", "json_recovered_from_backup", "warning", path=path)
                            return recovered
                except Exception as backup_error:
                    _flog(f"Text store backup load error ({backup_path}): {backup_error}", "warning")
            return fallback

    def _write_text_json(self, path: str, payload):
        tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        backup_path = f"{path}.bak"
        try:
            with self._io_lock:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                if os.path.exists(path):
                    try:
                        shutil.copy2(path, backup_path)
                    except Exception as backup_error:
                        _flog_kv("CONFIG", "json_backup_failed", "warning", path=path, error=backup_error)
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(json.dumps(payload, indent=2, ensure_ascii=False))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, path)
        except Exception as e:
            _flog(f"Text store save error ({path}): {e}", "warning")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

    def _use_ram_cookie_source(self) -> bool:
        return False

    def _load_cookie_store(self) -> Dict[str, str]:
        if self._use_ram_cookie_source():
            return {}
        raw = self._read_text_json(COOKIE_STORE_FILE, {})
        if isinstance(raw, dict):
            return {
                str(k).strip().lower(): str(v or "").strip()
                for k, v in raw.items()
                if str(k).strip()
            }
        return {}

    def save_cookies(self, accounts: List[Account]):
        if self._use_ram_cookie_source():
            return
        cookie_map: Dict[str, str] = {}
        for acc in accounts:
            username = str(acc.username or "").strip().lower()
            cookie = str(acc.cookie or "").strip()
            if username and cookie:
                cookie_map[username] = cookie
        self._write_text_json(COOKIE_STORE_FILE, cookie_map)

    def get_accounts(self) -> List["Account"]:
        from core import Account

        raw = self._read_text_json(ACCOUNTS_TEXT_FILE, None)
        if raw is None:
            with self._lock:
                raw = self._cfg.get("accounts", [])
            if raw:
                self._write_text_json(ACCOUNTS_TEXT_FILE, raw)
        use_ram_cookie_source = False
        cookie_store = {} if use_ram_cookie_source else self._load_cookie_store()
        accounts = []
        for d in raw:
            try:
                acc = Account.from_dict(d)
                saved_cookie = cookie_store.get(str(acc.username or "").strip().lower(), "")
                if saved_cookie and not acc.cookie:
                    acc.cookie = saved_cookie
                accounts.append(acc)
            except Exception as e:
                _flog(f"Account parse error: {e}", "warning")
        return accounts

    def save_accounts(self, accounts: List[Account]):
        payload = []
        for a in accounts:
            item = a.to_dict()
            item.pop("cookie", None)
            item["cookie_present"] = bool(str(getattr(a, "cookie", "") or "").strip())
            payload.append(item)
        self.update({"accounts": []})
        self.save()
        self._write_text_json(ACCOUNTS_TEXT_FILE, payload)

    def save_runtime(self, accounts: List[Account]):
        state = {"__schema_version": 1, "__saved_at": time.time()}
        saved_at = time.time()
        for a in accounts:
            runtime_snapshot = a.runtime_snapshot()
            entry: Dict[str, Any] = {
                "runtime": runtime_snapshot,
                "runtime_state": runtime_snapshot.get("runtime_state", RuntimeState.STOPPED.value),
                "runtime_generation": runtime_snapshot.get("runtime_generation", 0),
                "command_generation": runtime_snapshot.get("command_generation", 0),
                "retry":             a.retry_count,
                "fail":              a.fail_count,
                "crash":             a.crash_count,
                "launch_fail_count": a.launch_fail_count,
                "crash_retry_count": a.crash_retry_count,
                "network_retry_count": a.network_retry_count,
                "session_retry_count": a.session_retry_count,
                "last_crash_reason": a.last_crash_reason,
                "cooldown_until":    a.cooldown_until,
                "rapid_relaunch_count": a.rapid_relaunch_count,
                "last_network_lost_at": a.last_network_lost_at,
                "recovery_status":   a.recovery_status,
                "last_recovery_reason": a.last_recovery_reason,
                "recovery_scheduled_at": a.recovery_scheduled_at,
                "recovery_generation": a.recovery_generation,
                "recovery_active": bool(a.recovery_inflight or a.recovery_status),
                "last_recovery_at": a.last_recovery_at,
                "bound_pid": a.pid,
                "bound_process_name": a.bound_process_name,
                "bound_process_identity": a.bound_process_identity,
                "last_pid_change_at": a.last_pid_change_at,
                "last_relaunch_at": a.last_launch_at,
                "process_binding_status": a.process_binding_status,
                "binding_decision": a.binding_decision,
                "process_binding_confidence": a.process_binding_confidence,
                "process_reject_reason": a.process_reject_reason,
                "process_owner_claim": a.process_owner_claim,
                "unmanaged_live_process_count": a.unmanaged_live_process_count,
                "unmanaged_live_pids": list(a.unmanaged_live_pids or []),
                "adopt_candidate_pid": a.adopt_candidate_pid,
                "adopt_reject_reason": a.adopt_reject_reason,
                "liveness_state": a.liveness_state,
                "liveness_score": a.liveness_score,
                "session_id": a.session_id,
                "launch_nonce": a.launch_nonce,
                "account_runtime_id": a.account_runtime_id,
                "rejoin_transaction_id": a.rejoin_transaction_id,
                "server_validation": a.server_validation,
                "destination_validation": a.destination_validation,
                "scheduler_slot": a.scheduler_slot,
                "supervisor_state": a.supervisor_state,
                "last_transaction_status": a.last_transaction_status,
                "last_transaction_step": a.last_transaction_step,
                "last_transaction_reason": a.last_transaction_reason,
                "last_transaction_started_at": a.last_transaction_started_at,
                "last_transaction_completed_at": a.last_transaction_completed_at,
                "last_transaction_failure_reason": a.last_transaction_failure_reason,
                "session_started_at": a.session_started_at,
                "last_transaction_at": a.last_transaction_at,
                "launch_intent": a.launch_intent,
                "launch_intent_summary": a.launch_intent_summary,
                "runtime_saved_at":  saved_at,
            }
            if a._vip_tracker:
                try:
                    entry["vip_scores"] = a._vip_tracker.status()
                except Exception:
                    pass
            state[a._config_username] = entry
        self.update({"runtime_state": {}})
        self.save()
        self._write_text_json(RUNTIME_TEXT_FILE, state)
        _flog_kv("RUNTIME", "saved", accounts=len(accounts), saved_at=f"{saved_at:.3f}")

    def restore_runtime(self, accounts: List[Account]):
        state = self._read_text_json(RUNTIME_TEXT_FILE, None)
        if state is None:
            state = self.get("runtime_state", {})
        now = time.time()
        restore_window = max(0.0, float(self.get("recovery_restore_window", 3600) or 3600))
        for a in accounts:
            key = a._config_username
            if key in state:
                s = state[key]
                saved_at = float(s.get("runtime_saved_at") or 0.0)
                fresh = bool(saved_at and (now - saved_at) <= restore_window)
                a.crash_count = int(s.get("crash", 0) or 0)
                a.last_crash_reason = str(s.get("last_crash_reason", "") or "")
                if fresh:
                    a.retry_count = int(s.get("retry", 0) or 0)
                    a.fail_count = int(s.get("fail",  0) or 0)
                    a.launch_fail_count = int(s.get("launch_fail_count", 0) or 0)
                    a.crash_retry_count = int(s.get("crash_retry_count", 0) or 0)
                    a.network_retry_count = int(s.get("network_retry_count", 0) or 0)
                    a.session_retry_count = int(s.get("session_retry_count", 0) or 0)
                    a.cooldown_until = max(0.0, float(s.get("cooldown_until", 0.0) or 0.0))
                    if a.cooldown_until <= now:
                        a.cooldown_until = 0.0
                    a.rapid_relaunch_count = int(s.get("rapid_relaunch_count", 0) or 0)
                    last_network = s.get("last_network_lost_at")
                    a.last_network_lost_at = float(last_network) if last_network else None
                    a.recovery_status = str(s.get("recovery_status", "") or "")
                    if a.recovery_status in {"recovering", "queued", "launch_backoff", "due"}:
                        a.recovery_status = "restored"
                    a.last_recovery_reason = str(s.get("last_recovery_reason", "") or "")
                    a.recovery_scheduled_at = max(0.0, float(s.get("recovery_scheduled_at", 0.0) or 0.0))
                    if a.recovery_scheduled_at and a.recovery_scheduled_at <= now:
                        a.recovery_scheduled_at = 0.0
                    a.recovery_generation = int(s.get("recovery_generation", 0) or 0)
                    a.runtime_generation = int(s.get("runtime_generation", 0) or 0)
                    a.command_generation = int(s.get("command_generation", 0) or 0)
                    a.current_command_id = ""
                    a.current_command = ""
                    a.command_inflight_started_at = 0.0
                    a.last_recovery_at = max(0.0, float(s.get("last_recovery_at", 0.0) or 0.0))
                    a.pid = int(s.get("bound_pid") or 0) or None
                    a.bound_process_name = str(s.get("bound_process_name", "") or "")
                    a.bound_process_identity = str(s.get("bound_process_identity", "") or "")
                    a.last_pid_change_at = max(0.0, float(s.get("last_pid_change_at", 0.0) or 0.0))
                    a.last_launch_at = max(0.0, float(s.get("last_relaunch_at", 0.0) or 0.0)) or None
                    a.process_binding_status = str(s.get("process_binding_status", "") or "restored")
                    a.binding_decision = str(s.get("binding_decision", "") or "")
                    a.process_binding_confidence = float(s.get("process_binding_confidence", 0.0) or 0.0)
                    a.process_reject_reason = str(s.get("process_reject_reason", "") or "")
                    a.process_owner_claim = str(s.get("process_owner_claim", "") or "")
                    a.unmanaged_live_process_count = int(s.get("unmanaged_live_process_count", 0) or 0)
                    live_pids = s.get("unmanaged_live_pids", [])
                    a.unmanaged_live_pids = list(live_pids) if isinstance(live_pids, list) else []
                    a.adopt_candidate_pid = int(s.get("adopt_candidate_pid") or 0) or None
                    a.adopt_reject_reason = str(s.get("adopt_reject_reason", "") or "")
                    a.liveness_state = str(s.get("liveness_state", "") or "unknown")
                    a.liveness_score = float(s.get("liveness_score", 0.0) or 0.0)
                    a.session_id = str(s.get("session_id", "") or "")
                    a.launch_nonce = str(s.get("launch_nonce", "") or "")
                    a.account_runtime_id = str(s.get("account_runtime_id", "") or "")
                    a.rejoin_transaction_id = str(s.get("rejoin_transaction_id", "") or "")
                    a.server_validation = str(s.get("server_validation", "") or "restored")
                    a.destination_validation = str(s.get("destination_validation", "") or a.server_validation or "restored")
                    a.scheduler_slot = str(s.get("scheduler_slot", "") or "")
                    a.supervisor_state = str(s.get("supervisor_state", "") or "restored")
                    if a.supervisor_state in {"transaction_pending", "launching", "rejoining", "process_bound", "verifying"}:
                        a.supervisor_state = "restored"
                    a.last_transaction_status = str(s.get("last_transaction_status", "") or "")
                    a.last_transaction_step = str(s.get("last_transaction_step", "") or "")
                    a.last_transaction_failure_reason = str(s.get("last_transaction_failure_reason", "") or "")
                    if a.last_transaction_status in {"pending", "launching", "process_bound", "verifying", "binding_verified"}:
                        a.last_transaction_status = "rolled_back_on_restart"
                        a.last_transaction_step = "rolled_back_on_restart"
                        a.last_transaction_failure_reason = "backend_restart"
                    a.last_transaction_reason = str(s.get("last_transaction_reason", "") or "")
                    a.last_transaction_started_at = max(0.0, float(s.get("last_transaction_started_at", 0.0) or s.get("session_started_at", 0.0) or 0.0))
                    a.last_transaction_completed_at = max(0.0, float(s.get("last_transaction_completed_at", 0.0) or 0.0))
                    a.session_started_at = max(0.0, float(s.get("session_started_at", 0.0) or 0.0))
                    a.last_transaction_at = max(0.0, float(s.get("last_transaction_at", 0.0) or 0.0))
                    launch_intent = s.get("launch_intent", {})
                    a.launch_intent = launch_intent if isinstance(launch_intent, dict) else {}
                    launch_intent_summary = s.get("launch_intent_summary", {})
                    if not isinstance(launch_intent_summary, dict):
                        launch_intent_summary = {}
                    a.launch_intent_summary = launch_intent_summary or dict(a.launch_intent.get("launch_intent_summary", {}) or {})
                else:
                    a.retry_count = 0
                    a.fail_count = 0
                    a.launch_fail_count = 0
                    a.crash_retry_count = 0
                    a.network_retry_count = 0
                    a.session_retry_count = 0
                    a.cooldown_until = 0.0
                    a.rapid_relaunch_count = 0
                    a.last_network_lost_at = None
                    a.recovery_status = ""
                    a.last_recovery_reason = ""
                    a.recovery_scheduled_at = 0.0
                    a.recovery_generation = 0
                    a.binding_decision = ""
                    a.process_binding_confidence = 0.0
                    a.process_reject_reason = ""
                    a.process_owner_claim = ""
                    a.unmanaged_live_process_count = 0
                    a.unmanaged_live_pids = []
                    a.adopt_candidate_pid = None
                    a.adopt_reject_reason = ""
                    a.destination_validation = "unverified"
                    a.launch_intent = {}
                    a.launch_intent_summary = {}
                    a.runtime_generation = 0
                    a.command_generation = 0
                    a.current_command_id = ""
                    a.current_command = ""
                    a.command_inflight_started_at = 0.0
                    a.pid = None
                    a.bound_process_name = ""
                    a.bound_process_identity = ""
                    a.last_pid_change_at = 0.0
                    a.last_launch_at = None
                    a.process_binding_status = "unbound"
                    a.liveness_state = "unknown"
                    a.liveness_score = 0.0
                    a.session_id = ""
                    a.launch_nonce = ""
                    a.account_runtime_id = ""
                    a.rejoin_transaction_id = ""
                    a.server_validation = "unverified"
                    a.scheduler_slot = ""
                    a.supervisor_state = "stopped"
                    a.last_transaction_status = ""
                    a.last_transaction_step = ""
                    a.last_transaction_reason = ""
                    a.last_transaction_started_at = 0.0
                    a.last_transaction_completed_at = 0.0
                    a.last_transaction_failure_reason = ""
                    a.session_started_at = 0.0
                    a.last_transaction_at = 0.0
                    a.launch_intent = {}
                a.sync_runtime("restore_runtime")
                if a._vip_tracker and "vip_scores" in s:
                    try:
                        now = time.time()
                        with a._vip_tracker._lock:
                            for item in s["vip_scores"]:
                                link = item.get("link")
                                if not link or link not in a._vip_tracker._scores:
                                    continue
                                if "score" in item:
                                    a._vip_tracker._scores[link] = float(item["score"])
                                remaining = int(item.get("blacklist_remaining", 0) or 0)
                                if remaining > 0:
                                    a._vip_tracker._blacklist[link] = now + remaining
                    except Exception:
                        pass
                _flog_kv(
                    "RUNTIME",
                    "restored",
                    account=a.display_name,
                    fresh=fresh,
                    crash=a.crash_count,
                    fail=a.fail_count,
                    retry=a.retry_count,
                    cooldown_left=max(0, int(a.cooldown_until - time.time())) if a.cooldown_until else 0,
                    reason=a.last_recovery_reason or a.last_crash_reason,
                )
                if not fresh:
                    _flog_kv(
                        "STATE",
                        "forced_reset",
                        account=a.display_name,
                        account_id=a._config_username,
                        runtime_generation=a.runtime_generation,
                        recovery_generation=a.recovery_generation,
                        command_generation=a.command_generation,
                        runtime_state=a.runtime.lifecycle_state.value,
                        public_state=a.state.name,
                        PID=a.pid or "",
                        reason="restore_runtime_expired",
                        thread_name=threading.current_thread().name,
                    )
