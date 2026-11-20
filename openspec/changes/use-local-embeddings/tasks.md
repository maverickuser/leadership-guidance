## 0. Approvals

- [x] 0.1 Proposal approved 2026-11-20
- [x] 0.2 Specs approved 2026-11-20
- [x] 0.3 Design approved 2026-11-20
- [x] 0.4 Low-level design approved 2026-11-20 (spec tweak: `local_model_path` defaults to `{output_dir}/models/<name>`)
- [x] 0.5 PR plan approved 2026-11-20

## 1. Config and input formats (PR 1)

- [x] 1.1 `search.local_model_path` and `search.local_batch_size` settings; `embedding_dimension` derived from the model when not set; `embedding_model` validated (`gemini-…` or `local:<name>`); `local_model_dir()` helper (lld §2). Defaults unchanged in this PR.
- [x] 1.2 `InputFormat`, `GEMINI_FORMAT`, `LOCAL_FORMAT` and `input_format(model)`; the old module functions stay as Gemini aliases (lld §3)
- [x] 1.3 `post_fingerprint(..., model)`: Gemini payload byte-identical to before; local uses `"local-1"` (lld §3)
- [x] 1.4 Indexer and service use `input_format(model)` for the version being written or searched
- [x] 1.5 Unit tests: config derivation and validation, exact format strings, fingerprint stability

## 2. Local embedder, GPU lock and weights check (PR 2)

- [ ] 2.1 `search/gpu.py` with `GPU_LOCK`; the re-ranker's forward pass takes it (lld §4)
- [ ] 2.2 `LocalEmbedder`: offline load, MPS/CPU, last-token pooling, over-long input rejected, vectors validated, input order kept (lld §4)
- [ ] 2.3 Migration `003_embedding_models.sql` and `weights_sha256()`; the writer records the hash on first use and refuses a mismatch; the reader checks it (lld §4)
- [ ] 2.4 Unit tests with a fake encoder (order, too long, non-finite, lock never re-entered); `db` tests for the migration, reader grant and hash check; opt-in `model` marker and real-model test

## 3. Wiring: one embedder factory (PR 3)

- [ ] 3.1 `search/embedders.py` with `embedder_for(config, for_queries=...)`; a shared, lazily loaded local embedder (lld §5)
- [ ] 3.2 Indexer, MCP server, eval `reader_service` and the quality gate use it; the MCP server loads the model in the background at start-up
- [ ] 3.3 `GEMINI_API_KEY` is required only when a Gemini embedder or the summarizer is built
- [ ] 3.4 Dry run reports local embeddings at $0 and skips the embedding ceiling for local models (lld §6)
- [ ] 3.5 Degraded modes: missing or mismatched model gives a clear index error, and search falls back to keywords with a note (lld §7)
- [ ] 3.6 Tests: dispatch with fakes; index without a key; dry-run output; `db` end-to-end: build a local version, swap, search, roll back to a Gemini version (queries go through the Gemini path)

## 4. Switch the defaults and document (PR 4)

- [ ] 4.1 **Changed defaults:** `embedding_model: local:qwen3-embedding-0.6b-archive-v5`, `candidates: 100`, `fusion_weights: {keyword: 0.25, semantic: 2.0}`, with the measurements in the config comments
- [ ] 4.2 README: local embeddings setup (where the model folder goes, offline, private), the switch steps, rollback; `config.example.yaml`
- [ ] 4.3 Tests updated for the new defaults; the hybrid-search scenario where keyword matches don't crowd out meaning matches
- [ ] 4.4 After merge, with the user's go-ahead: copy the model, `--refresh --no-swap` (dry run first), `--swap` with the gate, check the side-by-side and p90 (lld §10)

## Every PR (definition of done)

- CI green: `test` (3.11, 3.12), `integration`, `coverage` (≥ 95%, new modules ≥ 95%), `lint`, `safety`
- Tests ship with the code; real-model checks only behind the opt-in `model` marker
- `/code-review high` run; fixes in a separate `Fix review findings on the <area>` commit
- PR template filled; this file's items ticked in the same PR
- The user merges; nothing is merged, force-pushed or skipped by the agent
- No weights, model paths or archive content committed
