# Low-Level Design: Several Newsletter Sources (v2)

The decisions and the reasons for them are in `design.md` (D1–D9); the behaviour is defined by the specs. This document says how each part changes. The example id for the existing newsletter is `main`; the user's local config sets the real id.

## 1. Decisions

| # | Decision | Design |
|---|---|---|
| 1 | `config.yaml` `sources:` list; a top-level `base_url` is refused with the entry to paste in | D1 |
| 2 | `SourceConfig` owns every path and the post id `{id}/{slug}` | D2 |
| 3 | `{output_dir}/{id}/{manifest.json, raw/, posts/}`; shared `postgres/`, `models/`, `logs/`, eval files at the top; default `output_dir` `../leadership-coach-datasets` | D1 |
| 4 | `archive migrate --source <id>`: copy, then validate, then delete; idempotent; other commands refuse the old layout | D2a |
| 5 | `platforms/` package: `Platform` protocol, `ghost.py` (existing code moved), `substack.py` | D3 |
| 6 | One `RateLimiter` shared by per-source `Client`s; each cookie goes only to its own origin | D4 |
| 7 | Sources run in config order; a stopped source is reported, others continue; exit 2 at the end | D5 |
| 8 | Substack sign-in, paywall and section read from `window._preloads` | D6 |
| 9 | "Every source represented": a swap-in after diversity, gated by `search.source_min_score` | D7 |
| 10 | Generation schema gains `source`; a fingerprint change triggers a rebuild; boilerplate detection per source | D8 |
| 11 | MCP lists the sources and takes `source`; eval gains cross-newsletter questions | D9 |

## 2. Architecture

```
config.yaml ─► Config ─► sources: [SourceConfig(main, ghost), SourceConfig(pragmatic-engineer, substack)]

archive run
  for source in selected sources (config order):
     discover ─► fetch ─► extract          paths from SourceConfig, rules from PLATFORMS[source.platform]
        │           │
        └─ Client(source) ──► shared RateLimiter (one request in flight, ≥ delay_seconds apart)
     on MissingCookie / NotSignedIn / DiscoveryError: record and go on to the next source
  index (once, over every source's posts) ─► new generation if the fingerprint changed
  exit 2 if any source stopped, 3 if index failed, else 0

archive mcp ─► search(query, source?, category?, since?, until?, kind?, limit?)
     BM25 + vector ─► RRF ─► re-rank ─► per-post diversity ─► every-source swap-in ─► limit
```

## 3. Configuration (`config.py`)

```yaml
output_dir: ../leadership-coach-datasets       # new default
sources:
  - id: main                                   # [a-z0-9][a-z0-9-]*; not postgres|models|logs
    name: "Main newsletter"                    # optional, defaults to the id
    platform: ghost                            # ghost | substack
    base_url: https://www.example.com
    cookie_env: NEWSLETTER_COOKIE
    boilerplate_prefixes: []                   # optional
  - id: pragmatic-engineer
    name: "The Pragmatic Engineer"
    platform: substack
    base_url: https://newsletter.pragmaticengineer.com
    cookie_env: PRAGMATIC_ENGINEER_COOKIE
search:
  source_min_score: <measured, §9.3>
```

- **`SourceConfig`:** a frozen dataclass with `id`, `name`, `platform`, `base_url`, `cookie_env`, `cookie: Secret` and `boilerplate_prefixes`, plus `root = output_dir / id`.
  - **Properties:** `manifest_path`, `raw_dir` and `posts_dir`.
  - **Methods:** `raw_path(slug)`, `post_path(slug)` and `post_id(slug) -> f"{id}/{slug}"`.
- **`Config`:**
  - **Removes:** `base_url`, `cookie`, `manifest_path`, `raw_dir`, `posts_dir`, `raw_path`, `post_path` and `boilerplate_prefixes`.
  - **Adds:** `sources: tuple[SourceConfig, ...]`, `source(id)` and `select(ids)` (unknown id → `ConfigError` listing the valid ids).
  - **Unchanged:** `logs_dir`, `lock_path` and the model and eval paths stay on `output_dir`.
  - **`secrets`:** every source cookie, plus the Gemini key and database passwords.
- **Validation** (`ConfigError`, exit 1):
  - **Sources:** an empty or missing `sources`; a bad id pattern, a reserved id or a duplicate id; an unknown platform; a missing `base_url` or `cookie_env`; a `cookie_env` that isn't `^[A-Z][A-Z0-9_]*$`; two sources with the same `base_url` origin.
  - **Top-level `base_url`:** the message prints a ready `sources:` block built from the old values (`id: <choose>`, `platform: ghost`, `cookie_env: NEWSLETTER_COOKIE`, plus the old `boilerplate_prefixes`).
- **Cookies** are read for every source at load time but checked only by `fetch` for that source, so `extract`, `index`, `mcp` and `migrate` work without cookies.
- **`config.example.yaml`:** one commented Ghost and one Substack example, with placeholder hosts for the Ghost one.
- **`.env.example`:** gains `PRAGMATIC_ENGINEER_COOKIE=`.
- **`docker-compose.yml`:** the `ARCHIVE_DB_DATA` default becomes `../leadership-coach-datasets/postgres`.

## 4. Platform interface (`platforms/__init__.py`)

```python
class Platform(Protocol):
    name: str
    def post_urls(self, sitemap_urls: list[str], base_url: str) -> list[str]: ...
    def discover_categories(self, client: Client, base_url: str) -> CategoryCrawl | None: ...  # None: from pages
    def request_url(self, url: str, base_url: str) -> str: ...
    def signed_in(self, html: str) -> bool: ...
    def extract(self, html: str, page_url: str, prefixes: tuple[str, ...], min_words: int) -> Extracted: ...
PLATFORMS = {"ghost": GhostPlatform(), "substack": SubstackPlatform()}
```

- **`Extracted`** gains `categories: list[str] | None` (`None` means keep the manifest's) and `paywalled: bool`. `store.build_record` takes the `SourceConfig`, sets `id`, `source` and `schema_version: 2`, and sets `status` from `paywalled` or the word count.
- **Shared, unchanged:** `collect_sitemap_urls`, `normalize_url`, `merge_manifest`, `build_sections`, `extract_links`, the meta/JSON-LD helpers, `to_iso_date` and `first_sentences`.
- **`ghost.py`** wraps today's code as is: `BODY_SELECTORS` and the remove list, `.gh-post-upgrade-cta`, `SIGNED_IN_MARKER`, the trailing-slash `fetch_url`, `is_post_url` and `crawl_categories`.

## 5. Substack platform (`platforms/substack.py`)

| Step | Rule |
|---|---|
| `post_urls` | same site as `base_url`, normalized path matching `^/p/[^/]+$`; drops `/archive`, `/about`, `/podcast`, `/p/*/comment/*`, off-site |
| `discover_categories` | `None`; no extra requests |
| `request_url` | the manifest URL unchanged (measured: 200, no redirect) |
| `_preloads` | **Measured 2026-11-11:** the data is plain JSON in `<script type="application/json" id="preloads-json">`; `window._preloads = JSON.parse(document.querySelector(…))` only reads it. Read that script first; fall back to the older `window._preloads = JSON.parse("…")` string literal (JSON-decode the literal, then the JSON). Missing or broken data → `ExtractError("no Substack page data")`: that post fails, the run continues |
| `signed_in` | `_preloads.user` is an object with an `id`. **Confirmed 2026-11-11:** signed in, `user` is an object; signed out, `user` is null. The session cookie on the custom domain is `connect.sid` |
| body | `div.available-content div.body.markup`; remove subscribe/share widgets, button rows, image and gallery containers incl. captions, embeds (video, tweet, post), the paywall block, footer and comments. The selector list is finalised from the saved page, and each removed class is asserted by a fixture test |
| `paywalled` | `div.paywall` is present (measured: after `div.available-content` when signed out; the signed-in page has only a `div.paywall-jump` anchor, which is not a paywall), or `post.audience == "only_paid"` with a body under `min_word_count` |
| metadata | title `og:title` → JSON-LD `headline` → `h1.post-title`; subtitle `h3.subtitle` else `null`; author JSON-LD → `meta[name=author]`; date JSON-LD `datePublished` → `post.post_date` |
| categories | `post.section_id` → the matching section's `name` → `[name]`; none → `[]`; navigation sections ignored |

Fixtures: one page saved with the user's cookie (with their go-ahead) is reduced with `scripts/make_fixtures.py`, extended for Substack. The extension replaces text with filler, replaces `user`/subscriber objects in `_preloads` with fake values, and strips emails and uuids. The 586-URL sitemap is reduced to a small fixture with every URL kind.

## 6. Fetching and HTTP (`http.py`, `fetch.py`)

- **`RateLimiter`:** holds `delay`, `clock`, `sleep` and `last_end`. `Client(source, settings, limiter, transport)` replaces `Client(config)`. Retries and redirect hops of every client count against the one limiter.
- **Cookie origin:** the cookie is sent only to `origin(source.base_url)`, as today.
- **`run_fetch(source, client, ...)`:** today's logic, with `platform.request_url` and `platform.signed_in`.
- **Errors:** `MissingCookieError` and `NotSignedInError` carry `source.id`, `source.name` and `cookie_env`. The message is "Your {name} cookie looks expired. Copy a fresh Cookie header into .env ({cookie_env})."

## 7. CLI (`cli.py`)

- **`--source`:** `discover`, `fetch`, `extract` and `run` take `--source` (multiple), resolved by `config.select` before any work.
- **Per-source loop:** `for source in selected:` runs today's stage calls, with `--limit` per source. A source-level stop error is caught and recorded as `(source, reason)`; the loop continues.
- **Index once:** `run` builds the index once after all sources.
- **Exit codes:** 2 if any source stopped (today's expired-cookie code), 3 if indexing failed, otherwise 0.
- **Summary lines**, parsed by `scheduled-run.sh`:
  - `Summary: fetched=N ...` totals across sources (unchanged format);
  - one `Stopped: {name} ({cookie_env}): {reason}` line per stopped source.
- **Old-layout guard:** every command except `migrate` and `mcp` exits 1 when `{output_dir}/manifest.json` exists: "Run `archive migrate --source <id>` first (see README)".
- **`migrate`:** new command (section 8).
- **`run_lock`:** stays one lock on `output_dir`.

## 8. Migration (`migrate.py`)

1. **Refuse:** an unknown `--source`, or `{id}/manifest.json` existing alongside a top-level `manifest.json` with different entries.
2. **Nothing to do:** no top-level `manifest.json` → print "Nothing to move" and exit 0 (idempotent).
3. **Plan:**
   - `manifest.json` → `{id}/manifest.json`
   - `raw/*.html` → `{id}/raw/`
   - `posts/*.json` (v1) → `{id}/posts/*.json` (v2: `schema_version: 2`, `source`, `id: {id}/{slug}`)
   - in `eval-queries.yaml` (`expected`) and `eval-judgments.yaml` (post keys), every id `x` → `{id}/x`, skipping ids that already contain `/`
4. **`--dry-run`:** print the counts per kind and the first 3 paths of each, then stop.
5. **Write phase (under the run lock):**
   - **Copy:** raw files are copied with `shutil.copy2`.
   - **Post files and manifest:** written atomically, each post validated against the v2 schema.
   - **Eval files:** written atomically to new temp names, then renamed over the originals.
   - **Old files** stay untouched in this phase.
6. **Commit phase:** delete the old `posts/*.json` and `raw/*.html`, then the empty `posts/` and `raw/` folders, then the top-level `manifest.json` last.
7. **Interrupted runs:** a kill at any point leaves the top-level `manifest.json`, so a rerun repeats the remaining steps. Copies and writes are idempotent, and existing identical targets are skipped.
8. **Finish:** print "Moved N posts into {id}/. Next: start the database and run `archive index --dry-run`, then `archive index`."

## 9. Search (`search/`)

### 9.1 Store
- **Migration `002_source.sql`:** redefines `create_generation(n, dim)` so that:
  - `posts` and `passages` gain `source text NOT NULL`;
  - there's an index `ON passages (source)`;
  - `source` is added to the BM25 index's fields, so keyword search can filter by it (syntax verified against the pinned pg_search, as in hybrid-search LLD 6.4).
- **Existing generations** (changed in the search PR): the migration also adds `source` to every existing generation, fills it from the `<source>/` prefix of each post id, and rebuilds its BM25 index with `source`. The live generation keeps taking weekly updates and rollback keeps working, with one write path. Rows from before `archive migrate` (slug-only ids) keep `source` NULL, are searchable with no newsletter name, and are replaced by the first index after the move.
- **Literal field (measured on paradedb 0.26.2):** `source` is indexed as `(source::pdb.literal)`, so `source = ANY(...)` is an exact match applied inside the BM25 search.

### 9.2 Indexer
- **`load_posts`:** iterates `config.sources`, loads `source.manifest_path`, reads `source.post_path(slug)` for eligible entries, and accepts only schema version 2. A v1 file is a load failure with "run `archive migrate`".
- **`Post`:** gains `source`.
- **Boilerplate:** `find_boilerplate` runs per source; the ≥ 5-posts rule applies within each source.
- **Fingerprint:** gains the sorted source ids, so the first index after the move builds a new generation. The caches are content-keyed, so the dry run shows ~0 Gemini cost for the existing posts.

### 9.3 Every source represented (`service.py`)
After re-rank and per-post diversity, before the limit. Applied only when the request has no `source` filter and the active generation has more than one source.

```
ranked = diversified results (best first)
top = ranked[:limit]
for src in sources_in_generation - {r.source for r in top}:
    best = first r in ranked[limit:] with r.source == src
    if best and qualifies(best):
        victim = last r in top whose source has > 1 result in top
        if victim: replace victim with best
top.sort(by original rank)
qualifies(r) = r.rerank_score >= source_min_score          (re-ranked searches only)
```

- **Changed in the PR (review):** without re-ranker scores the rule is off. A fused rank says nothing about relevance, so "among the first `candidates`" let weak keyword matches in. When re-ranking, each missing newsletter's best fused candidate is scored too, even below the `rerank_top` cut, and is kept only if it qualifies.
- **Measuring `source_min_score`:** run the eval with `search.source_min_score: 0`, so no expected result is held back by the current value. Only the cross-newsletter questions count.
- **Measured 2026-11-11 (837 posts, 36 questions, 6 cross-newsletter):**

  | `source_min_score` | source coverage | recall@10 (returned) |
  |---|---|---|
  | 1.0 (rule off) | 0.50 | 0.70 |
  | 0.95 (the "lowest expected score" rule) | 0.67 | 0.71 |
  | **0.5 (default kept)** | **0.83** | 0.71 |
  | 0 (anything qualifies) | 1.00 | 0.71 |

  The Qwen3 re-ranker's scores are nearly binary (relevant passages ≥ 0.95, the rest far lower), so "the lowest expected-result score" lands at 0.95 and gives up coverage. 0.5 keeps the meaning "the re-ranker judges it relevant", gains most of the coverage, and never forces a weak result in (0 would). Recall never drops.

- **Result content:** each result keeps its own score and rank. A swapped-in result shows its true (lower) score, so nothing is disguised.
- **`source_min_score` default:** measured in the search PR. It's the lowest re-ranker score among expected results across the eval questions (including the new cross-newsletter ones), rounded down to 0.05, and recorded with the measurement in `config.py` and the PR.

### 9.4 Filters, results, MCP, eval
- **`Filters`:** gains `sources`; `filter_sql` adds `source = ANY(%s)`. `SearchRequest` accepts `source` as a string or list, checked against the active generation's `all_sources()`, with an error listing the valid ids.
- **Results:** gain `source` and `source_name` (from config, falling back to the id).
- **MCP:**
  - **Tool description:** one line per source: "{name} ({id}): N posts, first–last date; categories: …".
  - **`search`:** gains `source`.
  - **Readable output:** each result's meta line starts with `{name}`.
- **Eval:**
  - **Ids:** `evaluate.py` is unchanged (ids are opaque strings), and the migration rewrites the YAML.
  - **New questions:** 6 cross-newsletter questions are added to the user's local `eval-queries.yaml`, written with the user (private, not committed). They have expected posts in both newsletters.
  - **Report:** the eval report gains "source coverage", the share of those questions where every expected source appears in the top 10.

## 10. Weekly script (`scripts/scheduled-run.sh`)

- **Exit 2:** the notification text is built from the `Stopped:` lines, e.g. "The Pragmatic Engineer: your cookie looks expired. Paste a fresh Cookie header into .env (PRAGMATIC_ENGINEER_COOKIE)." With several stopped sources, it lists them, followed by "Others updated."
- **Unchanged:** the fetched count comes from the `Summary:` line (now totalled across sources). Codes 0, 3 and other exits keep their behaviour.
- **README:** the launchd example paths become `/path/to/leadership-coach-datasets/logs/scheduled.log`.

## 11. Safety (`scripts/check_safety.py`)

- **Cookie patterns:** Substack session cookies (`substack.sid=`, `substack.lli=`, and any others on the saved page) are added to the cookie-value patterns, plus a generic `[A-Z_]+_COOKIE=<literal>` rule, with placeholders allowed as today.
- **Fixture checks:** also scan Substack fixtures for emails, uuids and `"user":{"id":<non-fake>`.

## 12. README

- **Data folder:** the new layout tree.
- **Moving an existing archive:** the 7 steps from the `newsletter-sources` Setup documentation requirement.
- **Adding a newsletter:** cookie, `sources` entry, `--limit 5` trial, `index --dry-run` cost, then the weekly job.
- **Publisher signals:** check `robots.txt` content signals, and use a paid Gemini plan when a publisher says `ai-train=no`.
- **Options table:** gains `--source`, plus `migrate`.

## 13. Testing

| Area | Tests |
|---|---|
| Config | `sources` parse; every validation error; old `base_url` message contains a valid `sources:` block; reserved ids; lazy cookie check |
| Paths | `SourceConfig` paths and `post_id`; old-layout guard on every command except `migrate`/`mcp` |
| HTTP | shared limiter keeps ≥ delay across two clients (fake clock); cookie only to its own origin; retries and redirects counted |
| Ghost | existing discover/fetch/extract tests moved behind the platform; only paths and ids in expectations change |
| Substack | sitemap filter; `_preloads` decode incl. broken and missing data (`hypothesis` on the string-literal decoding); signed-in true/false; body cleaning per removed class; metadata fallbacks; section → categories incl. navigation-only sections and none; paywalled vs full |
| Migration | full move of a small fake archive; dry run changes nothing; kill after each phase then rerun; second run no-op; eval YAML rewrite; conflicting state refused; v2 files validate |
| CLI end to end (`tests/integration/`) | fake Ghost and fake Substack sites: `archive run` over both; `--source`; one expired cookie stops only its source, exit 2, `Stopped:` line; summary totals |
| Weekly script | fake `archive` output → notification text for 0 / 2 (one and two stopped) / 3 |
| Search (`db`) | generation with `source`; source filter; unknown source error; per-source boilerplate; swap-in: both relevant, one relevant (no forced weak result), filter disables it, not re-ranked path; MCP description lists sources; rollback to a generation without `source` works |
| Live (opt-in, user's go-ahead) | one cookie-authenticated Pragmatic Engineer page saved for fixtures and the sign-in field; then `archive run --source pragmatic-engineer --limit 5` |

Combined coverage stays ≥ 95% with branch coverage, and new modules are ≥ 95%.
