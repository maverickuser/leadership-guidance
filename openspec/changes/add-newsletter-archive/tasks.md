## 1. Project setup

- [x] 1.1 Confirm repo location and run `git init`
- [ ] 1.2 Create `pyproject.toml` (Python 3.11+, deps, `archive` entry point) and `src/newsletter_archive/` package
- [ ] 1.3 Add `config.yaml` with defaults and `.env.example`; `.gitignore` already lists `.env` and `data/`
- [ ] 1.4 Implement `config.py` loading `config.yaml` + `.env` into a config object

## 2. Test fixtures from real pages

- [x] 2.1 Inspect the site: Ghost, sitemap layout, category pages and pagination, body selector, login marker
- [ ] 2.2 Build fixtures from `data/inspect/`: sitemap index + trimmed posts sitemap, `/categories/` page, a tag page with `rel="next"` and a last page
- [ ] 2.3 Build a signed-in paid post fixture and a signed-out version; scrub member name, email, uuid and any tokens
- [ ] 2.4 Add a synthetic post fixture with comments, images, captions, share and subscribe boxes to test cleaning

## 3. HTTP layer

- [ ] 3.1 Implement `http.Client` with cookie, user agent, 30s timeout and `delay_seconds` spacing
- [ ] 3.2 Add retries (2s/4s/8s) on timeout, connection error and 5xx; no retry on 4xx
- [ ] 3.3 Tests: retry/backoff and no-retry-on-4xx with a mocked transport and fake sleep

## 4. Stage 1 — Discover

- [ ] 4.1 Sitemap parser with nested index support; take posts from `sitemap-posts.xml`
- [ ] 4.2 Post URL filter and normalization + slug
- [ ] 4.3 Category crawler: `/categories/` → `/t/<slug>` → `/tag/<slug>/` pages via `rel="next"`; name from tag page title
- [ ] 4.4 Write/merge `data/manifest.json` without resetting progress
- [ ] 4.5 Tests: sitemap (Ghost index + nested), filtering/normalization, category map with pagination, re-discovery merge

## 5. Stage 2 — Fetch

- [ ] 5.1 Fetch posts (trailing-slash URL) to `data/raw/{slug}.html`, update manifest status and `last_attempt`
- [ ] 5.2 Incremental skip and `--refresh`
- [ ] 5.3 Login check: stop when `data-portal="account"` is missing, without caching that page
- [ ] 5.4 Missing-cookie check before any request
- [ ] 5.5 Tests: incremental skip (no requests on second run), signed-out stop on first post and mid-run

## 6. Stage 3 — Extract

- [ ] 6.1 Select `.gh-content`; remove images, figures, captions, paywall CTA, comments, subscribe, share and related blocks
- [ ] 6.1a Strip the leading newsletter boilerplate paragraphs ("Welcome to my newsletter!", "Free members can read")
- [ ] 6.2 trafilatura XML extraction → sections (`heading`, `level`, `text`) and `text` built from sections
- [ ] 6.3 In-body links (absolute URLs)
- [ ] 6.4 Metadata from og/article/JSON-LD with `<h1>` fallback; ISO date; summary fallback
- [ ] 6.5 `word_count` and `partial` status
- [ ] 6.6 Tests: no clutter in output, sections join equals text, metadata fallbacks, partial detection

## 7. Stage 4 — Store

- [ ] 7.1 JSON schema for post files (`schema.py`)
- [ ] 7.2 Atomic writes for post JSON and manifest
- [ ] 7.3 Rewrite post JSON only when `content_hash` changes; update manifest
- [ ] 7.4 Tests: schema validation, idempotency (no rewrite of unchanged files)

## 8. CLI, logging and docs

- [ ] 8.1 typer app: `discover`, `fetch`, `extract`, `run` with `--limit`, `--refresh`, `--dry-run`, `--verbose`
- [ ] 8.2 Run log file, one line per post, end-of-run summary, cookie redaction
- [ ] 8.3 Test: cookie value never appears in logs or outputs
- [ ] 8.4 Write `README.md` (setup, copying the two `ghost-members-ssr` cookies, commands, output format)
- [ ] 8.5 Run full `pytest` with network blocked

## 9. First live run (user-gated)

- [x] 9.1 User fills in `.env`
- [ ] 9.2 Run `archive discover` and review the manifest with the user
- [ ] 9.3 Run `archive run --limit 5`; check acceptance criteria against the output (paid post matches browser view)
- [ ] 9.4 Run `archive run --limit 5` again and confirm nothing new is downloaded
- [ ] 9.5 Full crawl only on the user's explicit go-ahead
