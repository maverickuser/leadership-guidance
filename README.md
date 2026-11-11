# Newsletter Archive

A personal command-line tool that saves the posts **you subscribe to** on a Ghost-based newsletter site as clean, structured JSON on your own computer. Your agent can then search it through a local MCP server.

- **Personal use only.** It uses your own login cookie and reads only what your subscription can read. It never bypasses the paywall.
- **Polite.** One request at a time, at least 2 seconds apart, with backoff on errors.
- **Private.** Posts are stored outside this repo (`../leadership-coach-datasets/` by default) and are never committed. Your cookie stays in `.env` and is never printed or logged.

This repository holds code and specs only, with no newsletter content. Test fixtures keep the site's HTML structure but replace article text with filler.

## Setup

Requires Python 3.11 or newer (macOS: `brew install python@3.12`).

```bash
git clone https://github.com/maverickuser/leadership-guidance.git
cd leadership-guidance
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
cp config.example.yaml config.yaml   # then add your newsletter under sources:
```

`config.yaml` lists your newsletters under `sources:`. Each one gets its own folder in the data folder, named by its `id`:

```yaml
sources:
  - id: main
    platform: ghost
    base_url: https://www.example.com
    cookie_env: NEWSLETTER_COOKIE   # the .env variable that holds this newsletter's cookie
```

`config.yaml` and `.env` are git-ignored, so the site addresses and your cookies stay on your machine.

### Add your cookie to `.env`

Each newsletter's cookie goes in the `.env` variable its `cookie_env` names.

1. Sign in to the newsletter site (its `base_url`) in your browser.
2. Open DevTools (⌥⌘I in Chrome), go to the **Network** tab, and reload the page.
3. Click the first request (the page itself). Under **Request Headers**, find `cookie`.
4. Copy its whole value into `.env`:

   ```
   NEWSLETTER_COOKIE=ghost-members-ssr=<value>; ghost-members-ssr.sig=<value>
   ```

Only the two `ghost-members-ssr` cookies matter, and they last several months. If a run says *"Your cookie looks expired"*, copy a fresh value the same way.

## Commands

```bash
archive discover            # stage 1: sitemap + categories -> manifest.json
archive fetch               # stage 2: download post pages -> raw/
archive extract             # stages 3-4: raw/ -> posts/{slug}.json (no network)
archive index               # build or update the search store (needs the database and GEMINI_API_KEY)
archive run                 # all of the above, in order
archive mcp                 # the search server for Claude Code / Claude Desktop (see Search)
archive migrate --source ID # one-time: move an old single-newsletter archive (see below)
```

Options:

| Option | Meaning |
|---|---|
| `--limit N` | Process at most N posts: the first N in the manifest (newest first). Repeating the same limit downloads nothing new. |
| `--refresh` | Download or rewrite again even if cached (`fetch`, `extract`, `run`). |
| `--dry-run` | Show what would be fetched or written. Makes no post requests and writes no post files. |
| `--verbose`, `-v` | Show debug output. |
| `--config PATH` | Use another `config.yaml`. `.env` is read from the same folder. |

Start small: `archive run --limit 5`, check the output, then run `archive run` for everything. At 2 seconds per request, a full first run of about 250 posts takes around 10 minutes. Later runs fetch only new posts.

`archive index` options: `--dry-run` (what would change and the estimated cost; nothing is written), `--retry-failed` (retry posts that failed 3 times), `--allow-mass-delete` (allow removing many posts at once), `--yes` (go ahead over the spending ceiling), `--limit N`. It re-indexes only posts whose content, metadata or settings changed, and never pays twice for the same text.

Exit codes: `0` done (individual post failures are listed in the summary), `1` config, cookie or API key missing (for `archive index`), or another run is already using the archive folder, `2` cookie expired, `3` (`archive run` only) posts were archived but updating the search index failed (database down, API key missing, Gemini outage, or a post stuck for 2 runs), `130` interrupted (progress is saved). To archive without search, set `search: {enabled: false}` in `config.yaml`.

Only one command runs at a time per archive folder; a second one exits with a message instead of overwriting progress.

## Weekly updates (macOS)

`scripts/scheduled-run.sh` runs `archive run` unattended. It sends a macOS notification when new posts are archived, when the cookie has expired, or when a run fails. A second run can't overlap a running one (the archive folder is locked).

To run it every Monday at 09:00, save this as `~/Library/LaunchAgents/com.<you>.newsletter-archive.plist`, replacing `/path/to` with your paths:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.you.newsletter-archive</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>/path/to/leadership-guidance/scripts/scheduled-run.sh</string></array>
  <key>StartCalendarInterval</key>
  <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/path/to/leadership-coach-datasets/logs/scheduled.log</string>
  <key>StandardErrorPath</key><string>/path/to/leadership-coach-datasets/logs/scheduled.log</string>
</dict>
</plist>
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.you.newsletter-archive.plist   # turn on
launchctl kickstart gui/$(id -u)/com.you.newsletter-archive                                # run once now
launchctl bootout gui/$(id -u)/com.you.newsletter-archive                                  # turn off
```

launchd runs a missed job when the Mac wakes from sleep, but not if the Mac was shut down at that time. On Linux, use cron instead: `0 9 * * 1 /path/to/leadership-guidance/scripts/scheduled-run.sh`.

## Settings (`config.yaml`, copied from `config.example.yaml`)

| Setting | Default | |
|---|---|---|
| `sources` | none (required) | your newsletters: each has `id`, `platform` (`ghost`), `base_url`, `cookie_env`, and optional `name` and `boilerplate_prefixes` |
| `delay_seconds` | `2` | minimum gap between requests, across all newsletters; lower values are refused |
| `max_retries` | `3` | retries on timeouts, connection errors and 5xx (2s, 4s, 8s); never on 4xx |
| `timeout_seconds` | `30` | |
| `min_word_count` | `300` | posts with fewer words, or with a paywall box in the body, are `partial` |
| `output_dir` | `../leadership-coach-datasets` | relative to `config.yaml` (or the current folder if there is none), or an absolute path |
| `user_agent` | `newsletter-archive/0.1 (...)` | |
| `sources[].boilerplate_prefixes` | `[]` | extra intro openings to strip from the top of each post; your site's wording stays in your local `config.yaml` |
| `search` | see [Search settings](#search-settings-search-in-configyaml) | |

## Output

```
../leadership-coach-datasets/
  <id>/                  # one folder per newsletter, named by its id in config.yaml
    manifest.json        # every post: url, slug, categories, status, last_attempt, error, content_hash
    raw/{slug}.html      # cached pages (extraction can re-run offline)
    posts/{slug}.json    # one clean record per post
  postgres/              # the search database (shared)
  models/                # the local re-ranker model (shared)
  logs/run-*.log         # one log per run, for all newsletters
  eval-queries.yaml      # your search test questions (optional)
  eval-judgments.yaml    # saved LLM judge grades (optional)
```

### Moving an existing archive (one-time)

Archives made before newsletters had their own folders keep `manifest.json`, `raw/` and `posts/` at the top. Every command refuses that layout until you move it once:

1. Back up the data folder.
2. Stop the database: `docker compose down`.
3. Rename `../newsletter-archive-data` to `../leadership-coach-datasets`.
4. Update `output_dir` in `config.yaml`, `ARCHIVE_DB_DATA` in `.env` (if set) and the launchd log path (if set).
5. Convert `config.yaml` to the `sources:` list. Any command prints the entry to paste in.
6. Run `archive migrate --source <id> --dry-run` to see what will move, then `archive migrate --source <id>`.
7. Start the database with `docker compose up -d --wait db`, then run `archive index --dry-run` (it should cost about $0) and `archive index`.

Post ids become `<id>/<slug>`, and the ids in your eval files are updated automatically. If the move stops partway, run it again; nothing old is deleted until every new file is in place.

Each `<id>/posts/{slug}.json` (schema version 2):

```json
{
  "schema_version": 2,
  "id": "main/example-post",
  "source": "main",
  "url": "https://www.example.com/example-post",
  "title": "Example Post",
  "subtitle": "…",
  "author": "Author Name",
  "published_date": "2026-01-15",
  "categories": ["Example Category"],
  "word_count": 2000,
  "summary": "…",
  "sections": [{"heading": null, "level": 0, "text": "…"}, {"heading": "First heading", "level": 2, "text": "…"}],
  "text": "…",
  "links": [{"text": "…", "url": "https://…"}],
  "content_hash": "sha256 of text",
  "fetched_at": "2026-10-31T11:03:00Z",
  "status": "ok"
}
```

- `text` is always the `sections` texts joined with blank lines.
- Images, captions, comments, share/subscribe/related boxes and the standard "Welcome to my newsletter!" intro are removed.
- A post file is rewritten only when its text or categories change.

Manifest statuses: `discovered → fetched → ok | partial`, or `failed` with `error` set.

## Search (MCP server)

Claude Code, Claude Desktop or any MCP client can search your archive through a local MCP server. It uses hybrid search: keywords (BM25) plus meaning (Gemini embeddings), re-ranked by a small local model. The server only returns archive data, passages and one stored summary per post. Your agent writes the answer and cites the post URLs.

### Set up (once)

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and start the database:

   ```bash
   docker compose up -d --wait db     # ParadeDB (Postgres 17 + pg_search + pgvector) on 127.0.0.1:5433
   ```

   Its data lives in `../leadership-coach-datasets/postgres/`, outside the repo.

2. In `.env`, add `GEMINI_API_KEY` (from [Google AI Studio](https://aistudio.google.com/apikey)) and three database passwords of your choice (see `.env.example`).
3. Install the local re-ranker (optional, ~1.2 GB download on first use, Apple Silicon or CPU):

   ```bash
   pip install -e '.[rerank]'
   ```

   Without it, search still works but isn't re-ranked.
4. Build the index: `archive index --dry-run` shows the cost, then run `archive index`. For about 250 posts, the first build takes ~20 minutes and costs about $1.30. Later runs only process new or changed posts, and the weekly `archive run` keeps the index up to date.

### Connect your agent

The server is called `leadership-coach`.

Claude Code (`--scope user` makes it work in every folder):

```bash
claude mcp add --scope user leadership-coach -- /path/to/leadership-guidance/.venv/bin/archive mcp --config /path/to/leadership-guidance/config.yaml
```

If you registered it earlier as `newsletter-archive`, remove that first: `claude mcp remove --scope user newsletter-archive`.

Claude Desktop: add this to `~/Library/Application Support/Claude/claude_desktop_config.json`, then restart the app:

```json
{
  "mcpServers": {
    "leadership-coach": {
      "command": "/path/to/leadership-guidance/.venv/bin/archive",
      "args": ["mcp", "--config", "/path/to/leadership-guidance/config.yaml"]
    }
  }
}
```

Then ask, for example: *"Using the newsletter archive, how should I give feedback to a senior engineer?"*

### Ask for a full answer

This has Claude research the question (post summaries first, then passages from the best posts) and answer as a short teaching post written to you, with a title and short sections, quoting and linking each post it draws on, and ending with a sources list. It also says where posts disagree or where the archive is silent.

- **Claude Code:** install the `/ask-coach` command once:

  ```bash
  archive mcp --install-command
  ```

  Then, in a new session: `/ask-coach how do I give implicit feedback?`

  It writes `~/.claude/commands/ask-coach.md` (or under `$CLAUDE_CONFIG_DIR`). Run it again after updating this project to pick up new instructions. It never replaces a file of that name that it didn't write, unless you add `--force`.
- **Claude Desktop:** pick **ask** from the leadership-coach prompts in the chat box menu, then type your question.

Why a separate command for Claude Code? Its built-in command for a server prompt (`/mcp__leadership-coach__ask`) passes only the first word of your question. The instructions only shape the answer: they don't search by themselves and cost nothing extra.

The `search` tool takes:
- `query`
- `limit` (1–30, default 10)
- `category` (one or more names)
- `since` and `until` (`YYYY-MM-DD`)
- `kind` (`all`, `passages` or `summaries`)

A search takes about 5 seconds. If Gemini is unreachable, results come from keywords only. If the re-ranker isn't installed or is busy, results keep their combined order. Either way, the response says so. The server listens on no network port. It logs to `../leadership-coach-datasets/logs/mcp-*.log` and to stderr, never the query text, passage text or any key. It picks up index updates without a restart.

### What is sent to Gemini

- **Indexing:** each passage's text, plus its post title and section heading, is sent for embedding. Each post's cleaned text is sent once to write its summary. Both results are cached, so unchanged text is never sent again.
- **Searching:** only your query text is sent, for embedding.
- Your cookie and member details are never sent. The re-ranker runs on your machine.

### Index versions

Changing `search.embedding_model` needs a new index version. `archive index --refresh` builds one beside the live one and then switches to it.
- `--no-swap`: build without switching; `--swap` switches later.
- `--rollback`: go back to the previous version.
- `--force`: switch even if the quality check fails.
- Every one of these honours `--dry-run`.

### Check search quality

Write your own test questions in `../leadership-coach-datasets/eval-queries.yaml`, which is never committed. Each question has the posts it should find:

```yaml
questions:
  - id: trust-equation
    type: local        # local (a fact in one post), intent (paraphrased), global (broad), multi-hop
    question: What is the trust equation?
    expected: [trust-equation-how-to-be-a-decent-human]    # post ids = file names in posts/
```

Then run `python scripts/eval_search.py`. For each stage, with and without summaries, overall and per question type, it prints:
- recall@10: the share of expected posts in the first 10
- MRR: how high the first expected post ranks
- which questions missed posts

The stages are keyword only, semantic only, combined, and re-ranked. Options:
- `--rerank-top N` and `--no-rerank` compare re-ranking settings.
- `--generation N` scores an index version that isn't live yet.
- `--json FILE` saves the full report.
- `--judge` also asks Gemini (`summaries.model`) to grade the results, so you can check your `expected` lists (below).

**LLM judge (`--judge`).** For each question, Gemini grades the posts that any stage put in its top 10, plus your expected ones, from 0 (not relevant) to 3 (essential). It sees each post's title, summary and the passages search found. Grades are saved in `../leadership-coach-datasets/eval-judgments.yaml` (never committed) and reused, so later runs grade only new posts or reworded questions. The report adds:
- posts to consider adding to `expected` (graded 2–3 but not listed)
- expected posts the judge rates weak (0–1), worth a second look
- nDCG@10 per stage, a graded score that rewards the best posts ranking first

The judge only advises: you decide what goes in the file, and the swap check below keeps using your lists. The first run makes about one request per question per 12 posts (a few cents); delete the grades file to grade everything again.

A run costs a fraction of a cent (query embeddings only). While the file exists, `archive index --refresh` and `--swap` compare a new index version with the live one, and won't switch if it scores more than 2 points worse unless you pass `--force`.

### Search settings (`search:` in `config.yaml`)

| Setting | Default | |
|---|---|---|
| `enabled` | `true` | `false` skips indexing in `archive run` |
| `embedding_model` / `embedding_dimension` | `gemini-embedding-2` / `3072` | changing either needs `archive index --refresh` |
| `summaries.enabled` / `summaries.model` | `true` / `gemini-3.8-flash` | one summary per post, written at index time |
| `max_embed_tokens_per_run` / `summaries.max_tokens_per_run` | `1500000` each | spending ceilings; `--yes` goes over them |
| `chunk_words` / `chunk_overlap_words` | `300` / `50` | passage size |
| `candidates` | `50` | results taken from each of keyword and semantic search |
| `rerank_top` / `rerank_timeout_seconds` | `20` / `10` | re-ranking takes ~0.2 s per passage on Apple Silicon |
| `query_timeout_seconds` | `8` | after this, search uses keywords only |
| `bm25_boosts` | heading `2`, title `1.5`, body `1` | |
| `fusion_weights` | keyword `1`, semantic `2` | how much each list counts when combining (semantic 2 scored best) |
| `max_per_post` | `3` | passages per post in one result list (plus one summary) |
| `db_host` / `db_port` / `db_name` | `127.0.0.1` / `5433` / `archive` | |

## Development

```bash
pip install -r requirements-dev.lock && pip install --no-deps -e .   # pinned dev setup (same as CI)
pre-commit install     # ruff, ruff-format, mypy and the safety check before each commit

pytest                 # unit + integration tests; network and DNS are blocked except loopback
pytest -m slow         # the real-clock spacing test (CI runs everything)
pytest -m db           # search-database tests (need `docker compose up -d --wait db`; skipped otherwise)
ARCHIVE_API_TESTS=1 pytest -m api   # opt-in checks against the real Gemini API (needs GEMINI_API_KEY; never in CI)
ruff check . && ruff format --check . && mypy   # lint, format, strict types (CI "lint" job)
python scripts/check_safety.py   # what CI runs to keep secrets and content out of the repo
```

Update the lockfile after changing dependencies: `pip-compile --extra dev --strip-extras --allow-unsafe -o requirements-dev.lock pyproject.toml`.

- Integration tests run the real CLI and HTTP stack against a fake Ghost site on `127.0.0.1`.
- CI jobs: `test` (unit, Python 3.11 and 3.12), `integration` (`db` tests against the compose database), `coverage` (unit + integration combined, branch coverage, fails under 95%), `lint`, `safety`. `main` requires all of them.
- Fixtures are rebuilt from pages saved in `data/inspect/` (git-ignored) with `python scripts/make_fixtures.py`.
- Specs live in `openspec/specs/`. The original design, low-level design and plan are in `openspec/changes/archive/2026-10-31-add-newsletter-archive/`.

**Never commit** `.env`, anything under `data/` or the archive folder, or real article text. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

The code is [MIT](LICENSE). It archives content only for the subscriber's own reading; the archived posts belong to their author and must not be shared or published.
