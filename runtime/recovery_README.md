# runtime/recovery_* — Recovery subgroup

Grouped (not a subpackage yet): all `runtime/recovery_*.py` (15 files).

- Facade: `recovery_engine.py` (no logic, `RecoveryCoordinator` mixins)
- Core/wiring: `recovery_core.py`, `recovery_txn.py`
- Policy/scoring: `recovery_policy.py`, `recovery_evaluator.py`, `recovery_context.py`, `recovery_support.py`, `recovery_slots.py`
- Routing/storm: `recovery_signal_router.py`, `recovery_seam.py`, `recovery_storm.py`, `recovery_owner.py`, `recovery_guard_net.py`
-Surface: `recovery_reports.py`, `recovery_view.py`

Rule: only `runtime/` orchestrates recovery. `services/` must not import `recovery_*` (guarded by `tests/test_layering.py`).
Future: move to `runtime/recovery/` package once imports are re-pointed; files stay flat for now to avoid breakage.
