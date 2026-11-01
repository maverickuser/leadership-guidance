## 0. Design (done)

- [x] 0.1 Low-level design (`lld.md`), principal review applied (v2), retrieval options reviewed and post summaries added (v3)
- [x] 0.2 Approach approved: hybrid BM25 + semantic + re-rank, plus one summary per post; contextual retrieval and graphs deferred to the evaluation

## Every PR (definition of done)

- CI green: `test` (unit, Python 3.11 and 3.12), `integration` (compose database), `coverage` (combined, branch coverage, fails under 95%), `lint` (ruff, mypy), `safety`
- New code ships with its tests; coverage of the PR's new modules ≥ 95%
- Claude runs `/code-review high` on the PR and fixes findings before handing over
- PR template checklist filled (tasks covered, spec scenarios, tests, coverage line, no secrets)
- The user reviews and merges; `main` requires all CI checks

## 1. Postgres setup (PR 1)

- [x] 1.1 `docker-compose.yml` with a pinned `paradedb/paradedb` tag, `127.0.0.1:5433`, bind mount `{output_dir}/postgres`, health check, restart policy
- [x] 1.2 `db/init/20_archive_roles.sh`: `archive_writer` and `archive_reader` roles, passwords from `.env`
- [x] 1.3 `migrations/001_init.sql`: extensions, `schema_version`, `generations`, `embedding_cache`, `summary_cache`, `index_runs`, `index_failures` (key post + stage), `create_generation(n)` with `passages.kind`, `posts.summary_sha256`, BM25 index
- [x] 1.4 `search/db.py`: connections per role (pool added with the MCP server in PR 9), migrations runner, advisory lock, active-generation lookup
- [x] 1.5 Config: `search` section incl. `summaries`, validation; `.env.example` with `GEMINI_API_KEY` and DB passwords; secrets in the `Secret` wrapper
- [x] 1.6 CI pipeline: split `unit` and `integration` jobs; `integration` starts the compose database (same pinned ParadeDB image); `coverage` job combines both and gates at 95% with branch coverage; `db` pytest marker, DB tests skipped locally when no DB is up
- [x] 1.7 `fail_under = 95`, `branch = true`; `pragma: no cover` allowed only on thin real-API/model adapters (tested by opt-in local smoke tests)
- [x] 1.8 PR template: search checklist items (coverage line, `/code-review` done, no API keys or DB passwords)

## 2. Pure preprocessing (PR 2)

- [x] 2.1 `boilerplate.py`: exact groups + MinHash/LSH near-duplicates, ≥ 5 posts, 20% loss warning
- [x] 2.2 `fingerprint.py`: canonical JSON incl. chunk settings, embed input version, summary model and prompt version
- [x] 2.3 `sentences.py`: in-repo splitter with abbreviation, decimal, initial and quote cases
- [x] 2.4 `chunking.py`: section merge, greedy packing 300/50, token guard, `CHUNKER_VERSION`
- [x] 2.5 Golden snapshot and property tests (hypothesis)
- [x] 2.6 Phase-1 extractor fix: extra intro prefixes from the local `config.yaml` (`boilerplate_prefixes`), stop-at-first-non-match rule kept; `archive extract --refresh` run locally (boilerplate left in post JSON: 208 → 38 paragraphs, removed at index time)

## 3. Gemini client and embedder (PR 3)

- [x] 3.1 `gemini.py`: client from `GEMINI_API_KEY`, token-bucket pacing, retries with backoff and `Retry-After`, no retry on 400/401/403, circuit breaker
- [x] 3.2 `embeddings.py`: passage, summary and query input formats; batching; response validation and normalisation; truncation disabled; embedding cache
- [x] 3.3 Verify `gemini-embedding-2` batch limit, truncation flag, normalisation; opt-in `api` smoke test

## 4. Post summarizer (PR 4)

- [ ] 4.1 Pin the Gemini Flash-class model id; verify pricing and `generate_content` config
- [ ] 4.2 `summaries.py`: prompt v1, temperature 0, output cap, validation (60–200 words), one retry, `summary_cache`
- [ ] 4.3 Tests with `FakeSummarizer`: cache hit, invalid output retry, failure path, disabled mode
- [ ] 4.4 Opt-in `api` smoke test: summarise 3 real posts and review the summaries by hand

## 5. Indexer (PR 5)

- [ ] 5.1 `planner.py`: fingerprint diff, missing summaries, deletion guard, embed and summary token ceilings, dry-run estimate
- [ ] 5.2 `indexer.py`: start (locks, stale runs, migrations) → load → clean → fingerprint → plan → chunk + summarise → embed → write (one transaction per post) → verify → report
- [ ] 5.3 Failure tracking per stage, 3-attempt skip, `--retry-failed`; summary failure keeps passages
- [ ] 5.4 Freshness check; cache GC for embeddings and summaries
- [ ] 5.5 `archive index` options; `archive run` ends with index; exit 3; `scripts/scheduled-run.sh` starts the DB first
- [ ] 5.6 Indexer tests against real Postgres with fakes: incremental, title change, guard, ceilings, breaker, stale-run abort, concurrent refusal

## 6. Generations (PR 6)

- [ ] 6.1 `generations.py`: build beside live, catch-up, verify, quality gate, swap, keep one retired, rollback
- [ ] 6.2 `--refresh`, `--no-swap`, `--swap`, `--rollback`, `--force`
- [ ] 6.3 Tests: catch-up before swap, gate blocks, rollback restores and catches up, summaries reused from cache on rebuild

## 7. Retrieval (PR 7)

- [ ] 7.1 `keyword.py`: safe query builder (phrases, stop words, escaping), boosted BM25 SQL, filters incl. `kind`
- [ ] 7.2 `vector.py`: exact cosine search with the same filters
- [ ] 7.3 `fusion.py`: RRF with deterministic ties and `found_by`
- [ ] 7.4 Verify pg_search DDL, stemmer, boost and score functions on the pinned tag

## 8. Re-ranker and search service (PR 8)

- [ ] 8.1 `rerank.py`: Qwen3-Reranker-0.6B on MPS, model-card prompt, timeout, `FakeReranker`
- [ ] 8.2 Diversification: at most 3 passages and 1 summary per post
- [ ] 8.3 `service.py`: request validation, concurrent BM25 + query embedding, degraded modes (`semantic: false`, `reranked: false`), hydration
- [ ] 8.4 Service tests with fakes and real Postgres

## 9. MCP server (PR 9)

- [ ] 9.1 `mcp_server.py` (FastMCP, stdio): `search(query, limit, category, since, until, kind)`, structured + text output, tool errors
- [ ] 9.2 Tool description from the active generation: post count, date range, categories, how to use summaries vs passages
- [ ] 9.3 Logging to file and stderr only; no keys or passage text
- [ ] 9.4 `archive mcp` command; in-process MCP client tests; README setup for Claude Code and Claude Desktop

## 10. Evaluation, safety and acceptance (PR 10)

- [ ] 10.1 `scripts/eval_search.py`: tagged questions, recall@10 and MRR per stage, with and without summaries, per question type
- [ ] 10.2 Write ~30 eval questions (local, intent, global, multi-hop) in the git-ignored `eval-queries.yaml`
- [ ] 10.3 Safety-check rules for API keys and DB passwords; secret grep tests over logs and tool responses
- [ ] 10.4 Acceptance on the real archive: full index, cost and timing recorded, eval run, tune boosts and candidates
- [ ] 10.5 Decide from the eval: contextual retrieval needed? (rule in `lld.md` §13)
