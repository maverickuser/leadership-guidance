## ADDED Requirements

### Requirement: CLI commands
The system SHALL provide a console command `archive` with subcommands `discover` (stage 1), `fetch` (stage 2), `extract` (stages 3 and 4) and `run` (all stages in order).

#### Scenario: Run all stages
- **WHEN** the user runs `archive run`
- **THEN** discover, fetch and extract run in that order

### Requirement: CLI options
Every subcommand SHALL accept `--limit N` (process at most N posts), `--refresh` (ignore caches and re-process), `--dry-run` and `--verbose`. `--dry-run` SHALL print what would be fetched or written without making post requests or writing post files.

#### Scenario: Limit
- **WHEN** the user runs `archive run --limit 5`
- **THEN** at most 5 posts are fetched and extracted

#### Scenario: Dry run
- **WHEN** the user runs `archive fetch --dry-run`
- **THEN** the list of posts that would be fetched is printed and no post requests are made and no files are written to `data/raw/` or `data/posts/`

### Requirement: Configuration
The system SHALL read settings from `config.yaml` (`base_url`, `delay_seconds`, `max_retries`, `timeout_seconds`, `min_word_count`, `output_dir`, `user_agent`) with the defaults given in the project spec, and SHALL read `SCARLETINK_COOKIE` from `.env`. The repository SHALL include `config.yaml`, `.env.example` and a `.gitignore` that lists `.env` and `data/`.

#### Scenario: Custom delay
- **WHEN** `config.yaml` sets `delay_seconds: 5`
- **THEN** requests are at least 5 seconds apart

### Requirement: Secret handling
The cookie MUST NOT appear in any output file, log, console message or git commit, and full request headers MUST NOT be logged.

#### Scenario: Verbose run
- **WHEN** `archive run --limit 5 --verbose` completes
- **THEN** searching `data/` and console output for the cookie value finds nothing

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
