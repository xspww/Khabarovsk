from __future__ import annotations

from typing import Any, List

from core import Account, AccountState, flog, flog_kv
from services.process_service import ProcessManager, ProcessService


def initial_state_sync(accounts: List[Account], state_mgr: Any) -> None:
    live_processes = ProcessManager.list_live_game_processes()
    if not live_processes:
        flog("[FARM] initial_state_sync: no live RobloxPlayerBeta.exe found")
        return

    claimed_pids = set()
    synced = 0

    for acc in accounts:
        with acc._lock:
            current_pid = acc.pid
            expected_identity = acc.bound_process_identity
            expected_tracker_id = acc.browser_tracker_id
            process_name = acc.bound_process_name or "RobloxPlayerBeta.exe"
            runtime_generation = acc.runtime_generation
        if current_pid and expected_identity and ProcessManager.is_bound_game_alive(
            current_pid,
            owner_key=acc._config_username,
            expected_identity=expected_identity,
            expected_browser_tracker_id=expected_tracker_id,
        ):
            with acc._lock:
                stale_snapshot = (
                    acc.pid != current_pid
                    or acc.bound_process_identity != expected_identity
                    or acc.browser_tracker_id != expected_tracker_id
                    or int(acc.runtime_generation or 0) != int(runtime_generation or 0)
                )
                if stale_snapshot:
                    flog_kv(
                        "FARM",
                        "initial_sync_existing_rejected",
                        "warning",
                        account=acc.display_name,
                        pid=current_pid,
                        reason="stale_account_snapshot",
                        expected_runtime_generation=runtime_generation,
                        current_runtime_generation=acc.runtime_generation,
                    )
                    continue
                bind_result = ProcessService.bind_account_process(
                    acc,
                    current_pid,
                    state_mgr,
                    reason="initial_state_sync_existing",
                    expected_identity=expected_identity,
                    process_name=process_name,
                    min_ram_mb=0.0,
                    increment_generation=False,
                    expected_runtime_generation=runtime_generation,
                )
                if not bind_result.get("ok"):
                    flog_kv(
                        "FARM",
                        "initial_sync_existing_rejected",
                        "warning",
                        account=acc.display_name,
                        pid=current_pid,
                        reason=bind_result.get("reason", ""),
                    )
                    continue
                claimed_pids.add(current_pid)
                state_mgr.transition(
                    acc,
                    AccountState.IN_GAME,
                    reason="initial_state_sync_existing",
                    force=True,
                    expected_generation=runtime_generation,
                )
                synced += 1

    candidates = [item for item in live_processes if item["pid"] not in claimed_pids]
    targets = [
        acc for acc in sorted(accounts, key=lambda a: int(a.priority or 50))
        if acc.desired_state == AccountState.IN_GAME and not acc.pid
    ]

    if candidates and len(targets) == 1:
        target = targets[0]
        with target._lock:
            runtime_generation = target.runtime_generation
        adopt = ProcessService.safe_adopt_visible_process(
            target,
            state_mgr,
            accounts=accounts,
            reason="initial_state_sync_visible_adopt",
            expected_runtime_generation=runtime_generation,
        )
        if adopt.get("ok"):
            claimed_pids.add(int(adopt.get("pid") or 0))
            state_mgr.transition(target, AccountState.IN_GAME, reason="initial_state_sync_visible_adopt", force=True)
            synced += 1
            candidates = [item for item in candidates if int(item.get("pid") or 0) not in claimed_pids]

    if candidates:
        flog_kv(
            "FARM",
            "initial_sync_unclaimed_skipped",
            "warning",
            candidates=len(candidates),
            targets=len(targets),
            reason="unclaimed_processes_not_auto_bound",
        )

    remaining = len(live_processes) - len(claimed_pids)
    flog(
        f"[FARM] initial_state_sync complete: synced={synced} "
        f"live_processes={len(live_processes)} remaining_unclaimed={max(0, remaining)}"
    )
