# post-fetching Specification

## Purpose
TBD - created by archiving change add-newsletter-archive. Update Purpose after archive.
## Requirements
### Requirement: Authenticate with the user's cookie
The system SHALL send the value of `NEWSLETTER_COOKIE` from `.env` as the `Cookie` header and the configured `user_agent` as `User-Agent` on every request to the site. The system MUST NOT attempt to bypass the paywall by any other means.

#### Scenario: Cookie present
- **WHEN** `NEWSLETTER_COOKIE` is set
- **THEN** each post request carries that value in the `Cookie` header

#### Scenario: Cookie missing
- **WHEN** `archive fetch` runs and `NEWSLETTER_COOKIE` is empty or unset
- **THEN** the command exits non-zero with a message telling the user to fill in `.env`, and makes no requests

### Requirement: Polite rate limiting
The system SHALL make at most one request at a time and SHALL wait at least `delay_seconds` (default 2) between consecutive requests, including sitemap and category requests.

#### Scenario: Consecutive requests
- **WHEN** two posts are fetched one after the other
- **THEN** the second request starts at least `delay_seconds` after the first one finished

### Requirement: Timeout and retries
Each request SHALL time out after `timeout_seconds` (default 30). The system SHALL retry up to `max_retries` (default 3) times with 2s, 4s and 8s backoff on timeouts, connection errors and 5xx responses, and MUST NOT retry on 4xx responses.

#### Scenario: Transient server error
- **WHEN** a request returns 503 twice and then 200
- **THEN** the system waits 2s then 4s, and the post is fetched successfully

#### Scenario: Retries exhausted
- **WHEN** a request fails with a connection error 4 times
- **THEN** the post is marked `failed` with the error and the run continues with the next post

#### Scenario: Client error
- **WHEN** a request returns 404
- **THEN** the post is marked `failed` without any retry

### Requirement: Raw HTML cache
The system SHALL save each fetched page to `data/raw/{slug}.html` so extraction can run offline, and SHALL set the post's manifest status to `fetched`.

#### Scenario: Successful fetch
- **WHEN** a post is fetched with status 200
- **THEN** `data/raw/{slug}.html` holds the response body and the manifest status is `fetched` with `last_attempt` set

### Requirement: Incremental fetching
The system SHALL skip posts whose raw HTML already exists unless `--refresh` is passed.

#### Scenario: Second run
- **WHEN** `archive fetch` runs again with no new posts in the manifest
- **THEN** no post requests are made

#### Scenario: Refresh
- **WHEN** `archive fetch --refresh` runs
- **THEN** every selected post is downloaded again

### Requirement: Stop when not logged in
Every fetched post page SHALL be checked for the signed-in marker `data-portal="account"`. If it is missing, the system SHALL stop the run, MUST NOT save that page to the raw cache, and SHALL print: "Your cookie looks expired. Copy a fresh Cookie header into .env."

#### Scenario: Expired cookie on first post
- **WHEN** the first fetched post page has no `data-portal="account"` (for example it shows `data-portal="signin"`)
- **THEN** the run stops with a non-zero exit code and the expired-cookie message, and nothing is written for that post

#### Scenario: Cookie expires mid-run
- **WHEN** posts 1–40 are signed in and post 41 is not
- **THEN** posts 1–40 are kept, the run stops at post 41, and post 41 stays `discovered`

#### Scenario: Valid cookie
- **WHEN** a fetched post page contains `data-portal="account"`
- **THEN** the page is saved and the run continues

### Requirement: Fetch canonical URLs without extra redirects
The system SHALL request each post at its canonical form with a trailing slash, as listed in the sitemap, so the site does not answer with a redirect. Any redirect that does happen SHALL be followed and counted within the same rate-limited request slot.

#### Scenario: Normalized URL in manifest
- **WHEN** the manifest URL is `https://www.example.com/foo`
- **THEN** the request goes to `https://www.example.com/foo/`

