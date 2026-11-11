## Why

The archive holds one newsletter. The user also pays for The Pragmatic Engineer (a Substack newsletter at `https://newsletter.pragmaticengineer.com`, 583 posts in its sitemap) and wants to ask one question and get relevant answers from both. The existing archive must keep working as it does today, with every newsletter's data kept in its own folder under one parent folder.

## What Changes

Requirements agreed with the user (2026-11-10; requirement 1 and the data layout revised the same day):

1. **Existing newsletter keeps working, its data moved once:**
   - **Unchanged:** its posts, login and behaviour.
   - **Moved:** its data moves once into its own folder. Its post ids become `<source>/<slug>`, and the ids in the eval files are rewritten to match.
   - **The move:** done by `archive migrate`, which has a dry run and is safe to rerun, after a documented backup and folder rename.
2. **The Pragmatic Engineer added** as a second newsletter, read with the user's paid login (its own cookie in `.env`).
3. **One parent folder, one subfolder per newsletter.** The data folder (default `../leadership-coach-datasets`, replacing `../newsletter-archive-data`) holds:
   - **One folder per newsletter,** named by its source id, containing `manifest.json`, `raw/` (pages as downloaded) and `posts/` (cleaned articles).
   - **Shared items at the parent level:** `postgres/`, `models/`, `logs/` and the eval files.
   - **Example:** the committed docs use the example id `main` for the existing newsletter; the user's local config names the real one.
4. **One-time full load:**
   - **Scope:** all 583 posts.
   - **Time:** about 20 minutes of fetching at one request every 2 seconds.
   - **Cost:** roughly $3–6 of Gemini to index.
   - **Order:** a 5-post trial first, and the indexing cost is shown before anything is spent.
5. **One weekly job:** the existing scheduled run (`scripts/scheduled-run.sh`) updates both newsletters, then search, in one go. Nothing new to set up in launchd.
6. **One search, mixed by default:**
   - **Ranking:** relevance across both newsletters.
   - **Both shown:** when both newsletters have a relevant match, each appears in the results, so neither can crowd the other out. When only one has anything relevant, only that one shows.
   - **Labelled:** each result names its newsletter.
   - **Narrowing:** the user can limit a search to one newsletter.
7. **Search only:** the new posts are archived for search; no other use is planned.
8. **Expired login:** if a newsletter's login expires, the notice names that newsletter and the `.env` line to fix, and the other newsletter still updates.
9. **Open to more newsletters later:** adding a third newsletter should be mostly a config entry, plus support for its platform if it isn't Ghost or Substack.
10. **Same rules as today:**
   - **Polite:** at least 2 seconds between requests, across both sites together, one request at a time.
   - **Own logins only:** no paywall workarounds, and logins never printed or logged.
   - **Publisher signals:** The Pragmatic Engineer's `robots.txt` says `ai-input=yes, ai-train=no`. The user's Gemini plan is paid, so its posts aren't used for training, and they can be embedded and summarised.

## Capabilities

### New Capabilities
- `newsletter-sources`: a list of newsletters, each with its own login and folder under one parent folder; the one-time move of the existing data; the weekly run covering all of them; and per-newsletter login-expiry notices.
- `substack-platform`: reading a Substack newsletter: which pages are posts, the signed-in check, the article text, categories from its sections, and paywall detection.

### Modified Capabilities
- `archive-cli`: a `sources` list in the settings (replacing the top-level `base_url`), choosing which newsletter a command runs on, and the `migrate` command.
- `post-discovery`, `post-fetching`, `content-extraction`, `post-storage`: work per newsletter, in each newsletter's own folder; post files gain `source`, and ids become `<source>/<slug>`.
- `search-indexing`: one index over both newsletters, recording which newsletter each post is from.
- `hybrid-search`: results from both newsletters by default, with each newsletter represented when it has a relevant match; a newsletter filter; each result names its newsletter.
- `mcp-server`: the search tool describes both newsletters and accepts the newsletter filter.

`search-indexing`, `hybrid-search` and `mcp-server` exist only in the unarchived change `add-hybrid-search`, so that change is archived into `openspec/specs/` first.

## Impact

- **Existing data:** moved once into its own folder, and post ids rewritten. The search index is rebuilt once with the new ids; the existing posts' embeddings and summaries are already cached, so this costs no Gemini calls, and the previous index stays available for rollback. **Breaking:** the old config format and folder layout are refused, with a message saying how to convert.
- **New data:** about 260 MB of saved pages, plus the cleaned posts.
- **Cost:** roughly $3–6 of Gemini once for the full load, then a few cents a week.
- **Setup for the user:**
  - back up the data folder;
  - stop the database and rename the folder to `leadership-coach-datasets`;
  - update `output_dir` (and `ARCHIVE_DB_DATA`, if set);
  - convert `config.yaml` to the `sources` list;
  - run `archive migrate`;
  - add the second cookie line to `.env`;
  - run the 5-post trial, then the full load.
- **Code:** settings, discovery, fetching, extraction and storage become per-newsletter; a new `migrate` command; new Substack reading; search gains the newsletter field, the filter and the "both represented" rule; the weekly script's notices name the newsletter.
