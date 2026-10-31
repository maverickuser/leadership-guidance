# Implementation plan: add-newsletter-archive

How `lld.md` gets built: the PR stack, the tests in each PR, the review flow, and the guardrails for a public repo.

- **Repo:** https://github.com/maverickuser/leadership-guidance (public)
- **Package / CLI:** `newsletter_archive` / `archive` (unchanged from the specs)
- **Sources:** `proposal.md`, `design.md`, `lld.md`, `specs/`, `tasks.md`

---

## 1. Ground rules for a public repo

The code is public; the archive is not. These rules hold for every PR.

| Never committed | How it's enforced |
|---|---|
| `.env`, the cookie value, any `ghost-members-ssr=` value | `.gitignore`; CI `safety` job greps the tree; `test_secrets.py` |
| `data/` (raw HTML, post JSON, manifest, logs, `data/inspect/`) | `.gitignore`; CI fails if any tracked path starts with `data/` |
| Real article text from Example Newsletter (paid or free) | Fixtures are **structure-only**: real Ghost markup, with article text replaced by deterministic filler (see §4) |
| Member name, email, uuid, tokens from signed-in pages | Scrub step in `scripts/make_fixtures.py`; CI greps fixtures for `@` emails and uuid patterns |

Post titles, slugs, URLs, category names and meta descriptions are public on the site and may appear in fixtures.

Commits use the GitHub noreply email so the personal email doesn't show up in public history.

## 2. Prerequisites (one time)

1. Python 3.11 or newer. The machine has 3.9.6. Run `brew install python@3.12`, then `python3.12 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'`.
2. Repo-local git identity: `maverickuser` / `<id>+maverickuser@users.noreply.github.com`.
3. Repo settings:
   - squash merge only
   - "Automatically delete head branches" on
   - `main` protected: PR required, CI checks `test` and `safety` required, no force push
4. Not needed: Graphite, spr, or any other stack tool. Plain git ≥ 2.38 (`--update-refs`) plus `gh` is enough.

## 3. The PR stack

Each PR targets the one below it. PR 1 targets `main`. Branches are named `stack/NN-name`.

```
main  ← bootstrap commit (specs, LLD, this plan, .gitignore, PR template)
 └─ stack/01-scaffold
     └─ stack/02-fixtures
         └─ stack/03-http
             └─ stack/04-storage-io
                 └─ stack/05-discover
                     └─ stack/06-fetch
                         └─ stack/07-extract
                             └─ stack/08-store-records
                                 └─ stack/09-cli-logging
                                     └─ stack/10-integration-readme
```

Target size: under about 400 changed lines of non-fixture code per PR. Every PR is green on its own: tests pass at each layer, not only at the top.

### PR 1: `stack/01-scaffold`, project skeleton and CI
- **Tasks:** 1.2, 1.3, 1.4
- **Files:**
  - `pyproject.toml` (hatchling, deps incl. `jsonschema`, `[dev]` = pytest)
  - `src/newsletter_archive/{__init__,config,errors,models}.py`
  - `config.yaml`, `.env.example`
  - `tests/conftest.py`: network block that allows loopback only, plus a `cfg` fixture
  - `.github/workflows/ci.yml`
- **CI:**
  - `test` job runs pytest on Python 3.11 and 3.12.
  - `safety` job greps tracked files for `ghost-members-ssr=` followed by a value, fails on any tracked `data/` or `.env` path, and greps fixtures for emails and uuids. Allowlist: the fake test values `TESTSECRET123` and `SIGSECRET456` used in `conftest.py` and the docs.
- **Unit tests (`test_config.py`):**
  - defaults load
  - yaml override
  - unknown key rejected
  - `delay_seconds < 2` rejected
  - `max_retries > 3` rejected
  - cookie read from env first, then `.env`
  - leading `Cookie:` stripped
  - `repr(Config)` and `str(Secret)` hide the value
  - empty cookie is falsy
- **Done when:** `pip install -e .` gives a working `archive --help` stub, and CI is green.

### PR 2: `stack/02-fixtures`, structure-only fixtures
- **Tasks:** 2.2, 2.3, 2.4
- **Files:** `scripts/make_fixtures.py` (run locally against `data/inspect/`; the output is committed, the input never is) and `tests/fixtures/*` (list in `lld.md` §13)
- **Unit tests (`test_fixtures.py`):**
  - every fixture parses
  - signed-in fixture has `data-portal="account"`; signed-out fixture doesn't
  - tag page 1 has `rel="next"`; page 2 doesn't
  - no email or uuid patterns
  - filler text has no 8-word run matching the source (checked locally by the script, not in CI)
- **Done when:** the fixtures are reviewed and contain no article prose.

### PR 3: `stack/03-http`, polite HTTP client
- **Tasks:** 3.1, 3.2, 3.3
- **Files:** `http.py` (`Client`, `Transport`, `RequestsTransport`, `Response`)
- **Unit tests (`test_http.py`, `FakeTransport` + fake clock/sleep):**
  - 503, 503, 200 → sleeps `[2, 4]` and returns 200
  - four ConnectionErrors → `FetchError(attempts=4)`
  - timeout counts as retryable
  - 404 → one call, no sleep
  - consecutive calls spaced ≥ `delay_seconds` from the end of the previous call
  - same-host 301 followed in one slot
  - cross-host redirect → `FetchError`, and the second host never receives a request
  - `Cookie` header only for the base host (host plus port)
  - `User-Agent` always sent
  - debug log line never includes headers

### PR 4: `stack/04-storage-io`, atomic writes and manifest I/O
- **Tasks:** 7.2, plus the manifest part of 4.4
- **Files:** `store.py` (`write_json_atomic`, `write_text_atomic`, `load_manifest`, `save_manifest`)
- **Unit tests (`test_store_io.py`):**
  - round-trip of a manifest
  - missing manifest → empty
  - order preserved
  - `os.replace` patched to raise → old file intact and no partial target
  - leftover `.tmp` doesn't break the next write
  - UTF-8 non-ASCII preserved

### PR 5: `stack/05-discover`, Stage 1
- **Tasks:** 4.1 through 4.5
- **Files:** `discover.py`
- **Unit tests (`test_discover.py`):**
  - Ghost index → only `sitemap-posts.xml` followed
  - nested index (index → index → urlset)
  - flat mixed sitemap → post only
  - `normalize_url` table test covering utm, ref, fragment, trailing slash, host case, other query kept
  - slug derivation and collision suffix
  - categories page → 9 `/t/` links, `newsletter` ignored
  - tag crawl follows `rel=next`, stops on no-next and on no-new-posts, and has a page cap
  - category name from `<title>`
  - post in two categories → sorted names
  - uncategorized → `[]`
  - failed category → warning, others still mapped
  - re-discover keeps `ok` status and refreshes categories
  - dry-run writes nothing

### PR 6: `stack/06-fetch`, Stage 2
- **Tasks:** 5.1 through 5.5
- **Files:** `fetch.py`
- **Unit tests (`test_fetch.py`):**
  - request URL has a trailing slash
  - raw saved, status `fetched`, `last_attempt` set
  - second run → zero requests
  - `--refresh` → refetch
  - limit applied before skip (`--limit 5` twice → second run makes zero requests)
  - signed-out first post → `NotSignedInError`, no raw file, entry untouched
  - signed out at post 3 of 5 → posts 1–2 kept, post 3 still `discovered`
  - missing cookie → error before any request, including in dry-run
  - 404 → `failed` and the run continues
  - manifest saved after each post
  - dry-run → no requests and no writes

### PR 7: `stack/07-extract`, Stage 3
- **Tasks:** 6.1, 6.1a, 6.2 through 6.6
- **Files:** `extract.py`
- **Unit tests (`test_extract.py`):**
  - clutter fixture has no nav, share, subscribe, comment, caption or alt text
  - boilerplate intro stripped (its link gone too); post-specific italic opener kept
  - intro, h2 and h3 → expected sections
  - empty sections dropped
  - `"\n\n".join(sections) == text`, as a property check across all fixtures
  - lists, quotes and tables render per the LLD table
  - trafilatura fallback triggers when its output is patched to drop headings
  - links only from the body, absolute, deduped, `mailto:` skipped
  - metadata from og and JSON-LD; `<h1>` fallback; summary fallback; ISO date to UTC date
  - missing date → `ExtractError`
  - `word_count` counted after stripping; signed-out fixture → below 300

### PR 8: `stack/08-store-records`, Stage 4
- **Tasks:** 7.1, 7.3, 7.4
- **Files:** `schema.py`, plus `build_record` and `run_extract` in `store.py`
- **Unit tests (`test_store.py`):**
  - record validates against the schema
  - extra or missing field fails
  - `content_hash == sha256(text)`
  - `fetched_at` = raw file mtime in UTC
  - unchanged rerun → post-file mtime unchanged
  - changed text → rewritten, manifest hash updated
  - changed categories only → rewritten
  - `--refresh` → rewritten
  - extract error → entry `failed`, run continues
  - no `Client` is reachable from `run_extract`, verified with a network-blocked test

### PR 9: `stack/09-cli-logging`, CLI, logging and summary
- **Tasks:** 8.1, 8.2, 8.3
- **Files:** `cli.py`, `logging.py`
- **Unit tests:**
  - `test_cli.py` (`CliRunner`): each subcommand wires the right stage; `run` order is discover → fetch → extract with one shared `Client`; exit codes 0, 1, 2 and 130; summary line format; `fetch --dry-run` leaves `raw/` and `posts/` empty
  - `test_logging.py`: `RedactFilter` masks the exact value, each cookie pair and `Cookie:`/`Set-Cookie:` lines; the log file is created under `logs/`; one line per post
  - `test_secrets.py`: full `run --verbose` on fakes, then grep the console output and every file under `data/` → no secret

### PR 10: `stack/10-integration-readme`, integration tests and README
- **Tasks:** 8.4, 8.5
- **Files:** `tests/integration/`, `README.md`
- **Integration tests** (see §5): end-to-end over a local HTTP server with the real `RequestsTransport`.
- **README:** setup (Python 3.12 via brew, venv), copying the Cookie header (DevTools → Network → request headers), commands and flags, output format, what never to commit.
- **Done when:** `pytest` (unit and integration) passes with the network blocked, both locally and in CI.

### After the stack: live run (user-gated, no PR unless fixes are needed)
- **Tasks:** 9.2 through 9.5. Run `archive discover`, review the manifest, then run `archive run --limit 5` twice.
- Compare one paid post against the browser view.
- Any fixes go in `stack/11-live-fixes`, on top of `main` once the stack is merged.
- **No full crawl** without an explicit go-ahead.

## 4. Fixture strategy (structure-only)

`scripts/make_fixtures.py` reads `data/inspect/*.html` and writes `tests/fixtures/`. It:
1. Keeps `<head>` meta tags (og, article, description, JSON-LD) and the `<h1>`, excerpt and byline markup.
2. Keeps the element tree of `.gh-content`: the same tags, classes and heading count/levels.
3. Replaces every text node inside `.gh-content` with seeded filler words, using the same word count per node. This preserves word counts, so the "about 2.7k words → ok" check still holds. Exceptions:
   - The two boilerplate paragraphs keep their real opening phrases ("Welcome to my newsletter!", "Free members can read"). Those are the patterns under test, and they appear on every free page anyway.
   - Heading text is kept. Headings are short and public on free previews.
4. Removes member data from signed-in pages: name, email, uuid, `data-key`, any Portal or Stripe tokens.
5. Strips scripts except JSON-LD, inline CSS other than the `gh-members-styles` block, and nav/footer content past what tests need.

Synthetic fixtures (`post-clutter.html`, nested sitemap, flat mixed sitemap, meta-fallback) are hand-written.

## 5. Test plan

### Layers

| Layer | Where | Runs | Network |
|---|---|---|---|
| Unit | `tests/test_*.py` | every PR, CI | blocked (loopback allowed) |
| Integration | `tests/integration/test_*.py` | every PR from PR 10 on, CI | loopback only |
| Live smoke | `archive run --limit 5` by hand | once, user-gated | real site |

The network block lives in `conftest.py`: `socket.socket.connect` raises unless the address is `127.0.0.1` or `::1`.

### Integration harness (`tests/integration/conftest.py`)
- `FixtureSite`: a `ThreadingHTTPServer` on `127.0.0.1:<random port>` serving `tests/fixtures/` as a fake Ghost site:
  - `/sitemap.xml`, `/sitemap-posts.xml`, `/categories/`
  - `/t/<slug>` → 301 → `/tag/<slug>/`, and `/tag/<slug>/page/2/`
  - `/<slug>/` → signed-in post if the request's `Cookie` matches the test cookie, signed-out post otherwise
  - Switches for the test: `flaky(slug, [503, 503])`, `expire_after(n)`, `redirect_offsite(slug)` (redirects to a second server on another port), and a request log that records headers.
- `Config` points `base_url` at the server and `output_dir` at `tmp_path`. `Client` gets the real `RequestsTransport` and a fake `sleep`/`clock`, so runs are instant while the spacing logic still runs.
- The CLI runs in-process through `CliRunner`.

### Integration scenarios

| # | Scenario | Checks |
|---|---|---|
| I1 | `archive run --limit 5` on a fresh dir | manifest has all fixture posts; 5 raw, 5 posts, 1 log; every post validates; categories correct, including the paginated one; summary counts |
| I2 | run again | zero post requests in the server log; post-file mtimes unchanged |
| I3 | `archive run` with `expire_after(2)` | exit 2, expired-cookie message; posts 1–2 kept, post 3 `discovered` and no raw file |
| I4 | flaky post (503, 503, 200) | fetched; recorded sleeps include 2 and 4 |
| I5 | 404 post | `failed` with `HTTP 404`; others succeed; exit 0 |
| I6 | offsite redirect | that post `failed`; offsite server log empty, so the cookie was never sent |
| I7 | `extract` after the server stops | still works from `raw/`, so it is offline |
| I8 | `--verbose` full run | cookie value absent from console, logs and every file in `data/` |
| I9 | Spacing | with the real clock and `delay_seconds=2` on 3 requests, gaps are ≥ 2s; marked `slow` and run in CI only |
| I10 | `fetch --dry-run` | no post requests; `raw/` empty |

### Coverage target
Aim for 90% or more line coverage on `src/newsletter_archive/`, reported in CI (`pytest --cov`, with `pytest-cov` in `[dev]`). This is not a hard gate.

## 6. PR workflow and review

### Opening the stack
```bash
git switch main && git switch -c stack/01-scaffold
# ...work, commit...
git push -u origin stack/01-scaffold
gh pr create --base main --title "[1/10] Scaffold: project skeleton, config, CI" --body-file ...
git switch -c stack/02-fixtures          # branches off 01
gh pr create --base stack/01-scaffold --title "[2/10] Structure-only test fixtures" ...
```

Each PR body follows `.github/pull_request_template.md`:
- **Stack:** a list of all 10 PRs with links, this one marked
- **What:** a summary and the `tasks.md` IDs it covers
- **Spec:** which requirements and scenarios it implements
- **Tests:** new tests, plus the `pytest` output summary
- **Safety checklist:** no secrets, no `data/`, fixtures are structure-only
- **Out of scope:** what lands in a later PR

### Review steps (every PR)
1. **Self-check (Claude):** `pytest` green locally, then `/code-review` on the PR diff, then fix the findings in that branch.
2. **CI:** `test` and `safety` must pass. Branch protection enforces this on PR 1 into `main`. For higher PRs, CI runs on `pull_request` against any base.
3. **User review on GitHub:** comment on lines, or ask for changes in chat. GitHub doesn't allow approving your own PR, so a "LGTM" in chat or a comment counts as approval.
4. **Address feedback** with new commits on that branch, then restack the branches above:
   ```bash
   git switch stack/10-integration-readme
   git rebase --update-refs stack/0N-<fixed-branch>   # moves every branch in between
   git push --force-with-lease origin $(git for-each-ref --format='%(refname:short)' refs/heads/stack/)
   ```

### Merging (bottom-up)
1. Squash-merge PR 1 into `main` (`gh pr merge N --squash`). GitHub deletes `stack/01-scaffold` and retargets PR 2's base to `main`.
2. Restack the rest onto the new `main`:
   ```bash
   git fetch origin
   git switch stack/10-integration-readme
   git rebase --update-refs --onto origin/main stack/01-scaffold
   git push --force-with-lease origin stack/02-fixtures ... stack/10-integration-readme
   ```
3. Wait for CI on PR 2, then merge. Repeat up the stack.

The PRs can be reviewed in any order, or all at once. Merging always goes bottom-up.

## 7. Definition of done (whole change)

- All 10 PRs merged and `main` CI green.
- Every scenario in `specs/` has at least one test that names it (`# spec: post-fetching / Expired cookie on first post`).
- `tasks.md` 1.x–8.x checked.
- The live run (9.2–9.4) is done and reviewed with the user. The full crawl (9.5) runs only on the user's go-ahead.
- Then `/opsx:archive` the change.
