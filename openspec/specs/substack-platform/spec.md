# substack-platform Specification

## Purpose
How the archive reads a Substack newsletter, including one on a custom domain: which sitemap URLs are posts, how a signed-in page is recognised from the page data, how the article body, paywall and metadata are extracted, and how Substack sections become categories.

## Requirements
### Requirement: Substack discovery
For a `substack` source, discovery SHALL read `{base_url}/sitemap.xml` (a flat `urlset`, or an index that it follows) and SHALL keep only same-site URLs whose path is `/p/<slug>`, dropping `/archive`, `/about`, `/podcast` and every other page. URLs SHALL be normalized and deduplicated as in `post-discovery`. Discovery SHALL make no other requests for a Substack source.

#### Scenario: Pragmatic Engineer sitemap
- **WHEN** the sitemap lists `/archive`, `/about`, `/podcast` and 583 `/p/<slug>` URLs
- **THEN** the manifest holds exactly the 583 posts, with slugs taken from the `/p/` path

#### Scenario: Comment and off-site links
- **WHEN** the sitemap lists `/p/foo/comment/123` or a URL on another host
- **THEN** neither is kept

### Requirement: Substack sign-in check
Each fetched Substack post page SHALL be checked for a signed-in reader: the page's embedded data names a logged-in user. A page without one SHALL stop the source, MUST NOT be saved to the raw cache, and SHALL print "Your <source name> cookie looks expired. Copy a fresh Cookie header into .env (<cookie_env>)."

#### Scenario: Signed in
- **WHEN** the page's embedded data has a logged-in user
- **THEN** the page is saved and fetching continues

#### Scenario: Signed out
- **WHEN** the page's embedded data has no user
- **THEN** the source stops with the expired-cookie message, and nothing is written for that post

### Requirement: Substack request URLs
Substack posts SHALL be requested at the URL listed in the sitemap, with no trailing slash added.

#### Scenario: Post URL
- **WHEN** the manifest URL is `https://newsletter.pragmaticengineer.com/p/ai-sre`
- **THEN** the request goes to exactly that URL

### Requirement: Substack article body
The system SHALL extract only the post body (`div.body.markup` inside `div.available-content`). It MUST remove subscribe and share widgets, button rows, the paywall block, image containers including captions, embedded media, and the footer and comments.

#### Scenario: Post page with clutter
- **WHEN** a saved Substack page contains a subscribe widget, a share button row, a captioned image, an embedded video and a comment thread
- **THEN** the extracted text contains none of their text

### Requirement: Substack metadata
For a Substack post, `title`, `subtitle`, `author`, `published_date` and `summary` SHALL come from the page's meta tags and JSON-LD first, with the post title and subtitle elements as fallbacks, following the same rules as `content-extraction` Metadata extraction.

#### Scenario: Subtitle present
- **WHEN** the page has a post subtitle
- **THEN** `subtitle` holds it; otherwise it is `null`

### Requirement: Substack section categories
A Substack post's `categories` SHALL be the name of the section its own embedded post data points to (for example `["The Pulse"]`), or `[]` when the post is in no section. Sections listed in the site's navigation that the post does not belong to MUST NOT be used.

#### Scenario: Post in a section
- **WHEN** a post's embedded data names section `the-pulse`, called "The Pulse"
- **THEN** its categories are `["The Pulse"]`

#### Scenario: Post without a section
- **WHEN** a post's embedded data has no section
- **THEN** its categories are `[]`

### Requirement: Substack paywall detection
A Substack post SHALL get `status: "partial"` when its word count is below `min_word_count`, or when the body is cut off by the paywall block (the reader's plan does not include the post). Otherwise its status SHALL be `ok`.

#### Scenario: Paid subscriber reads a paid post
- **WHEN** a paid post's page holds the full body and no paywall block
- **THEN** its status is `ok`

#### Scenario: Preview only
- **WHEN** a paid post's page ends in the paywall block after a preview
- **THEN** its status is `partial`

