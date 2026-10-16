# Changelog

Single source of truth is `version.py:APP_VERSION`. Tag `vX.Y.Z` must match.

## [Unreleased]

- Arch debt cleared (direct imports only): `domain/` x2, `infra/` x1, `services/` x4 no longer top-level import banned layers (lazy + local copies); `tests/test_layering.py` guards direct imports, transitive coupling via `core.py` re-exports remains by design and is pinned by `test_core_hub_transitive_documented`
- Tests: 216 total (+28) `test_version_compare` / `test_session_identity` / `test_browser_tracker` / `test_config_validation` / `test_moderation_guards` / `test_proof_verdict`; `version.py` 25%->83%, `config/validation.py` 12%->88%, `process_proof_policy.py` 31%->83%
- Review fixes: `_urlopen` fallback scoped to `ImportError` only (no silent retry on network errors); layering guard restored `services↛runtime` / `infra↛desktop` bans + `test_core_hub_transitive_documented`; lazy fallbacks narrowed to `ImportError` with lock/mirror-state parity test
- CI: `ruff check` clean, total `coverage --fail-under=15` + pure-surface `--fail-under=43` (config/domain/version + guards); total 60% explicitly requires Windows integration job, not more hermetic tests

## [3.2.4] - 2026-10-15

- Layered layout: split `roblox_hybrid` (1662 LOC) / `recovery_engine` (1488 LOC) god files
- Docs: Thai README per package (`README.th.md`)
- Chore: drop dead shims, py311 annotation modernize, ruff green

## [3.2.3] - 2026-10-14

- Maintenance sweep: lint + guard-path fixes

## [3.2.2] - 2026-10-13

- Update-check hardening, install cleanup

Rules going forward (see `CONTRIBUTING.md`): Conventional Commits, squash-merge, CI = compileall + ruff + unittest + coverage>=60%.
