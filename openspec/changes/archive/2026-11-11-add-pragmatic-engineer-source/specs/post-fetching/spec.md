## MODIFIED Requirements

### Requirement: Authenticate with the user's cookie
The system SHALL send the value of the source's `cookie_env` variable from `.env` as the `Cookie` header, and the configured `user_agent` as `User-Agent`, on every request to that source's site, and to no other origin. The system MUST NOT attempt to bypass the paywall by any other means.

#### Scenario: Cookie present
- **WHEN** `NEWSLETTER_COOKIE` is set for the `main` source
- **THEN** each `main` post request carries that value in the `Cookie` header

#### Scenario: Cookie missing
- **WHEN** `archive fetch` runs and a source's cookie variable is empty or unset
- **THEN** that source is skipped with a message telling the user to fill in that variable in `.env`, no requests are made to it, and the command exits non-zero at the end

### Requirement: Stop when not logged in
Every fetched post page SHALL be checked for its platform's signed-in marker: for Ghost, `data-portal="account"`; for Substack, see `substack-platform`. If the marker is missing, the system SHALL stop that source, MUST NOT save that page to the raw cache, and SHALL print: "Your <source name> cookie looks expired. Copy a fresh Cookie header into .env (<cookie_env>)."

#### Scenario: Expired cookie on first post
- **WHEN** the first fetched Ghost post page has no `data-portal="account"` (for example it shows `data-portal="signin"`)
- **THEN** the source stops with the expired-cookie message, nothing is written for that post, and the command exits non-zero

#### Scenario: Cookie expires mid-run
- **WHEN** posts 1–40 are signed in and post 41 is not
- **THEN** posts 1–40 are kept, the source stops at post 41, and post 41 stays `discovered`

#### Scenario: Valid cookie
- **WHEN** a fetched Ghost post page contains `data-portal="account"`
- **THEN** the page is saved and the run continues

### Requirement: Fetch canonical URLs without extra redirects
The system SHALL request each post at its canonical form, so the site does not answer with a redirect: with a trailing slash for Ghost sources, and exactly as listed in the sitemap for Substack sources. Any redirect that does happen SHALL be followed and counted within the same rate-limited request slot.

#### Scenario: Normalized Ghost URL in manifest
- **WHEN** a Ghost source's manifest URL is `https://www.example.com/foo`
- **THEN** the request goes to `https://www.example.com/foo/`
