# Principal Review: Indexing & Embedding (lld.md §4–5)

Date: 2026-10-31 · Reviewer stance: principal engineer, data-pipeline / search infrastructure.

## Verdict

The design is sound at the macro level: content-addressed embedding cache, blue-green generations, failure tracking and a data-driven boilerplate filter are the right building blocks. Five correctness bugs need fixing before implementation, and the plan logic should be reorganised around **one derived hash per post**, which fixes three of them and removes most reasons for full rebuilds.

## P0: correctness (fix before building)

| # | Finding | Failure scenario | Fix |
|---|---|---|---|
| 1 | **Plan compares the wrong hash.** `content_hash` covers only the post text, but the index also depends on title, heading, categories, date, URL, the boilerplate set, chunker version and chunk settings. | A post's title is corrected; its passages keep the old title in both the embedding input and the BM25 title field forever. | Compute a per-post **index fingerprint** = sha256(every input that feeds the index: cleaned text, title, headings, categories, date, url, chunker version, chunk settings, embed-input version). Plan = compare fingerprints. Changed fingerprint → re-chunk that post; the embedding cache makes unchanged passages free. |
| 2 | **Boilerplate set drifts between rebuilds.** The set is computed only on full rebuild. | A new recurring outro appears in 6 new posts: it is indexed in all of them. Or a paragraph crosses the threshold and stays in older posts that already contain it. | Recompute paragraph counts every run (≈7.5k paragraphs, milliseconds, in memory). The boilerplate set feeds the fingerprint (#1), so exactly the affected posts re-chunk. |
| 3 | **A `--no-swap` candidate goes stale.** Weekly incremental runs update only the live generation. | Build g8 with `--no-swap`, evaluate for a week, swap: g8 is missing that week's posts. | Before any swap, run an incremental **catch-up** on the candidate against the current manifest; refuse the swap if the candidate's fingerprints don't match the manifest. (Same as the "backfill then catch up" step in production migrations.) |
| 4 | **No guard against mass deletion.** "Removed = in DB, not in archive". | A truncated or half-written manifest (disk full, bad edit) makes one run delete most of the index. | Refuse to remove more than 10% of posts (or more than 5) in one run without `--allow-mass-delete`; log the would-be deletions. |
| 5 | **Possible silent truncation by the API.** Embedding APIs can truncate long inputs without error. | A malformed post produces a giant passage; the API embeds only its start; search quality silently degrades. | Set the SDK's truncation option to fail instead of truncate (verify the flag in PR 3), keep the 2,000-token chunk guard, and log the token count per batch. |

## P1: robustness and quality

| # | Finding | Fix |
|---|---|---|
| 6 | **BM25 title field is skewed by duplication.** Every passage repeats its post's title, so a long post's title words get a higher document frequency (lower IDF), and a title match lifts all of that post's passages equally. | Lower the title boost to 1.5 (tunable), let the evaluation script decide, and rely on max-3-per-post diversification. Option if eval shows a problem: a separate post-level BM25 over `posts(title, subtitle)` fused as a third list. |
| 7 | **Exact-hash boilerplate misses near-duplicates.** The two intro wordings were caught only because each is frequent; one-off variants (a changed job title, a new sentence) slip through. | Near-duplicate clustering: word 5-gram shingles + Jaccard ≥ 0.8 (or MinHash) over paragraphs; a cluster spanning ≥ 5 posts is boilerplate. ~7.5k paragraphs: trivial cost. |
| 8 | **Lock lives in the wrong place.** The file lock protects the archive folder, but the shared state is the database. | Also take a Postgres advisory lock (`pg_try_advisory_lock`) for the whole index run; the database is the source of truth for "is an index run active". |
| 9 | **Crashed runs stay "running".** | At start, mark `index_runs` rows still `running` with no live advisory lock as `aborted`. |
| 10 | **No circuit breaker or cost ceiling.** An outage means every batch walks the full retry ladder; a mistaken `--refresh` with a new model could spend unexpectedly. | Circuit breaker: after 3 consecutive failed batches, stop calling the API, mark the rest failed, finish the run. Cost ceiling: `search.max_embed_tokens_per_run` (default 1.5M); a plan above it needs `--yes`. |
| 11 | **Chunker determinism depends on dependencies.** A sentence-splitter library upgrade would silently change passages without a `CHUNKER_VERSION` bump. | Use an in-repo regex sentence splitter (no library). Golden snapshot tests on fixture posts: if output changes and `CHUNKER_VERSION` didn't, the test fails. Property tests (hypothesis): every word is covered, no window over the limit, overlap ≤ setting, same input → same output. |
| 12 | **Generation tables by name suffix are awkward.** Migrations, grants and pg_search indexes must be repeated per `_gN` table with dynamic SQL. | One Postgres **schema per generation** (`idx_g7.posts`, `idx_g7.passages`, …) created by a single `create_generation(n)` SQL function; `GRANT USAGE` + default privileges for the reader role; queries use a schema name built from a validated integer. Drop old generations with `DROP SCHEMA … CASCADE`. |

## P2: smaller improvements

| # | Finding | Fix |
|---|---|---|
| 13 | Swap has no quality gate. | For model or dimension changes, `--swap` requires the evaluation script not to regress recall@10 by more than 2 points versus live, unless `--force`. |
| 14 | `embedding_cache` grows forever; halfvec cache loses precision before re-use. | Store cached vectors as `vector(3072)` (float32) and cast to `halfvec` when writing passages; delete cache rows not referenced by any generation and older than 90 days. |
| 15 | No freshness signal. | Record per post `fetched_at` → `indexed_at`; summary shows the oldest un-indexed post. A post fetched but not indexed after 2 runs triggers the exit-3 notification. |

## Simplification enabled by the fingerprint

With per-post fingerprints (#1), a new generation (full rebuild) is needed only when the **vector space** changes: embedding model or dimension. Chunker, chunk-setting, input-format and boilerplate changes become ordinary incremental runs that re-chunk only affected posts, with unchanged passages served from the cache. That shrinks the riskiest code path to the rare case it is meant for.

## Deliberately not recommended (right-sizing)

- A queue or workflow engine (Kafka, Airflow, Temporal): one machine, ~250 posts, weekly runs. The run table + advisory lock gives the needed guarantees.
- An approximate vector index (HNSW): exact scan is fast and exact at ~3,600 rows; revisit past ~50k.
- Change-data-capture from the JSON folder: the manifest is already an ordered change log.
- Async batch embedding: ~1M tokens once; the 50% saving is not worth a second code path.

## Proposed edits to the LLD

1. §5.1 Plan: fingerprints replace content-hash comparison; full rebuild only on model/dimension change; deletion guard; cost ceiling.
2. §5.2 Boilerplate: per-run recompute; near-duplicate clustering.
3. §5.3 Chunking: precise greedy-packing algorithm, in-repo sentence splitter, golden and property tests.
4. §5.4 Generations: schema per generation, catch-up before swap, quality gate.
5. §5.5 Embedding: no silent truncation, circuit breaker, float32 cache.
6. §5.6 Runs: advisory lock, stale-run cleanup, freshness.
7. §4 Schema: `posts.fingerprint`, generation schemas, cache as `vector(3072)`.
8. §6 Search: title boost 1.5 (tunable).
