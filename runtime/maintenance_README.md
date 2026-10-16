# runtime/maintenance_* — Watchdog subgroup

Grouped (not a subpackage yet): all `runtime/maintenance_*.py` (8 files) + aggregator `system_maintenance.py`.

| File | Watches |
| ---- | ------- |
| `maintenance_queue.py` | Stuck/empty queue |
| `maintenance_liveness.py` | Dead clients |
| `maintenance_captcha.py` | Captcha holds |
| `maintenance_verify.py` | Face/ID + passkey holds |
| `maintenance_moderation.py` | Ban/suspend/lock |
| `maintenance_performance.py` | FPS/CPU/RAM |
| `maintenance_lua_timeout.py` | Lua heartbeat |
| `maintenance_watchdog_actions.py` | Shared kill/relaunch |

Ticked by `runtime_scheduler.py` via `system_maintenance.py`. One file per concern; shared helpers go in `maintenance_watchdog_actions.py`, not copy-pasted.
