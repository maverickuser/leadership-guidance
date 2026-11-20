# Low-level design: use local embeddings

Builds on the approved `design.md`. Measured facts come from the private experiment (`experiments/2026-11-embedding-finetune/`, 15–20 Nov 2026) unless stated otherwise.

## 1. Measured facts

| Fact | Value | Source |
|---|---|---|
| Model | Qwen3-Embedding-0.6B + LoRA r16, merged (v5) | `models/v5/merged`, about 1.2 GB `model.safetensors` |
| Output dimension | 1,024, last-token pooling, L2-normalised | sandbox `archive_qwen_ft` |
| Rows to embed | 17,122 (16,283 passages, 839 summaries) | sandbox count, 20 Nov |
| Full re-embed on MPS (float16, batch 8) | about 35 min | `results/embed-v5.log` |
| Query embedding on MPS | about 50 ms | evals |
| p90 search, setting A, human 36 | 7.8 s | `results/v5A-human.txt` |
| Recall@10, setting A (unseen 191 / human 36) | v5 0.83 / 0.85; Gemini 0.87 / 0.86 | `results/v5A-*`, `geminiA-*` |
| Combined-stage recall, human 36 (the quality gate) | v5 0.85, Gemini 0.86 | same runs; inside the 2-point limit |
| Library versions | torch 2.14.1, transformers 5.20.2 (already pinned for the re-ranker) | `requirements-dev.lock` |
| Concurrency hazard | embedder and re-ranker threads in one process: the MPS command buffer never completed (10 min, 0.4% CPU) | `sample` stack, 20 Nov |

## 2. Config (`config.py`)

| Setting | Before | After |
|---|---|---|
| `embedding_model` | `gemini-embedding-2` | `local:qwen3-embedding-0.6b-archive-v5` (PR 4) |
| `embedding_dimension` | 3072 | **derived** when not set: 3,072 for `gemini-*`, 1,024 for `local:*` |
| `local_model_path` (new) | — | `""`, which means `{output_dir}/models/<name>`; relative paths resolve against the config file's folder |
| `local_batch_size` (new) | — | 8 |
| `candidates` | 50 | 100 (PR 4) |
| `fusion_weights` | keyword 1.0, semantic 2.0 | keyword 0.25, semantic 2.0 (PR 4) |

- **Validation:** `embedding_model` must start with `gemini-` or match `local:[a-z0-9._-]+`; otherwise it's a `ConfigError`.
- **Helpers:**
  - `SearchConfig.is_local_model(name)` is a static method, so it works for any index version's model.
  - `config.local_model_dir(name) -> Path` resolves the folder.
- **Upgrade safety:** existing configs that set `embedding_model: gemini-embedding-2` and leave out the dimension still get 3,072.

## 3. Input formats (`search/embeddings.py`)

`InputFormat` is a frozen dataclass with `version: str`, `query(q)`, `passage(title, heading, body)` and `summary(title, text)`.

- **`GEMINI_FORMAT`:** today's functions, with `version = "1"`. The stored fingerprint already holds `embed_input_version: 1`, and the fingerprint payload keeps the integer `1` for Gemini, so no Gemini post is re-indexed.
- **`LOCAL_FORMAT`:** `version = "local-1"`.
  - query: `"Instruct: Given a question, find passages from a leadership newsletter that answer it\nQuery:" + q`
  - passage: the non-empty lines of `title`, `heading` and `body`, joined by `"\n"`
  - summary: `title + "\n" + text`
- **`input_format(model) -> InputFormat`:** dispatches on the `local:` prefix.
- **Callers:**
  - The module-level `passage_input`, `summary_input` and `query_input` stay as Gemini aliases, so existing tests are untouched.
  - The indexer calls `input_format(self.model)` at lines 450 and 569.
  - The service calls `input_format(gen.embedding_model).query(query)` in `_embed_query`.
- **Fingerprint (`fingerprint.py`):** `post_fingerprint(post, sections, search, model)`, where `embed_input_version` = `EMBED_INPUT_VERSION` (int 1) for Gemini and `"local-1"` for local. The indexer passes `self.model`, the model of the version being written.
  - So a weekly run that updates a live Gemini version while the config names the local model leaves fingerprints unchanged. This is tested.

## 4. Local embedder (`search/embeddings.py`, `search/gpu.py`)

```python
# search/gpu.py
GPU_LOCK = threading.Lock()   # every forward pass on the local GPU models holds it

class LocalEmbedder:          # Embedder protocol: model, dim, batch_size, embed()
    def __init__(self, name: str, folder: Path, dim: int, batch_size: int = 8, max_tokens: int = 2048) -> None
    def embed(self, texts: Sequence[str]) -> list[np.ndarray]
```

- **Loading:**
  - Sets `HF_HUB_OFFLINE=1`, then loads `AutoTokenizer.from_pretrained(folder, padding_side="left", local_files_only=True)` and `AutoModel.from_pretrained(folder, dtype=float16 on MPS / float32 on CPU, local_files_only=True)`.
  - Then `.to(device).eval()`.
  - Any `OSError` or `ValueError` becomes `LocalModelError("…not found or not a model: <folder>; set search.local_model_path")`.
- **embed:**
  1. `check_input` on each text (same as Gemini).
  2. Tokenize and reject texts over `max_tokens` with `EmbeddingInputError`, never truncating.
  3. Sort by length and run batches under `GPU_LOCK` with `torch.no_grad()`.
  4. Take `last_hidden_state[:, -1]`, convert to float, run `validate_vector` (normalise, check finite, non-zero, dim), then restore the input order.
- **Coverage:** the class is a thin adapter, `pragma: no cover`. The logic around it (format, order, validation, errors) sits in pure helpers with unit tests through a fake `encode` callable.
- **Weights check:** `weights_sha256(folder)` is the sha256 of `model.safetensors`, streamed, about 3 s for 1.2 GB.
  - Migration `003_embedding_models.sql` adds `public.embedding_models(name text PRIMARY KEY, weights_sha256 text NOT NULL, created_at timestamptz NOT NULL DEFAULT now())`, with `GRANT SELECT … TO archive_reader`.
  - **Writer (`archive index`):** inserts on first use. If a different hash is stored for the name, it raises `LocalModelError("the weights in <folder> differ from the ones <name> was indexed with; give the new weights a new name")`.
  - **Reader (MCP server):** on a mismatch or a missing row, semantic search is unavailable with that message, and keyword search continues.
- **Re-ranker:** `Qwen3Reranker._batch` takes `GPU_LOCK` around its forward pass. That's a 2-line change.

## 5. Choosing the embedder (`search/embedders.py`, new)

```python
def embedder_for(config: Config, *, for_queries: bool) -> Callable[[str, int], Embedder]
```

- **Local name:** returns one shared `LocalEmbedder` per (name, dim), loaded lazily under a lock. Queries use `batch_size=1`.
- **`gemini-*`:** the existing builders. `for_queries=True` is today's `gemini_query_embedder`, with one attempt and the query timeout. `for_queries=False` is today's indexer builder, with retries and the breaker.
- **Gemini key:** `MissingGeminiKeyError` is raised only when a Gemini embedder is actually built. `index_archive` drops its up-front key check, which moves to "Gemini embedder built" or "summarizer built".
- **Wiring:**
  - `indexer.index_archive`: `embedder = embedder_for(config, for_queries=False)`.
  - `mcp_server.make_service`, `evaluate.reader_service` and `gate_for`: `embedder_for(config, for_queries=True)`.
  - The gate therefore embeds the live Gemini version's queries with Gemini and the candidate's with the local model.
- **MCP start-up:** when the configured model is local, a background thread calls `factory(name, dim)` once, so the model loads while the server starts, like `Reranking`.
  - A search before it is ready waits up to `query_timeout_seconds` (8 s), then runs on keywords with the existing "semantic search skipped" note.
  - `gemini_query_embedder` stays as a thin alias for one release, then goes.

## 6. Indexer, dry run and spending ceiling

- `Indexer._embed_plan` keeps using `CachedEmbedder.missing_tokens`.
  - For a local model it reports `local embeddings: N inputs ($0)` and skips the `max_embed_tokens_per_run` check.
  - For Gemini nothing changes.
- **Empty or over-long input:** the `check_input` / `EmbeddingInputError` per-post failure path is reused unchanged.
- **Non-finite vector:** raises `EmbeddingResponseError`, so the post fails with a message and nothing is stored.
- **Embedding cache:** local vectors are cached under (sha256 of input, `local:<name>`, 1024), so a re-index after a rollback re-embeds nothing.

## 7. Errors and degraded modes

| Situation | `archive index` | MCP `search` |
|---|---|---|
| Model folder missing or unusable | exit 1: "Local model not found at <folder>. Set search.local_model_path or copy the model there." Store unchanged | keyword results + note "semantic search unavailable: local model not found …" |
| Weights differ from the recorded hash | exit 1, message names the fix | keyword results + note |
| Model still loading | n/a (loads first) | waits ≤ 8 s, then keywords + note |
| Live version is Gemini and the key is missing | only if a Gemini embedder is built: today's message | keyword results (today's behaviour) |

No message ever contains passage text or secrets. Paths are printed, and they are the user's own folders.

## 8. Secrets and privacy

- **The repo never contains weights,** model hashes or paths:
  - `config.example.yaml` shows `# local_model_path: ../leadership-coach-datasets/models/<name>`.
  - `check_safety.py` already blocks the datasets folder.
- **No network for the local model:** `local_files_only=True` and `HF_HUB_OFFLINE=1`, with telemetry off (`HF_HUB_DISABLE_TELEMETRY=1`, as the re-ranker does).

## 9. Testing

- **Unit (offline):**
  - Config derivation and validation.
  - `input_format` strings: exact text, heading omitted, summary.
  - Fingerprint: the Gemini payload is byte-identical to before, and the local one differs.
  - The `embedder_for` dispatch with fakes.
  - GPU lock: two threads, and a fake model asserts it's never re-entered.
  - Local-embedder helpers (order restore, too-long input, non-finite vector) through a fake encoder.
  - Dry run reports local inputs at $0, with no ceiling.
  - Index without `GEMINI_API_KEY`, using the local fake and summaries off.
- **Integration (`db`):**
  - Migration 003, and the reader can select from it.
  - Hash recorded and mismatch refused.
  - Build a version with a fake 8-dimension local embedder, swap, query, then roll back to a fake Gemini version whose queries go through the Gemini path.
- **Opt-in real model:** a new marker `model` ("loads the real local model; skipped unless `ARCHIVE_MODEL_TESTS=1`") embeds 3 texts and checks the dimension, the norm, and that query/passage cosine ranks a matching pair first.
- **Coverage:** at least 95% on new modules. `LocalEmbedder.__init__` and its forward pass are `pragma: no cover`, the same as `Qwen3Reranker`.

## 10. Rollout (after the stack is merged; each step needs the user's go-ahead)

1. Copy `experiments/2026-11-embedding-finetune/models/v5/merged/` to `../leadership-coach-datasets/models/qwen3-embedding-0.6b-archive-v5/`.
2. `archive index --refresh --no-swap --dry-run`: expect about 17,100 local embeddings, $0, and summaries reused from the cache.
3. `archive index --refresh --no-swap` (about 35 min). Do this right after merging, because until a local version is waiting, the weekly run stops with "model changed".
4. `archive index --swap`: the quality gate runs (Gemini query embeddings for the live version, under 1¢). Expected 0.85 against 0.86, a pass.
5. Check: the side-by-side questions through the MCP server, and the p90 time from `scripts/eval_search.py`.
6. Rollback if needed: `archive index --rollback`.
