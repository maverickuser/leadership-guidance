## Why

The user pays for a newsletter and wants every post they can read (free and paid) saved locally as clean, structured JSON, so a later phase can add semantic and keyword search and LLM Q&A over the archive. Today there is no local copy, and the site's pages mix the article with navigation, comments, images and subscribe boxes.

## What Changes

- New Python CLI `archive` with commands `discover`, `fetch`, `extract`, `run` and options `--limit N`, `--refresh`, `--dry-run`, `--verbose`.
- Stage 1 (discover): read the sitemap (including nested sitemap indexes), keep post pages only, normalize URLs, and build a post → categories map by crawling the nine category (Ghost tag) listings linked from `/categories/`, with pagination. Output `data/manifest.json`.
- Stage 2 (fetch): download post HTML with the user's own session cookie, one request at a time with a delay, retries with backoff, incremental skip, raw cache in `data/raw/`, and a stop as soon as a page is not signed in.
- Stage 3 (extract): keep the article body only (no nav, comments, images, captions, subscribe/share/related widgets), split into heading-based sections, fill metadata from meta tags/JSON-LD, detect partial (paywalled) posts. No free/paid flag: the user's paid account can read every post.
- Stage 4 (store): write one schema-versioned JSON file per post to `data/posts/{slug}.json` atomically; rewrite only when `content_hash` changes; keep per-post status in the manifest.
- Config in `config.yaml`; cookie in `.env` (never committed, printed or logged); per-run log files and an end-of-run summary.
- Offline pytest suite with saved HTML fixtures; README with setup and cookie instructions.

Out of scope: chunking, embeddings, vector/keyword search, LLM Q&A, images, comments, PDF, web UI, scheduling.

## Capabilities

### New Capabilities

- `post-discovery`: sitemap parsing, post URL filtering and normalization, category map, manifest creation.
- `post-fetching`: authenticated, rate-limited, retrying HTTP fetch with raw HTML cache, incremental runs and expired-cookie detection.
- `content-extraction`: article-body cleaning, section splitting, metadata extraction, paywall (`partial`) detection.
- `post-storage`: per-post JSON schema, manifest entries, atomic writes, idempotent re-runs, folder layout.
- `archive-cli`: CLI commands and options, configuration, secret handling, logging and run summary.

### Modified Capabilities

None (new project).

## Impact

- New repository at `~/Workspace/newsletter-archive` (location may change; see design open questions).
- New dependencies: requests, trafilatura, beautifulsoup4, lxml, typer, pyyaml, python-dotenv, pytest.
- External system: https://www.example.com, accessed politely with the user's own subscription.
- Local data under `data/` (git-ignored) holding copyrighted content for personal use only.
