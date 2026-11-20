## Context

Search uses Gemini (`gemini-embedding-2`, 3,072 dimensions) to embed about 17,100 rows (16,283 passages and 839 summaries) and every query. A private experiment (`experiments/2026-11-embedding-finetune/` in the datasets folder) fine-tuned Qwen3-Embedding-0.6B on this archive. Its best version (v5, distilled from Gemini) with "setting A" scored as follows, measured on 20 Nov 2026:

| Recall@10, full pipeline | Gemini + A | v5 + A |
|---|---|---|
| 191 questions on unseen posts | 0.87 | 0.83 |
| · situational | 0.89 | 0.80 |
| 36 human questions | 0.86 | 0.85 |
| p90 search time | 9.0 s (busy Mac) | 7.9 s |

The re-ranker already runs a Qwen model locally with `transformers` on Apple MPS, so the runtime pieces are in place. The sandbox copy `archive_qwen_ft` is the reference: it was embedded and evaluated with exactly the formats chosen below.

## Goals / Non-Goals

**Goals:**
- Index and search with the local model, with no Gemini embedding calls.
- Reproduce the measured setup exactly: the same model, input formats, precision and search settings.
- Keep the Gemini index version usable for `--rollback`.
- Keep the fine-tuned weights private.

**Non-Goals:**
- Training or retraining in the repo. The scripts stay in the private experiment folder.
- Replacing Gemini for summaries, `archive ask` or the answer judge.
- Running other embedding models (Ollama, ONNX, sentence-transformers). Only Qwen3-Embedding-style models are supported.

## Decisions

### 1. Model identity: `local:<name>` plus a folder path
- `search.embedding_model: local:qwen3-embedding-0.6b-archive-v5` selects the local embedder. `search.local_model_path` points at the folder holding the weights.
- The name, not the path, is what an index version and the embedding cache record. Moving the folder therefore doesn't invalidate the index.
- **Why not store the path:** paths change when folders move, and a path in the database says nothing about which weights it held.
- **Guard against a silent weight swap:** at load, the tool hashes the weights file and compares it with the hash recorded in the database the first time that name was used (details in the LLD). New weights must get a new name. The README says so.
- Any other value (`gemini-embedding-2`) keeps today's Gemini embedder.

### 2. Input formats (versioned)
- **Query:** `Instruct: Given a question, find passages from a leadership newsletter that answer it\nQuery:{query}`. This is the instruction v5 was trained with; the stock Qwen instruction scored 3 points lower.
- **Passage:** `{title}\n{heading}\n{text}`, with the heading line left out when there is none. **Summary:** `{title}\n{summary}`. No instruction on either, as the model card says.
- **Precision and pooling:** float16 weights on MPS (float32 on CPU), last-token pooling with left padding, then L2-normalised. These match the sandbox.
- **Versioning:** the format is versioned per model family (`EMBED_INPUT_VERSION` stays 1 for Gemini; local is 1 of its own). The fingerprint includes it, so a later format change re-embeds only what it must.

### 3. One embedder interface, chosen per index version
- A `LocalEmbedder` implements the existing `Embedder` protocol (`model`, `dim`, `batch_size`, `embed`). The embedding cache, the indexer and validation (`validate_vector`) are reused unchanged.
- One factory, `embedder_for(model, dim)`, returns the Gemini or local embedder. The indexer uses it for the version it is building. The search service already asks its factory for the active version's model, so searching a Gemini version after a rollback still embeds queries with Gemini.
- `archive index` and the MCP server need `GEMINI_API_KEY` only when the chosen model is Gemini, or when summaries are on.

### 4. One GPU at a time
- A process-wide lock in a small `search/gpu.py` wraps every forward pass, by both the local embedder and the re-ranker.
- **Why:** during the experiment, query embedding and re-ranking ran in different threads of one process and the MPS queue hung for 10 minutes. Serialising costs little, because a query embedding takes about 50 ms and re-ranking a few seconds.
- **Why not one model worker thread:** a lock is simpler, and it keeps the re-ranker's existing timeout logic.

### 5. Loading and failure modes
- **MCP server:** the model loads in the background at start-up, like the re-ranker. A search that arrives before the model is ready, or after loading failed, falls back to keyword search with a note. This uses the existing "semantic search skipped" path.
- **`archive index`:** loads the model up front, and stops with a message naming `search.local_model_path` when the folder is missing or unusable. The store stays unchanged.
- **Offline only:** `local_files_only=True` and `HF_HUB_OFFLINE=1` are set for this model, so it is never downloaded and nothing is sent to Hugging Face.

### 6. Search defaults (setting A)
- `search.candidates` goes from 50 to 100, and `fusion_weights` from `{keyword 1.0, semantic 2.0}` to `{keyword 0.25, semantic 2.0}`.
- These are config defaults, not model-specific, because they helped Gemini too (0.86 → 0.87 on unseen posts, 0.84 → 0.86 on human questions).
- `rerank_posts` stays at 10. Re-ranking 15 or 20 posts lost 3–5 points on broad human questions (measured as settings B and C).

### 7. Switching the live index
- **Steps:** `archive index --refresh --no-swap` builds a new version with the local model (about 35 min, $0). Then `archive index --swap` catches it up and runs the existing quality gate.
- **The gate:** it embeds the 36 human questions with Gemini for the live version (under 1¢, a live call) and with the local model for the new one. It then compares combined-stage recall: measured 0.85 against 0.86, inside the 2-point limit.
- The Gemini version is kept for `--rollback`.
- Each of these steps needs the user's go-ahead, as with any real-index change.

### 8. Where the weights live
- The merged v5 folder (about 1.2 GB) is copied once from the experiment to `{output_dir}/models/qwen3-embedding-0.6b-archive-v5/`, next to the re-ranker's cached model.
- `config.yaml` (never committed) points at it. `config.example.yaml` shows the setting with a placeholder path. Nothing about the weights enters the public repo.

## Decisions (made with the user)

- Use v5 + setting A and stop using Gemini embeddings, accepting the measured gap (20 Nov 2026).
- Keep Gemini for summaries, `ask` and the judge (proposal approved 20 Nov 2026).
- The fine-tuned model stays private and is never pushed to Hugging Face (standing rule).

## Risks / Trade-offs

- **About 4 points lower recall than Gemini + A**, and about 9 on situational questions. The user accepted this. Gemini stays available for rollback.
- **Search time:** p90 is 7.9 s, just under the 8 s target. The GPU lock adds waiting only when searches overlap. **Mitigation:** query embedding is short, and the latency check is in the acceptance PR.
- **New posts aren't in the training set.** The model has never seen them, but it was evaluated on unseen posts (0.83), so that number is the honest expectation. Retraining is a later change.
- **Memory:** two 0.6B models in one process take about 2.5 GB in float16. This was measured during the experiment's evals with no swapping.
- **Weights swapped under the same name** could silently mismatch the index. **Mitigation:** the hash check in Decision 1.

## Migration Plan

1. Merge the PR stack. Defaults change, and an existing `config.yaml` that names `gemini-embedding-2` keeps working.
2. Copy the v5 folder to the models folder, then set `embedding_model` and `local_model_path` in `config.yaml`.
3. `archive index --refresh --no-swap --dry-run`, then the real build, with the user's go-ahead.
4. `archive index --swap`, with the user's go-ahead. Rollback is `archive index --rollback`, which goes back to Gemini instantly.

## Open Questions

None.
