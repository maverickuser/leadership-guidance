## 1. Project setup

- [x] 1.1 Confirm repo location and run `git init`
- [x] 1.2 Create `pyproject.toml` (Python 3.11+, deps, `archive` entry point) and `src/scarlet_archive/` package
- [x] 1.3 Add `config.yaml` with defaults and `.env.example`; `.gitignore` already lists `.env` and `data/`
- [x] 1.4 Implement `config.py` loading `config.yaml` + `.env` into a config object

## 2. Test fixtures from real pages

- [x] 2.1 Inspect the site: Ghost, sitemap layout, category pages and pagination, body selector, login marker
- [x] 2.2 Build fixtures from `data/inspect/`: sitemap index + trimmed posts sitemap, `/categories/` page, a tag page with `rel="next"` and a last page
- [x] 2.3 Build a signed-in paid post fixture and a signed-out version; scrub member name, email, uuid and any tokens
- [x] 2.4 Add a synthetic post fixture with comments, images, captions, share and subscribe boxes to test cleaning

## 3. HTTP layer

- [x] 3.1 Implement `http.Client` with cookie, user agent, 30s timeout and `delay_seconds` spacing
- [x] 3.2 Add retries (2s/4s/8s) on timeout, connection error and 5xx; no retry on 4xx
- [x] 3.3 Tests: retry/backoff and no-retry-on-4xx with a mocked transport and fake sleep

## 4. Stage 1 — Discover

- [x] 4.1 Sitemap parser with nested index support; take posts from `sitemap-posts.xml`
- [x] 4.2 Post URL filter and normalization + slug
- [x] 4.3 Category crawler: `/categories/` → `/t/<slug>` → `/tag/<slug>/` pages via `rel="next"`; name from tag page title
- [x] 4.4 Write/merge `data/manifest.json` without resetting progress
- [x] 4.5 Tests: sitemap (Ghost index + nested), filtering/normalization, category map with pagination, re-discovery merge

## 5. Stage 2 — Fetch

- [x] 5.1 Fetch posts (trailing-slash URL) to `data/raw/{slug}.html`, update manifest status and `last_attempt`
- [x] 5.2 Incremental skip and `--refresh`
- [x] 5.3 Login check: stop when `data-portal="account"` is missing, without caching that page
- [x] 5.4 Missing-cookie check before any request
- [x] 5.5 Tests: incremental skip (no requests on second run), signed-out stop on first post and mid-run

## 6. Stage 3 — Extract

- [x] 6.1 Select `.gh-content`; remove images, figures, captions, paywall CTA, comments, subscribe, share and related blocks
- [x] 6.1a Strip the leading newsletter boilerplate paragraphs ("Welcome to my newsletter!", "Free members can read")
- [x] 6.2 trafilatura XML extraction → sections (`heading`, `level`, `text`) and `text` built from sections
- [x] 6.3 In-body links (absolute URLs)
- [x] 6.4 Metadata from og/article/JSON-LD with `<h1>` fallback; ISO date; summary fallback
- [x] 6.5 `word_count` and `partial` status
- [x] 6.6 Tests: no clutter in output, sections join equals text, metadata fallbacks, partial detection

## 7. Stage 4 — Store

- [x] 7.1 JSON schema for post files (`schema.py`)
- [x] 7.2 Atomic writes for post JSON and manifest
- [x] 7.3 Rewrite post JSON only when `content_hash` changes; update manifest
- [x] 7.4 Tests: schema validation, idempotency (no rewrite of unchanged files)

## 8. CLI, logging and docs

- [x] 8.1 typer app: `discover`, `fetch`, `extract`, `run` with `--limit`, `--refresh`, `--dry-run`, `--verbose`
- [x] 8.2 Run log file, one line per post, end-of-run summary, cookie redaction
- [x] 8.3 Test: cookie value never appears in logs or outputs
- [x] 8.4 Write `README.md` (setup, copying the two `ghost-members-ssr` cookies, commands, output format)
- [x] 8.5 Run full `pytest` with network blocked

## 9. First live run (user-gated)

- [x] 9.1 User fills in `.env`
- [ ] 9.2 Run `archive discover` and review the manifest with the user
- [x] 9.3 Run `archive run --limit 5`; check acceptance criteria against the output (paid post matches browser view)
- [x] 9.4 Run `archive run --limit 5` again and confirm nothing new is downloaded
- [ ] 9.5 Full crawl only on the user's explicit go-ahead
