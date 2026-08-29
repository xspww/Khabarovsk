# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability, please **do not** open a public issue.
Instead, report it privately:

- Open a GitHub Security Advisory via `Security > Report a vulnerability`, or
- Contact the maintainers through GitHub private reporting.

We aim to acknowledge reports within 48 hours.

## Sensitive Data

- Roblox cookies (`.ROBLOSECURITY`) are stored locally only, encrypted with Windows DPAPI (`account_hybrid.py:dpapi_protect`).
- Cookies are never committed to the repository — `AccountData.json` and `data/` are ignored via `.gitignore`.
- Logs redact cookies and `privateServerLinkCode` via `core_logging.py:_redact_value`.

## Supported Versions

Only the latest `main` branch is actively supported. Packaged releases (`build-*` tags) are built by CI and should be updated via the built-in updater.

## Safe Usage

- Run only on Windows 10/11 with Python 3.11+.
- Build the exe with `python ops/build_exe.py --onedir` to reduce antivirus false positives; code-sign when distributing.
