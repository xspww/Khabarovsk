from __future__ import annotations

from typing import Any, List

from core import AccountState, flog


def handle_network_restored(recovery: Any, accounts: List[Any]) -> None:
    if not recovery._cfg.get("auto_rejoin", True):
        flog("[RECOVERY] Auto rejoin disabled - skip reconcile on network restore", "warning")
        return
    for acc in accounts:
        if acc.desired_state != AccountState.IN_GAME or acc.state == AccountState.FAILED:
            continue
        with acc._lock:
            acc.network_retry_count = 0
            expedite = (
                acc.state in {AccountState.NETWORK_LOST, AccountState.COOLDOWN, AccountState.CRASH}
                or acc.recovery_status in {"network_lost", "cooldown", "scheduled"}
                or bool(acc.cooldown_until)
            )
            if expedite:
                recovery._runtime_state.set_cooldown(acc, 0.0, reason="network_restored")
                acc.recovery_scheduled_at = 0.0
                acc.scheduler_slot = ""
            if acc.recovery_status == "network_lost" or expedite:
                recovery._runtime_state.set_recovery(acc, status="network_restored", reason="network_restored", inflight=True)
                acc.sync_runtime("network_restored")
        if expedite:
            recovery._scheduler.cancel(f"recovery:{acc._config_username}", reason="network_restored")
        recovery._log_recovery_decision("network_restored", acc, "network_restored", expedited=expedite)
        recovery.request_evaluate(acc, trigger="network_restored", force_restart=True)

