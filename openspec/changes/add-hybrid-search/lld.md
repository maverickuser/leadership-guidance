# Low-Level Design: Hybrid Search over the Archive (v3)

Status: approved approach, ready to build · Change: `add-hybrid-search` · Date: 2026-11-01

Requirements: `specs/search-indexing`, `specs/hybrid-search`, `specs/mcp-server`, `specs/archive-cli`.
v2 applies all 15 findings of `review-indexing.md` (mapping in §17). v3 adds the post summary layer chosen in `retrieval-options.md` (§5.10).

> Retrieval approach (decided 2026-11-01, see `retrieval-options.md`): hybrid BM25 + semantic + re-rank, plus one LLM summary per post as a second searchable layer. Contextual retrieval and knowledge graphs are deferred until the evaluation shows a need.

## 1. Decisions

| Area | Decision | Why |
|---|---|---|
| Data source | `posts/*.json` (manifest status `ok` / `partial`) | Clean, structured, already validated |
| Storage | **Postgres in Docker**, image `paradedb/paradedb` (Postgres + `pg_search` + `pgvector`), data persisted on disk | Persistent real database; one container covers BM25 and vectors |
| Keyword search | **BM25 via ParadeDB `pg_search`**, English stemming, boosts heading 2 · title 1.5 · body 1 (tunable), phrase queries | Postgres's built-in `ts_rank` is not BM25 |
| Embeddings | **`gemini-embedding-2`**, 3,072 dimensions, Gemini API (`google-genai`) | Best quality setting; size is trivial at this scale |
| Vector storage | `halfvec(3072)`, **exact scan** | ~3,600 rows in tens of ms; exact search never drops filtered results |
| Change detection | **Per-post fingerprint** of everything that feeds the index | Catches title/category/setting changes; re-indexes only what changed |
| Index versions | **Generation = one Postgres schema** (`idx_gN`), created only when the embedding model or dimension changes; build beside live, catch up, gate, swap, rollback | Safe model changes without downtime |
| Combining | **Reciprocal Rank Fusion**, k = 60, top 50 per list | Rank-based, no tuning, rewards agreement |
| Re-ranker | **Qwen3-Reranker-0.6B**, local (Apple MPS) | Free, private, no extra key |
| Interface | **Local MCP server**, stdio, one tool `search` | Any agent connects; data only |
| Post summaries | **One summary per post** (Gemini Flash-class model, same `GEMINI_API_KEY`), stored as kind `summary` beside passages in the same BM25 and vector indexes | Covers broad "across all posts" questions; 252 calls once |
| Answer writing | The calling agent | Server never calls an LLM at search time |

## 2. Architecture

```
            INDEX (archive index / archive run, weekly)
posts/*.json ─► clean (boilerplate) ─► fingerprint ─► plan ─► chunk + summarise (cache → Gemini Flash) ─► embed (cache → paced Gemini → validate)
             ─► write to active generation (writer role) ─► verify ─► report
                 (model/dim change only: build new generation ─► catch-up ─► quality gate ─► swap)

            SEARCH (MCP tool call from Claude Code / Claude Desktop / any MCP client)
query ─► validate ─┬─► Gemini embed (query, active generation's model) ─► vector top-50 ─┐
                   └─► BM25 top-50 (pg_search, boosted, filtered) ───────────────────────┴─► RRF ─► top-40
                        ─► Qwen3 re-rank ─► max 3 per post ─► top-N ─► MCP result (structured + text)
```

Two processes share the database: the **indexer** (short-lived CLI, writer role, archive file lock + database advisory lock) and the **MCP server** (long-lived, started by the agent, read-only role). The server queries Postgres live, so updates appear on the next search with no restart.

## 3. Infrastructure

### 3.1 Docker Compose (`docker-compose.yml`, in the repo, no secrets)

```yaml
services:
  db:
    image: paradedb/paradedb:<pinned-tag>      # exact tag pinned in PR 1 (Postgres 17)
    container_name: newsletter-archive-db
    restart: unless-stopped
    ports: ["127.0.0.1:5433:5432"]            # localhost only, non-default port
    environment:
      POSTGRES_PASSWORD: ${ARCHIVE_DB_ADMIN_PASSWORD}
      ARCHIVE_DB_WRITER_PASSWORD: ${ARCHIVE_DB_WRITER_PASSWORD}
      ARCHIVE_DB_READER_PASSWORD: ${ARCHIVE_DB_READER_PASSWORD}
    volumes:
      - ${ARCHIVE_DATA_DIR:-../newsletter-archive-data}/postgres:/var/lib/postgresql/data
      - ./db/init:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      retries: 10
```

- Data lives in `newsletter-archive-data/postgres/`, outside the repo; survives restarts and upgrades. (Fallback if bind-mount permissions misbehave: a named volume.)
- `restart: unless-stopped` + Docker Desktop "start at login".
- `db/init/20_archive_roles.sh` creates the `archive` database, both extensions, and roles `archive_writer` (owns schemas, used by indexing) and `archive_reader` (read-only, used by the MCP server).

### 3.2 Secrets (`.env`, never committed, printed or logged)

| Key | Used by |
|---|---|
| `NEWSLETTER_COOKIE` | fetch (existing) |
| `GEMINI_API_KEY` | indexer; MCP server (query embedding) |
| `ARCHIVE_DB_ADMIN_PASSWORD` / `_WRITER_PASSWORD` / `_READER_PASSWORD` | Docker init; app builds connection strings |

All wrapped in `Secret`. Logs show host, port, database and role only.

## 4. Database schema

### 4.1 Shared tables (schema `public`, migration `001_init.sql`)

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_search;

CREATE TABLE schema_version (version int PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE generations (                       -- the "alias": exactly one row is active
  generation      int PRIMARY KEY,
  status          text NOT NULL CHECK (status IN ('building','ready','active','retired')),
  embedding_model text NOT NULL,
  embedding_dim   int  NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  activated_at    timestamptz
);
CREATE UNIQUE INDEX one_active ON generations ((status)) WHERE status = 'active';

CREATE TABLE embedding_cache (                   -- content-addressed; full precision
  embed_sha256 text NOT NULL,                    -- sha256 of the exact input text
  model        text NOT NULL,
  dim          int  NOT NULL,
  embedding    vector(3072) NOT NULL,            -- float32; cast to halfvec when written to passages
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (embed_sha256, model, dim)
);

CREATE TABLE summary_cache (                     -- survives generation rebuilds
  summary_sha256 text NOT NULL,                  -- sha256(model, prompt version, title, cleaned text)
  model          text NOT NULL,
  prompt_version int  NOT NULL,
  summary        text NOT NULL,
  input_tokens   int, output_tokens int,
  created_at     timestamptz NOT NULL DEFAULT now(),
  last_used_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (summary_sha256)
);

CREATE TABLE index_runs (
  run_id      bigserial PRIMARY KEY,
  started_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  mode        text NOT NULL,                     -- incremental | build | swap | rollback | dry-run
  generation  int,
  counts      jsonb,
  status      text NOT NULL DEFAULT 'running',   -- running | ok | partial | failed | aborted
  error       text
);

CREATE TABLE index_failures (
  post_id      text NOT NULL,
  stage        text NOT NULL,                    -- load | clean | chunk | summarize | embed | write
  error        text NOT NULL,
  fingerprint  text NOT NULL,                    -- attempts reset when the fingerprint changes
  attempts     int  NOT NULL DEFAULT 1,
  first_failed timestamptz NOT NULL DEFAULT now(),
  last_attempt timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (post_id, stage)                   -- a summary failure doesn't mark the passages failed
);

GRANT SELECT ON generations, schema_version TO archive_reader;
```

### 4.2 Per-generation schema (`create_generation(n)` SQL function)

One function creates `idx_g{n}` with identical tables, indexes and grants, so migrations change one function, not N tables:

```sql
-- inside schema idx_g{n}
CREATE TABLE posts (
  post_id        text PRIMARY KEY,
  title          text NOT NULL,
  subtitle       text,
  url            text NOT NULL,
  author         text,
  published_date date,
  categories     text[] NOT NULL DEFAULT '{}',
  word_count     int  NOT NULL,
  status         text NOT NULL,                  -- ok | partial
  fingerprint    text NOT NULL,                  -- §5.2
  summary_sha256 text,                           -- null = summary missing; retried next run
  fetched_at     timestamptz,                    -- from the manifest, for freshness
  indexed_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE passages (
  passage_id     text PRIMARY KEY,               -- {post_id}#{n}, or {post_id}#summary
  post_id        text NOT NULL REFERENCES posts ON DELETE CASCADE,
  kind           text NOT NULL DEFAULT 'passage' CHECK (kind IN ('passage','summary')),
  n              int  NOT NULL,                  -- -1 for the summary
  heading        text,                           -- null for the summary
  body           text NOT NULL,
  title          text NOT NULL,                  -- denormalised for BM25 boost
  published_date date,                           -- denormalised for filters
  categories     text[] NOT NULL DEFAULT '{}',
  word_count     int  NOT NULL,
  embed_sha256   text NOT NULL,                  -- links to embedding_cache; used for cache GC
  embedding      halfvec(3072) NOT NULL,         -- dimension set from the generation row
  UNIQUE (post_id, n)
);
CREATE INDEX ON passages (published_date);
CREATE INDEX ON passages (kind);
CREATE INDEX ON passages USING gin (categories);
CREATE INDEX passages_bm25 ON passages           -- syntax sketch; verified against pinned pg_search
  USING bm25 (passage_id, title, heading, body) WITH (key_field = 'passage_id');

CREATE TABLE boilerplate (                       -- snapshot of the last run's boilerplate set
  cluster_id  int  NOT NULL,
  para_sha256 text PRIMARY KEY,
  post_count  int  NOT NULL,
  sample      text NOT NULL
);

GRANT USAGE ON SCHEMA idx_g{n} TO archive_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA idx_g{n} TO archive_reader;
```

- Searches read the active generation from `generations` (cached 5 s) and use schema `idx_g{n}`; the schema name is built from a validated integer, never from input.
- Dropping a generation is `DROP SCHEMA idx_g{n} CASCADE`.
- Migrations: numbered SQL files; `archive index` applies them (writer). A migration that changes generation tables updates `create_generation` and alters existing generation schemas in the same file. The MCP server checks `schema_version` at start.

## 5. Indexing (`archive index`, last stage of `archive run`)

### 5.1 Pipeline

```
start ─► load ─► clean ─► fingerprint ─► plan (+ guards) ─► chunk ─► embed ─► write ─► verify ─► report
                                              model/dim change: new generation ─► catch-up ─► gate ─► swap
```

1. **Start**: config, secrets; archive file lock; connect as writer; `pg_try_advisory_lock(INDEX_LOCK_KEY)`; if held, exit "an index run is already active". Mark `index_runs` rows still `running` as `aborted` (we hold the lock, so they are dead). Apply migrations. Open a run row.
2. **Load** eligible posts from the manifest and JSON; invalid JSON → `index_failures` (stage `load`), continue.
3. **Clean**: compute the boilerplate set over all eligible posts and remove it (§5.3).
4. **Fingerprint** every post (§5.2).
5. **Target**: the active generation. If config model/dim differ from it → stop with "a new index version is needed; run `archive index --refresh`" (exit 1, or 3 under `archive run`), unless `--refresh`.
6. **Plan**: compare fingerprints with the target's `posts` → `new`, `changed`, `unchanged`, `removed`; add `index_failures` posts under 3 attempts (or all with `--retry-failed`).
   - **Deletion guard**: if `removed` > max(5, 10% of indexed posts) and no `--allow-mass-delete`, remove nothing, list them, continue.
   - **Spending ceiling**: estimate embedding tokens for passages and summaries missing from the cache, and summary tokens (input + output) for posts missing from `summary_cache`; either above its limit without `--yes` → stop with both estimates.
   - `--dry-run`: print the plan, passages and summaries to embed or generate, cache hits, estimated tokens and cost; stop.
7. **Chunk** new and changed posts (§5.4) and **summarise** them (§5.10), plus posts whose summary is missing.
8. **Embed** passages and summaries (§5.6).
9. **Write**, one transaction per post: delete its passages, upsert the post with its new fingerprint, insert passages. A new generation is bulk-loaded and its BM25 index built once after loading.
10. **Verify**: every eligible, non-failed post's stored fingerprint equals the computed one; passage counts match; every vector has the generation's dimension; one BM25 and one vector smoke query return rows.
11. **Report**: per-post lines; summary (indexed, unchanged, removed, guarded, failed, boilerplate removed, passages, embedding calls, cache hits, tokens, estimated cost, time, freshness §5.7); close the run row; release locks; cache GC (§5.6).

### 5.2 Fingerprint

`fingerprint = sha256(canonical JSON of)`:

| Input | Why it matters |
|---|---|
| `post_id`, `title`, `url`, `published_date`, sorted `categories`, `status` | Stored on passages, used in BM25 and filters |
| cleaned sections (headings + paragraphs after boilerplate removal) | Passage text |
| `CHUNKER_VERSION`, `chunk_words`, `chunk_overlap_words` | Passage boundaries |
| `EMBED_INPUT_VERSION` | Embedding input format |
| `summaries.enabled`, `summaries.model`, `SUMMARY_PROMPT_VERSION` | Summary text |

Effects:
- A title fix, a category change, a new boilerplate paragraph or a chunk-setting change re-indexes exactly the affected posts.
- Re-indexing re-chunks the post; the embedding cache serves every passage whose input text is unchanged, so only changed passages cost anything.
- A new generation is needed **only** for a new embedding model or dimension (the vector space changes).

### 5.3 Boilerplate filter

Measured on the current archive: 207 copies of 3 boilerplate paragraphs remain in post text (two welcome-intro wordings, a "free members can read…" note, a "welcome to those new…" note).

1. Split every eligible post into paragraphs; keep those with ≥ 8 words; normalise (lowercase, strip punctuation, collapse spaces).
2. **Exact groups**: by sha256 of the normalised text.
3. **Near-duplicate groups**: word 5-gram shingles → MinHash (64 permutations, in-repo numpy code) → LSH banding (16 × 4) to find candidate pairs → exact Jaccard ≥ `boilerplate_similarity` (0.8) → union-find clusters. Measured on the archive: 1.5 s, 3 clusters (the welcome intros' 3 wordings merged into one). With 5-gram shingles a one-word change only stays above 0.8 for paragraphs of ~50+ words, which real intros are.
4. A cluster whose members appear in ≥ `boilerplate_min_posts` (5) **distinct posts** is boilerplate. Remove its paragraphs from those posts before chunking.
5. Warn for any post losing > 20% of its words (protects real content that happens to repeat).
6. Store the set in the generation's `boilerplate` table (cluster, hash, post count, sample) for inspection. The set is recomputed every run; its effect flows through fingerprints (§5.2).

No site-specific strings in the repo; post JSON is not modified. The phase-1 extractor is fixed separately (§5.9).

### 5.4 Chunking (`search/chunking.py`, pure)

1. Merge sections under 40 words into the next section (or the previous if last); the merged passage keeps the first section's heading.
2. Split each section into paragraphs (`\n\n`), and paragraphs into sentences with an **in-repo regex splitter** (handles common abbreviations, decimals, initials, ellipses, quotes). No third-party splitter, so library upgrades can't change output.
3. **Greedy packing** into windows of ≤ `chunk_words`:
   - add whole paragraphs while they fit;
   - a paragraph that doesn't fit is added sentence by sentence;
   - a single sentence longer than the limit is split by words.
4. **Overlap**: each window after the first starts with the previous window's trailing whole sentences totalling ≤ `chunk_overlap_words`.
5. Ids `{post_id}#{n}`, `n` from 0 in reading order.
6. **Token guard**: a passage over 2,000 estimated tokens (chars / 4) is split again by words.
7. `CHUNKER_VERSION` constant, part of the fingerprint.

Tests:
- **Golden snapshots** for fixture posts; a test fails if the output changes while `CHUNKER_VERSION` did not.
- **Property tests** (hypothesis): every non-boilerplate word appears in some passage; no passage over the limit; overlap ≤ setting; same input → same output.

Measured: ~3,600 passages after cleaning, median ~240 words. The evaluation script compares 300/50 with 400/100 (Microsoft's study found ~512 tokens with 25% overlap best on its data).

### 5.5 Generations: build, catch-up, gate, swap, rollback

Used only for a new embedding model or dimension (or an explicit `--refresh`).

| Step | What happens |
|---|---|
| Build | `create_generation(n+1)` (status `building`); embed and bulk-load all posts; build BM25 index; verify → status `ready`. The active generation keeps serving and receiving weekly updates. |
| Catch-up | Before any swap: re-plan the candidate against current fingerprints and apply the difference (new posts, edits, removals). Refuse to swap if verification still fails. |
| Quality gate | If `{output_dir}/eval-queries.yaml` exists: run the evaluation on candidate and active. Recall@10 drop > 2 points → no swap unless `--force`. No eval file → warn and continue. |
| Swap | One transaction: candidate `active`, old active `retired` (only the newest retired one is kept; older retired schemas are dropped). |
| Rollback | `archive index --rollback`: catch the retired generation up, then swap it back in. Requires its embedding model to still be available. |

Commands: `--refresh` builds and swaps; `--refresh --no-swap` builds to `ready`; `--swap` catches up, gates and swaps a `ready` generation later.

The query embedder always uses the **active generation's** model and dimension (from `generations`), so queries and passages can't disagree during or after a swap.

### 5.6 Embedding (`search/embeddings.py`)

| Concern | Design |
|---|---|
| SDK | `google-genai` 2.31: `client.models.embed_content(model="gemini-embedding-2", contents=[Content(parts=[Part(text=t)]) for t in batch], config=EmbedContentConfig(output_dimensionality=3072))`. **Verified in PR 3:** a plain list of strings is embedded as ONE input, so each text must be its own `Content`; batch results equal single calls (cosine 1.0); at most 100 texts per request (HTTP 400 above). |
| Input format | No `task_type` on this model; task in the text. Passage `title: {post title} \| text: {heading}\n{body}` (heading omitted when null). Query `task: search result \| query: {q}`. `EMBED_INPUT_VERSION` is in the fingerprint. |
| Cache | Key `sha256(exact input text)` + model + dim. Stored as float32 `vector`; cast to `halfvec` on write. `last_used_at` updated on hits; estimates read without touching it. Each batch is cached as soon as it returns, so a later failure never wastes paid work. |
| Truncation | Never silent. **Verified in PR 3:** the API silently truncates over-long input (a 9,000-token text was accepted) and `auto_truncate=False` is rejected in Developer API mode. So every embedding input is capped at 8,000 characters (under 8,192 tokens even at one character per token) and refused before sending above that; chunking caps passage bodies at 7,000 characters, leaving room for the title and heading (`CHUNKER_VERSION` 2). Token counts (characters / 4) are used only for pacing and cost. |
| Pacing | Token bucket for requests and tokens per minute (`embed_rpm`, `embed_tpm`, set to the key's quota tier); one batch at a time (simpler than 2 in flight; ~37 batches for the whole archive). |
| Retries | 429, 5xx, timeouts, connection errors: 2, 4, 8, 16 s with jitter, honouring `Retry-After`. 400/401/403: no retry. |
| Circuit breaker | 3 consecutive calls failed after retries, or returned unusable responses → no more API calls this run; remaining posts recorded failed (stage `embed`); run status `partial`. Invalid requests (400/404) don't count; programming errors propagate unchanged. Every request has a 60 s timeout (`HttpOptions`), treated as a retryable timeout. |
| Validation | Response count = request count; each vector exactly `dim` values, finite, non-zero norm; then L2-normalised (the API already returns unit vectors at 3,072 and 768, verified). Otherwise the batch fails. |
| Batching | `embed_batch_size` (default 50, capped at the API's per-request limit). |
| Cache GC | End of run: delete cache rows whose hash no live, ready or retired generation references and whose `last_used_at` is older than `cache_retention_days` (90). |
| Not used | Gemini async batch mode (~50% cheaper, up to 24 h): ~1M tokens once makes the saving small. |
| Sent to Google | Passage text with title and heading; search queries. Nothing else. |

### 5.7 Runs, failures and freshness

- `index_runs` records every run; crashed runs become `aborted` at the next start.
- `index_failures`: one row per failing post with stage, error, fingerprint and attempts. Retried each run; after 3 attempts skipped with a warning until the fingerprint changes (attempts reset) or `--retry-failed`.
- **Freshness**: the summary lists posts archived but not indexed, with time since fetch. A post unindexed after 2 consecutive runs makes `archive run` exit 3, so the weekly job notifies.
- Exit codes: `archive index` 0 (failures listed), 1 (config, key, lock, guard needing a flag, ceiling); `archive run` 3 if the index step failed or freshness tripped.

### 5.8 Deferred: contextual retrieval (add only if the evaluation shows passages failing for lack of context)

Anthropic's method prepends a 50–100-token, chunk-specific context written by an LLM that reads the whole post, before embedding and BM25. Their tests: failed retrievals down 35% (embeddings), 49% (+ BM25), 67% (+ re-ranking). Here: ~3,600 LLM calls once (whole post cached per post), for example a Gemini model on the existing key; whole posts sent to that model. If accepted: a `context` column on passages, included in the embedding input and BM25 body, with its prompt version in the fingerprint.

### 5.9 Related phase-1 fix (done in PR 2)

Phase 1's extractor matched one intro wording and stopped at the first non-matching paragraph. Measured: 79 posts had boilerplate only in the leading paragraphs, 38 had it deeper (positions 3–16, after the post's own opener). Fix: extra intro prefixes in the local, git-ignored `config.yaml` (`boilerplate_prefixes`); the stop-at-first-non-match rule is kept so real content near the top is never removed (spec delta `content-extraction`). After `archive extract --refresh`, boilerplate in post JSON fell from 208 to 38 paragraphs; the index-time filter removes the rest.

### 5.10 Post summaries (`search/summaries.py`)

| Concern | Design |
|---|---|
| Model | `search.summaries.model`, a Gemini Flash-class model (exact id pinned in PR 4), via `google-genai` `client.models.generate_content`, temperature 0, max ~400 output tokens. Same `GEMINI_API_KEY`. |
| Input | Post title + cleaned sections (after boilerplate removal). Longest post ~2,600 words (~4k tokens); well inside the context window, no truncation. |
| Prompt (v1) | "Summarise this newsletter post in 100–150 words using only its content. Name its main argument, the frameworks or models it introduces (with their names), concrete advice, and who it is for. No preamble." `SUMMARY_PROMPT_VERSION = 1`. |
| Validation | Non-empty, 60–200 words, no refusal text; else one retry, then failure (stage `summarize`). |
| Cache | `summary_cache` keyed by sha256(model, prompt version, title, cleaned text). Unchanged posts never re-summarised, even across generations. GC with the embedding cache. |
| Storage | One row in `passages`: `passage_id = {post_id}#summary`, `kind = 'summary'`, `n = -1`, `heading = null`, `body = summary`, post metadata copied. `posts.summary_sha256` set. |
| Embedding input | `title: {post title} \| text: Summary\n{summary}` (same model and dimension as passages). |
| API use | Shared Gemini client (`search/gemini.py`): token-bucket pacing (`summary_rpm`, `summary_tpm`), retries with backoff, circuit breaker, no retry on 400/401/403. ≤ 2 requests in flight. |
| Failure | Passages are still written; `posts.summary_sha256 = null`; `index_failures` row (stage `summarize`); retried next run; counts toward freshness only for passages, not summaries. |
| Cost | 252 posts × ~3.5k input + ~200 output tokens ≈ 0.9M tokens once (well under $1 on a Flash-class model); ~1 post a week after. |
| Privacy | Full cleaned post text goes to Google once per post (the same text already sent in passages for embedding). |

## 6. Search (`search/service.py`)

### 6.1 Request validation (`SearchRequest`, Pydantic)

| Field | Rule |
|---|---|
| `query` | required, trimmed, 1–1,000 chars |
| `limit` | 1–30, default 10 |
| `category` | list; each must match a category in the active generation (case-insensitive); error lists valid names |
| `since`, `until` | ISO dates; `since ≤ until`; error shows `YYYY-MM-DD` |
| `kind` | `all` (default), `passages`, `summaries` → SQL filter on `passages.kind` |

### 6.2 Pipeline

1. Resolve the active generation (cached 5 s): schema, model, dimension.
2. **Query embedding** (Gemini, active model) and **BM25** run concurrently, each with a timeout.
3. **BM25 top-50**: safe query builder (keep `"quoted phrases"`, drop stop words unless nothing else is left, escape each term, OR-join). Boosts heading × 2, title × 1.5, body × 1 (`bm25_boosts`, tunable by the eval script). Filters in the same SQL.
4. **Vector top-50**: exact cosine distance over passages passing the filters.
5. **RRF**: `score = Σ 1/(60 + rank)`; ties → better single rank, then `passage_id`; keep `found_by`.
6. **Re-rank** the top 40 with Qwen3 (§7).
7. **Diversify**: at most 3 passages and 1 summary per post.
8. **Hydrate** and return the top `limit`.

Title boost note: every passage repeats its post's title, which lowers title-term IDF for long posts and lifts all of a post's passages on a title match. Hence the lower title boost; if evaluation shows a problem, add a post-level BM25 over `posts(title, subtitle)` as a third RRF list.

### 6.3 Degraded modes

| Problem | Behaviour | Flag |
|---|---|---|
| Gemini down, timeout, quota | Skip vector list; BM25 → re-rank | `semantic: false` + note |
| Re-ranker fails or times out | Return RRF order | `reranked: false` + note |
| DB down | Tool error: "Start the database: `docker compose up -d`" | — |
| No active generation | Tool error: "Run `archive index`" | — |
| Invalid arguments | Tool error with the allowed values | — |

### 6.4 BM25 query sketch (verified against the pinned pg_search version in PR 5)

```sql
SELECT passage_id, paradedb.score(passage_id) AS bm25
FROM idx_g{n}.passages
WHERE passage_id @@@ paradedb.boolean(should => ARRAY[
        paradedb.boost(2,   paradedb.match('heading', $1)),
        paradedb.boost(1.5, paradedb.match('title',   $1)),
        paradedb.match('body', $1)
      ])
  AND ($2::text[] IS NULL OR categories && $2)
  AND ($3::date IS NULL OR published_date >= $3)
  AND ($4::date IS NULL OR published_date <= $4)
ORDER BY bm25 DESC
LIMIT 50;
```

## 7. Re-ranker (`search/rerank.py`)

- `Qwen/Qwen3-Reranker-0.6B` (Apache 2.0, ~1.2 GB), cached in `{output_dir}/models/`, offline after first download.
- `transformers` (`AutoModelForCausalLM`) on Apple MPS (CPU fallback), fp16, batch size 8.
- Yes/no relevance prompt from the model card (instruction: "Given a question, find passages from a leadership newsletter that answer it"); score = probability of "yes". Template copied from the model card in PR 6.
- Pair input: query + `"{title} — {heading}\n{body}"`, truncated to 512 tokens.
- Loaded at MCP server start (background thread); timeout `rerank_timeout_seconds` (10).
- `Reranker` protocol; tests use `FakeReranker`. Expected ~1–2 s for 40 pairs (measured by the eval script).

## 8. MCP server (`mcp_server.py`, started by `archive mcp`)

- Official `mcp` Python SDK (`FastMCP`), stdio, no network port.
- Startup: config and secrets; read-only `psycopg` pool; schema check; re-ranker loads in background.
- Tool `search` (read-only hint): input per §6.1; structured output plus a readable text rendering:

```json
{
  "query": "how to give feedback to a senior engineer",
  "filters": {"category": null, "since": null, "until": null},
  "semantic": true,
  "reranked": true,
  "results": [{
    "rank": 1, "score": 0.93, "found_by": "both", "kind": "passage",
    "id": "giving-hard-feedback#4",
    "heading": "Feedback up the chain",
    "text": "…",
    "post": {"id": "giving-hard-feedback", "title": "…", "url": "https://…",
             "published_date": "2025-03-12", "categories": ["Leadership"]}
  }],
  "notes": []
}
```

- Tool description built from the active generation: what the archive is, post count, date range, valid categories, "results are passages and post summaries; summaries help find posts, passages are the evidence to quote; for broad questions start with `kind: \"summaries\"`, then search passages; cite the post URLs".
- Logs: `{output_dir}/logs/mcp-YYYYMMDD.log` + stderr, never stdout; no keys or passage text.
- Registration: `claude mcp add newsletter-archive -- /path/to/leadership-guidance/.venv/bin/archive mcp --config /path/to/leadership-guidance/config.yaml` (Claude Desktop: same command in `claude_desktop_config.json`).

## 9. Configuration (`config.yaml` → `search:`)

| Setting | Default |
|---|---|
| `embedding_model` / `embedding_dimension` | `gemini-embedding-2` / `3072` |
| `embed_batch_size` | `50` |
| `embed_rpm` / `embed_tpm` | the key's quota tier |
| `max_embed_tokens_per_run` | `1500000` |
| `circuit_breaker_failures` | `3` |
| `cache_retention_days` | `90` |
| `boilerplate_min_posts` / `boilerplate_similarity` | `5` / `0.8` |
| `chunk_words` / `chunk_overlap_words` | `300` / `50` |
| `mass_delete_min` / `mass_delete_fraction` | `5` / `0.10` |
| `quality_gate_max_recall_drop` | `2` (points) |
| `bm25_boosts` | heading `2`, title `1.5`, body `1` |
| `candidates` / `rerank_top` | `50` / `40` |
| `reranker_model` / `rerank_timeout_seconds` | `Qwen/Qwen3-Reranker-0.6B` / `10` |
| `max_per_post` / `default_limit` / `max_limit` | `3` / `10` / `30` |
| `db_host` / `db_port` / `db_name` | `127.0.0.1` / `5433` / `archive` |
| `summaries.enabled` / `summaries.model` | `true` / a Gemini Flash-class model id (pinned in PR 4) |
| `summaries.max_tokens_per_run` | `1500000` |
| `summary_rpm` / `summary_tpm` | the key's quota tier |

## 10. Code layout

```
docker-compose.yml
db/init/20_archive_roles.sh
src/newsletter_archive/
  search/
    models.py          # Passage, SearchRequest, SearchResult (Pydantic)
    boilerplate.py     # exact + MinHash/LSH near-duplicate clustering (pure)
    fingerprint.py     # canonical JSON + sha256 (pure)
    sentences.py       # in-repo sentence splitter (pure)
    chunking.py        # greedy packing with overlap (pure)
    gemini.py          # shared Gemini client: token-bucket pacing, retries, circuit breaker
    embeddings.py      # Embedder protocol, GeminiEmbedder, validation, cache
    summaries.py       # Summarizer protocol, GeminiSummarizer, prompt, validation, cache
    db.py              # connections, advisory lock, migrations, generation helpers
    migrations/001_init.sql
    planner.py         # fingerprint diff, guards, cost estimate (pure over inputs)
    indexer.py         # orchestration: start → report
    generations.py     # build, catch-up, gate, swap, rollback
    keyword.py         # safe BM25 query + SQL
    vector.py          # exact vector search SQL
    fusion.py          # RRF, diversification (pure)
    rerank.py          # Reranker protocol, Qwen3Reranker
    service.py         # search pipeline + fallbacks
  mcp_server.py        # FastMCP app and search tool
scripts/eval_search.py
```

New dependencies (capped, in the lockfile): `psycopg[binary,pool]`, `pgvector`, `google-genai`, `mcp`, `transformers`, `torch`, `numpy`, `pydantic`; `hypothesis` (dev).

## 11. CLI and the weekly job

```
archive index [--limit N] [--dry-run] [--verbose] [--config PATH]
              [--refresh] [--no-swap] [--swap] [--rollback] [--force]
              [--retry-failed] [--allow-mass-delete] [--yes]
archive mcp [--verbose] [--config PATH]
archive run            # discover → fetch → extract → index; exit 3 if indexing failed or freshness tripped
```

`scripts/scheduled-run.sh` starts the database first (`docker compose up -d db`, waits for healthy) and notifies on exit 3. Docker Desktop must start at login.

## 12. Testing

| Layer | How | Runs in |
|---|---|---|
| Pure logic | Boilerplate clustering, fingerprint, sentences, chunking (golden + property), planner guards and cost estimate, RRF, diversification, validation | Everywhere |
| Summaries | `FakeSummarizer`: cache hits, validation retry, failure keeps passages, retry next run, disabled mode, fingerprint change on prompt version | Everywhere / CI |
| Indexer and generations | Fake embedder and summarizer + real Postgres: incremental, title-change re-index, deletion guard, ceiling, breaker, catch-up before swap, gate, rollback, stale-run abort, concurrent-run refusal, cache GC | CI (ParadeDB service container); locally when the DB is up |
| Search service | Fake embedder and re-ranker + real Postgres; degraded modes | CI + local |
| MCP tool | In-process MCP client with fakes | CI + local |
| Real Gemini / Qwen3 | Opt-in smoke tests (`api`, `model` markers) | Local only |
| Secrets | Grep logs, outputs and tool responses for keys and passwords | CI |

No test reaches the internet; Postgres on 127.0.0.1 is allowed by the existing network guard.

**CI pipeline (GitHub Actions, every PR and push to `main`):**

| Job | What | Gate |
|---|---|---|
| `unit` | pytest, no DB, Python 3.11 and 3.12, coverage data saved | all pass |
| `integration` | pytest `-m db` against the compose database (pinned ParadeDB tag), migrations applied; fakes for Gemini, summarizer, re-ranker; no PyTorch | all pass |
| `coverage` | combines unit + integration coverage, branch coverage on | fails under **95%** |
| `lint` | ruff check, ruff format, mypy | clean |
| `safety` | `scripts/check_safety.py` (cookie, keys, DB passwords, site names) | clean |

`pragma: no cover` only on thin adapters that call the real Gemini API or load the Qwen3 model; they are covered by opt-in local smoke tests (`api`, `model` markers). `main` is protected: all jobs must pass before merge.

**Review per PR:** Claude runs `/code-review high` and fixes findings; the PR template checklist is filled; the user reviews and merges.

## 13. Evaluation (`scripts/eval_search.py`)

- Reads `{output_dir}/eval-queries.yaml` (git-ignored): ~30 questions, each tagged `local`, `intent`, `global` or `multi-hop`, with expected post ids.
- Reports post-level recall@10, MRR and latency for keyword only, semantic only, RRF, and RRF + re-rank, each with and without summaries, overall and per question type; per generation (`--generation N`) for the quality gate.
- Decision rules: add contextual retrieval if `local`/`intent` recall@10 < 0.8; consider a light graph if `multi-hop` stays low after that.
- Used to tune boosts, `candidates`, `rerank_top` and chunk size.

## 14. Risks

| Risk | Mitigation |
|---|---|
| Docker not running | Auto-restart; scheduled script starts it; clear tool error |
| pg_search syntax changes | Pinned image tag; SQL in one module; DB tests in CI |
| Post text sent to Google | Accepted; paid-tier key; only passage text + title/heading |
| PyTorch adds ~1 GB | Accepted for local re-ranking |
| Re-ranker latency | Measured; configurable; RRF fallback |
| Truncated manifest wipes the index | Deletion guard |
| Outage or runaway spend | Circuit breaker; spending ceiling; dry-run estimate |
| Boilerplate in results | Per-run exact + near-duplicate filter; phase-1 extractor fix |
| Stale or bad new generation | Catch-up, verification, quality gate, rollback |
| Silent chunking drift | In-repo splitter; golden tests tied to `CHUNKER_VERSION` |
| Data loss | Rebuildable from JSON; cache avoids re-paying; retired generation kept |
| Key or password leak | `Secret` wrapper; no connection strings in logs; safety check; tests |

## 15. To verify while building

Verified in PR 1 (ParadeDB `0.26.2-pg17`, pg_search 0.26.2, pgvector 0.8.6):
- BM25 index: `USING bm25 (passage_id, (col::pdb.simple('stemmer=english','ascii_folding=true')), …)`; `key_field` is deprecated and omitted. Queries: `col ||| 'terms'` (match any), `col ### 'phrase'`, `'term'::pdb.boost(2)`, `pdb.score(passage_id)`; SQL filters combine in the same query.
- **Stemming runs before accent folding** ("résumé" → `resume`, "resume" → `resum`), so passages store accent-folded `search_title`, `search_heading`, `search_body` (folded in Python) and queries are folded the same way; `title`, `heading`, `body` keep the original text for display.
- The image installs `vector` and `pg_search` in `template1`, so test databases get them; the roles script is mounted beside the image's own bootstrap script.
- First-start health check uses TCP so it passes only after the init scripts finish.

Verified in PR 3 (google-genai 2.31, gemini-embedding-2): batching, the 100-per-request limit, normalised output, silent truncation and no way to disable it in the Developer API (see §5.6); invalid dimensions return HTTP 400; `count_tokens` works for the model.

Still to verify:

- ParadeDB image tag; `pg_search` DDL, English stemmer, boost and score functions; BM25 index on per-schema tables.
- `gemini-embedding-2` pricing (check before the first full build in PR 5).
- `Qwen3-Reranker-0.6B` prompt template and latency on MPS.
- `mcp` SDK version and structured-output API.
- Gemini Flash-class model id for summaries, its pricing, rate limits and `generate_content` config fields.

## 16. PR stack

1. Postgres setup: compose, roles, `001_init.sql` (incl. `summary_cache`, `kind`), `create_generation`, migrations runner, advisory lock, config + `.env` keys, CI database
2. Pure preprocessing: boilerplate clustering, fingerprint, sentence splitter, chunker with golden and property tests; phase-1 extractor fix
3. Shared Gemini client + embedder: pacing, retries, circuit breaker, validation, truncation guard, embedding cache
4. Post summarizer: prompt v1, validation, `summary_cache`, failure handling
5. Indexer: planner (guards, both ceilings, dry-run estimate), writes (passages + summaries), verify, runs and failures, freshness, cache GC, `run` stage, exit 3, scheduled script
6. Generations: build, catch-up, quality gate, swap, rollback
7. Retrieval: BM25 query builder, vector search, RRF, filters incl. `kind`
8. Re-ranker, diversification (3 passages + 1 summary per post), search service with fallbacks
9. MCP server, tool description, setup docs
10. Evaluation script (tagged questions, with/without summaries), safety-check rules, README, acceptance on the real archive

## 17. Review findings → where addressed

| # | Finding | Section |
|---|---|---|
| 1 | Plan compared text hash only | §5.2 fingerprint |
| 2 | Boilerplate set drifted | §5.3 per-run recompute, via fingerprint |
| 3 | `--no-swap` candidate went stale | §5.5 catch-up |
| 4 | No mass-deletion guard | §5.1 step 6 |
| 5 | Silent truncation | §5.4 token guard, §5.6 truncation |
| 6 | BM25 title skew | §6.2 boosts, title note |
| 7 | Near-duplicate boilerplate | §5.3 MinHash/LSH |
| 8 | Lock in the wrong place | §5.1 advisory lock |
| 9 | Crashed runs stuck "running" | §5.1 step 1 |
| 10 | No breaker or ceiling | §5.6 breaker, §5.1 ceiling |
| 11 | Chunker drift via dependencies | §5.4 in-repo splitter, golden + property tests |
| 12 | Table-name suffixes | §4.2 schema per generation |
| 13 | No quality gate | §5.5 gate |
| 14 | Cache growth, precision | §4.1 float32 cache, §5.6 GC |
| 15 | No freshness signal | §5.7 freshness |
