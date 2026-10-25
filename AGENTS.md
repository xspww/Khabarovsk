# AGENTS.md

Windows-only, local-first Roblox launcher: Python 3.11+ backend (FastAPI/uvicorn on
`127.0.0.1`), PySide6 + WebView2 shell, plain-ESM dashboard. Package READMEs are the
per-layer file maps.

## Commands

```powershell
python -m pip install -r requirements.txt        # Python 3.11+
python -m compileall -q .                        # CI step 1
python -m unittest discover -s tests -v          # CI step 2 (full suite, ~2s)
python -m unittest tests.test_recovery_policy -v # single file
ruff check .                                     # configured; not in CI or requirements
```

- `tests/` has no `__init__.py`, uses `unittest` (not pytest), and must stay hermetic:
  no Windows APIs, network, browser, or real account data.
- `main.py` exits immediately on non-Windows (`sys.platform != "win32"`), so never
  import it from tests. Run from source with `Run.cmd` (finds Python 3.11+, sets
  UTF-8 console env) or `python main.py`.
- Build: generate the icon, then PyInstaller (`README.md` has the exact commands).
  Releases are cut only by pushing tag `v<APP_VERSION>` from `version.py`;
  `release.yml` rejects mismatches, then builds, smokes the guard, signs, and publishes.
  `build_info.py`, `assets/cronus_icon.ico`, `dist/`, `build/`, `guard_out.txt` are
  generated/ignored.

## Wiring and boundaries

- `main.py` is the composition root: imports, singletons, `FarmController`, then
  `ApiContext` and `register_api_routes` near the bottom. New route modules must be
  registered in `api_routes/__init__.py` and wired through `ApiContext` in `main.py`.
- `infra/` paths/logging, `config/` JSON config, `domain/` models/predicates,
  `services/` external boundaries, `runtime/` orchestration, `api_routes/` thin
  FastAPI adapters, `desktop/` Windows shell, `ui/` frontend. The intended dependency
  direction is `runtime/` → `services/`; a few legacy `services/` modules still import
  `runtime/`, so avoid adding more reverse imports.
- `core.py` is the public facade re-exporting config/domain/runtime symbols.
  `runtime/farm.py` (`FarmController`) is the control-plane entry point for START/STOP,
  queue, launch, and recovery; `runtime/start_readiness.py` owns the START gate. Read
  `runtime/README.md` before changing recovery or scheduling logic.
- `ui/` is served by `desktop/ui_dashboard.py`; `main.py` middleware sets `no-store`.
  There is no bundler, `package.json`, or JS test runner; validate syntax with
  `node --check`. Runtime scripts are injected by `desktop/ui_dashboard.py`, not present
  in `ui/index.html`; API token/version placeholders are replaced by
  `api_routes/system_routes.py`.
- Bundled-asset loads must use `infra.app_paths.resource_path()` (handles
  `sys._MEIPASS`), not `__file__`. PyInstaller bundles whole `assets/`, `ui/`, `lua/`
  trees; a new top-level data directory needs a `cronus_launcher.spec` change.

## Gotchas

- **Cache busters are manual.** After editing a JS/CSS file, bump its `?v=` in
  `ui/app.js`, `ui/app/dashboard.js`, `ui/dashboard.css`, `ui/index.html`, or
  `desktop/ui_dashboard.py` — otherwise a running dashboard can keep the old module.
- `ui/index.html` is one physical line; use exact string replacement, never reformat it.
- Account state lives in `%LOCALAPPDATA%\Cronus Launcher\data` (override with
  `CRONUS_USER_ROOT`). Never commit cookies, account data, or logs.
- Startup dependency checks parse `requirements.txt`; a package whose import name
  differs from its distribution name needs an entry in `_REQUIREMENT_IMPORT_NAMES`
  in `main.py` (e.g. `pillow` → `PIL`).
- Domain terminology is fixed in `GLOSSARY.md` (Thai: `GLOSSARY.th.md`) — use those
  terms in code, commits, and docs.
