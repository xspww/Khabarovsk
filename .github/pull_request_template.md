## What / Why

<!-- Conventional title, e.g. `fix: ...`. Link issue if any. -->

## Manual test

- [ ] `python -m unittest discover -s tests -v`
- [ ] `python -m ruff check .`
- [ ] Smoke: `python main.py` or `dist/CronusLauncher.exe --multi-roblox-guard`

## Checklist

- [ ] No cookies / data / logs / keys / binaries committed
- [ ] `requirements.in` + `requirements-lock.txt` updated if deps changed
- [ ] Docs updated (`README.md` / package README if architecture changed)
