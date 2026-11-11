# archive-cli Specification

## Purpose
The `archive` command line: running the archive's stages (discover, fetch, extract, index) together or one at a time, with limits, dry runs and a clear end-of-run report.
## Requirements
### Requirement: CLI commands
The system SHALL provide a console command `archive` with these subcommands:
- `discover` (stage 1);
- `fetch` (stage 2);
- `extract` (stages 3 and 4);
- `index` (build or update the search store);
- `mcp` (start the local MCP search server);
- `run` (discover, fetch, extract and index, in that order);
- `migrate` (move a single-newsletter archive into its own folder, see `newsletter-sources`).

Search itself SHALL be available only through the MCP server. If the index step of `run` fails, the posts already saved SHALL be kept and `run` SHALL exit with code 3 and a message that search was not updated.

#### Scenario: Run all stages
- **WHEN** the user runs `archive run`
- **THEN** discover, fetch and extract run for every configured source, and then index runs once

#### Scenario: Index failure in run
- **WHEN** `archive run` saves 2 new posts and then the index step fails
- **THEN** the 2 post files exist, the previous store is intact, and the exit code is 3

### Requirement: CLI options
The subcommands `discover`, `fetch`, `extract`, `index` and `run` SHALL accept:
- `--limit N`: process at most N posts, per source.
- `--refresh`: ignore caches and re-process.
- `--dry-run`: print what would be fetched or written, without making post requests or embedding calls, and without writing post or store files.
- `--verbose`.

`discover`, `fetch`, `extract` and `run` SHALL also accept `--source <id>` (repeatable; default every source). `mcp` SHALL accept `--config` and `--verbose`. `migrate` SHALL accept `--source <id>` (required), `--dry-run` and `--verbose`.

#### Scenario: Limit
- **WHEN** the user runs `archive run --limit 5` with one source configured
- **THEN** at most 5 posts are fetched and extracted

#### Scenario: Dry run
- **WHEN** the user runs `archive fetch --dry-run`
- **THEN** the list of posts that would be fetched is printed for each source, no post requests are made, and no post files are written

#### Scenario: Source option
- **WHEN** the user runs `archive extract --source pragmatic-engineer`
- **THEN** only `pragmatic-engineer/raw/` is read and only `pragmatic-engineer/posts/` is written

### Requirement: Configuration
The system SHALL read these settings from `config.yaml`:
- **`sources`:** the list of newsletters (see `newsletter-sources`).
- **Global settings:** `delay_seconds`, `max_retries`, `timeout_seconds`, `min_word_count`, `output_dir` (default `../leadership-coach-datasets`), `user_agent`.
- **`search`:** an optional section with `embedding_model` (default `gemini-embedding-2`), `embedding_dimension`, `chunk_words`, `chunk_overlap_words`, `candidates`, re-ranker settings, `source_min_score` (the relevance a source's best result needs to be guaranteed a place, see `hybrid-search`), and a `summaries` sub-section with `enabled` (default true), `model` and `max_tokens_per_run`.

Defaults SHALL be those given in the project spec. The system SHALL read each source's cookie from the `.env` variable named by its `cookie_env`, and `GEMINI_API_KEY`, all from `.env`.

These invalid settings SHALL be rejected with a config error:
- non-positive numbers;
- an overlap not smaller than `chunk_words`;
- a missing, empty or invalid `sources` list;
- a top-level `base_url`.

The repository SHALL include `config.example.yaml` (with one example source per platform), `.env.example`, and a `.gitignore` that lists `.env`, `config.yaml` and `data/`.

#### Scenario: Custom delay
- **WHEN** `config.yaml` sets `delay_seconds: 5`
- **THEN** requests are at least 5 seconds apart, across all sources

#### Scenario: Invalid chunk overlap
- **WHEN** `config.yaml` sets `search.chunk_words: 100` and `search.chunk_overlap_words: 100`
- **THEN** the command exits 1 with a config error naming `chunk_overlap_words`

### Requirement: Secret handling
Every source's cookie and every API key (including `GEMINI_API_KEY`) MUST NOT appear in any output file, log, console message, MCP response, error message or git commit, and full request headers MUST NOT be logged. Cookies and API keys SHALL be held in the same never-printed secret wrapper and SHALL be read only from `.env` or the environment. A source's cookie SHALL be sent only to that source's own origin.

#### Scenario: Verbose run
- **WHEN** `archive run --limit 5 --verbose` completes with two sources
- **THEN** searching the data folder and console output for either cookie value finds nothing

#### Scenario: Cookie stays with its source
- **WHEN** posts are fetched from both sources
- **THEN** each request carries only its own source's cookie, and no request to one source carries the other's

#### Scenario: Key never exposed
- **WHEN** `archive index --verbose` and an MCP search both complete or fail
- **THEN** searching the data folder, the logs, console output and MCP responses for the Gemini API key finds nothing

### Requirement: Per-post logging and summary
The system SHALL log one line per post with slug, stage, status and error if any, to the console and to `data/logs/run-{timestamp}.log`, and SHALL print an end-of-run summary with discovered, fetched, ok, partial and failed counts.

#### Scenario: End of run
- **WHEN** a run finishes
- **THEN** the summary shows counts for discovered, fetched, ok, partial and failed

### Requirement: Offline test suite
The project SHALL include a pytest suite that runs without network access, using saved HTML fixtures and a mocked HTTP layer, covering sitemap parsing (including nested index), URL filtering and normalization, category map with pagination, extraction cleanliness and section/text invariant, paywall detection and expired-cookie stop, retry/backoff, idempotency, and JSON schema validation.

#### Scenario: No network
- **WHEN** `pytest` runs with networking blocked
- **THEN** all tests pass

### Requirement: README
The project SHALL include a `README.md` covering setup, how to copy the Cookie header from the browser (DevTools → Network → any request → Request Headers → `cookie`), the commands, and the output format.

#### Scenario: New user setup
- **WHEN** a user follows the README
- **THEN** they can install the tool, fill in `.env` and run `archive run --limit 5`

### Requirement: Search tests and documentation
The offline test suite SHALL cover passage splitting, keyword ranking, safe query handling, combining results, re-ranking order, filters, result content, incremental indexing, API error handling and the MCP `search` tool, using fake embedding and re-ranking clients so that no API is called and no model is downloaded in tests or CI. The README SHALL document `archive index`, `archive mcp`, how to register the server in Claude Code and Claude Desktop, the Gemini API key, what data is sent to the Gemini API, and the search settings.

#### Scenario: Tests need no API
- **WHEN** `pytest` runs with networking blocked and no API keys set
- **THEN** all search and MCP tests pass

