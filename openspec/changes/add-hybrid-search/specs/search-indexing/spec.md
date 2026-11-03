## ADDED Requirements

### Requirement: Index input
The indexer SHALL use the archived post records in `{output_dir}/posts/*.json` as its only data source, indexing posts whose manifest status is `ok` or `partial`. It SHALL NOT read raw HTML or fetch posts from the newsletter site. A post file that fails schema validation SHALL be skipped with a logged error, and indexing SHALL continue.

#### Scenario: Only archived posts are indexed
- **WHEN** the manifest has 3 posts with status `ok`, 1 `partial` and 1 `failed`
- **THEN** the store contains passages from exactly the 4 `ok` and `partial` posts

#### Scenario: Invalid post file
- **WHEN** one `posts/*.json` file is not valid JSON
- **THEN** that post is reported as failed in the index summary and every other post is indexed

### Requirement: Boilerplate filter
Before splitting, the indexer SHALL remove boilerplate paragraphs: paragraphs of 8 or more words whose exact or near-duplicate copies (word-shingle similarity of at least `search.boilerplate_similarity`, default 0.8) appear in at least `search.boilerplate_min_posts` (default 5) distinct posts. The boilerplate set SHALL be recomputed on every run and stored with the index so it can be inspected. The rule SHALL be data-driven, with no site-specific text in the repository, and SHALL NOT modify the post JSON files. A post that loses more than 20% of its words to the filter SHALL be reported with a warning.

#### Scenario: Repeated intro dropped
- **WHEN** the same welcome paragraph appears in 70 posts
- **THEN** no passage contains it and the index summary reports it as boilerplate

#### Scenario: Reworded copy dropped
- **WHEN** a welcome paragraph appears in 40 posts and a copy with one changed job title appears in 3 more
- **THEN** all 43 copies are treated as boilerplate

#### Scenario: Shared quote kept
- **WHEN** a paragraph appears in only 3 posts
- **THEN** it is indexed normally

#### Scenario: New boilerplate appears
- **WHEN** a new sign-off paragraph reaches 5 posts during weekly updates
- **THEN** the next `archive index` removes it from all 5 posts' passages

### Requirement: Passages
The indexer SHALL split each cleaned post into passages along its `sections`. Sections shorter than 40 words SHALL be merged into the following section of the same post (or the previous one if last). A section longer than `search.chunk_words` (default 300) SHALL be split by packing whole paragraphs, then whole sentences, into windows of at most that many words, where each window after the first begins with trailing whole sentences of the previous window totalling at most `search.chunk_overlap_words` (default 50) words; a single sentence longer than the limit SHALL be split by words. Sentence splitting SHALL use code in this repository, not a third-party library. Each passage SHALL have a stable id `{post_id}#{n}`, numbered from 0 in reading order. Splitting SHALL be deterministic, and any change to its output SHALL require a new chunker version.

#### Scenario: Long section
- **WHEN** a section has 700 words with default settings
- **THEN** it becomes 3 passages, none longer than 300 words, each after the first starting with at most 50 words repeated from the one before

#### Scenario: Tiny section
- **WHEN** a post has a 12-word section followed by a 200-word section
- **THEN** both are in one passage whose heading is the first section's heading

#### Scenario: Deterministic ids
- **WHEN** the same post is split twice
- **THEN** both runs produce identical passage ids and text

### Requirement: Stored passage data
For each passage the store SHALL keep the passage text, its section heading, its position in the post, and its post's `id`, `title`, `url`, `author`, `published_date`, `categories` and `word_count`, so a search result can be returned without reading the JSON files.

#### Scenario: Self-contained result
- **WHEN** a passage is found by search
- **THEN** its post title, URL, date, categories and section heading are available from the store alone

### Requirement: Post summaries
When `search.summaries.enabled` is true (the default), the indexer SHALL generate one summary per post with a configurable Gemini text model (`search.summaries.model`) through the same `GEMINI_API_KEY`, from the post's title and cleaned text. The summary SHALL be 60–200 words, written only from the post's own content, and SHALL name the post's main ideas, frameworks and advice. Each summary SHALL be stored as a searchable unit of kind `summary` with id `{post_id}#summary`, carrying the same post metadata as passages, and SHALL be indexed for both keyword and semantic search alongside passages. Summaries SHALL be cached by post text, model and prompt version, so an unchanged post is never summarised twice. A response that is empty or outside the word range SHALL be retried once and then recorded as a failure. If a summary cannot be generated, the post's passages SHALL still be indexed, the missing summary SHALL be recorded as a failure (stage `summarize`), and it SHALL be retried on the next run.

#### Scenario: One summary per post
- **WHEN** indexing finishes for 252 posts with summaries enabled
- **THEN** the store holds 252 units of kind `summary`, one per post, each with a vector

#### Scenario: Unchanged post
- **WHEN** `archive index` runs again with no post changes
- **THEN** no summary generation calls are made

#### Scenario: Summary failure does not block passages
- **WHEN** summary generation fails for a new post after retries
- **THEN** its passages are searchable, the failure is recorded, and the next run generates the summary

#### Scenario: Summaries disabled
- **WHEN** `search.summaries.enabled` is false
- **THEN** no summary calls are made and no `summary` units are stored

### Requirement: Keyword index
The store SHALL support BM25-ranked keyword search over passages, with case- and accent-insensitive matching and English stemming, and SHALL weight matches in the section heading and post title above matches in the passage body, with configurable weights.

#### Scenario: Stemming
- **WHEN** a passage contains "delegating" and the query is "delegate"
- **THEN** the passage is a keyword match

#### Scenario: Heading outranks body
- **WHEN** two otherwise similar passages contain the query term, one in its section heading and one only in its body
- **THEN** the heading match ranks higher in keyword search

### Requirement: Semantic index
The indexer SHALL embed every passage with Google's `gemini-embedding-2` model through the Gemini API, using the post title and section heading as context together with the passage text, and SHALL store the vectors with their passages. Queries SHALL be embedded with the same model and dimension as the live store, using the model's retrieval-query form. The model name and output dimension SHALL be configurable.

#### Scenario: Every passage embedded
- **WHEN** indexing finishes with N passages
- **THEN** the store holds N vectors of the configured dimension, one per passage

### Requirement: Change detection
For each post the indexer SHALL compute a fingerprint covering every input that affects its index entries: cleaned text and headings, title, URL, published date, categories, status, the boilerplate set's effect on the post, chunker version, chunk settings, embedding input format, and, when summaries are enabled, the summary model and prompt version. A later `archive index` SHALL re-index exactly the posts whose fingerprint changed or that are new, SHALL remove posts no longer in the archive, and SHALL reuse stored embeddings for any passage whose embedding input is unchanged. A new store SHALL be required only when the embedding model or dimension changes; `--refresh` SHALL force a new store.

#### Scenario: Nothing changed
- **WHEN** `archive index` runs twice with no post changes
- **THEN** the second run makes no embedding calls and reports every post as unchanged

#### Scenario: Title corrected
- **WHEN** a post's title changes but its text does not
- **THEN** that post's passages are rewritten with the new title and re-embedded

#### Scenario: Chunk setting changed
- **WHEN** `search.chunk_words` changes
- **THEN** all posts are re-split in the live store, and only passages whose text changed are sent for embedding

#### Scenario: Model changed
- **WHEN** `search.embedding_model` differs from the live store's model
- **THEN** `archive index` stops and says a new store is needed, unless `--refresh` is given

### Requirement: Deletion guard
In one run the indexer SHALL NOT remove more than 10% of indexed posts or more than 5 posts, whichever is larger, unless `--allow-mass-delete` is given. When the guard trips, no posts SHALL be removed, the would-be removals SHALL be listed, and the rest of the run SHALL continue.

#### Scenario: Truncated manifest
- **WHEN** the manifest suddenly lists 20 of 252 indexed posts
- **THEN** no posts are removed and the run reports that the deletion guard stopped 232 removals

### Requirement: Embedding API use
Embedding calls SHALL be batched and paced to stay within configured requests-per-minute and tokens-per-minute limits, SHALL retry on rate limits, timeouts and server errors with exponential backoff (honouring any retry-after hint), and SHALL NOT retry on authentication or invalid-request errors. After 3 consecutive failed batches the indexer SHALL stop calling the API for the rest of the run and mark the remaining posts as failed. Inputs SHALL never be silently truncated: passages over 2,000 estimated tokens SHALL be split further, and API truncation SHALL be disabled where the API allows it. Every returned vector SHALL be checked for the expected count, size, finite values and non-zero length before it is stored. Identical input text SHALL never be embedded twice for the same model and dimension. If the API key is missing or rejected, indexing SHALL stop with a clear message and leave the store unchanged. Only passage and summary text with its title and heading SHALL be sent for embedding, and only a post's title and cleaned text for summary generation. Summary generation SHALL use the same pacing, retry, circuit-breaker and validation rules, with its own rate limits.

#### Scenario: Missing key
- **WHEN** `GEMINI_API_KEY` is not set and the user runs `archive index`
- **THEN** the command exits 1 telling the user to add the key to `.env`, and the store is unchanged

#### Scenario: Rate limited
- **WHEN** the Gemini API returns a rate-limit error once
- **THEN** the batch is retried after a backoff and indexing completes

#### Scenario: Outage
- **WHEN** 3 batches in a row fail after retries
- **THEN** no further API calls are made in that run and the remaining posts are recorded as failed

### Requirement: Spending ceiling
Before calling the API the indexer SHALL estimate the tokens it will send (cache misses only), separately for embedding and for summary generation (input plus output). If either estimate exceeds its limit, `search.max_embed_tokens_per_run` (default 1,500,000) or `search.summaries.max_tokens_per_run` (default 1,500,000), it SHALL stop with the estimates unless `--yes` is given.

#### Scenario: Large rebuild
- **WHEN** a `--refresh` with a new model would send 2,000,000 tokens
- **THEN** the command prints the estimate and exits 1 without calling the API

### Requirement: Safe index updates
Index updates SHALL be atomic: a search SHALL always see either the previous complete store or the new complete one. A new store SHALL be built alongside the live store and switched in only after it passes verification (row counts, vector sizes, fingerprints matching the archive, smoke queries) and has been caught up with any posts that changed since it was started. When an evaluation file exists, a store built with a different model or dimension SHALL NOT be switched in if its recall@10 is more than 2 points below the live store's, unless `--force` is given. The previous store SHALL be kept so the switch can be undone with `archive index --rollback`, which SHALL also catch the restored store up. `--no-swap` SHALL build without switching; `--swap` SHALL switch a built store later. An interrupted or failed run SHALL leave the live store usable.

#### Scenario: Interrupted run
- **WHEN** `archive index` is interrupted while embedding
- **THEN** search still answers from the previous store

#### Scenario: Catch-up before switching
- **WHEN** a store built with `--no-swap` is switched in a week later with `--swap`
- **THEN** posts added that week are indexed into it before it goes live

#### Scenario: Rollback
- **WHEN** a new store has been switched in and the user runs `archive index --rollback`
- **THEN** search answers from the previous store again, including posts added since the switch

### Requirement: Runs, locking and failure tracking
Only one index run SHALL be active at a time, enforced both by the archive lock and by a database lock; a second run SHALL exit with a message. Each run SHALL be recorded with start and end time, mode, store, counts and status; runs left unfinished by a crash SHALL be marked aborted by the next run. A post that fails to index SHALL be recorded with the stage and error and retried on the next run; after 3 failed attempts it SHALL be skipped with a warning until its fingerprint changes or `--retry-failed` is passed. Failures of some posts SHALL NOT stop the others.

#### Scenario: Concurrent runs
- **WHEN** a second `archive index` starts while one is running
- **THEN** the second exits with a message that an index run is already active

#### Scenario: Retry next run
- **WHEN** a post's embedding batch fails after all retries
- **THEN** the other posts are indexed, the failure is recorded, and the next `archive index` tries that post again

### Requirement: Freshness
The index summary SHALL list posts that are archived but not yet indexed, with how long ago they were fetched. If a post remains unindexed after 2 consecutive runs, `archive run` SHALL exit with code 3 so the scheduled job notifies the user.

#### Scenario: Stuck post
- **WHEN** a new post fails to index in two weekly runs in a row
- **THEN** the second `archive run` exits 3 and names the post

### Requirement: Index summary and dry run
`archive index` SHALL log one line per indexed, unchanged, removed or failed post and print a summary with those counts, boilerplate paragraphs removed, the passage and summary totals, embedding and summary calls, cache hits, tokens sent, estimated cost and elapsed time. With `--dry-run` it SHALL report which posts would be indexed or removed, how many passages and summaries would be embedded or generated, how many are already cached, and the estimated tokens and cost, without calling the Gemini API or writing to the store.

#### Scenario: Dry run
- **WHEN** the user runs `archive index --dry-run` with 2 new posts
- **THEN** the 2 posts and the estimated tokens and cost are listed, no API call is made, and the store is unchanged
