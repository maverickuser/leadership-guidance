## 0. Design (done)

- [x] 0.1 Requirements agreed in conversation (2026-11-10); live check approved (robots.txt, sitemap, one public post)
- [x] 0.2 Proposal approved (2026-11-10; revised the same day for one folder per newsletter)
- [x] 0.3 Specs approved (2026-11-10; revised the same day)
- [x] 0.4 Design approved (2026-11-10)
- [x] 0.5 Low-level design approved (2026-11-10)
- [x] 0.6 PR plan (this file) approved (2026-11-10)

## Every PR (definition of done)

- **CI green:** `test` (unit, Python 3.11 and 3.12), `integration` (compose database), `coverage` (combined, branch coverage, fails under 95%), `lint` (ruff, mypy), `safety`.
- **Tests:** new code ships with its tests; coverage of the PR's new modules ≥ 95%.
- **Ghost unchanged:** the existing Ghost tests pass, with only paths and ids updated where the layout changes (PR 3 onwards).
- **Review:** Claude runs `/code-review high` on the PR and fixes findings in a "Fix review findings on the <area>" commit before handing over.
- **PR template:** filled (Stack, tasks covered, spec scenarios, tests, coverage line, safety checklist).
- **Privacy:** no real newsletter name, address, cookie or member data in the diff; the example id is `main`.
- **Live calls:** any live call (Pragmatic Engineer pages, Gemini, the real index) only with the user's go-ahead.
- **Merging:** the user reviews and merges; `main` requires all CI checks.

## 1. Platform interface (PR 1)

- [x] 1.1 `platforms/__init__.py`: `Platform` protocol, `PLATFORM_NAMES` and a cached `get_platform(name)` registry (lld §4)
- [x] 1.2 `platforms/ghost.py`: wrap today's Ghost discovery, request URL, sign-in check and extraction behind the protocol, unchanged (lld §4: "wraps today's code as is")
- [x] 1.3 `Extracted` gains `categories` (None = keep the manifest's; `paywalled` already existed); `store.build_record` uses them
- [x] 1.4 Discovery, fetch and extract call the platform; every existing test passes unchanged

## 2. Shared rate limiter and per-source HTTP client (PR 2)

- [x] 2.1 `RateLimiter` (delay, clock, sleep) shared by clients; retries count against it, and a request's redirect hops stay within its one slot (post-fetching / Fetch canonical URLs)
- [x] 2.2 `Client` takes a base URL and cookie per instance; the cookie is sent only to that origin
- [x] 2.3 Tests: two clients keep ≥ delay between all requests (fake clock); one client's cookie never reaches the other's host

## 3. Sources config, per-newsletter folders and the move (PR 3)

- [ ] 3.1 Config: `sources` list, `SourceConfig` (paths, `post_id`), every validation rule, reserved ids, lazy cookie check, the old-`base_url` error with a ready `sources:` block (lld §3)
- [ ] 3.2 Defaults: `output_dir` → `../leadership-coach-datasets`; `docker-compose.yml` `ARCHIVE_DB_DATA` default; `config.example.yaml`, `.env.example`
- [ ] 3.3 Storage: post schema v2 (`source`, `id = {id}/{slug}`); `build_record` and every stage read and write `{id}/` paths
- [ ] 3.4 Indexer `load_posts` reads every source's manifest and v2 posts; a v1 file fails with "run `archive migrate`"
- [ ] 3.5 `archive migrate --source <id>` with `--dry-run`: copy, then write and validate, then delete; eval-file id rewrite; idempotent; refuses conflicting state (lld §8)
- [ ] 3.6 Old-layout guard on every command except `migrate` and `mcp`
- [ ] 3.7 README: new data-folder tree and the 7 move steps
- [ ] 3.8 Tests: config errors; paths; migration incl. kill after each phase then rerun, dry run, second run no-op, eval rewrite; guard
- [ ] 3.9 PR body tells the user that after merging, the weekly job refuses to run until the move steps are done, and lists them

## 4. Runs over several newsletters (PR 4)

- [ ] 4.1 `--source` (repeatable) on `discover`, `fetch`, `extract`, `run`; unknown id rejected before any request; `--limit` per source
- [ ] 4.2 Per-source loop; a missing or expired cookie or a discovery failure stops only that source; `Stopped: {name} ({cookie_env}): {reason}` lines; exit 2 at the end; index once
- [ ] 4.3 Expired-cookie message names the newsletter and its `.env` variable
- [ ] 4.4 `scripts/scheduled-run.sh`: notification built from the `Stopped:` lines; new-post count totalled across sources
- [ ] 4.5 End-to-end tests with two fake Ghost sites: both run; `--source`; one expired cookie stops only its source; script notification texts for exit 0, 2 (one and two stopped) and 3

## 5. Substack platform (PR 5)

- [ ] 5.1 Live, with the user's go-ahead: save one Pragmatic Engineer post page with the user's cookie, to confirm the signed-in field and the cookie names, and as fixture material (never committed as is)
- [ ] 5.2 `scripts/make_fixtures.py` Substack mode: filler text, fake `user`/subscriber objects, no emails or uuids; small sitemap fixture with every URL kind
- [ ] 5.3 `platforms/substack.py`: post URLs, `_preloads` decoding, sign-in check, body cleaning, paywall, metadata, section categories (lld §5)
- [ ] 5.4 `check_safety.py`: Substack cookie patterns, generic `*_COOKIE=` rule, Substack fixture checks
- [ ] 5.5 Tests per lld §13 Substack row, incl. `hypothesis` on the string-literal decoding; fake-Substack end-to-end run beside a fake Ghost site
- [ ] 5.6 Check whether `news_sitemap.xml` lists posts missing from `sitemap.xml`; record the answer in the PR

## 6. Search by newsletter (PR 6)

- [ ] 6.1 Migration `002_source.sql`: `source` on `posts` and `passages`, index, BM25 field; older generations stay readable (`NULL AS source`)
- [ ] 6.2 Indexer: `source` on every row; boilerplate detection per source; fingerprint includes the source ids
- [ ] 6.3 `Filters.sources` and `source = ANY(...)`; `SearchRequest.source` validated against the active generation with an error listing valid ids
- [ ] 6.4 Results carry `source` and `source_name`; MCP `search` takes `source`; the tool description lists each newsletter with count, dates and categories; readable lines start with the newsletter name
- [ ] 6.5 `db` tests: filter, unknown source, per-source boilerplate, MCP description, rollback to a generation without `source`

## 7. Every newsletter represented (PR 7)

- [ ] 7.1 `service.py`: swap-in after diversity, before the limit, only without a `source` filter and with more than one source (lld §9.3)
- [ ] 7.2 `search.source_min_score` setting and validation
- [ ] 7.3 Eval: "source coverage" in the report; with the user, add 6 cross-newsletter questions to their local `eval-queries.yaml`
- [ ] 7.4 Live, with the user's go-ahead: run the eval on the real index and set the `source_min_score` default from the lowest expected-result score (rounded down to 0.05); record the measurement in the PR
- [ ] 7.5 `db` tests: both relevant, only one relevant (no weak result forced in), filter disables the rule, not-re-ranked path

## 8. Adding a newsletter and acceptance (PR 8)

- [ ] 8.1 README: adding a newsletter (cookie, `sources` entry, `--limit 5` trial, `index --dry-run` cost, weekly job), publisher signals and the paid-Gemini note; options table with `--source` and `migrate`
- [ ] 8.2 Acceptance, each step with the user's go-ahead:
  - the user's archive moved (`migrate`);
  - `archive index` at ~$0;
  - Pragmatic Engineer trial (`--limit 5`) checked;
  - full load;
  - index cost shown, then indexed;
  - a mixed search returns both newsletters.
- [ ] 8.3 One weekly run (or `scripts/scheduled-run.sh` by hand) updates both newsletters and the index
- [ ] 8.4 After merge: sync the delta specs into `openspec/specs/` and archive this change
