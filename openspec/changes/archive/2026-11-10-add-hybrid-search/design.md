## Context

Phase 1 produced `{output_dir}/posts/{slug}.json` for 252 posts (about 730k words; 2,319 sections, median 270 words, longest 2,000) plus `manifest.json`. Each record has clean `sections`, `title`, `categories`, `published_date`, `url` and `content_hash`. This change turns that JSON into a searchable store and exposes search to AI agents through a local MCP server. The calling agent (for example Claude Code) collates the returned passages into an answer.

Constraints carried over: runs on the user's Mac; content is personal and never shared or published; offline tests; one writer per archive folder (run lock); atomic writes; secrets only in `.env`.

## Goals / Non-Goals

**Goals:**
- Right answers: retrieval quality first (hybrid search, a strong embedding model, re-ranking, measured on the user's own questions).
- One search interface: an MCP `search` tool any agent can use.
- Cheap incremental updates as part of the weekly `archive run`.

**Non-Goals:**
- Generating answers in the server; CLI or Python search interfaces; structured or statistical queries over metadata; web UI.

## Decisions (made with the user)

- **Data source**: the JSON post records only.
- **Passages**: split along sections; ~300-word windows with ~50-word overlap; tiny sections merged. Simulated on the real archive: 3,696 passages, median 240 words.
- **Embeddings**: `gemini-embedding-2` through the Gemini API (GA since April 2026; up to 8,192 input tokens; output size 128–3,072, recommended 768, 1,536 or 3,072). Key in `.env` as `GEMINI_API_KEY`.
- **Hybrid**: BM25 and semantic candidates combined, then **re-ranked** (required).
- **Interface**: local MCP server over stdio, `search(query, limit, category, since, until)` returning passages with post metadata; data only.
- **No Claude API** in this change.
- **Retrieval approach (2026-11-01)**: hybrid search with re-ranking plus one Gemini-generated summary per post, searchable beside passages (`kind` = `passage` | `summary`). Same `GEMINI_API_KEY` for embeddings and summaries. Contextual retrieval and knowledge graphs deferred to the evaluation; no `get_post` tool for now.

## Low-level design decisions (see `lld.md`)

- **Storage**: Postgres in Docker (`paradedb/paradedb` image: Postgres + `pg_search` + `pgvector`), data persisted in `newsletter-archive-data/postgres/`.
- **BM25**: ParadeDB `pg_search`, English stemming, boosts heading 2 · title 1.5 · body 1 (tunable).
- **Vectors**: `gemini-embedding-2` at 3,072 dimensions in a `halfvec(3072)` column, exact search (no approximate index at this scale).
- **Combining**: Reciprocal Rank Fusion (k = 60, top 50 per list).
- **Re-ranker**: Qwen3-Reranker-0.6B, local.
- **MCP**: official `mcp` Python SDK, stdio, one `search` tool; read-only database role.
- **Indexing (v2, after principal review `review-indexing.md`)**: per-post fingerprints drive change detection; a new generation (one Postgres schema) only for a new embedding model or dimension, with catch-up, quality gate, swap and rollback; per-run exact + near-duplicate boilerplate filter; in-repo sentence splitter with golden and property tests; deletion guard, spending ceiling, circuit breaker, no silent truncation; advisory lock, stale-run cleanup, failure tracking, freshness; full-precision embedding cache with GC; title boost 1.5. Post summaries cached by post text, model and prompt version (`lld.md` §5.10). Contextual retrieval deferred.

## Retrieval approach options (see `retrieval-options.md`)

- Reviewed hybrid search against knowledge-graph RAG (GraphRAG, LightRAG, HippoRAG 2), hierarchical summaries (RAPTOR), contextual retrieval, long context and late interaction.
- **Chosen**: keep hybrid BM25 + dense + re-rank, add one LLM summary per post as a searchable layer for broad questions, let the calling agent iterate; add contextual retrieval or a graph only if the evaluation shows failures.
- A knowledge graph is not recommended now: the posts are entity-poor advice essays, and research shows graphs trail plain RAG on detail questions.

## Risks / Trade-offs

- [Post text sent to Google for embedding and summaries] → user accepted; use paid-tier terms so content isn't used to improve Google's products; only passage text with title and heading is sent.
- [Gemini API outage or quota] → search falls back to keyword-only results, marked as such; indexing retries with backoff and never leaves a half-built store.
- [Key leak] → same secret wrapper as the cookie; safety check flags key-like strings; tests grep logs and responses for the key.
- [Summaries drift from the source] → summaries only help find posts; passages are the evidence; the tool description says so; prompt versioned and cached.
- [Re-ranker adds latency or cost] → measured with the evaluation script before settling settings.
