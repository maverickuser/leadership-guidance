# Contributing

This is a personal tool, but fixes are welcome.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.lock && pip install --no-deps -e .
pre-commit install
```

## Before opening a PR

- `pytest -m "slow or not slow"` passes. Tests are offline; never add one that touches the real site.
- `ruff check .`, `ruff format --check .` and `mypy` are clean.
- `python scripts/check_safety.py` passes.
- Keep PRs small and focused; describe which spec requirement (in `openspec/`) the change implements.

## Rules that never bend

- Polite scraping: one request at a time, at least 2 seconds apart. Don't lower the floor.
- Use only the subscriber's own cookie. Never try to get around the paywall.
- The cookie lives only in `.env`. Never print, log or commit it.
- Fixtures are structure-only: real page markup with the text replaced by filler
  (`scripts/make_fixtures.py`). No real article text, names, emails or ids.
- Never commit `.env`, `data/`, or the archive folder.
