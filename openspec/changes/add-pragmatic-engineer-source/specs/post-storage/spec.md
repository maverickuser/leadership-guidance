## MODIFIED Requirements

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
