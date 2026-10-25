from __future__ import annotations

import json
import os
import shutil
import threading
import time
from typing import TYPE_CHECKING, Any

from config.validation import CONFIG_SCHEMA_VERSION, validate_config_payload
from domain._util import as_float as _saved_float
from domain._util import as_int as _saved_int
from domain._util import mapping_text as _saved_str
from domain.states import RuntimeState
from infra.app_paths import APP_DATA_DIR
from infra.core_logging import flog as _flog
from infra.core_logging import flog_kv as _flog_kv

if TYPE_CHECKING:
    from domain.account_model import Account


CONFIG_FILE = os.path.join(APP_DATA_DIR, "cronus_rt1_config.json")
ACCOUNTS_TEXT_FILE = os.path.join(APP_DATA_DIR, "cronus_rt12_accounts.txt")
RUNTIME_TEXT_FILE = os.path.join(APP_DATA_DIR, "cronus_rt12_runtime.txt")

DEFAULTS: dict[str, Any] = {
    "auto_rejoin":              True,
    "executor_monitor_enabled": False,
    "executor_selected":        "",
    "executor_check_interval_seconds": 30,
    "roblox_auto_update_enabled": False,
    "executor_relaunch_enabled": True,
    "executor_path_volt": "",
    "executor_path_potassium": "",
    "executor_path_real": "",
    "executor_path_madium": "",
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
    "max_concurrent_accounts":  40,
    "lua_enabled":             True,
    "lua_timeout_seconds":      60,
    "machine_supervisor_enabled": True,
    "machine_supervisor_max_launching_accounts": 1,
    "machine_supervisor_cpu_high_percent": 96.0,
    "machine_supervisor_memory_high_percent": 96.0,
    "game_private_server_url":  "",
    "game_place_id":            "",
    "game_mode":                "shared",
    "games":                    [],
    "games_migrated":           False,
    "auto_create_private_server_enabled": False,
    "auto_create_private_server_free_only": True,
    "block_same_server_enabled": False,
    "auto_close_enabled":       False,
    "auto_close_minutes":       0,
    "auto_minimize_enabled":    False,
    "auto_minimize_seconds":    10,
    "not_responding_timeout":   30,
    "network_check_interval":   5,
    "network_debounce":         5,
    "periodic_reconcile_interval": 15,
    "status_snapshot_cache_ttl_seconds": 5.0,
    "dashboard_process_cache_ttl_seconds": 5.0,
    "maintenance_liveness_interval_seconds": 5.0,
    "maintenance_queue_interval_seconds": 10.0,
    "maintenance_performance_interval_seconds": 15.0,
    "maintenance_housekeeping_interval_seconds": 20.0,
    "moderation_monitor_enabled": True,
    "moderation_check_interval_seconds": 900,
    "moderation_check_tick_seconds": 30,
    "moderation_check_batch_size": 2,
    "queue_timeout":            90,
    "cooldown_after_crash":     5,
    "relaunch_loop_window":     45,
    "relaunch_loop_limit":      3,
    "relaunch_loop_fatal":      False,
    "relaunch_loop_cooldown_seconds": 300.0,
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
    "recovery_storm_enabled": True,
    "recovery_storm_max_active": 3,
    "recovery_storm_min_spacing_seconds": 5,
    "recovery_storm_jitter_seconds": 3,
    "recovery_storm_outage_backoff_seconds": 30,
    "recovery_budget_enabled": True,
    "recovery_budget_max_attempts": 8,
    "recovery_budget_window_seconds": 300,
    "session_conflict_window_seconds": 90,
    "runtime_invariant_monitor_enabled": True,
    "runtime_invariant_suppress_seconds": 60,
    "runtime_scheduler_dispatch_workers": 0,
    "orphan_sweeper_enabled": True,
    "orphan_sweeper_kill_enabled": True,
    "orphan_sweeper_min_confidence": 45.0,
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
    "roblox_memory_guard_enabled": True,
    "roblox_memory_guard_mb":    6144.0,
    "roblox_memory_guard_hold_seconds": 30.0,
    "ram_cleanup_enabled":      False,
    "ram_cleanup_threshold_pct": 85.0,
    "ram_cleanup_interval_min": 15,
    "process_trim_enabled":     False,
    "process_trim_threshold_mb": 1200,
    "process_trim_cooldown_sec": 300,
    "process_trim_max_per_cycle": 1,
    "virtual_memory_mode":      "system_managed",
    "virtual_memory_size_gb":   16,
    "fps_limiter_enabled":      False,
    "fps_limit":                240,
    "graphics_auto_enabled":    False,
    "graphics_low_enabled":     False,
    "graphics_quality_level":   1,
    "roblox_volume_muted":      False,
    "roblox_volume_level":      10,
    "auto_process_priority_enabled": False,
    "process_priority":         "low",
    "cpu_limiter_enabled":      False,
    "cpu_limiter_mode":         "hard",
    "cpu_limiter_default_percent": 20,
    "cpu_limiter_apply_all":    True,
    "cpu_limiter_accounts":     {},
    "roblox_window_unlock_size_enabled": True,
    "roblox_window_resize_enabled": False,
    "roblox_window_size_preset": "200x150",
    "roblox_window_width":      200,
    "roblox_window_height":     150,
    "roblox_window_resize_interval_seconds": 10,
    "roblox_window_arrange_enabled": False,
    "roblox_window_arrange_columns": 6,
    "roblox_window_arrange_rows": 4,
    "roblox_window_arrange_gap": 0,
    "roblox_window_arrange_margin": 0,
    "roblox_window_hide_enabled": False,
    "multi_roblox_enabled": True,
    "multi_roblox_guard_self_heal_enabled": True,
    "multi_roblox_guard_self_heal_delay_seconds": 5.0,
    "multi_roblox_guard_self_heal_retry_seconds": 30.0,
    "multi_roblox_guard_self_heal_max_retry_seconds": 300.0,
    "multi_roblox_guard_self_heal_close_all": True,
    "rt_rotation_enabled": False,
    "runtime_account_allowlist": [],
    "ui_col_widths": {},
    "start_on_boot":          False,
    "start_farming_on_boot":  False,
    "auto_update_on_boot":    False,
    "privacy_blur_usernames":      False,
    "privacy_randomize_usernames": False,
    "discord_presence_enabled":      False,
    "discord_client_id":             "",
    "discord_show_accounts":         True,
    "discord_show_game":             True,
    "accounts":                 [],
    "runtime_state":            {},
}

def _restore_vip_scores(account: Account, saved: dict[str, Any]) -> None:
    """Restore persisted VIP scores/blacklists onto the account tracker."""
    if not (account._vip_tracker and "vip_scores" in saved):
        return
    try:
        now = time.time()
        with account._vip_tracker._lock:
            for item in saved["vip_scores"]:
                link = item.get("link")
                if not link or link not in account._vip_tracker._scores:
                    continue
                if "score" in item:
                    account._vip_tracker._scores[link] = float(item["score"])
                remaining_raw = item.get("blacklist_remaining", 0)
                remaining = int(remaining_raw) if remaining_raw else 0
                if remaining > 0:
                    account._vip_tracker._blacklist[link] = now + remaining
    except Exception:
        "intentional: ignore exception"


def _saved_max_float(saved: dict[str, Any], key: str) -> float:
    return max(0.0, _saved_float(saved, key))


def _restore_retry_counters(account: Any, saved: dict[str, Any], now: float) -> None:
    account.retry_count = _saved_int(saved, "retry")
    account.fail_count = _saved_int(saved, "fail")
    account.launch_fail_count = _saved_int(saved, "launch_fail_count")
    account.crash_retry_count = _saved_int(saved, "crash_retry_count")
    account.network_retry_count = _saved_int(saved, "network_retry_count")
    account.session_retry_count = _saved_int(saved, "session_retry_count")
    account.cooldown_until = _saved_max_float(saved, "cooldown_until")
    if account.cooldown_until <= now:
        account.cooldown_until = 0.0
    account.rapid_relaunch_count = _saved_int(saved, "rapid_relaunch_count")
    last_network = saved.get("last_network_lost_at")
    account.last_network_lost_at = float(last_network) if last_network else None


def _restore_recovery_state(account: Any, saved: dict[str, Any], now: float) -> None:
    account.recovery_status = _saved_str(saved, "recovery_status")
    if account.recovery_status in {"recovering", "queued", "launch_backoff", "due"}:
        account.recovery_status = "restored"
    account.last_recovery_reason = _saved_str(saved, "last_recovery_reason")
    account.recovery_scheduled_at = _saved_max_float(saved, "recovery_scheduled_at")
    if account.recovery_scheduled_at and account.recovery_scheduled_at <= now:
        account.recovery_scheduled_at = 0.0
    account.recovery_generation = _saved_int(saved, "recovery_generation")
    account.runtime_generation = _saved_int(saved, "runtime_generation")
    account.command_generation = _saved_int(saved, "command_generation")
    account.current_command_id = ""
    account.current_command = ""
    account.command_inflight_started_at = 0.0
    account.last_recovery_at = _saved_max_float(saved, "last_recovery_at")


def _restore_process_binding(account: Any, saved: dict[str, Any]) -> None:
    account.pid = _saved_int(saved, "bound_pid") or None
    account.bound_process_name = _saved_str(saved, "bound_process_name")
    account.bound_process_identity = _saved_str(saved, "bound_process_identity")
    account.last_pid_change_at = _saved_max_float(saved, "last_pid_change_at")
    account.last_launch_at = _saved_max_float(saved, "last_relaunch_at") or None
    account.process_binding_status = _saved_str(saved, "process_binding_status", "restored")
    account.binding_decision = _saved_str(saved, "binding_decision")
    account.process_binding_confidence = _saved_float(saved, "process_binding_confidence")
    account.process_reject_reason = _saved_str(saved, "process_reject_reason")
    account.process_owner_claim = _saved_str(saved, "process_owner_claim")
    account.unmanaged_live_process_count = _saved_int(saved, "unmanaged_live_process_count")
    live_pids = saved.get("unmanaged_live_pids", [])
    account.unmanaged_live_pids = list(live_pids) if isinstance(live_pids, list) else []
    account.adopt_candidate_pid = _saved_int(saved, "adopt_candidate_pid") or None
    account.adopt_reject_reason = _saved_str(saved, "adopt_reject_reason")


def _restore_identity_state(account: Any, saved: dict[str, Any]) -> None:
    account.liveness_state = _saved_str(saved, "liveness_state", "unknown")
    account.liveness_score = _saved_float(saved, "liveness_score")
    account.session_id = _saved_str(saved, "session_id")
    account.launch_nonce = _saved_str(saved, "launch_nonce")
    account.account_runtime_id = _saved_str(saved, "account_runtime_id")
    account.rejoin_transaction_id = _saved_str(saved, "rejoin_transaction_id")
    account.server_validation = _saved_str(saved, "server_validation", "restored")
    account.destination_validation = str(
        saved.get("destination_validation") or account.server_validation or "restored"
    )
    account.scheduler_slot = _saved_str(saved, "scheduler_slot")
    account.supervisor_state = _saved_str(saved, "supervisor_state", "restored")
    if account.supervisor_state in {"transaction_pending", "launching", "rejoining", "process_bound", "verifying"}:
        account.supervisor_state = "restored"


def _restore_transaction_state(account: Any, saved: dict[str, Any]) -> None:
    account.last_transaction_status = _saved_str(saved, "last_transaction_status")
    account.last_transaction_step = _saved_str(saved, "last_transaction_step")
    account.last_transaction_failure_reason = _saved_str(saved, "last_transaction_failure_reason")
    if account.last_transaction_status in {"pending", "launching", "process_bound", "verifying", "binding_verified"}:
        account.last_transaction_status = "rolled_back_on_restart"
        account.last_transaction_step = "rolled_back_on_restart"
        account.last_transaction_failure_reason = "backend_restart"
    account.last_transaction_reason = _saved_str(saved, "last_transaction_reason")
    account.last_transaction_started_at = max(
        0.0,
        float(saved.get("last_transaction_started_at", 0.0) or saved.get("session_started_at", 0.0) or 0.0),
    )
    account.last_transaction_completed_at = _saved_max_float(saved, "last_transaction_completed_at")
    account.session_started_at = _saved_max_float(saved, "session_started_at")
    account.last_transaction_at = _saved_max_float(saved, "last_transaction_at")


def _restore_launch_intent(account: Any, saved: dict[str, Any]) -> None:
    launch_intent = saved.get("launch_intent", {})
    account.launch_intent = launch_intent if isinstance(launch_intent, dict) else {}
    launch_intent_summary = saved.get("launch_intent_summary", {})
    if not isinstance(launch_intent_summary, dict):
        launch_intent_summary = {}
    account.launch_intent_summary = launch_intent_summary or dict(
        account.launch_intent.get("launch_intent_summary", {}) or {}
    )


def _restore_fresh_fields(account: Any, saved: dict[str, Any], now: float) -> None:
    _restore_retry_counters(account, saved, now)
    _restore_recovery_state(account, saved, now)
    _restore_process_binding(account, saved)
    _restore_identity_state(account, saved)
    _restore_transaction_state(account, saved)
    _restore_launch_intent(account, saved)


def _reset_stale_counters(account: Any) -> None:
    account.retry_count = 0
    account.fail_count = 0
    account.launch_fail_count = 0
    account.crash_retry_count = 0
    account.network_retry_count = 0
    account.session_retry_count = 0
    account.cooldown_until = 0.0
    account.rapid_relaunch_count = 0
    account.last_network_lost_at = None
    account.recovery_status = ""
    account.last_recovery_reason = ""
    account.recovery_scheduled_at = 0.0
    account.recovery_generation = 0
    account.binding_decision = ""
    account.process_binding_confidence = 0.0
    account.process_reject_reason = ""
    account.process_owner_claim = ""
    account.unmanaged_live_process_count = 0
    account.unmanaged_live_pids = []
    account.adopt_candidate_pid = None
    account.adopt_reject_reason = ""
    account.destination_validation = "unverified"
    account.launch_intent = {}
    account.launch_intent_summary = {}


def _reset_stale_runtime(account: Any) -> None:
    account.runtime_generation = 0
    account.command_generation = 0
    account.current_command_id = ""
    account.current_command = ""
    account.command_inflight_started_at = 0.0
    account.pid = None
    account.bound_process_name = ""
    account.bound_process_identity = ""
    account.last_pid_change_at = 0.0
    account.last_launch_at = None
    account.process_binding_status = "unbound"
    account.liveness_state = "unknown"
    account.liveness_score = 0.0
    account.session_id = ""
    account.launch_nonce = ""
    account.account_runtime_id = ""
    account.rejoin_transaction_id = ""
    account.server_validation = "unverified"
    account.scheduler_slot = ""
    account.supervisor_state = "stopped"
    account.last_transaction_status = ""
    account.last_transaction_step = ""
    account.last_transaction_reason = ""
    account.last_transaction_started_at = 0.0
    account.last_transaction_completed_at = 0.0
    account.last_transaction_failure_reason = ""
    account.session_started_at = 0.0
    account.last_transaction_at = 0.0
    account.launch_intent = {}


def _log_restored_runtime(account: Any, fresh: bool) -> None:
    _flog_kv(
        "RUNTIME",
        "restored",
        account=account.display_name,
        fresh=fresh,
        crash=account.crash_count,
        fail=account.fail_count,
        retry=account.retry_count,
        cooldown_left=max(0, int(account.cooldown_until - time.time())) if account.cooldown_until else 0,
        reason=account.last_recovery_reason or account.last_crash_reason,
    )
    if not fresh:
        _flog_kv(
            "STATE",
            "forced_reset",
            account=account.display_name,
            account_id=account._config_username,
            runtime_generation=account.runtime_generation,
            recovery_generation=account.recovery_generation,
            command_generation=account.command_generation,
            runtime_state=account.runtime.lifecycle_state.value,
            public_state=account.state.name,
            PID=account.pid or "",
            reason="restore_runtime_expired",
            thread_name=threading.current_thread().name,
        )


def _migrate_volume_level(raw: dict[str, Any]) -> None:
    try:
        legacy_volume = raw.get("roblox_volume_level")
        if isinstance(legacy_volume, bool):
            pass
        elif isinstance(legacy_volume, (int, float)) and float(legacy_volume) > 10:
            raw["roblox_volume_level"] = int(round(float(legacy_volume) / 10.0))
    except Exception:
        "intentional: ignore exception"


def _migrate_auto_close(raw: dict[str, Any]) -> None:
    try:
        seconds = max(0.0, float(raw.get("auto_close_seconds") or 0))
        raw["auto_close_minutes"] = int((seconds + 59) // 60) if seconds > 0 else 0
    except Exception:
        raw["auto_close_minutes"] = 0


def _apply_lua_legacy_keys(
    raw: dict[str, Any],
    had_lua_enabled: bool,
    had_lua_timeout: bool,
    legacy_use_lua: Any,
    legacy_lua_wait_timeout: Any,
) -> None:
    if not had_lua_enabled and legacy_use_lua is not None:
        raw["lua_enabled"] = bool(legacy_use_lua)
    if not had_lua_timeout and legacy_lua_wait_timeout is not None:
        try:
            raw["lua_timeout_seconds"] = max(1, min(300, int(float(legacy_lua_wait_timeout))))
        except Exception:
            raw["lua_timeout_seconds"] = 60


def _apply_other_legacy_keys(raw: dict[str, Any]) -> None:
    if "zombie_timeout" in raw and "not_responding_timeout" not in raw:
        raw["not_responding_timeout"] = raw["zombie_timeout"]
    if "auto_close_minutes" not in raw and "auto_close_seconds" in raw:
        _migrate_auto_close(raw)


def _migrate_games(cfg: dict[str, Any]) -> None:
    try:
        from domain.games import ensure_games_migrated

        ensure_games_migrated(cfg)
    except Exception:
        if not isinstance(cfg.get("games"), list):
            cfg["games"] = []


def _read_json_body(path: str) -> tuple[bool, Any]:
    """Return (has_body, parsed) for a JSON file; an empty file parses to nothing."""
    with open(path, encoding="utf-8") as handle:
        body = handle.read().strip()
    if not body:
        return False, None
    return True, json.loads(body)


_MISSING = object()


class ConfigManager:
    def __init__(self):
        self._cfg: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._io_lock = threading.RLock()
        self.load()

    def load(self):
        raw = self._read_text_json(CONFIG_FILE, {})
        # Capture legacy keys before validation (they are not in DEFAULTS anymore)
        had_lua_enabled = "lua_enabled" in raw
        had_lua_timeout = "lua_timeout_seconds" in raw
        legacy_use_lua = raw.get("use_lua")
        legacy_lua_wait_timeout = raw.get("lua_wait_timeout")
        # Migrate volume scale 0-100 (percent) to 0-10 (in-game menu scale).
        # Runs before validation clamps, so e.g. stored 50 becomes 5, not 10.
        _migrate_volume_level(raw)
        raw = validate_config_payload(raw, DEFAULTS)

        # Migration from old config filenames/keys
        _apply_lua_legacy_keys(
            raw,
            had_lua_enabled,
            had_lua_timeout,
            legacy_use_lua,
            legacy_lua_wait_timeout,
        )
        _apply_other_legacy_keys(raw)
        with self._lock:
            self._cfg = {k: raw.get(k, v) for k, v in DEFAULTS.items()}
            self._cfg["schema_version"] = int(raw.get("schema_version") or CONFIG_SCHEMA_VERSION)
            _migrate_games(self._cfg)

    def save(self):
        with self._lock:
            data = dict(self._cfg)
        data.pop("accounts", None)
        data.pop("runtime_state", None)
        data["schema_version"] = CONFIG_SCHEMA_VERSION
        self._write_text_json(CONFIG_FILE, data)

    def get(self, key: str, default=None) -> Any:
        with self._lock:
            return self._cfg.get(key, default if default is not None else DEFAULTS.get(key))

    def update(self, updates: dict[str, Any]):
        with self._lock:
            self._cfg = validate_config_payload({**self._cfg, **updates}, DEFAULTS)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._cfg)
    def _read_text_json(self, path: str, fallback):
        if not os.path.exists(path):
            return fallback
        try:
            with self._io_lock:
                has_body, parsed = _read_json_body(path)
            if not has_body:
                return fallback
            return parsed
        except Exception as e:
            _flog(f"Text store load error ({path}): {e}", "warning")
            recovered = self._read_backup_json(path, f"{path}.bak")
            if recovered is _MISSING:
                _flog_kv("CONFIG", "json_corrupt_using_fallback", "warning", path=path)
                return fallback
            return recovered

    def _read_backup_json(self, path: str, backup_path: str) -> Any:
        if not os.path.exists(backup_path):
            return _MISSING
        try:
            with self._io_lock:
                with open(backup_path, encoding="utf-8") as handle:
                    body = handle.read().strip()
                if not body:
                    return _MISSING
                recovered = json.loads(body)
            _flog_kv("CONFIG", "json_recovered_from_backup", "warning", path=path)
            return recovered
        except Exception as backup_error:
            _flog(f"Text store backup load error ({backup_path}): {backup_error}", "warning")
            return _MISSING

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
                "intentional: ignore exception"
    def get_accounts(self) -> list[Account]:
        from domain.account_model import Account

        raw = self._read_text_json(ACCOUNTS_TEXT_FILE, None)
        if raw is None:
            with self._lock:
                raw = self._cfg.get("accounts", [])
            if raw:
                self._write_text_json(ACCOUNTS_TEXT_FILE, raw)
        accounts = []
        for d in raw or []:
            try:
                acc = Account.from_dict(d)
                accounts.append(acc)
            except Exception as e:
                _flog(f"Account parse error: {e}", "warning")
        return accounts

    def save_accounts(self, accounts: list[Account]):
        payload = []
        for a in accounts:
            item = a.to_dict()
            item.pop("cookie", None)
            item["cookie_present"] = bool(str(getattr(a, "cookie", "") or "").strip())
            payload.append(item)
        self.update({"accounts": []})
        self.save()
        self._write_text_json(ACCOUNTS_TEXT_FILE, payload)

    def save_runtime(self, accounts: list[Account]):
        state = {"__schema_version": 1, "__saved_at": time.time()}
        saved_at = time.time()
        for a in accounts:
            runtime_snapshot = a.runtime_snapshot()
            entry: dict[str, Any] = {
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
                    "intentional: ignore exception"
            state[a._config_username] = entry
        self.update({"runtime_state": {}})
        self.save()
        self._write_text_json(RUNTIME_TEXT_FILE, state)
        _flog_kv("RUNTIME", "saved", accounts=len(accounts), saved_at=f"{saved_at:.3f}")

    def restore_runtime(self, accounts: list[Account]):
        state = self._read_text_json(RUNTIME_TEXT_FILE, None)
        if state is None:
            state = self.get("runtime_state", {})
        now = time.time()
        restore_window = max(0.0, float(self.get("recovery_restore_window", 3600) or 3600))
        for a in accounts:
            key = a._config_username
            if key not in state:
                continue
            saved = state[key]
            saved_at = _saved_float(saved, "runtime_saved_at")
            fresh = bool(saved_at and (now - saved_at) <= restore_window)
            a.crash_count = _saved_int(saved, "crash")
            a.last_crash_reason = _saved_str(saved, "last_crash_reason")
            if fresh:
                _restore_fresh_fields(a, saved, now)
            else:
                _reset_stale_counters(a)
                _reset_stale_runtime(a)
            a.sync_runtime("restore_runtime")
            _restore_vip_scores(a, saved)
            _log_restored_runtime(a, fresh)
