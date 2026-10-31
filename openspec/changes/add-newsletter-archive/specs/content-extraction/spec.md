## ADDED Requirements

### Requirement: Article body only
The system SHALL extract only the article body (on this Ghost site, the `.gh-content` element inside `article.gh-article`) and MUST remove the paywall call-to-action box (`.gh-post-upgrade-cta`), navigation, header, footer, subscribe or upgrade boxes, share buttons, related-post widgets, comments, and images including captions and alt text.

#### Scenario: Post page with clutter
- **WHEN** a saved post page contains a nav bar, a subscribe box, share buttons, a related-posts widget, a comment thread and a figure with a caption
- **THEN** the extracted text contains none of the nav, subscribe, share, related, comment or caption text, and no image alt text

### Requirement: Strip newsletter boilerplate
The system SHALL remove the standard newsletter intro paragraphs from the top of the article body: leading paragraphs whose text starts with "Welcome to my newsletter!" or "Free members can read". It MUST stop at the first paragraph that does not match, and MUST NOT remove matching text elsewhere in the body.

#### Scenario: Standard intro
- **WHEN** a post body opens with the "Welcome to my newsletter!" and "Free members can read" paragraphs
- **THEN** neither paragraph, nor any link inside them, appears in `text`, `sections` or `links`

#### Scenario: Post-specific italic opener
- **WHEN** a post opens with its own italic note such as "I'm writing about something different today!"
- **THEN** that note is kept

### Requirement: Heading-based sections
The system SHALL preserve heading structure and split the body into `sections`, each with `heading`, `level` and `text`. Text before the first heading MUST become a section with `heading: null` and `level: 0`. Empty sections MUST be dropped.

#### Scenario: Intro and two headings
- **WHEN** a post has an intro paragraph, an `<h2>Why promotions stall</h2>` and an `<h3>Example</h3>`
- **THEN** sections are `[{null, 0, intro}, {"Why promotions stall", 2, ...}, {"Example", 3, ...}]`

### Requirement: Text equals joined sections
The post `text` SHALL equal the `sections[].text` values joined with `"\n\n"`.

#### Scenario: Invariant holds
- **WHEN** any post is extracted
- **THEN** `"\n\n".join(s["text"] for s in sections) == text`

### Requirement: Metadata extraction
The system SHALL fill `title`, `subtitle`, `author`, `published_date` and `summary` from page meta tags (`og:*`, `article:*`, JSON-LD) first, and SHALL fall back to visible elements such as `<h1>` when a meta value is missing. `summary` SHALL be the meta description, or the first ~2 sentences of `text` when there is none. `subtitle` SHALL be `null` when absent.

#### Scenario: Meta tags present
- **WHEN** a page has `og:title`, `article:published_time` and JSON-LD author
- **THEN** those values fill `title`, `published_date` (as `YYYY-MM-DD`) and `author`

#### Scenario: Missing og:title
- **WHEN** a page has no `og:title` or JSON-LD headline
- **THEN** `title` comes from the page's `<h1>`

### Requirement: In-body links
The system SHALL collect links inside the article body only, each as `{text, url}` with absolute URLs.

#### Scenario: Links in body and footer
- **WHEN** the body has two links and the footer has five
- **THEN** `links` holds only the two body links

### Requirement: Paywall detection
The system SHALL compute `word_count` from `text`, and SHALL set `status: "partial"` when `word_count < min_word_count` (default 300); otherwise `status: "ok"`.

#### Scenario: Truncated paid post
- **WHEN** a paid post's extracted text has 120 words
- **THEN** its status is `partial`

#### Scenario: Full post
- **WHEN** a post's extracted text has 2140 words
- **THEN** its status is `ok` and `word_count` is 2140

### Requirement: Offline re-extraction
Extraction SHALL read only from `data/raw/` and MUST NOT make network requests.

#### Scenario: Extract without network
- **WHEN** `archive extract` runs with networking disabled and raw HTML cached
- **THEN** it completes and writes post JSON files
