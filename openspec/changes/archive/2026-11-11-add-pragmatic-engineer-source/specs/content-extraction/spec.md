## MODIFIED Requirements

### Requirement: Article body only
For a Ghost source, the system SHALL extract only the article body (the `.gh-content` element inside `article.gh-article`). It MUST remove the paywall call-to-action box (`.gh-post-upgrade-cta`), navigation, header, footer, subscribe or upgrade boxes, share buttons, related-post widgets, comments, and images including captions and alt text. Substack bodies follow `substack-platform`.

#### Scenario: Post page with clutter
- **WHEN** a saved Ghost post page contains a nav bar, a subscribe box, share buttons, a related-posts widget, a comment thread and a figure with a caption
- **THEN** the extracted text contains none of the nav, subscribe, share, related, comment or caption text, and no image alt text

### Requirement: Paywall detection
The system SHALL compute `word_count` from `text`. For a Ghost source it SHALL set `status: "partial"` when `word_count < min_word_count` (default 300) or when the paywall box (`.gh-post-upgrade-cta`) is present inside the article body; otherwise `status: "ok"`. Substack paywall detection follows `substack-platform`.

#### Scenario: Truncated paid post
- **WHEN** a paid Ghost post's extracted text has 120 words
- **THEN** its status is `partial`

#### Scenario: Paywall box inside the body
- **WHEN** a signed-in Ghost page shows a 700-word preview followed by the `.gh-post-upgrade-cta` box inside `.gh-content` (for example a free member reading a paid post)
- **THEN** its status is `partial`

#### Scenario: Paywall class only in page CSS
- **WHEN** the page's `<style>` mentions `.gh-post-upgrade-cta` but the article body has no such element
- **THEN** the status is decided by word count alone

#### Scenario: Full post
- **WHEN** a post's extracted text has 2140 words
- **THEN** its status is `ok` and `word_count` is 2140
