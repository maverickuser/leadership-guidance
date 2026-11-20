## Why

Search embeds every passage and every query with Gemini (`gemini-embedding-2`). That needs the network and the API key for each search, and costs money to index. A fine-tuned local model (Qwen3-Embedding-0.6B, trained on this archive) now comes close: measured on 20 Nov 2026 with the tuned search settings below, it scores 0.83 recall@10 on 191 questions about unseen posts and 0.85 on the 36 human questions, against Gemini's 0.87 and 0.86. The user chose to accept that gap and stop using Gemini for embeddings.

## What Changes

- **Local embedding model.** Passages, summaries and queries are embedded on this Mac with a local Qwen3-Embedding model loaded from a folder named in `config.yaml`, the same way the re-ranker runs today. No Gemini call is made to index or to search.
- **The fine-tuned model stays private.** The repo holds only the code that loads a model folder. The weights stay outside the repo, in the user's private folder, and are never uploaded anywhere.
- **Same input formats the model was trained and measured with.** Queries get the archive instruction ("Given a question, find passages from a leadership newsletter that answer it"). Passages and summaries are embedded as title, heading and text.
- **New index version.** Switching models builds a new index version (1,024 dimensions instead of 3,072) and swaps it in through the existing `--swap` path and quality check. The Gemini version stays available for `--rollback`.
- **Search setting A becomes the default:** keyword weight 0.25 (was 1.0) and 100 candidates per list (was 50). Measured on both models, it helps each one (Gemini 0.86 → 0.87; Qwen 0.80 → 0.83).
- **Gemini embeddings stay supported, not default.** An index version built with Gemini still works and is still searched with Gemini, so a rollback keeps working. Gemini is still used for post summaries, `archive ask` and the answer judge. This change is about embeddings only.
- **One GPU at a time.** The embedder and the re-ranker share the Mac's GPU. Calls into the models are serialised, because two at once hung the GPU during the experiment.
- **BREAKING (config):** the default `search.embedding_model` changes, and a new setting names the local model folder. An existing `config.yaml` that sets `embedding_model: gemini-embedding-2` keeps working.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `search-indexing`: the semantic index uses the configured local model, which needs no API, no spending estimate and no rate limits. Gemini embedding becomes an option.
- `hybrid-search`: queries are embedded with the model that built the live index version. Defaults change to keyword weight 0.25 and 100 candidates.
- `archive-cli`: new `search` settings (local model folder), new defaults, and the README setup steps.

## Impact

- **Code:** `search/embeddings.py` (a local embedder next to `GeminiEmbedder`), `search/indexer.py` and `search/service.py` (pick the embedder by the index version's model), `mcp_server.py` (load the model at start-up, like the re-ranker), `config.py` (new setting and defaults), and the re-ranker (share a GPU lock).
- **Dependencies:** none new. `torch` and `transformers` are already used by the re-ranker.
- **Live index:** one full rebuild with the local model, about 35 minutes on this Mac and $0, then a `--swap`. Both need the user's go-ahead.
- **Speed:** measured p90 search time is 7.9 s with setting A, within the 8 s target.
- **Out of scope:** the training scripts. They stay in the private experiment folder (`experiments/2026-11-embedding-finetune/`), and retraining on new posts is a later change.
