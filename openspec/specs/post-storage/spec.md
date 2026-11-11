# post-storage Specification

## Purpose
Where and how posts are kept on disk: the raw pages, the extracted Markdown, the manifest and the run logs, all in the private data folder.
## Requirements
### Requirement: Folder layout
The system SHALL store each source's data in `{output_dir}/{source}/`: `manifest.json`, `raw/{slug}.html` and `posts/{slug}.json`. Run logs (`logs/run-{timestamp}.log`) are shared by all sources at the top of `output_dir`.

#### Scenario: After a run
- **WHEN** `archive run --source main --limit 5` completes
- **THEN** `main/manifest.json`, 5 files in `main/raw/`, 5 files in `main/posts/` and one file in `logs/` exist

### Requirement: Post JSON schema
Each `{source}/posts/{slug}.json` SHALL contain exactly these fields:
- `schema_version` (2)
- `id` (`{source}/{slug}`)
- `source` (the source id)
- `url`, `title`, `subtitle` (string or null), `author`
- `published_date` (`YYYY-MM-DD`)
- `categories` (list of strings), `word_count` (int), `summary`
- `sections` (list of `{heading: string|null, level: int, text: string}`), `text`
- `links` (list of `{text, url}`)
- `content_hash` (sha256 hex of `text`)
- `fetched_at` (UTC ISO 8601)
- `status` (`ok` | `partial`)

Every file MUST validate against this schema. Version-1 files (the old layout) SHALL be accepted only by `archive migrate`.

#### Scenario: Valid output
- **WHEN** a post is stored
- **THEN** its JSON validates against the schema, `content_hash == sha256(text).hexdigest()`, and `id` equals its folder's source id, a `/`, and its file name without `.json`

### Requirement: Manifest entries
Each entry in a source's `manifest.json` SHALL contain `url`, `slug`, `categories`, `status` (`discovered` | `fetched` | `ok` | `partial` | `failed`), `last_attempt` (UTC ISO 8601 or null), `error` (string or null) and `content_hash` (string or null). For a Substack source, `categories` SHALL be updated at extraction from the post page.

#### Scenario: Failed post
- **WHEN** a post fetch fails with a 404
- **THEN** its manifest entry has `status: "failed"`, an `error` describing the 404, and `last_attempt` set

### Requirement: Atomic writes
The system SHALL write post JSON files and `manifest.json` atomically by writing a temp file in the same directory and renaming it over the target.

#### Scenario: Interrupted write
- **WHEN** the process is killed while writing a post file
- **THEN** the previous version of the file (or no file) remains, never a half-written file

### Requirement: Idempotent re-runs
Re-runs SHALL create no duplicate entries, and a post's JSON SHALL be rewritten only when its `content_hash` or its `categories` change (or `--refresh` is passed).

#### Scenario: Unchanged post
- **WHEN** `archive extract` runs twice on the same raw HTML
- **THEN** the second run does not rewrite the post file (its modification time is unchanged)

#### Scenario: Changed post
- **WHEN** a refreshed raw HTML yields different text
- **THEN** the post file is rewritten with the new `content_hash` and the manifest is updated

#### Scenario: Categories changed, text unchanged
- **WHEN** re-discovery adds a category to a post whose text is unchanged and `archive extract` runs
- **THEN** the post file is rewritten with the new `categories` and the same `content_hash`

