# post-discovery Specification

## Purpose
Finding every post in the newsletter, through the site's sitemap and archive pages, without fetching more than needed.
## Requirements
### Requirement: Read post URLs from the sitemap
For each source, the system SHALL read `{base_url}/sitemap.xml` and SHALL follow every nested sitemap file listed in a sitemap index until all URL entries are collected. Which URLs count as posts depends on the source's platform (Ghost below; Substack in `substack-platform`).

#### Scenario: Flat sitemap
- **WHEN** `sitemap.xml` is a `urlset` with post URLs
- **THEN** every `<loc>` URL is collected

#### Scenario: Ghost sitemap index
- **WHEN** a Ghost source's `sitemap.xml` lists `sitemap-pages.xml`, `sitemap-posts.xml`, `sitemap-authors.xml` and `sitemap-tags.xml`
- **THEN** post candidates come from `sitemap-posts.xml` only

#### Scenario: Nested sitemap index
- **WHEN** `sitemap.xml` is a `sitemapindex` pointing to child sitemaps, one of which is itself an index
- **THEN** the system fetches every child sitemap and collects the URLs from all `urlset` files

### Requirement: Keep post pages only
The system SHALL keep only post page URLs and MUST drop tag, author, category and static pages.

#### Scenario: Mixed URLs in the sitemap
- **WHEN** the sitemap contains a post URL, a `/tag/...` URL, an `/author/...` URL, a `/categories/...` URL and an about page
- **THEN** only the post URL is kept

### Requirement: Normalize and dedupe URLs
The system SHALL normalize every URL by removing tracking query parameters (such as `utm_*`, `ref`) and fragments and by removing any trailing slash, and MUST keep each normalized URL only once.

#### Scenario: Duplicate with tracking params
- **WHEN** the sitemap contains `https://www.example.com/foo/?utm_source=x#top` and `https://www.example.com/foo`
- **THEN** the manifest holds one entry with URL `https://www.example.com/foo`

### Requirement: Derive a slug per post
The system SHALL derive a stable, filesystem-safe slug from each post URL's final path segment, unique within its source. The post id SHALL be `{source}/{slug}` (see `newsletter-sources`).

#### Scenario: Slug from URL
- **WHEN** the `main` source's post URL is `https://www.example.com/why-promotions-stall/`
- **THEN** the slug is `why-promotions-stall` and the post id is `main/why-promotions-stall`

#### Scenario: Slug for a Substack source
- **WHEN** the `pragmatic-engineer` source's post URL is `https://newsletter.pragmaticengineer.com/p/ai-sre`
- **THEN** the slug is `ai-sre` and the post id is `pragmatic-engineer/ai-sre`

### Requirement: Build the post-to-categories map
For a Ghost source, the system SHALL read the category list from the `/categories/` page (links of the form `/t/<slug>`, which redirect to Ghost tag pages `/tag/<slug>/`), crawl each category listing including every page reached through `<link rel="next">` (`/tag/<slug>/page/N/`), and record which posts appear in it. The category name SHALL be the tag page's title. Category listings MUST be treated as the source of truth for a Ghost post's categories. Substack categories come from each post page instead (`substack-platform`).

#### Scenario: Categories from the categories page
- **WHEN** `/categories/` links to `/t/career-growth` and eight other `/t/` pages
- **THEN** exactly those nine categories are crawled, and tags not linked there (such as `newsletter`) are ignored

#### Scenario: Post in two categories
- **WHEN** a post appears on the `Career Growth` listing and on page 2 of the `Managing Up` listing
- **THEN** its categories are `["Career Growth", "Managing Up"]`

#### Scenario: Pagination ends
- **WHEN** a category listing page has no `<link rel="next">` or adds no new posts
- **THEN** the crawl of that category stops

#### Scenario: Post without category
- **WHEN** a post appears in no category listing
- **THEN** its categories are `[]`

### Requirement: Write the discovery manifest
The system SHALL write one manifest per source, `{source}/manifest.json`. Each manifest SHALL contain every discovered post of its source, with its `url`, `slug`, `categories` and status `discovered`. Re-running discovery MUST NOT create duplicates and MUST NOT reset the status of posts already fetched or extracted.

#### Scenario: First discovery
- **WHEN** `archive discover --source pragmatic-engineer` runs for the first time
- **THEN** `pragmatic-engineer/manifest.json` lists every post URL with its slug, categories `[]` and status `discovered`, and no other source's manifest is touched

#### Scenario: Re-discovery keeps progress
- **WHEN** `archive discover` runs again after some posts reached status `ok`
- **THEN** those posts keep status `ok`, their categories are refreshed (Ghost) or kept (Substack, where extraction sets them), and new posts are added as `discovered`

