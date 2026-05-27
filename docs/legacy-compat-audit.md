# Legacy Compatibility Audit

Date: 2026-05-25

This pass did not remove compatibility surfaces that still have runtime or test evidence.

## Do Not Delete Yet

- `app_paths.LEGACY_FILENAME_ALIASES` and legacy Argus data root migration:
  Production path still calls `migrate_legacy_data_files(...)`, and tests cover legacy config filename migration.
- `account_hybrid.dpapi_unprotect_compatible`:
  Used by account data decoding, with tests for legacy RoboGuard DPAPI account/cookie payloads.
- `AccountDataStore.ensure_from_legacy`:
  Used during `main.py` startup to seed `AccountData.json` from legacy config accounts.
- `domain.runtime_lifecycle.lifecycle_for_legacy_runtime`:
  Used by runtime model/observability code and covered by runtime state machine tests.
- `api_routes` token aliases for `X-Argus-Token`, `X-RoboGuard-Token`, and `argus_token`:
  Tests still assert legacy API and Lua token aliases during migration.
- `services.process_service.ProcessManager` compatibility facade:
  Production modules and tests still import or patch `ProcessManager` through the legacy-compatible surface.

## Potential Future Deletions

- Route-local dead helpers should be removed only after an `rg` call-site scan and route tests. `_global_launch_target` had no call site in `api_routes/accounts_routes.py` and was removed during this refactor.
- Compatibility test facades such as `tests/test_hybrid_account.py` and `tests/test_runtime_hardening.py` can only be removed if the project switches discovery to the split case modules.
