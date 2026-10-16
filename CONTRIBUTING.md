# Contributing

## Commits (Conventional Commits, enforced by review)

Format: `<type>: <short summary>`

Allowed types: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `build`, `ci`

Good:
- `feat: add smart-queue delay for full slots`
- `fix: stop rejoin storm after kick`
- `refactor: split recovery_engine god file`
- `test: add layering guard for services->runtime`

Bad (will be asked to reword):
- `Nothing`, `Ai slop`, `update`, `fix stuff`

## Branches / PRs

- Work on feature branches, open PR against `main`, squash-merge.
- PR must: describe what + why, list manual test (`python main.py` smoke or `python -m unittest discover -s tests`), no cookies/data/logs/binaries.
- CI must be green: `compileall` + `ruff check` + `unittest` + total `coverage --fail-under=15` + pure-surface `--fail-under=43` (config/domain/version + guards; roadmap: raise pure gate, total 60% needs Windows integration job — see `pyproject.toml`).

## What never goes in a commit

Account cookies, `data/`, `AccountData.json`, `*.key`, `*.pem`, `secrets.json`, `*.db`, `*.log`, `dist/`, `build/`. See `.gitignore`.

## Deps

- Edit `requirements.in` (lower bounds), regenerate `requirements-lock.txt` (`==` pins).
- CI installs from `requirements-lock.txt` only.
