from __future__ import annotations

from typing import Any, NamedTuple

from core import Account, AccountState, flog, flog_kv
from runtime.lua_liveness_policy import mark_waiting_for_lua
from services.process_service import ProcessManager, ProcessService


class _AccountSnapshot(NamedTuple):
    current_pid: Any
    expected_identity: str
    expected_tracker_id: Any
    process_name: str
    runtime_generation: Any


def _account_snapshot(acc: Account) -> _AccountSnapshot:
    with acc._lock:
        return _AccountSnapshot(
            current_pid=acc.pid,
            expected_identity=acc.bound_process_identity,
            expected_tracker_id=acc.browser_tracker_id,
            process_name=acc.bound_process_name or "RobloxPlayerBeta.exe",
            runtime_generation=acc.runtime_generation,
        )


def _snapshot_is_stale(acc: Account, snapshot: _AccountSnapshot) -> bool:
    return (
        acc.pid != snapshot.current_pid
        or acc.bound_process_identity != snapshot.expected_identity
        or acc.browser_tracker_id != snapshot.expected_tracker_id
        or int(acc.runtime_generation or 0) != int(snapshot.runtime_generation or 0)
    )


def _sync_existing_account(
    acc: Account,
    snapshot: _AccountSnapshot,
    state_mgr: Any,
    lua_required: bool,
    runtime_state: Any,
) -> bool:
    if not (
        snapshot.current_pid
        and snapshot.expected_identity
        and ProcessManager.is_bound_game_alive(
            snapshot.current_pid,
            owner_key=acc._config_username,
            expected_identity=snapshot.expected_identity,
            expected_browser_tracker_id=snapshot.expected_tracker_id,
        )
    ):
        return False
    with acc._lock:
        if _snapshot_is_stale(acc, snapshot):
            flog_kv(
                "FARM",
                "initial_sync_existing_rejected",
                "warning",
                account=acc.display_name,
                pid=snapshot.current_pid,
                reason="stale_account_snapshot",
                expected_runtime_generation=snapshot.runtime_generation,
                current_runtime_generation=acc.runtime_generation,
            )
            return False
        bind_result = ProcessService.bind_account_process(
            acc,
            snapshot.current_pid,
            state_mgr,
            reason="initial_state_sync_existing",
            expected_identity=snapshot.expected_identity,
            process_name=snapshot.process_name,
            min_ram_mb=0.0,
            increment_generation=False,
            expected_runtime_generation=snapshot.runtime_generation,
        )
        if not bind_result.get("ok"):
            flog_kv(
                "FARM",
                "initial_sync_existing_rejected",
                "warning",
                account=acc.display_name,
                pid=snapshot.current_pid,
                reason=bind_result.get("reason", ""),
            )
            return False
        if lua_required:
            mark_waiting_for_lua(
                acc,
                runtime_state,
                state_mgr,
                "initial_state_sync_existing",
            )
        else:
            state_mgr.transition(
                acc,
                AccountState.IN_GAME,
                reason="initial_state_sync_existing",
                force=True,
                expected_generation=snapshot.runtime_generation,
            )
        return True


def _visible_targets(accounts: list[Account]) -> list[Account]:
    return [
        acc
        for acc in sorted(accounts, key=lambda a: int(a.priority or 50))
        if acc.desired_state == AccountState.IN_GAME and not acc.pid
    ]


def _adopt_visible_once(
    target: Account,
    accounts: list[Account],
    state_mgr: Any,
    lua_required: bool,
    runtime_state: Any,
    claimed_pids: set[Any],
) -> bool:
    with target._lock:
        runtime_generation = target.runtime_generation
    adopt = ProcessService.safe_adopt_visible_process(
        target,
        state_mgr,
        accounts=accounts,
        reason="initial_state_sync_visible_adopt",
        expected_runtime_generation=runtime_generation,
    )
    if not adopt.get("ok"):
        return False
    claimed_pids.add(int(adopt.get("pid") or 0))
    if lua_required:
        mark_waiting_for_lua(
            target,
            runtime_state,
            state_mgr,
            "initial_state_sync_visible_adopt",
        )
    else:
        state_mgr.transition(
            target,
            AccountState.IN_GAME,
            reason="initial_state_sync_visible_adopt",
            force=True,
        )
    return True


def initial_state_sync(accounts: list[Account], state_mgr: Any, lua_required: bool = False, runtime_state: Any = None) -> None:
    live_processes = ProcessManager.list_live_game_processes()
    if not live_processes:
        flog("[FARM] initial_state_sync: no live RobloxPlayerBeta.exe found")
        return

    claimed_pids: set[Any] = set()
    synced = 0

    for acc in accounts:
        snapshot = _account_snapshot(acc)
        if _sync_existing_account(
            acc,
            snapshot,
            state_mgr,
            lua_required,
            runtime_state,
        ):
            claimed_pids.add(snapshot.current_pid)
            synced += 1

    candidates = [item for item in live_processes if item["pid"] not in claimed_pids]
    targets = _visible_targets(accounts)

    if candidates and len(targets) == 1:
        if _adopt_visible_once(
            targets[0],
            accounts,
            state_mgr,
            lua_required,
            runtime_state,
            claimed_pids,
        ):
            synced += 1
            candidates = [
                item
                for item in candidates
                if int(item.get("pid") or 0) not in claimed_pids
            ]

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
