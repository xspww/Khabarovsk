# GLOSSARY — Khabarovsk domain language

Use these terms consistently in code, commits, and documentation. Do not
substitute a file name or class name for the term itself. The owning module is
the single source of truth for the concept: link to it instead of restating its
rules.

| Term | Meaning | Owning module |
| --- | --- | --- |
| Recovery | Restoring an account after a crash, disconnect, or guard loss, including the rejoin. | Recovery modules (`runtime/recovery_*.py`) |
| Recovery Policy | The rules for which recovery signals are deduplicated, how long to wait, and what budget applies. | Recovery policy (`runtime/recovery_policy.py`) |
| Recovery Owner | The single owner allowed to recover a given account, so two recovery paths cannot race. | Recovery owner (`runtime/recovery_owner.py`) |
| Recovery Storm | A circuit breaker that delays or spaces out recovery when many accounts ask to recover at the same time. | Recovery storm (`runtime/recovery_storm.py`) |
| Recovery Slots | The slot policy: how many accounts may recover concurrently and which accounts wait in the queue. | Recovery slots (`runtime/recovery_slots.py`) |
| Farm | The set of managed accounts and the start / stop / reconcile lifecycle that drives them. | Farm modules (`runtime/farm*.py`) |
| Launch | The Roblox launch pipeline: validate → guard → VIP → ticket → launch. | Roblox launcher (`services/roblox_launcher.py`, `services/roblox_launch_service.py`) |
| Process Binding | Binding a PID to an account and deciding ownership, adoption, and kill rights. | Process service (`services/process_service.py`) |
| Window | Arranging, hiding, and showing Roblox windows. | Window control (`services/window_control.py`, adapter) |
| Settings | Normalizing, clamping, and fanning out configuration values. | Settings state (`api_routes/settings_state.py`, `runtime/system_maintenance.py`) |
