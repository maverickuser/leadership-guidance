---
name: archive-dev-workflow
description: How to make any code change in the leadership-guidance (Newsletter Archive) repo, from idea to merged PR stack - openspec spec, low-level design (lld.md), implementation plan in tasks.md, then a stack of small "[N/M]" PRs, each with unit and `db` integration tests, a /code-review pass with a "Fix review findings" commit, and green GitHub Actions CI. Use this skill whenever the user asks to add, change, fix or refactor anything in this repo (scraper, extractor, CLI, search, indexer, MCP server, eval scripts, CI), to plan or design a feature, to continue or open the next PR of a stack, or to address review findings - even a small fix, and even when they don't mention openspec, PRs or tests.
---

# Developing in the Newsletter Archive repo

Every change here follows one path: **proposal → specs → design → LLD → implementation plan → stack of small PRs**. Each planning document is a gate: write one, show it to the user, and wait for their approval before starting the next (see "Review gates"). The user merges every PR themselves. This keeps a personal scraper that holds a private cookie and an API key safe, and keeps each PR small enough to review properly.

## Review gates

The user reviews each artifact before the next one is written, because each one builds on the last: a wrong proposal produces wrong specs, and a wrong spec produces a wrong design. Writing everything at once and asking for one approval at the end wastes the user's review and your work.

| Gate | Write | Then stop and ask |
|---|---|---|
| 1 | `proposal.md` | "Approve the proposal, or what should change?" |
| 2 | `specs/**/spec.md` (run `openspec validate <change> --strict`) | "Approve the specs?" |
| 3 | `design.md` | "Approve the design decisions?" |
| 4 | `lld.md` | "Approve the low-level design?" |
| 5 | `tasks.md` (the PR stack) | "Approve the PR plan?" Then start PR 1 |

At each gate:

- Give a short summary of what the artifact says (the key decisions, what changed since the last version) and its path, so the user can open it.
- Ask with `AskUserQuestion`: approve, or request changes.
- Apply any changes, show what changed, and ask again. Don't move to the next artifact until the user approves this one.
- Record each approval in `tasks.md` section 0 once that file exists (for example "0.1 Proposal approved 2026-11-10"); until then, keep a note of the approvals.

Research and questions come before gate 1. That includes approved live checks: facts measured there go into the proposal and design.

Read `CONTRIBUTING.md` and `openspec/config.yaml` at the start of a session. They hold the project context and the rules below.

## Rules that never bend

These protect the user's subscription and secrets. Breaking one is worse than not shipping.

- **Polite scraping:** one request at a time, at least 2 seconds apart, with backoff. Never lower the floor, add concurrency, or work around the paywall.
- **Secrets live only in `.env`:** `NEWSLETTER_COOKIE`, `GEMINI_API_KEY` and the `ARCHIVE_DB_*_PASSWORD`s are never printed, logged, put in error messages or MCP responses, or committed. Wrap new secrets in the `Secret` type.
- **Fixtures are structure-only:** real page markup with the text replaced by filler (`scripts/make_fixtures.py`). Never use real article text, names, emails or ids.
- **Never commit** `.env`, `config.yaml`, `data/`, or anything from the archive folder (`../newsletter-archive-data/`).
- **Tests are offline.** Fake HTTP, Gemini, summarizer and re-ranker (`tests/fakes.py`). Real-API tests carry the `api` marker and stay opt-in.

### Ask before any live call

Offline tests and local tools are always fine. Before anything that reaches a real service, stop and get a yes from the user:

- **Fetching from the newsletter site** (`archive discover/fetch/run`). The first live run is always `archive run --limit 5`, never a full crawl.
- **Calling Gemini** (`archive index`, `eval_search.py`, `api` tests, live search checks). Run `archive index --dry-run` first and show the user the estimated cost.
- **Changing the real index:** `--swap`, `--rollback`, or a full `--refresh`.

## Phase 1 - Spec (openspec)

Every change starts as an openspec change, even a one-line fix. A small fix gets a short proposal and a one-PR task list. It still names the requirement it changes.

1. Create the change with `openspec new change <change-id>`. Then write the artifacts one gate at a time, using `openspec instructions <artifact> --change <change-id> --json` for each artifact's template. Don't use `openspec-propose` to write them all in one go; that skips the gates.
   - `proposal.md` (gate 1): Why, What Changes, Capabilities, Impact.
   - Delta specs under `specs/<capability>/` (gate 2): requirements with scenarios.
2. Check existing specs in `openspec/specs/` first. Modify an existing capability rather than inventing a parallel one. If a capability you need exists only in an unarchived change, plan to archive that change first.
3. If something is unclear, ask focused questions. Record the decisions you make with the user in `design.md` under "Decisions (made with the user)".

## Phase 2 - Low-level design

For anything beyond a trivial fix, write `openspec/changes/<change-id>/lld.md`. Model it on `openspec/changes/add-hybrid-search/lld.md`:

- **Decisions:** numbered sections covering architecture, schema/migrations, data flow, config, errors and degraded modes, secrets, and testing approach.
- **Measured facts:** state limits, versions and costs as measured or verified, not assumed. Pin versions.
- **Trade-offs:** where options compete, compare them in a side document (like `retrieval-options.md`), recommend one, and state why.

`design.md` (gate 3) comes before `lld.md` (gate 4). Each is approved on its own before the next is written, and no plan or code is written before gate 4 passes. Mark the approvals in `tasks.md` section 0 ("Approach approved: ...").

## Phase 3 - Implementation plan (`tasks.md`)

Split the work into a stack of small PRs, each one reviewable on its own and leaving `main` working:

- **One section per PR:** `## N. <Area> (PR N)`, with numbered task IDs (`N.1`, `N.2`, ...).
- **Ordering:** foundations first (schema, config, pure functions), then the pieces that use them, then CLI/MCP wiring, then evaluation and acceptance.
- **Size:** aim for a few hundred lines of production code per PR. Split anything bigger.
- **Definition of done:** keep an `## Every PR (definition of done)` section listing CI jobs green, tests shipped with code, coverage ≥ 95% on new modules, `/code-review high` run and fixed, PR template filled, and user merges.

This is gate 5: show the user the PR breakdown (titles plus one line each) and wait for approval before starting PR 1.

## Phase 4 - One PR at a time

For PR N of M:

1. **Branch:** `<change-prefix>/NN-<slug>` (e.g. `hybrid/07-retrieval`). Branch from the PR below it, or from an up-to-date `main` for PR 1. Check `git status` first and never sweep unrelated changes into the commit.
2. **Implement** only the tasks in this PR's section. Note anything you defer under "Out of scope" in the PR body.
3. **Tests ship with the code:**
   - **Unit tests:** in `tests/` or `tests/search/`, offline, using the fakes. Use `hypothesis` for property-style code (parsing, chunking, text).
   - **Integration tests:** code that touches Postgres gets `tests/search/test_<module>_integration.py` with `pytestmark = pytest.mark.db`. These run against the real compose database (`docker compose up -d --wait db`). They're skipped locally when it's down, and CI always runs them (`ARCHIVE_REQUIRE_DB=1`, see `tests/search/conftest.py`). End-to-end CLI tests against a fake site live in `tests/integration/`.
   - **Real-clock tests** get the `slow` marker. Opt-in real-API checks get the `api` marker.
   - **Pragmas:** `pragma: no cover` is allowed only on thin real-API/model adapters.
4. **Local gates** must all pass before pushing:
   ```bash
   source .venv/bin/activate
   pytest -m "slow or not slow"            # db tests run if the compose DB is up
   ruff check . && ruff format --check . && mypy
   python scripts/check_safety.py
   ```
   If you can't start the database, say so in the PR body. CI's `integration` job still has to pass.
5. **Commit and push:**
   - **Message:** title `[N/M] <Area>: <what>`. The body is short plain-language bullets of what changed and why.
   - **Trailer:** end with the session's co-author line.
6. **Open the PR** with `gh pr create` using `.github/pull_request_template.md`. The base is the branch below it, or `main` for PR 1. Fill in every section (see "PR body" below).
7. **Tick `tasks.md`:** change this PR's items to `[x]` in the same PR.

## Phase 5 - Review and CI before handing over

1. **Review:** run `/code-review high` on the PR.
2. **Fix findings** in a separate commit titled `Fix review findings on the <area>` (e.g. "Fix review findings on the indexer"). Its body lists each fix in a few words. If you decline a finding, give the reason in the PR.
3. **Wait for CI:** push and wait for every GitHub Actions job to go green (`gh pr checks <n> --watch`): `test` (3.11 and 3.12), `integration`, `coverage` (combined, branch, ≥ 95%), `lint`, and `safety`. Fix any failure and push again.
4. **Report to the user:** link the PR, the CI result, the coverage line, the review fixes, and anything deferred. **The user merges.** Never merge, force-push `main`, or skip a check.

Then start the next PR. When the user merges a lower PR, rebase the ones above it onto `main`.

When the whole stack is merged, use `openspec-sync-specs` / `openspec-archive-change` to fold the delta specs into `openspec/specs/` and archive the change.

## PR body

Follow the template sections in order:

- **Stack:** every PR in the stack, bottom to top, with PR numbers, and `👉` on this one. Example:
  ```
  - [6/10] Index versions (#21)
  - 👉 [7/10] Retrieval: keyword, vector, fusion (this PR)
  - [8/10] Re-ranker and search service
  ```
  When you open a new PR, update the Stack list on the other open PRs in the stack too.
- **What:** start with the tasks.md IDs covered (e.g. "tasks.md 7.1–7.4"), then short bullets per module. Call out any **changed default** in bold with the reason and the measurement behind it.
- **Live check:** only when the user approved a live run. Show a small table of what was run and what came back.
- **Spec:** `capability / Requirement (scenario)` lines for each requirement implemented.
- **Tests:** the new unit and integration tests, the pytest summary line, and the **coverage line** (combined from CI, plus each new module's percentage, all ≥ 95%).
- **Safety and review checklists:** tick only what is true.
- **Out of scope:** what lands in a later PR.
- **Footer:** end with the session's PR attribution line.

## Writing style

Write PR bodies, commit messages, docs, README sections and error messages the way the README is written: short, plain words, outcome first, no jargon the user didn't use. Error messages tell the user what to do next (for example, "run `archive index`", or "Your cookie looks expired"). When behaviour or setup changes, update the README in the same PR.
