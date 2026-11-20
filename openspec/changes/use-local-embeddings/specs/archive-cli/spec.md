## MODIFIED Requirements

### Requirement: Configuration
The system SHALL read these settings from `config.yaml`:
- **`sources`:** the list of newsletters (see `newsletter-sources`).
- **Global settings:** `delay_seconds`, `max_retries`, `timeout_seconds`, `min_word_count`, `output_dir` (default `../leadership-coach-datasets`), `user_agent`.
- **`search`:** an optional section with `embedding_model` (default the local Qwen3-Embedding model; `gemini-embedding-2` is also accepted), `embedding_dimension`, `local_model_path` (the folder holding the local model's weights; default `{output_dir}/models/<model name>`), `chunk_words`, `chunk_overlap_words`, `candidates`, `fusion_weights`, re-ranker settings, `source_min_score` (the relevance a source's best result needs to be guaranteed a place, see `hybrid-search`), and a `summaries` sub-section with `enabled` (default true), `model` and `max_tokens_per_run`.

Defaults SHALL be those given in the project spec. The system SHALL read each source's cookie from the `.env` variable named by its `cookie_env`, and `GEMINI_API_KEY`, all from `.env`. `GEMINI_API_KEY` SHALL be required only for the features that call Gemini: `gemini-embedding-2` embeddings, summaries, `archive ask` and the answer judge.

These invalid settings SHALL be rejected with a config error:
- non-positive numbers;
- an overlap not smaller than `chunk_words`;
- a missing, empty or invalid `sources` list;
- a top-level `base_url`;
- an `embedding_model` that is neither a Gemini model (`gemini-…`) nor `local:<name>`.

The repository SHALL include `config.example.yaml` (with one example source per platform), `.env.example`, and a `.gitignore` that lists `.env`, `config.yaml` and `data/`.

#### Scenario: Custom delay
- **WHEN** `config.yaml` sets `delay_seconds: 5`
- **THEN** requests are at least 5 seconds apart, across all sources

#### Scenario: Invalid chunk overlap
- **WHEN** `config.yaml` sets `search.chunk_words: 100` and `search.chunk_overlap_words: 100`
- **THEN** the command exits 1 with a config error naming `chunk_overlap_words`

#### Scenario: Default model folder
- **WHEN** `config.yaml` has no `search` section
- **THEN** the local model is loaded from `{output_dir}/models/qwen3-embedding-0.6b-archive-v5`

#### Scenario: Old config keeps Gemini
- **WHEN** an existing `config.yaml` sets `embedding_model: gemini-embedding-2`
- **THEN** indexing and search keep using Gemini embeddings
