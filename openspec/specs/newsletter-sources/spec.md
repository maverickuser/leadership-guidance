# newsletter-sources Specification

## Purpose
The archive holds several newsletters at once. Each is a `sources:` entry in config.yaml with its own platform, site, cookie and folder; post ids are `<source>/<slug>`; runs, the weekly job and search cover every newsletter unless one is chosen; and an archive made before this layout is moved once with `archive migrate`.

## Requirements
### Requirement: Source list
`config.yaml` SHALL hold a non-empty `sources` list, one entry per newsletter. Each source SHALL have:
- `id`: lowercase letters, digits and hyphens, unique.
- `platform`: `ghost` or `substack`.
- `base_url`.
- `cookie_env`: the `.env` variable that holds its cookie.

It MAY also have a display `name` (default the id) and `boilerplate_prefixes`. An id SHALL NOT be a name used for shared items in `output_dir` (`postgres`, `models`, `logs`, or a file name). Global settings (`delay_seconds`, `max_retries`, `timeout_seconds`, `min_word_count`, `output_dir`, `user_agent`, `search`) SHALL apply to every source.

#### Scenario: Two newsletters
- **WHEN** `config.yaml` lists a `ghost` source `main` and a `substack` source `pragmatic-engineer`
- **THEN** both load, each with its own base URL and cookie variable

#### Scenario: Duplicate id
- **WHEN** two sources have the id `main`
- **THEN** every command exits 1 with a config error naming the duplicate id

#### Scenario: Reserved id
- **WHEN** a source has the id `logs`
- **THEN** every command exits 1 with a config error listing the reserved names

#### Scenario: Unknown platform
- **WHEN** a source has `platform: wordpress`
- **THEN** every command exits 1 with a config error listing `ghost` and `substack`

#### Scenario: Old single-site config
- **WHEN** `config.yaml` has a top-level `base_url` and no `sources`
- **THEN** every command exits 1 with a message showing the equivalent `sources:` entry to paste in (`platform: ghost`, `cookie_env: NEWSLETTER_COOKIE`, and an id to choose)

### Requirement: One folder per newsletter
Each source SHALL keep all of its own data in `{output_dir}/{id}/`: `manifest.json`, `raw/{slug}.html` and `posts/{slug}.json`. Data shared by all sources SHALL stay at the top of `output_dir`: `postgres/`, `models/`, `logs/`, `eval-queries.yaml` and `eval-judgments.yaml`. A source's stages SHALL never read or write another source's folder. The default `output_dir` SHALL be `../leadership-coach-datasets`.

#### Scenario: Layout after a run
- **WHEN** `archive run --limit 5` completes with sources `main` and `pragmatic-engineer`
- **THEN** `main/` and `pragmatic-engineer/` each hold a `manifest.json` and up to 5 files in `raw/` and in `posts/`, and `logs/` holds the run log

### Requirement: Post id
A post's id SHALL be `{id}/{slug}`, where the slug is unique within its source. The id SHALL be used in post files, the search store, search results and the eval files.

#### Scenario: Same slug in two newsletters
- **WHEN** both newsletters publish a post with the slug `on-call`
- **THEN** the archive holds `main/on-call` and `pragmatic-engineer/on-call` as two posts

### Requirement: Choosing sources
`discover`, `fetch`, `extract` and `run` SHALL accept `--source <id>`, repeatable. Without it, they SHALL process every source in config order. Requests to all sources SHALL share one rate limiter, so there is never more than one request in flight and requests are at least `delay_seconds` apart across sources. `--limit N` SHALL apply per source. An unknown id SHALL be rejected with the list of valid ids, before any request is made.

#### Scenario: One source
- **WHEN** the user runs `archive run --source pragmatic-engineer --limit 5`
- **THEN** only that source is discovered, fetched and extracted, for at most 5 posts; the index is updated; and the other newsletter's folder is untouched

#### Scenario: All sources
- **WHEN** the user runs `archive fetch` with two sources configured
- **THEN** the first source's posts are fetched, then the second's, and no two requests are less than `delay_seconds` apart

#### Scenario: Unknown source
- **WHEN** the user runs `archive fetch --source nope`
- **THEN** the command exits 1 listing the valid source ids, and makes no requests

### Requirement: One source failing
A stop condition in one source (expired cookie, missing cookie, discovery failure) SHALL stop that source only. The run SHALL continue with the next source and SHALL exit non-zero at the end, naming each source that stopped and why.

#### Scenario: One cookie expired
- **WHEN** `archive run` finds the `pragmatic-engineer` cookie expired at its first post
- **THEN** that source stops with the expired-cookie message naming `PRAGMATIC_ENGINEER_COOKIE`, the `main` source still runs, and the command exits non-zero

### Requirement: Move the existing data once
`archive migrate --source <id>` SHALL move a single-newsletter archive (top-level `manifest.json`, `raw/*.html`, `posts/*.json`) into `{output_dir}/{id}/`. It SHALL:
- rewrite each post file to `schema_version` 2, with `source` set and `id` set to `{id}/{slug}`;
- prefix every post id in `eval-queries.yaml` and `eval-judgments.yaml` with `{id}/`;
- leave `postgres/`, `models/` and `logs/` where they are.

Every file SHALL be written atomically, and the old files removed only after all new files exist and validate. `--dry-run` SHALL print what would move without changing anything. Running it again on an already moved archive SHALL change nothing. The command SHALL end by telling the user to run `archive index`. Renaming the parent folder is a separate, documented manual step (see "Setup documentation").

#### Scenario: Move the current archive
- **WHEN** the archive holds 253 posts in the old layout and the user runs `archive migrate --source main`
- **THEN** `main/posts/` holds 253 version-2 post files, `main/manifest.json` and `main/raw/` exist, the old top-level `manifest.json`, `raw/` and `posts/` are gone, the eval files reference `main/<slug>` ids, and `postgres/`, `models/` and `logs/` are untouched

#### Scenario: Interrupted move
- **WHEN** the move is killed after writing half the new files
- **THEN** every old file still exists, and running the command again completes the move

#### Scenario: Dry run
- **WHEN** the user runs `archive migrate --source main --dry-run`
- **THEN** the counts and example paths of what would move are printed, and no file changes

#### Scenario: Old layout detected
- **WHEN** any other command finds a top-level `manifest.json` in `output_dir`
- **THEN** it exits 1 telling the user to run `archive migrate --source <id>`

### Requirement: Weekly run covers every source
`scripts/scheduled-run.sh` SHALL keep running a single `archive run`, which updates every configured source and then the search index once. Its macOS notification SHALL name each source whose login expired and the `.env` variable to fix (for example "The Pragmatic Engineer: your cookie looks expired. Paste a fresh Cookie header into .env (PRAGMATIC_ENGINEER_COOKIE)."). It SHALL count new posts across all sources. No new launchd job SHALL be needed.

#### Scenario: Both sources updated
- **WHEN** the weekly job runs and each source has 1 new post
- **THEN** both posts are archived and indexed in that one run, and the notification says "Archived 2 new post(s)."

#### Scenario: One login expired in the weekly run
- **WHEN** the weekly job runs and only The Pragmatic Engineer's cookie has expired
- **THEN** the other newsletter's new posts are archived and indexed, and the notification names The Pragmatic Engineer and `PRAGMATIC_ENGINEER_COOKIE`

### Requirement: Setup documentation
The README SHALL explain moving an existing archive to the new layout, in order:
1. Back up the data folder.
2. Stop the database (`docker compose down`).
3. Rename the folder to `leadership-coach-datasets`.
4. Update `output_dir`, `ARCHIVE_DB_DATA` (if set) and the launchd log path (if set).
5. Convert `config.yaml` to the `sources` list.
6. Run `archive migrate --source <id>` (dry run first).
7. Start the database and run `archive index`.

The README SHALL also explain adding a newsletter:
- copying its cookie into `.env` and adding it to `sources`;
- a first trial run with `archive run --source <id> --limit 5`;
- checking `archive index --dry-run` for the cost before the full load;
- that the weekly job then covers it.

It SHALL also tell the user to check each publisher's `robots.txt` content signals, and to use a Gemini API plan that does not use inputs for training when a publisher declares `ai-train=no`.

#### Scenario: Following the README
- **WHEN** the user follows the README to move the existing archive and add The Pragmatic Engineer
- **THEN** the existing posts are searchable under their new ids with no Gemini spend, the trial archives 5 Pragmatic Engineer posts, the dry run shows the indexing cost before any Gemini call, and the next weekly run includes both newsletters

