## MODIFIED Requirements

### Requirement: CLI commands
The system SHALL provide a console command `archive` with subcommands `discover` (stage 1), `fetch` (stage 2), `extract` (stages 3 and 4), `index` (build or update the search store), `mcp` (start the local MCP search server) and `run` (discover, fetch, extract and index, in that order). Search itself SHALL be available only through the MCP server. If the index step of `run` fails, the posts already saved SHALL be kept and `run` SHALL exit with code 3 and a message that search was not updated.

#### Scenario: Run all stages
- **WHEN** the user runs `archive run`
- **THEN** discover, fetch, extract and index run in that order

#### Scenario: Index failure in run
- **WHEN** `archive run` saves 2 new posts and then the index step fails
- **THEN** the 2 post files exist, the previous store is intact, and the exit code is 3

### Requirement: CLI options
The subcommands `discover`, `fetch`, `extract`, `index` and `run` SHALL accept `--limit N` (process at most N posts), `--refresh` (ignore caches and re-process), `--dry-run` and `--verbose`. `--dry-run` SHALL print what would be fetched or written without making post requests, embedding calls, or writing post or store files. `mcp` SHALL accept `--config` and `--verbose`.

#### Scenario: Limit
- **WHEN** the user runs `archive run --limit 5`
- **THEN** at most 5 posts are fetched and extracted

#### Scenario: Dry run
- **WHEN** the user runs `archive fetch --dry-run`
- **THEN** the list of posts that would be fetched is printed and no post requests are made and no files are written to `data/raw/` or `data/posts/`

### Requirement: Configuration
The system SHALL read settings from `config.yaml` (`base_url`, `delay_seconds`, `max_retries`, `timeout_seconds`, `min_word_count`, `output_dir`, `user_agent`, and an optional `search` section with `embedding_model` (default `gemini-embedding-2`), `embedding_dimension`, `chunk_words`, `chunk_overlap_words`, `candidates`, re-ranker settings and a `summaries` sub-section with `enabled` (default true), `model` and `max_tokens_per_run`) with the defaults given in the project spec, and SHALL read `NEWSLETTER_COOKIE` and `GEMINI_API_KEY` from `.env`. Invalid search settings (non-positive numbers, overlap not smaller than `chunk_words`) SHALL be rejected with a config error. The repository SHALL include `config.example.yaml`, `.env.example` and a `.gitignore` that lists `.env`, `config.yaml` and `data/`.

#### Scenario: Custom delay
- **WHEN** `config.yaml` sets `delay_seconds: 5`
- **THEN** requests are at least 5 seconds apart

#### Scenario: Invalid chunk overlap
- **WHEN** `config.yaml` sets `search.chunk_words: 100` and `search.chunk_overlap_words: 100`
- **THEN** the command exits 1 with a config error naming `chunk_overlap_words`

### Requirement: Secret handling
The cookie and every API key (including `GEMINI_API_KEY`) MUST NOT appear in any output file, log, console message, MCP response, error message or git commit, and full request headers MUST NOT be logged. API keys SHALL be held in the same never-printed secret wrapper as the cookie and SHALL be read only from `.env` or the environment.

#### Scenario: Verbose run
- **WHEN** `archive run --limit 5 --verbose` completes
- **THEN** searching `data/` and console output for the cookie value finds nothing

#### Scenario: Key never exposed
- **WHEN** `archive index --verbose` and an MCP search both complete or fail
- **THEN** searching `data/`, the logs, console output and MCP responses for the Gemini API key finds nothing

## ADDED Requirements

### Requirement: Search tests and documentation
The offline test suite SHALL cover passage splitting, keyword ranking, safe query handling, combining results, re-ranking order, filters, result content, incremental indexing, API error handling and the MCP `search` tool, using fake embedding and re-ranking clients so that no API is called and no model is downloaded in tests or CI. The README SHALL document `archive index`, `archive mcp`, how to register the server in Claude Code and Claude Desktop, the Gemini API key, what data is sent to the Gemini API, and the search settings.

#### Scenario: Tests need no API
- **WHEN** `pytest` runs with networking blocked and no API keys set
- **THEN** all search and MCP tests pass
