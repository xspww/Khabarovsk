from __future__ import annotations

from typing import Any, Dict, Mapping, Optional


def _update_config(component: Any, cfg: Dict[str, Any]) -> None:
    update = getattr(component, "update_config", None)
    if callable(update):
        update(cfg)


def apply_runtime_config_snapshot(
    *,
    cfg: Dict[str, Any],
    accounts: list,
    machine_supervisor: Optional[Any] = None,
    recovery: Optional[Any] = None,
    maintenance: Optional[Any] = None,
    workers: Optional[Mapping[str, Any]] = None,
    dispatcher: Optional[Any] = None,
) -> None:
    if machine_supervisor:
        machine_supervisor.update_config(cfg)
        machine_supervisor.set_accounts(accounts)

    if recovery:
        update = getattr(recovery, "update_config", None)
        if callable(update):
            update(cfg, accounts)

    _update_config(maintenance, cfg)

    for worker in (workers or {}).values():
        _update_config(worker, cfg)

    _update_config(dispatcher, cfg)
