# Newsletter Archive

A personal command-line tool that saves the posts **you subscribe to** on a Ghost-based newsletter site as clean, structured JSON on your own computer. Later it can feed search and Q&A over your archive.

- **Personal use only.** It uses your own login cookie and reads only what your subscription can read. It never bypasses the paywall.
- **Polite.** One request at a time, at least 2 seconds apart, with backoff on errors.
- **Private.** Posts are stored outside this repo (`../newsletter-archive-data/` by default) and are never committed. Your cookie stays in `.env` and is never printed or logged.

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
cp config.example.yaml config.yaml   # then set base_url to the newsletter's address
```

`config.yaml` and `.env` are git-ignored, so the site address and your cookie stay on your machine.

### Add your cookie to `.env`

1. Sign in to the newsletter site (your `base_url`) in your browser.
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
archive run                 # all of the above, in order
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

Exit codes: `0` done (individual post failures are listed in the summary), `1` config or cookie missing, or another run is already using the archive folder, `2` cookie expired, `130` interrupted (progress is saved).

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
  <key>StandardOutPath</key><string>/path/to/newsletter-archive-data/logs/scheduled.log</string>
  <key>StandardErrorPath</key><string>/path/to/newsletter-archive-data/logs/scheduled.log</string>
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
| `base_url` | none (required) | the Ghost newsletter site, e.g. `https://www.example.com` |
| `delay_seconds` | `2` | minimum gap between requests; lower values are refused |
| `max_retries` | `3` | retries on timeouts, connection errors and 5xx (2s, 4s, 8s); never on 4xx |
| `timeout_seconds` | `30` | |
| `min_word_count` | `300` | posts with fewer words, or with a paywall box in the body, are `partial` |
| `output_dir` | `../newsletter-archive-data` | relative to `config.yaml` (or the current folder if there is none), or an absolute path |
| `user_agent` | `newsletter-archive/0.1 (...)` | |

## Output

```
../newsletter-archive-data/
  manifest.json          # every post: url, slug, categories, status, last_attempt, error, content_hash
  raw/{slug}.html        # cached pages (extraction can re-run offline)
  posts/{slug}.json      # one clean record per post
  logs/run-*.log         # one log per run
```

Each `posts/{slug}.json` (schema version 1):

```json
{
  "schema_version": 1,
  "id": "example-post",
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

## Development

```bash
pip install -r requirements-dev.lock && pip install --no-deps -e .   # pinned dev setup (same as CI)
pre-commit install     # ruff, ruff-format, mypy and the safety check before each commit

pytest                 # unit + integration tests; network and DNS are blocked except loopback
pytest -m slow         # the real-clock spacing test (CI runs everything)
ruff check . && ruff format --check . && mypy   # lint, format, strict types (CI "lint" job)
python scripts/check_safety.py   # what CI runs to keep secrets and content out of the repo
```

Update the lockfile after changing dependencies: `pip-compile --extra dev --strip-extras --allow-unsafe -o requirements-dev.lock pyproject.toml`.

- Integration tests run the real CLI and HTTP stack against a fake Ghost site on `127.0.0.1`.
- Fixtures are rebuilt from pages saved in `data/inspect/` (git-ignored) with `python scripts/make_fixtures.py`.
- Specs live in `openspec/specs/`. The original design, low-level design and plan are in `openspec/changes/archive/2026-10-31-add-newsletter-archive/`.

**Never commit** `.env`, anything under `data/` or the archive folder, or real article text. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

The code is [MIT](LICENSE). It archives content only for the subscriber's own reading; the archived posts belong to their author and must not be shared or published.
