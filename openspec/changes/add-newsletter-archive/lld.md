# Low-level design: add-newsletter-archive

Implementation detail for `proposal.md`, `design.md` and `specs/`. Where this file and a spec disagree, the spec wins. Items marked **[decided]** are choices the specs left open. The user confirmed them on 2026-10-31.

Site facts below come from the saved pages in `data/inspect/` (2026-10-31).

---

## 1. Repository layout

```
pyproject.toml
config.yaml
.env.example                 # NEWSLETTER_COOKIE=
README.md
src/newsletter_archive/
  __init__.py                # __version__
  cli.py
  config.py
  http.py
  discover.py
  fetch.py
  extract.py
  store.py
  schema.py
  logging.py                 # module name shadows stdlib only inside the package; import as `from . import logging as alog`
  errors.py
  models.py
tests/
  conftest.py                # network block, tmp data dir, fake client, fixture loader
  fixtures/
    sitemap-index.xml        # Ghost index (pages/posts/authors/tags)
    sitemap-posts.xml        # trimmed to ~6 posts
    sitemap-nested.xml       # synthetic: index -> index -> urlset
    sitemap-flat-mixed.xml   # synthetic: post, /tag/, /author/, /categories/, /about/
    categories.html
    tag-career-growth-p1.html   # has <link rel="next">
    tag-career-growth-p2.html   # last page, no rel=next
    post-signed-in.html      # scrubbed copy of post1-auth.html
    post-signed-out.html     # post1-anon.html
    post-clutter.html        # synthetic: nav, figure+caption, share, signup card, comments, related
    post-meta-fallback.html  # synthetic: no og:title, no JSON-LD
    post-italic-opener.html  # anon-guide-to-building-a-newsletter.html: own italic opener, no boilerplate
  test_config.py
  test_http.py
  test_discover.py
  test_fetch.py
  test_extract.py
  test_store.py
  test_cli.py
  test_secrets.py
```

## 2. pyproject.toml

- `requires-python = ">=3.11"`, build backend `hatchling`, src layout.
- Runtime deps: `requests`, `trafilatura`, `beautifulsoup4`, `lxml`, `typer`, `pyyaml`, `python-dotenv`, `jsonschema`.
- Dev deps (optional group `dev`): `pytest`, `pytest-cov`.
- Entry point: `[project.scripts] archive = "newsletter_archive.cli:app"`.

**[decided]** `jsonschema` is added as a runtime dependency, on top of the agreed library list, to meet "every file MUST validate against this schema".

## 3. Configuration (`config.py`)

### config.yaml (defaults)

```yaml
base_url: https://www.example.com
delay_seconds: 2
max_retries: 3
timeout_seconds: 30
min_word_count: 300
output_dir: data
user_agent: "newsletter-archive/0.1 (personal archive; one request every 2s)"
```

### Types

```python
class Secret:                      # wraps the cookie
    def __init__(self, value: str): self._v = value
    def reveal(self) -> str: return self._v
    def __repr__(self): return "Secret(****)"
    __str__ = __repr__
    def __bool__(self): return bool(self._v.strip())

@dataclass(frozen=True)
class Config:
    base_url: str          # no trailing slash
    delay_seconds: float
    max_retries: int
    timeout_seconds: float
    min_word_count: int
    output_dir: Path
    user_agent: str
    cookie: Secret         # empty Secret if unset

    @property
    def manifest_path(self) -> Path: ...   # output_dir / "manifest.json"
    raw_dir, posts_dir, logs_dir           # same pattern

def load_config(path: Path = Path("config.yaml"),
                env_file: Path = Path(".env"),
                overrides: dict | None = None) -> Config
```

### Rules

- Missing `config.yaml` means built-in defaults (same values as above). Unknown keys raise `ConfigError`.
- Cookie: `os.environ["NEWSLETTER_COOKIE"]` first, then `.env` via `dotenv_values` (never `load_dotenv`, so it doesn't leak into child processes). Strip whitespace. Strip a leading `Cookie:` if the user pasted the whole header line.
- `delay_seconds < 2` raises `ConfigError("delay_seconds must be >= 2")`. This enforces the politeness hard rule.
- `max_retries` must be in 0..3, because the backoff table has 3 steps. `timeout_seconds` must be > 0.
- `Config` and `Secret` never put the cookie value into `repr`.

## 4. Errors (`errors.py`)

```python
class ArchiveError(Exception): ...
class ConfigError(ArchiveError): ...          # exit 1
class MissingCookieError(ArchiveError): ...   # exit 1
class NotSignedInError(ArchiveError): ...     # exit 2, carries slug
class FetchError(ArchiveError):               # per-post, not fatal
    def __init__(self, url, reason, status=None, attempts=1): ...
class ExtractError(ArchiveError): ...         # per-post, not fatal
```

Messages, word for word:
- Missing cookie: `NEWSLETTER_COOKIE is not set. Copy your Cookie header into .env (see README).`
- Not signed in: `Your cookie looks expired. Copy a fresh Cookie header into .env.`

## 5. Data models (`models.py`)

```python
Status = Literal["discovered", "fetched", "ok", "partial", "failed"]

@dataclass
class ManifestEntry:
    url: str                 # normalized, no trailing slash
    slug: str
    categories: list[str]
    status: Status = "discovered"
    last_attempt: str | None = None   # UTC ISO 8601, "2026-10-31T11:03:00Z"
    error: str | None = None
    content_hash: str | None = None

@dataclass
class Manifest:
    entries: dict[str, ManifestEntry]   # key = slug, insertion order = sitemap order
    def ordered(self) -> list[ManifestEntry]

@dataclass
class Section:
    heading: str | None
    level: int
    text: str

@dataclass
class Extracted:          # output of extract_post, before storage adds hash/categories/fetched_at
    title: str
    subtitle: str | None
    author: str
    published_date: str   # YYYY-MM-DD
    summary: str
    sections: list[Section]
    text: str
    links: list[dict]     # {"text","url"}
    word_count: int
```

### manifest.json format

```json
{
  "version": 1,
  "updated_at": "2026-10-31T11:03:00Z",
  "posts": [ { "url": "...", "slug": "...", "categories": [...], "status": "...",
               "last_attempt": null, "error": null, "content_hash": null } ]
}
```

`posts` keeps sitemap order (newest first). That order is what `--limit` counts against (see §11).

## 6. HTTP layer (`http.py`)

```python
@dataclass
class Response:
    status: int
    url: str        # final URL after redirects
    text: str

class Transport(Protocol):
    def __call__(self, url: str, headers: dict, timeout: float) -> Response: ...
    # one HTTP GET, no redirect following; raises TransportTimeout / TransportConnectionError

class RequestsTransport:  # default; wraps requests.Session, allow_redirects=False

class Client:
    def __init__(self, config: Config, transport: Transport | None = None,
                 sleep=time.sleep, clock=time.monotonic): ...
    def get(self, url: str) -> Response     # raises FetchError
```

### get() algorithm

```
BACKOFF = [2, 4, 8]
for attempt in 0..max_retries:
    wait_for_slot()                    # sleep until clock() >= last_end + delay_seconds
    try:
        resp = follow_redirects(url)   # every hop inside this one slot
    except (Timeout, ConnectionError) as e:
        err = e
    else:
        if resp.status < 400: last_end = clock(); return resp
        if resp.status < 500: last_end = clock(); raise FetchError(url, f"HTTP {status}", status)   # no retry
        err = f"HTTP {status}"
    last_end = clock()
    if attempt == max_retries: raise FetchError(url, f"{err} after {attempt+1} attempts", attempts=attempt+1)
    sleep(BACKOFF[attempt])            # wait_for_slot then adds any shortfall vs delay_seconds
```

- The delay is measured from the **end** of the previous request on the monotonic clock. That covers sitemap, category and post requests alike, because there is one `Client` per run.
- Headers on every request: `User-Agent`, `Accept: text/html,application/xml`. `Cookie` goes only when the cookie is set **and** the target `host:port` equals `base_url`'s `host:port`. Including the port keeps the loopback integration tests honest.
- Redirects are followed by hand: at most 5 hops, and only to the same host. A cross-host redirect raises `FetchError("redirect to other host")`, so the cookie can never go to another site. `/t/<slug>` → `/tag/<slug>/` is a same-host 301.
- The body is decoded with the response's declared encoding, falling back to UTF-8.
- The client never logs headers. It logs only `GET <url> -> <status> (<ms> ms)` at DEBUG level.

## 7. Stage 1: discover (`discover.py`)

```python
def run_discover(client: Client, config: Config, manifest: Manifest, dry_run=False) -> DiscoverResult
def collect_sitemap_urls(client, sitemap_url: str) -> list[str]
def is_post_url(url: str, base_host: str) -> bool
def normalize_url(url: str) -> str
def slug_from_url(url: str) -> str
def crawl_categories(client, base_url: str) -> dict[str, list[str]]   # normalized post url -> category names
def merge_manifest(manifest, post_urls, cat_map) -> MergeStats
```

### collect_sitemap_urls

- Parse with `lxml.etree.fromstring(resp.text.encode())` and match tags by namespace-agnostic local name.
- If the root is `sitemapindex`, look at the child `<loc>`s. **Ghost rule:** if any child's filename matches `sitemap-posts*.xml`, follow only those. Otherwise follow every child, recursing with a `visited` set to avoid loops.
- If the root is `urlset`, collect every `<url>/<loc>` and ignore `image:*`.
- A sitemap fetch failure is fatal for discover: raise and leave the manifest untouched.

### is_post_url

Keep a URL only if:
- its host equals the base host (with or without `www.`), and
- its path has exactly one non-empty segment, and
- that segment is not in `EXCLUDED_SEGMENTS = {"tag","t","author","categories","page","p","rss","about","contact","subscribe","signin","signup","account","archive","membership"}`.

The Ghost sitemap index already keeps static pages out, because only `sitemap-posts.xml` is read. The deny list covers flat or mixed sitemaps and the spec scenario.

### normalize_url

- Lowercase the scheme and host.
- Drop the fragment.
- Drop query params named `ref`, `source`, `fbclid`, `gclid`, `mc_cid`, `mc_eid`, and anything starting with `utm_`. Keep the rest, sorted.
- Strip the trailing `/` from the path.

`slug_from_url`: take the last path segment, URL-decode it, lowercase it, and replace runs of characters outside `[a-z0-9-]` with `-`. Two different URLs with the same slug get a warning and a `-2` suffix on the second.

### crawl_categories

1. `GET {base}/categories/`. Collect `a[href]` whose path matches `^/t/([^/]+)/?$`, absolute or relative, deduped in page order. The current site has 9 categories. `dave` is one of them; it's on the page, so it counts. Tags not linked there, such as `newsletter` and `page`, are ignored.
2. For each category: `url = {base}/t/<slug>`, `seen_pages = set()`, `posts_in_cat = set()`.
   - `GET url`. The client follows the redirect to `/tag/<slug>/`.
   - On the first page, the name is `<title>` with a trailing ` - Example Newsletter` removed (rsplit on `" - "`). If the title is missing, use the slug.
   - The post links are `article.gh-card a.gh-card-link[href]`, normalized and joined to the base. There are 12 per page.
   - Stop if the page has no new posts. Otherwise follow `link[rel=next][href]` if it exists and hasn't been seen. Stop at 200 pages as a safety cap.
3. Build `post_url -> sorted(category names)`. Posts that appear in listings but not in the sitemap are ignored and logged at DEBUG.

**[decided]** If one category fails (a `FetchError`), log a warning and continue. Its posts lose that category for this run only, and the summary notes `categories incomplete`.

### merge_manifest

For each sitemap post, in sitemap order:
- **New slug:** add a `ManifestEntry` with status `discovered`.
- **Existing slug:** replace `categories` and `url` and keep everything else. This covers status, `last_attempt`, `error` and `content_hash`.

Entries no longer in the sitemap are kept, logged and not deleted. Order follows the new sitemap order, with stale entries appended at the end. In dry-run mode, the counts are printed and nothing is written.

## 8. Stage 2: fetch (`fetch.py`)

```python
SIGNED_IN_MARKER = 'data-portal="account"'

def select_for_fetch(manifest, raw_dir, limit, refresh) -> list[ManifestEntry]
def run_fetch(client, config, manifest, limit=None, refresh=False, dry_run=False,
              on_post=callback) -> FetchResult
def fetch_url(entry) -> str          # entry.url + "/"
```

### Selection

1. `candidates = manifest.ordered()[:limit]`. The limit is applied **before** the skip check, so `run --limit 5` twice touches the same 5 posts and the second run downloads nothing (task 9.4).
2. Without `--refresh`, drop candidates where `raw/{slug}.html` exists.

### Pre-flight

If `config.cookie` is empty, raise `MissingCookieError` before any request. This check also runs in `--dry-run`.

### Per post

```
try:
    resp = client.get(entry.url + "/")
except FetchError as e:
    entry.status = "failed"; entry.error = e.reason; entry.last_attempt = now()
    save_manifest(); log; continue
if SIGNED_IN_MARKER not in resp.text:
    # do not touch entry, do not write raw
    save_manifest(); raise NotSignedInError(entry.slug)
write_text_atomic(raw_dir / f"{slug}.html", resp.text)
entry.status = "fetched"; entry.error = None; entry.last_attempt = now()
save_manifest()                      # after every post, so Ctrl-C loses at most one
```

- The marker is a plain substring check on the attribute. Signed-out pages contain only `data-portal="signin"` and `"signup"`.
- `--dry-run` prints `would fetch <slug> <url>` for each selected post and makes no requests.
- Status 200 is required. Other 2xx/3xx final statuses become `failed`, `HTTP <code>`.

## 9. Stage 3: extract (`extract.py`)

```python
def extract_post(html: str, page_url: str) -> Extracted     # pure, no I/O
def clean_body(body: Tag) -> Tag
def sections_from_trafilatura(xml: str) -> list[Section] | None
def sections_from_soup(body: Tag) -> list[Section]          # fallback walker
def extract_links(body: Tag, page_url: str) -> list[dict]
def extract_metadata(soup, text: str) -> dict
```

### 9.1 Body selection

Parse with `BeautifulSoup(html, "lxml")`. Select `body = soup.select_one("article.gh-article section.gh-content")`, falling back to `article.gh-article .gh-content`.

If neither exists, log a warning and run `trafilatura.extract(html, output_format="xml", …)` on the whole page. There is no structure for links in that case, so `links = []`.

### 9.2 clean_body: remove these from the copy of `body`

```
img, picture, figure, figcaption, svg, video, audio, iframe, object, embed,
script, style, noscript, form, button, input,
.gh-post-upgrade-cta, [data-portal], [data-members-form],
.kg-image-card, .kg-gallery-card, .kg-embed-card, .kg-video-card, .kg-audio-card,
.kg-file-card, .kg-bookmark-card, .kg-signup-card, .kg-cta-card, .kg-button-card,
.kg-product-card, .kg-header-card,
[class*="share"], [class*="subscribe"], [class*="related"], [class*="comment"],
.gh-comments, #ghost-comments-root
```

Also remove the `alt` and `title` attributes from what's left. The rest of the page (nav, header, footer, related `gh-card` posts, comments) sits outside `.gh-content`, so selecting the body removes it.

**Newsletter boilerplate.** Almost every post opens with the same two italic paragraphs:
- "Welcome to my newsletter! I'm Dave Anderson, an ex-Amazon Tech Director and GM…"
- "Free members can read some amount of each article, while paid members can read the full article…"

These are removed in `strip_boilerplate(body)`, called at the end of `clean_body`:

```python
BOILERPLATE_PREFIXES = ("Welcome to my newsletter!", "Free members can read")

for el in leading top-level element children of body, at most the first 3:
    text = " ".join(el.get_text().split())
    if el.name == "p" and text.startswith(BOILERPLATE_PREFIXES):
        el.decompose()
    else:
        break                        # stop at the first real paragraph
```

- The check matches the start of the text exactly. Being italic is not enough: some posts open with their own italic note that is part of the article, for example `guide-to-building-a-newsletter` ("I'm writing about something different today!"). Those notes are kept.
- The check runs only at the top of the body, so a quoted "Welcome to my newsletter!" further down survives.
- Links inside the removed paragraphs (such as Dave's LinkedIn) leave `links` too, because links are read from the cleaned body.
- The saved posts have no repeated closing text, so there is no outro rule.

### 9.3 Sections

**Primary path:** trafilatura run on `str(cleaned_body)` with these options:

```python
trafilatura.extract(html_str, output_format="xml", include_comments=False,
                    include_images=False, include_tables=True, include_links=False,
                    include_formatting=False, favor_recall=True, deduplicate=False)
```

Walk the `<main>` children of the XML in order:

| XML element | Treatment |
|---|---|
| `<head rend="hN">` | starts a new section, `level = N` (no `rend` → 2) |
| `<p>`, `<quote>` | one block: `" ".join(itertext()).split()` joined with single spaces |
| `<list>` | one block: each `<item>` as `- item` on its own line |
| `<table>` | one block: each `<row>` as cells joined with ` \| ` |
| `<code>` | one block, whitespace preserved |

- **Section text** is its blocks joined with `"\n\n"`.
- **Intro:** blocks before the first heading go into `Section(None, 0, …)`.
- Sections with empty text are dropped. A heading with no text under it is dropped too.

**Guard and fallback:** let `ref_words` be the word count of `cleaned_body.get_text(" ")`. Use `sections_from_soup` instead, and log a warning, if trafilatura returns `None`, if its word count is below 0.8 × `ref_words`, or if `ref_words > 0` and it found no `<head>` while the soup has `h2`–`h6`.

`sections_from_soup` walks the top-level children of `cleaned_body`, using the same block rules with `h1`–`h6`, `p`, `blockquote`, `ul`/`ol`, `table` and `pre`.

**[decided]** This guard goes beyond design.md. It protects against trafilatura silently dropping headings or paragraphs, a risk design.md already lists.

**text** is `"\n\n".join(s.text for s in sections)`, built after the empty sections are dropped. That makes the invariant hold by construction.

### 9.4 Links

Read links from `cleaned_body` (BeautifulSoup, not trafilatura):
- Take each `a[href]`. Skip `#…`, `mailto:` and `javascript:`.
- Set `url = urljoin(page_url + "/", href)` and keep only http(s) URLs.
- Set `text = " ".join(a.get_text().split())` and skip links with empty text.
- Dedupe on `(text, url)`, keeping the first occurrence.

The URL is kept as written in the post: no tracking params are stripped, so `?ref=example.com` stays.

### 9.5 Metadata (first non-empty value wins)

| Field | Sources in order |
|---|---|
| title | `meta[property=og:title]` → JSON-LD `headline` → `h1.gh-article-title` → first `h1` → `<title>` minus ` - Example Newsletter` |
| subtitle | `p.gh-article-excerpt` → `null` |
| author | JSON-LD `author.name` (a list is joined with `", "`) → `meta[name=author]` → `.gh-article-author-name` text → `"Unknown"` + warning |
| published_date | `meta[property=article:published_time]` → JSON-LD `datePublished` → `time[datetime]` → raise `ExtractError("no published date")` |
| summary | `meta[name=description]` → `og:description` → first 2 sentences of `text` (split on `(?<=[.!?])\s+`, max 400 chars) |

- JSON-LD: parse each `script[type="application/ld+json"]` and use the first object with `@type` in {`Article`, `BlogPosting`, `NewsArticle`}. Bad JSON is skipped.
- Dates: parse with `datetime.fromisoformat` (after replacing a trailing `Z` with `+00:00`), convert to UTC, then format as `%Y-%m-%d`.
- For the sample post, the subtitle and summary are the same string (Ghost's custom excerpt). That's expected.

### 9.6 Counts

- `word_count = len(text.split())`.
- `status = "partial" if word_count < min_word_count else "ok"`. The status is set by the caller in store, because it needs config.
- The word count is taken after the boilerplate is stripped, so a truncated paid post can't reach 300 words on the intro text alone.

## 10. Stage 4: store (`store.py`, `schema.py`)

```python
def load_manifest(path) -> Manifest            # missing file -> empty Manifest
def save_manifest(manifest, path) -> None      # atomic
def write_json_atomic(path, obj) -> None
def write_text_atomic(path, text) -> None
def build_record(entry, ex: Extracted, fetched_at: str, min_word_count: int) -> dict
def run_extract(config, manifest, limit=None, refresh=False, dry_run=False, on_post=cb) -> ExtractResult
```

### Atomic write

```python
tmp = path.with_name(path.name + ".tmp")
with open(tmp, "w", encoding="utf-8") as f:
    f.write(data); f.flush(); os.fsync(f.fileno())
os.replace(tmp, path)
```

JSON is written with `json.dumps(obj, indent=2, ensure_ascii=False) + "\n"`. Parent directories are created on demand. A leftover `*.tmp` is overwritten on the next write.

### Record (field order as in the spec)

```python
{
  "schema_version": 1, "id": slug, "url": entry.url, "title", "subtitle", "author",
  "published_date", "categories": entry.categories, "word_count", "summary",
  "sections": [asdict(s) ...], "text", "links",
  "content_hash": sha256(text.encode("utf-8")).hexdigest(),
  "fetched_at": mtime of raw/{slug}.html as UTC ISO 8601 "…Z",
  "status": "ok" | "partial",
}
```

`fetched_at` comes from the raw file's modification time. That keeps it stable across re-extracts and accurate after `--refresh`.

### schema.py

`POST_SCHEMA` is a JSON Schema (draft 2020-12) with `additionalProperties: false`. All 16 fields are required. It uses patterns for `published_date` (`^\d{4}-\d{2}-\d{2}$`) and `content_hash` (`^[0-9a-f]{64}$`), an enum for `status`, and `const: 1` for `schema_version`. `validate_post(record)` raises `ExtractError` with the first error path.

### run_extract per post

1. Selection: `manifest.ordered()[:limit]`, keeping entries whose `raw/{slug}.html` exists.
2. Read the raw file and run `extract_post`, then `build_record` and `validate_post`. Any `ExtractError` or exception sets the entry to `failed` with the error, and the run continues.
3. If `not refresh and entry.content_hash == record.content_hash and posts/{slug}.json exists`, skip the write. The manifest status is still set from the record (`ok`/`partial`), so the mtime doesn't change.
4. Otherwise write the JSON atomically, then set `entry.content_hash`, `status` and `error = None`.
5. Save the manifest after each post.

There are no network calls: `run_extract` takes no `Client`.

**[decided]** The spec says to rewrite only when `content_hash` changes, which would leave stale categories in the post file. This design also rewrites when `entry.categories` differs from the `categories` in the existing `posts/{slug}.json`. Step 3 above then reads: skip the write only if the hash matches **and** the categories match **and** the file exists.

## 11. CLI (`cli.py`)

```
archive discover [--limit N] [--refresh] [--dry-run] [--verbose]
archive fetch    [same]
archive extract  [same]
archive run      [same]        # discover -> fetch -> extract
```

Shared options go through a typer callback into a `RunContext(config, limit, refresh, dry_run, verbose)`.

| Command | `--limit N` | `--refresh` | `--dry-run` |
|---|---|---|---|
| discover | caps how many new posts are added to the manifest | no effect (discover always re-reads) | fetches the sitemap and categories, prints counts, writes nothing |
| fetch | first N manifest posts | re-download | lists posts, no requests |
| extract | first N with raw HTML | rewrite even if the hash is the same | lists what would be written |
| run | passed to fetch and extract; discover is not limited | passed down | passed down |

Exit codes:
- 0: done. Per-post failures still exit 0 and show up in the summary.
- 1: `ConfigError` or `MissingCookieError`.
- 2: `NotSignedInError`.
- 130: Ctrl-C. The manifest is already saved.

`run` builds one `Client` that discover and fetch share, so the 2s spacing holds across stages.

## 12. Logging and summary (`logging.py`)

- The logger is named `newsletter_archive`.
- Console: INFO, or DEBUG with `--verbose`. The format is `%(message)s`.
- File: `{output_dir}/logs/run-{YYYYmmdd-HHMMSS}Z.log` at DEBUG level, format `%(asctime)s %(levelname)s %(message)s`.
- `RedactFilter(secret)` sits on both handlers and rewrites `record.msg` and `args` after formatting. It replaces:
  - the exact cookie value,
  - each `name=value` cookie pair inside it, split on `;`, with values of 8 characters or more,
  - the regexes `(?i)(set-)?cookie\s*[:=]\s*[^\n]+` and `ghost-members-ssr(\.sig)?=[^;\s]+`, which become `[REDACTED]`.
- One line per post: `log_post(slug, stage, status, error=None)` writes `<slug>  <stage>  <status>[  <error>]`.
- The `RunSummary` counter prints `discovered=<manifest total> fetched=<this run> ok=<this run> partial=<this run> failed=<this run>`, plus `skipped=<n>` if any.
- Exceptions are logged with `logger.exception` only after redaction. Tracebacks from `requests` don't include headers, because the client never puts them into exception messages.

## 13. Testing

`conftest.py`:
- An autouse fixture blocks the network by monkeypatching `socket.socket.connect` to raise for any address except `127.0.0.1` and `::1`. Loopback stays open for the integration fixture site (see `implementation-plan.md` §5). This covers task 8.5 without pytest-socket.
- `FakeTransport(routes: dict[url, list[Response | Exception]])` records calls. Each call pops the next item for its URL.
- A `fake_clock` / `fake_sleep` pair makes time advance only through sleep.
- `cfg(tmp_path)` builds a Config with `output_dir = tmp_path/"data"` and cookie `"ghost-members-ssr=TESTSECRET123; ghost-members-ssr.sig=SIGSECRET456"`.

| File | Covers |
|---|---|
| test_config | defaults, override, `delay_seconds<2` rejected, `Cookie:` prefix stripped, repr hides cookie |
| test_http | 503,503,200 sleeps [2,4]; 4× ConnectionError → FetchError attempts=4; 404 no retry; spacing ≥ delay between calls; same-host redirect followed in one slot; cross-host redirect refused; no Cookie header to other hosts |
| test_discover | Ghost index → posts only; nested index; flat mixed filter; normalize/dedupe (`?utm_source=x#top`); slug; categories page → 9 `/t/` links; pagination via rel=next and stop when no new posts; post in two categories → sorted list; uncategorized → `[]`; re-discovery keeps `ok` status and refreshes categories |
| test_fetch | trailing-slash URL requested; raw saved and status `fetched`; second run makes zero requests; `--refresh` refetches; signed-out first post → NotSignedInError, no raw file, status `discovered`; mid-run stop keeps earlier raws; missing cookie → no requests; 404 → `failed` and continue; limit-before-skip |
| test_extract | clutter fixture: no nav/share/subscribe/comment/caption/alt text; intro + h2 + h3 sections; join == text; metadata from og/JSON-LD; `<h1>` fallback; summary fallback to first sentences; links body-only and absolute; signed-in fixture has 6 h2 sections and ~2.7k words → `ok`; signed-out fixture → `partial`; trafilatura fallback path; boilerplate: text does not start with "Welcome to my newsletter!" nor contain "Free members can read", the LinkedIn link is gone, and a non-boilerplate italic opener (newsletter-guide fixture) is kept |
| test_store | record validates; hash == sha256(text); unchanged → mtime unchanged; changed text → rewritten and manifest updated; atomic write leaves the old file if `os.replace` is patched to raise; manifest round-trip |
| test_cli | `typer.testing.CliRunner`: exit codes 0/1/2; summary line present; `fetch --dry-run` writes nothing to raw/posts |
| test_secrets | full `run --verbose` on fakes, then grep the console output, every file under `data/` and the log for `TESTSECRET123` / `SIGSECRET456` → none |

### Fixture preparation (tasks 2.2–2.4)

The repo is public, so fixtures are **structure-only** and generated by `scripts/make_fixtures.py` from `data/inspect/`. That input is never committed. The script:
- keeps the real Ghost markup and meta tags,
- replaces article text with seeded filler that has the same word count per text node,
- keeps headings and the two boilerplate opening phrases,
- removes member name, email, uuid, `data-key` and tokens.

See `implementation-plan.md` §4 for the full procedure. Test expectations that depend on word counts (≈2.7k words → `ok`) still hold because the counts are preserved.
- Trim the sitemap to 6 posts. Produce tag page 2 by copying page 1 with different cards and no `rel=next`.
- Integration tests: `tests/integration/` runs the full CLI against a local `ThreadingHTTPServer` fixture site with the real `RequestsTransport` (scenarios I1–I10 in the plan).

## 14. Build order

1. pyproject, package skeleton, config, errors, models (tasks 1.2–1.4)
2. Fixtures (2.2–2.4)
3. http + tests (3.x)
4. discover + tests (4.x)
5. store atomic writes and manifest I/O (7.2 part), since fetch needs them
6. fetch + tests (5.x)
7. extract + schema + store record logic + tests (6.x, 7.x)
8. cli, logging, secrets test, README, network-blocked pytest (8.x)
9. User-gated live steps (9.2–9.5): `archive discover`, then `archive run --limit 5` twice. No full crawl without the user's go-ahead.

## 15. Decisions (2026-10-31)

1. Add `jsonschema` as a dependency (§2).
2. A failed category crawl logs a warning and discover continues (§7).
3. trafilatura output is checked, with a BeautifulSoup fallback (§9.3).
4. The newsletter boilerplate intro is stripped from every post (§9.2).
5. The post JSON is rewritten when its categories change, even if the text is the same. This goes beyond the post-storage spec (§10).
