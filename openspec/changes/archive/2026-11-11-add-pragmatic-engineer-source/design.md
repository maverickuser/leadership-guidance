## Context

The archive was built for one Ghost newsletter:
- **Settings:** one `base_url` and one cookie (`NEWSLETTER_COOKIE`).
- **Files and ids:** files are named by slug (`manifest.json`, `raw/{slug}.html`, `posts/{slug}.json`), and the slug is the post id everywhere: the manifest, the search store, search results and the eval files.
- **Ghost-specific steps:** discovery, the sign-in check, body extraction and paywall detection all use Ghost markup.
- **Weekly update:** one launchd job runs `scripts/scheduled-run.sh`, which runs `archive run`.

The approved proposal adds The Pragmatic Engineer (Substack, paid). Every newsletter gets its own folder under one parent folder (`../leadership-coach-datasets`), the existing data moves there once, the same weekly job covers both, and one search mixes both, with each newsletter shown when it has a relevant match. The approved specs define the behaviour; this document explains how it is achieved and why. `lld.md` (gate 4) gives the detail.

Measured on 2026-11-10 with 3 unauthenticated requests, 3 seconds apart:
- **`robots.txt`:** named AI crawlers are blocked. `User-agent: *` allows `/p/` posts and disallows `/action/`, `/sign-in`, `/subscribe`, `/embed`, `/p/*/comment/*` and private feeds. It also declares `Content-Signal: search=yes, ai-input=yes, ai-train=no`.
- **`sitemap.xml`:** a flat `urlset` of 586 URLs: 583 `/p/<slug>` posts plus `/archive`, `/about` and `/podcast`.
- **A post page:** 200, no redirect, 451 KB.
  - **Body:** `div.available-content > div.body.markup`.
  - **Embedded data:** in `window._preloads`, including the post's `audience` and its `section` (`{"slug":"the-pulse","name":"The Pulse",...}`).
- **Caches:** the embedding and summary caches are keyed by content hash (`embed_sha256`, `summary_sha256`), not by post id.
- **Search today:** BM25 and vector candidates are fused (RRF), re-ranked by Qwen3-Reranker (the score is P("yes"), from 0 to 1), then diversified to at most 3 passages and 1 summary per post.

## Goals / Non-Goals

**Goals:**
- The existing newsletter keeps working; its data moves once, safely, into its own folder.
- One parent folder with one subfolder per newsletter, and the same shape for each.
- The Pragmatic Engineer is archived in full with the user's login.
- One weekly run covers both newsletters, and an expired login names its newsletter.
- One search mixes both by relevance and never lets one newsletter crowd out a relevant match from the other.
- A third newsletter later is mostly a config entry.

**Non-Goals:**
- Moving the database's files automatically. The parent-folder rename is a documented manual step.
- Other platforms now (WordPress, Beehiiv). The platform split makes them possible later.
- Podcast audio or video, comments, Substack Notes.
- Mapping categories across newsletters.
- Parallel fetching.

## Decisions (made with the user)

1. **Paid login:** The Pragmatic Engineer is read with the user's paid-subscriber cookie, kept in `.env` as `PRAGMATIC_ENGINEER_COOKIE`.
2. **One parent folder, one subfolder per newsletter.** `leadership-coach-datasets/{<id>/, postgres/, models/, logs/, eval files}`. The existing data moves once into its own folder, and every post id becomes `<source>/<slug>`, eval files included. This was revised the same day, replacing an earlier "existing untouched" choice.
3. **Categories:** Substack categories come from each post's section; a post in no section gets `[]`.
4. **Live check approved:** robots.txt, the sitemap and one post page, unauthenticated (facts above).
5. **Gemini plan:** the user's Gemini plan is paid, which satisfies the publisher's `ai-train=no`.
6. **Podcast and video posts:** kept, with whatever text the page has; under `min_word_count` they're `partial`.
7. **Weekly job:** the existing weekly job covers both newsletters.
8. **Future newsletters:** keep it open to adding more.
9. **Search mixing:** results are mixed by default and ranked by relevance, and each newsletter that has a relevant match is shown.

## Decisions

**D1. One `sources` list; every newsletter is an equal entry with its own folder.**
- **Config:** `config.yaml` lists newsletters under `sources` (id, name, platform, `base_url`, `cookie_env`, optional `boilerplate_prefixes`). Global settings stay global.
- **Folders:** each source owns `{output_dir}/{id}/` (`manifest.json`, `raw/`, `posts/`). Shared items stay at the top: `postgres/`, `models/`, `logs/` and the eval files.
- **Ids:** every post id is `{id}/{slug}`.
- *Why:* this is the layout the user chose: one shape for every newsletter, so adding a third is a config entry and a folder.
- *Alternative, rejected by the user:* keep the existing data at the top level and put only new newsletters in subfolders. That avoids a move, but leaves two layouts and two id styles.
- *Alternative:* separate configs and indexes per newsletter, which doesn't fit "one search" or "one weekly job".
- *Old config:* a top-level `base_url` is refused, with the `sources:` entry to paste in. Old configs aren't silently converted, because the id is the user's choice and decides a folder name.

**D2. Each source has a location object, and the code asks it for paths.** A `SourceConfig` gives its manifest path, raw and post paths, and post id (`{id}/{slug}`). Discovery, fetching, extraction and indexing take a `SourceConfig` instead of reading `config.base_url`, `config.manifest_path` and similar. This keeps every path rule in one place.

**D2a. The one-time move is an explicit command; the folder rename is manual.**
- **`archive migrate --source <id>`** works inside `output_dir`:
  1. copies `manifest.json` and `raw/*.html` into `{id}/`;
  2. writes each post as schema version 2 (`source`, `id = {id}/{slug}`) into `{id}/posts/`, checked against the schema;
  3. rewrites the eval files' post ids;
  4. only when every new file exists and validates, deletes the old top-level files, the manifest last.
- **Interruptions:** if stopped at any point, a rerun finishes the job. On an already moved archive it changes nothing. `--dry-run` shows counts and example paths.
- **The parent-folder rename** (`newsletter-archive-data` → `leadership-coach-datasets`) is a README step done with the database stopped, because Postgres's own files live inside that folder. Moving a database's live files from the tool is the wrong default.
- **Old layout guard:** every other command refuses an `output_dir` with a top-level `manifest.json` and points to `migrate`.
- *Alternative:* migrate automatically on first run. That's convenient, but surprising for the user's only copy of the archive.

**D3. A platform interface, with the existing Ghost code moved behind it unchanged.** `ghost` and `substack` each provide:
- which sitemap URLs are posts;
- the categories step (Ghost crawls its tag pages; Substack has none);
- the request URL (Ghost adds a trailing slash);
- the sign-in check;
- body selection and cleaning;
- metadata;
- categories from the page;
- paywall detection.

The sitemap walker, URL normalisation, HTTP client, section building, links and storage stay shared. Ghost's existing tests must pass with only their paths and ids updated. That is the proof that the existing newsletter behaves the same.

**D4. One shared rate limiter, plus one HTTP client per source.** Each client sends only its own cookie, and only to its own origin, keeping today's rule that the cookie goes only to `base_url`'s origin. The shared limiter keeps the 2-second floor and one-request-at-a-time across sources, so the weekly run never doubles the load on either site.

**D5. Sources run one after another; a stopped source doesn't stop the run.** `archive run` processes each source in config order, then builds the index once.
- **A stop** (missing or expired cookie, discovery failure) ends only that source's stages.
- **Exit code:** the run exits 2 at the end, as today, and the summary lists each stopped source with its name and `cookie_env`.
- **Notification:** `scheduled-run.sh` reads those lines to build a notification that names the newsletter.

**D6. Substack is read from the page's embedded data.**
- **Embedded data:** the sign-in check, paywall signal and section all come from `window._preloads`.
- **Body:** from `div.available-content`.
- **Categories:** from the post's own `section_id`. Navigation sections are ignored.
- **Requests:** none beyond the sitemap and the post pages.
- **Fixtures:** made from a page saved with the user's cookie (with permission), then reduced to structure only by `scripts/make_fixtures.py`.
- *Alternative:* parse the visible HTML for a "Sign in" button and section links. That's fragile with Substack's client-side rendering.

**D7. Every source represented: a guaranteed slot only above a relevance threshold.** This is applied after re-ranking and per-post diversity, and before the limit, only when there's no `source` filter and more than one source is indexed:
1. Take the ranked list.
2. For each source missing from the top `limit` results, find its best result.
3. If that result's re-ranker score is at least `search.source_min_score`, swap it in for the lowest-ranked result of a source that has more than one result.
4. When the re-ranker is unavailable, the result qualifies if it's within the first `search.candidates` fused candidates (it was retrieved at all).

- **Threshold:** the default is set from the evaluation set in the search PR. The proposed method is the lowest re-ranker score among the eval questions' expected results, rounded down, recorded with the measurement.
- *Alternative:* an even split (5 + 5), which the user rejected because it lets weak matches in.
- *Alternative:* pure relevance, which the user rejected because one newsletter could fill the page.
- *Alternative:* per-source score normalisation (fusing each source separately), which is more complex. The re-ranker already scores both sources on one scale, so a threshold on that scale is simpler and explainable.

**D8. Search store: a `source` column, rebuilt once with the new ids.**
- **Schema:** the per-generation `posts` and `passages` tables gain `source` (migration `002`).
- **Rebuild:** after the move, post ids change, and the index fingerprint includes the source list, so the next `archive index` builds a new generation automatically.
- **Cost:** the caches are content-keyed, so existing posts cost no Gemini calls.
- **Rollback:** the old generation stays available for `--rollback`.
- **Boilerplate:** detection (repeated lines across ≥ 5 posts) runs per source, since intro and outro lines are a per-newsletter pattern.

**D9. MCP and eval.**
- **MCP:** the tool description lists each newsletter with its post count, date range and categories. `search` gains `source`, and the readable output starts each result with the newsletter's name.
- **Eval:** existing eval questions and judgments keep working after `migrate` rewrites their ids. A few cross-newsletter questions are added to check the "every source represented" rule and to set its threshold.

## Risks / Trade-offs

- **[The move touches the only copy of the archive]** → The README makes a backup the first step. `migrate` deletes nothing until every new file exists and validates, is safe to stop and rerun, and has a dry run. Tests cover interruption at each phase.
- **[Paths outside the tool]** (the Docker bind mount, launchd log path) → The README lists each one to update. `ARCHIVE_DB_DATA`'s default in `docker-compose.yml` changes to `../leadership-coach-datasets/postgres`.
- **[Substack markup changes]** → Extraction is tied to fixtures made from real saved pages. A missing body or embedded data fails that post with a clear error, never a silent empty post.
- **[Threshold too high or too low]** → It's measured on the eval set and adjustable in config. The eval report shows how often a source was swapped in, and the default is revisited after the full load.
- **[Larger newsletter dominates by volume]** (583 vs ~253 posts, and longer) → This is handled by D7 for results. Per-post diversity already limits any single post.
- **[Cookie lifetime differs]** → The expired-login notice names the newsletter and its `.env` variable.
- **[Large pages]** (~450 KB each) → They're well within `http.py`'s 10 MB response cap (`MAX_RESPONSE_BYTES`).
- **[Cost]** → Indexing ~583 longer posts once costs roughly $3–6. `archive index --dry-run` gives the real figure first, and the spending ceiling still applies.

## Migration Plan

1. **Merge the code PRs.** From then on, the old config and layout are refused with instructions.
2. **Back up** `../newsletter-archive-data`.
3. **Stop the database:** `docker compose down`.
4. **Rename the folder** to `../leadership-coach-datasets`.
5. **Update the paths:** `output_dir` (or rely on the new default), `ARCHIVE_DB_DATA` if set, and the launchd log path if set.
6. **Convert `config.yaml`** to the `sources` list (the error message prints the entry).
7. **Move the data:** `archive migrate --source <id> --dry-run`, then without `--dry-run`.
8. **Re-index:**
   - start the database (`docker compose up -d --wait db`);
   - `archive index --dry-run` should show ~0 Gemini cost;
   - `archive index` builds the new generation.
9. **Add The Pragmatic Engineer:** `PRAGMATIC_ENGINEER_COOKIE` in `.env` and its `sources` entry.
10. **Trial run (with the user's go-ahead):** `archive run --source pragmatic-engineer --limit 5`, then check the 5 posts.
11. **Full load (with the user's go-ahead):**
    - `archive run --source pragmatic-engineer`;
    - `archive index --dry-run` for the cost;
    - `archive index`.
12. **Weekly updates:** the job covers both from then on.
13. **Rollback:**
    - **Index:** `archive index --rollback`.
    - **Data:** restore the backup folder and the old config, and use the previous release from git.

## Open Questions

- **Substack sign-in field:** which embedded-data field reliably marks a logged-in reader on a custom-domain Substack, and which cookie names the user's header carries (likely `substack.sid`)? This is answered from one page saved with the user's cookie, with their go-ahead, in the Substack PR.
- **Default `source_min_score`:** measured from the eval set in the search PR (D7).
- **`news_sitemap.xml`:** is it ever a superset of `sitemap.xml`? Assumed not; checked in the Substack PR.
