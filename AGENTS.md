# AGENTS.md

Guidance for coding agents working in this repo. Start with `README.md`, which covers what the tool does and how to use it, and `CONTRIBUTING.md`.

## What this is

Newsletter Archive is a personal command-line tool (`archive`). It saves posts the user subscribes to (Ghost or Substack) as JSON outside the repo and indexes them for hybrid search, which a local MCP server exposes (`archive mcp`). The repo is public. It holds code and specs only, never newsletter content.

- `src/newsletter_archive/`: the CLI (`cli.py`), the scraping stages (`discover`, `fetch`, `extract`), storage, and `platforms/` (Ghost, Substack).
- `src/newsletter_archive/search/`: indexing, keyword and vector retrieval, fusion, re-ranking, and the evaluation code.
- `tests/`: offline unit tests and fakes (`tests/fakes.py`). Also `tests/search/` (Postgres integration tests marked `db`) and `tests/integration/` (end-to-end CLI tests against a fake site).
- `scripts/`: `check_safety.py`, `make_fixtures.py`, `eval_search.py`, `scheduled-run.sh`.
- `openspec/`: `specs/` holds the current requirements; `changes/` holds work in progress and the archive.

## Rules that never bend

These protect the user's subscription and secrets. Breaking one is worse than not shipping.

- **Polite scraping.** One request at a time, at least 2 seconds apart, with backoff. Never lower the floor, add concurrency, or work around the paywall.
- **Secrets stay in `.env`.** Never print, log or commit the newsletter cookies (`*_COOKIE`), `GEMINI_API_KEY` or `ARCHIVE_DB_*_PASSWORD`, and keep them out of error messages and MCP responses. Wrap new secrets in the `Secret` type.
- **Fixtures are structure-only.** Use real page markup with the text replaced by filler (`scripts/make_fixtures.py`). No real article text, names, emails or ids.
- **Never commit** `.env`, `config.yaml`, `data/`, or the archive folder (`../leadership-coach-datasets/`).
- **Tests are offline.** Real-API tests carry the `api` marker and stay opt-in.

## Ask before any live call

Offline tests and local tools are always fine. Get a yes from the user before anything that reaches a real service:

- **The newsletter site** (`archive discover`, `fetch`, `run`). The first live run is `archive run --limit 5`, never a full crawl.
- **Gemini** (`archive index`, `scripts/eval_search.py`, `api` tests). Run `archive index --dry-run` first and show the estimated cost.
- **The real index.** Ask before `--swap`, `--rollback`, or a full `--refresh`.

## Setup and checks

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.lock && pip install --no-deps -e .
docker compose up -d --wait db           # optional locally: enables the `db` tests

pytest -m "slow or not slow"
ruff check . && ruff format --check . && mypy
python scripts/check_safety.py
```

Test markers: `slow` (real clock), `db` (needs the compose database; skipped when it's down, required in CI), `api` (real Gemini; runs only with `ARCHIVE_API_TESTS=1`). CI runs `test` (Python 3.11 and 3.12), `integration`, `coverage` (≥ 95%), `lint` and `safety`.

## How changes are made

Every change follows proposal → specs → design → low-level design → task plan → a stack of small `[N/M]` PRs. The user approves each step before the next one starts, and the user merges every PR. The full process is in `.claude/skills/archive-dev-workflow/SKILL.md`. In short:

- Start with an openspec change (`openspec new change <id>`). Extend an existing capability in `openspec/specs/` instead of creating a parallel one.
- Keep each PR to a few hundred lines. Ship its tests with it, fill in `.github/pull_request_template.md`, and tick its items in `tasks.md`.
- Run a code review and put the fixes in a separate `Fix review findings on the <area>` commit. Wait for green CI.
- Never merge, force-push `main`, or skip a check.

## Writing style

Write like the README: short, plain words, outcome first. Error messages tell the user what to do next (for example, "run `archive index`"). When behaviour or setup changes, update the README in the same PR.
