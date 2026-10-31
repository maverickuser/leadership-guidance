## Context

Example Newsletter is a paid newsletter site. The user is a subscriber and wants a personal, offline, structured copy of every post to feed a later search/Q&A phase. The tool runs on the user's Mac, uses only their own login cookie, and must be polite to the site. Category tags are not reliably shown on post pages, so category listing pages are the source of truth.

## Site findings (inspected 2026-10-31)

- The site runs on **Ghost**. Login is two cookies: `ghost-members-ssr` and `ghost-members-ssr.sig` (expire around May 2027). Stripe cookies are not needed.
- `sitemap.xml` is an index of `sitemap-pages.xml`, `sitemap-posts.xml` (252 posts), `sitemap-authors.xml` and `sitemap-tags.xml`. Post URLs are `https://www.example.com/<slug>/`.
- `/categories/` links to 9 categories at `/t/<slug>`, which 301 to `/tag/<slug>/`. Listings show 12 posts per page and link the next page with `<link rel="next" href=".../tag/<slug>/page/2/">`. Tags `newsletter` and `page` exist but are not categories.
- Post body is `article.gh-article .gh-content`. The article element also carries `tag-<slug>` classes.
- Signed-in pages contain `data-portal="account"`; signed-out pages contain `data-portal="signin"` and the paywall box `.gh-post-upgrade-cta` ("This post is for paying subscribers only").
- Logged-in pages carry no free/paid marker. The user's paid account reads every post, so `access` is dropped from the schema.

## Goals / Non-Goals

**Goals:**
- Produce one clean, validated JSON file per post, with sections that make natural chunk boundaries later.
- Make every stage re-runnable and incremental: no duplicate requests, no needless file rewrites.
- Allow extraction to be re-run offline from cached HTML.
- Fail loudly and early when the cookie has expired instead of saving empty posts.

**Non-Goals:**
- Chunking, embeddings, search, LLM Q&A (later phases).
- Saving images, comments or PDFs; any web UI; scheduling.
- Any paywall bypass or parallel/fast crawling.

## Decisions

### Package layout
```
src/newsletter_archive/
  cli.py         # typer app: discover, fetch, extract, run
  config.py      # load config.yaml + .env into a frozen dataclass
  http.py        # Session wrapper: cookie, UA, timeout, delay, retries
  discover.py    # sitemap + category crawl → manifest
  fetch.py       # manifest → data/raw/{slug}.html
  extract.py     # HTML → Post dict
  store.py       # atomic JSON writes, manifest load/save
  schema.py      # JSON schema for posts/{slug}.json
  logging.py     # run log + redaction
tests/fixtures/  # saved sitemap, category and post HTML
```
Each stage is a pure-ish function over the manifest so `run` is just the three in order. Alternative (one monolithic crawler) rejected: harder to test offline and to re-run extraction alone.

### One HTTP wrapper owns politeness and secrets
All requests go through `http.Client`, which enforces the delay (monotonic clock, measured from the end of the previous request), the 30s timeout, retry policy (2s/4s/8s on timeouts, connection errors and 5xx; never on 4xx), and the `Cookie` header. Tests inject a fake transport. This keeps the cookie in one place and makes "never log headers" easy to enforce.

### Extraction: trafilatura first, BeautifulSoup for structure and gaps
trafilatura isolates the article body well but its plain-text output loses headings. Approach: select `article.gh-article .gh-content` with BeautifulSoup, pre-clean it (drop `img`, `figure`, `figcaption`, `picture`, `svg`, `.gh-post-upgrade-cta`, Ghost cards for signup/subscribe, comments, share and related blocks), run trafilatura with `output_format="xml"` and `include_comments=False, include_images=False` to keep `<head>` elements, then walk that XML to build sections and collect in-body links. Metadata: `og:*`, `article:*`, JSON-LD first; `<h1>` and visible bylines as fallback. If `.gh-content` is missing, fall back to trafilatura on the whole page and log a warning.

### Sections and text invariant
Content before the first heading becomes `{heading: null, level: 0}`. Each heading starts a new section with its level (h2 → 2, etc.). `text` is built **from** sections (`"\n\n".join(s.text for s in sections)`), never independently, so the invariant holds by construction. Empty sections are dropped before joining.

### Status model
Manifest statuses: `discovered → fetched → ok | partial`, or `failed` at any stage with `error` set. `partial` means `word_count < min_word_count`. Fetch skips posts whose raw HTML exists unless `--refresh`; extract writes a post JSON only if its `content_hash` differs from the manifest's.

### Login check
Every fetched post page must contain `data-portal="account"`. If it doesn't, the cookie is missing or expired: stop the run immediately with the spec's message and don't cache the page. This replaces the original "first 3 paid posts are short" rule. It is exact, works on the first request, and also catches a cookie that expires mid-run.

### Canonical URLs
The manifest stores normalized URLs (no trailing slash) for dedupe and ids, but fetches use the trailing-slash form so Ghost doesn't answer with a 301 and an extra request.

### Atomic writes
Write to `<path>.tmp` in the same directory, `fsync`, then `os.replace`. Applies to post JSON and `manifest.json`.

### Config precedence
`config.yaml` defaults, overridden by CLI flags. Cookie only from environment / `.env` (`NEWSLETTER_COOKIE`). Missing cookie fails `fetch` with a clear message; `discover` does not need it.

## Risks / Trade-offs

- [Site HTML changes] → selectors live in one module; fixtures from real pages; extraction can re-run from `data/raw` without re-fetching.
- [Cookie leakage] → never pass headers to the logger; redact any `Cookie`/`Set-Cookie` text in log formatter; `.env` and `data/` in `.gitignore`; a test greps logs and outputs for the cookie value.
- [Long crawl time at 2s/request] → acceptable; incremental runs keep later crawls short.
- [Category pagination changes] → follow `<link rel="next">` rather than building URLs, and stop when a page adds no new posts.
- [trafilatura drops wanted content] → compare against browser view in acceptance check #3; tune pre-clean selectors.

## Migration Plan

New project; nothing to migrate. Rollout: build with offline tests → user fills `.env` → `archive discover` → `archive run --limit 5` → review output → full run on the user's say-so.

## Open Questions

- None blocking. Repo location (`~/Workspace/newsletter-archive`) is confirmed; free/paid detection is dropped by the user's decision.
