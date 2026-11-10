## MODIFIED Requirements

### Requirement: Index input
The indexer SHALL use archived post records as its only data source: every configured source's `{output_dir}/{source}/posts/*.json`. It SHALL index posts whose manifest status is `ok` or `partial`, all into one index. It SHALL NOT read raw HTML or fetch posts from any newsletter site. A post file that fails schema validation SHALL be skipped with a logged error, and indexing SHALL continue.

#### Scenario: Only archived posts are indexed
- **WHEN** the `main` manifest has 3 posts with status `ok`, 1 `partial` and 1 `failed`, and the `pragmatic-engineer` manifest has 2 `ok`
- **THEN** the store contains passages from exactly those 6 `ok` and `partial` posts

#### Scenario: Invalid post file
- **WHEN** one post file is not valid JSON
- **THEN** that post is reported as failed in the index summary and every other post is indexed

### Requirement: Stored passage data
For each passage the store SHALL keep the passage text, its section heading, its position in the post, and its post's `id`, `source`, `title`, `url`, `author`, `published_date`, `categories` and `word_count`, so a search result can be returned without reading the JSON files.

#### Scenario: Self-contained result
- **WHEN** a passage is found by search
- **THEN** its post's source, title, URL, date, categories and section heading are available from the store alone
