from __future__ import annotations

from typing import Any, Dict, List

from core import Account, AccountState, flog_kv
from runtime.recovery_support import _clear_account_cookie_block
from services.auth_gate import evaluate_account_auth_gate, mark_account_auth_quarantined


def preflight_cookie_blocks(
    accounts: List[Account],
    recovery: Any,
    state_mgr: Any,
    runtime_state: Any,
) -> Dict[str, str]:
    blocked: Dict[str, str] = {}
    if not recovery or not state_mgr:
        return blocked
    for acc in accounts:
        try:
            decision = evaluate_account_auth_gate(acc)
        except Exception as e:
            flog_kv("FARM", "preflight_auth_gate_error", "warning", account=acc.display_name, error=e)
            continue
        if decision.blocked:
            try:
                mark_account_auth_quarantined(acc, decision, source="preflight", runtime_writer=runtime_state)
                recovery.fail_account(acc, decision.reason_key, decision.reason)
                blocked[acc._config_username] = decision.reason
                flog_kv("FARM", "account_preflight_blocked", "warning", account=acc.display_name, **decision.to_dict())
            except Exception as e:
                flog_kv("FARM", "preflight_fail_account_error", "warning", account=acc.display_name, error=e)
            continue
        try:
            with acc._lock:
                if acc.state == AccountState.FAILED and acc.last_crash_reason == "cookie_mismatch":
                    _clear_account_cookie_block(acc)
                    runtime_state.clear_recovery(acc, reason="cookie_mismatch_cleared", inflight=False)
                    runtime_state.set_cooldown(acc, 0.0, reason="cookie_mismatch_cleared")
                    state_mgr.transition(acc, AccountState.IDLE, reason="cookie_mismatch_cleared", force=True)
        except Exception as e:
            flog_kv("FARM", "preflight_cookie_clear_error", "warning", account=acc.display_name, error=e)
    return blocked
