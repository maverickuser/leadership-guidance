## MODIFIED Requirements

### Requirement: Semantic index
The indexer SHALL embed every passage and post summary with the configured embedding model, using the post title and section heading as context together with the text, and SHALL store the vectors with their passages. By default the model SHALL be a local Qwen3-Embedding model loaded from a folder named in `config.yaml` and run on this machine, so indexing makes no embedding API calls. Google's `gemini-embedding-2` through the Gemini API SHALL remain available as a configured option. Queries SHALL be embedded with the same model and dimension as the store being searched, in that model's query form. For the local model the query form is the archive instruction ("Given a question, find passages from a leadership newsletter that answer it") followed by the query, and passages and summaries are embedded without an instruction. The model, its output dimension and its input format SHALL be recorded with each store.

#### Scenario: Every passage embedded
- **WHEN** indexing finishes with N passages
- **THEN** the store holds N vectors of the configured dimension, one per passage

#### Scenario: Local model needs no API key
- **WHEN** the configured model is the local one, `GEMINI_API_KEY` is not set and summaries are disabled
- **THEN** `archive index` embeds every passage and makes no Gemini call

#### Scenario: Store searched with its own model
- **WHEN** the live store was built with `gemini-embedding-2` and the configured model is the local one
- **THEN** queries against the live store are embedded with `gemini-embedding-2`, and queries against a store built with the local model use the local model

### Requirement: Embedding API use
When the embedding model is `gemini-embedding-2`, embedding calls SHALL be batched and paced to stay within configured requests-per-minute and tokens-per-minute limits, SHALL retry on rate limits, timeouts and server errors with exponential backoff (honouring any retry-after hint), and SHALL NOT retry on authentication or invalid-request errors. After 3 consecutive failed batches the indexer SHALL stop calling the API for the rest of the run and mark the remaining posts as failed. Inputs SHALL never be silently truncated: passages over 2,000 estimated tokens SHALL be split further, and API truncation SHALL be disabled where the API allows it. Every vector, from the API or the local model, SHALL be checked for the expected count, size, finite values and non-zero length before it is stored. Identical input text SHALL never be embedded twice for the same model and dimension. If the API key is needed and is missing or rejected, indexing SHALL stop with a clear message and leave the store unchanged. Only passage and summary text with its title and heading SHALL be sent for embedding, and only a post's title and cleaned text for summary generation. Summary generation SHALL use the same pacing, retry, circuit-breaker and validation rules, with its own rate limits.

#### Scenario: Missing key
- **WHEN** the embedding model is `gemini-embedding-2`, `GEMINI_API_KEY` is not set and the user runs `archive index`
- **THEN** the command exits 1 telling the user to add the key to `.env`, and the store is unchanged

#### Scenario: Rate limited
- **WHEN** the Gemini API returns a rate-limit error once
- **THEN** the batch is retried after a backoff and indexing completes

#### Scenario: Outage
- **WHEN** 3 batches in a row fail after retries
- **THEN** no further API calls are made in that run and the remaining posts are recorded as failed

#### Scenario: Bad local vector
- **WHEN** the local model returns a vector with a non-finite value
- **THEN** that post fails with a clear message and nothing is stored for it

### Requirement: Spending ceiling
Before calling the API the indexer SHALL estimate the tokens it will send (cache misses only), separately for embedding with an API model and for summary generation (input plus output). If either estimate exceeds its limit, `search.max_embed_tokens_per_run` (default 1,500,000) or `search.summaries.max_tokens_per_run` (default 1,500,000), it SHALL stop with the estimates unless `--yes` is given. Embedding with the local model costs nothing and SHALL NOT count towards the ceiling; the dry run SHALL report how many inputs it would embed locally.

#### Scenario: Large rebuild
- **WHEN** a `--refresh` with `gemini-embedding-2` would send 2,000,000 tokens
- **THEN** the command prints the estimate and exits 1 without calling the API

#### Scenario: Local rebuild
- **WHEN** a `--refresh` with the local model would embed 9,000 passages
- **THEN** the dry run reports 9,000 local embeddings and $0 for embedding

## ADDED Requirements

### Requirement: Local embedding model
The local embedding model SHALL be loaded offline from the configured folder (a fine-tuned model is a folder of weights the user keeps outside the repo) and SHALL never be downloaded from, or uploaded to, any service by this tool. It SHALL run on Apple MPS when available, with a CPU fallback. If the folder is missing or is not a usable model, indexing and search SHALL stop with a message naming the setting to fix; keyword search SHALL keep working while the semantic side is unavailable. The local embedder and the local re-ranker SHALL never run on the GPU at the same time.

#### Scenario: Missing model folder
- **WHEN** `search.local_model_path` names a folder that does not exist
- **THEN** `archive index` exits 1 saying the folder was not found and which setting to fix, and the store is unchanged

#### Scenario: Search while the model is missing
- **WHEN** the MCP server starts and the local model folder is missing
- **THEN** searches still return keyword results and say semantic search is unavailable

#### Scenario: Embedder and re-ranker share the GPU
- **WHEN** a query embedding and a re-ranking are requested at the same moment
- **THEN** one waits for the other to finish, and both complete
