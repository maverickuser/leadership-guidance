# hybrid-search Specification

## Purpose
Searching the archive by keywords and by meaning together, combining the results, re-ranking them and returning passages and post summaries with their sources.
## Requirements
### Requirement: Hybrid retrieval
For each query the system SHALL run BM25 keyword search and semantic search over all passages and post summaries, take the top `search.candidates` (default 50) from each, and combine them into one candidate list in which passages found by both methods rank above passages found by only one at similar positions. Combining SHALL be deterministic: the same query against the same store always gives the same order.

#### Scenario: Semantic match without shared words
- **WHEN** the query is "how to handle an underperforming engineer" and a post discusses "coaching a struggling team member" without those words
- **THEN** that post's passage is among the candidates

#### Scenario: Exact term match
- **WHEN** the query is "bar raiser"
- **THEN** passages containing the exact phrase are among the candidates even if their meaning vectors are not the closest

### Requirement: Safe keyword queries
Any query text SHALL be accepted for keyword search without causing a query-syntax error: punctuation and search-operator words SHALL be treated as ordinary text, and text in double quotes SHALL be matched as a phrase.

#### Scenario: Punctuation and operators
- **WHEN** the query is `what's "bar raiser" AND (hiring)?`
- **THEN** the search runs without error, matching the phrase "bar raiser" and the word "hiring" ("what's" splits into stop-word fragments, as the index splits it)

### Requirement: Re-ranking
The combined candidates SHALL be re-scored by a re-ranking model that reads the query together with each candidate's text, and results SHALL be ordered by the re-ranker's score. If the re-ranker is unavailable, the system SHALL return the combined order and mark the response as not re-ranked.

#### Scenario: Re-ranker reorders
- **WHEN** the combined list puts a loosely related passage first and a directly relevant one fifth
- **THEN** after re-ranking the directly relevant passage ranks above the loosely related one

#### Scenario: Re-ranker unavailable
- **WHEN** the re-ranker fails
- **THEN** results are returned in combined order with `reranked: false`

### Requirement: Filters
Search SHALL accept these optional filters:
- `source`: one or more source ids, exact.
- `category`: one or more category names, case-insensitive.
- `since` and `until`: ISO dates, inclusive.
- `kind`: `all` (the default), `passages` or `summaries`.

Filters SHALL be applied before results are limited, so up to the requested number of matching results is still returned. An unknown source, unknown category or invalid date SHALL be rejected with an error that lists the valid sources, the valid categories, or the expected date format.

#### Scenario: Source filter
- **WHEN** a search uses `source: ["pragmatic-engineer"]`
- **THEN** every result belongs to a post from that source

#### Scenario: Category filter
- **WHEN** a search uses `category: ["Leadership"]`
- **THEN** every result belongs to a post with the category "Leadership"

#### Scenario: Summaries only
- **WHEN** a search uses `kind: "summaries"`
- **THEN** every result is a post summary

#### Scenario: Unknown category
- **WHEN** a search uses a category that does not exist
- **THEN** it is rejected with an error listing the valid categories

#### Scenario: Unknown source
- **WHEN** a search uses a source id that is not in the store
- **THEN** it is rejected with an error listing the valid source ids

### Requirement: Result content
Each result SHALL contain: rank, relevance score, kind (`passage` or `summary`), id, text, section heading (null for summaries), post id, source, post title, post URL, published date, categories, and which methods found it (`keyword`, `semantic` or both). The number of results SHALL default to 10 and be capped at 30. Results SHALL include at most 3 passages and 1 summary from the same post, so one long post does not crowd out others. Without a `source` filter, results SHALL be ranked by relevance across all sources, and every source that has a relevant match SHALL appear in the results (see "Every source represented").

#### Scenario: Broad question
- **WHEN** the query is "recurring advice on hiring across posts" and 10 posts are about hiring
- **THEN** results include summaries of several of those posts, each marked `kind: "summary"` with its post URL and source

#### Scenario: Diverse results
- **WHEN** 6 of the top 10 re-ranked passages come from the same post
- **THEN** only its best 3 are returned and the remaining slots go to other posts

### Requirement: Empty and missing cases
An empty or whitespace-only query SHALL be rejected. A query with no matches SHALL return an empty result list, not an error. If no store exists yet, search SHALL return an error telling the user to run `archive index`.

#### Scenario: No store yet
- **WHEN** a search runs before any `archive index`
- **THEN** it returns an error saying to run `archive index`

### Requirement: Search quality evaluation
The project SHALL include a script that reads a local, git-ignored file of test questions, each tagged with a type (`local`, `intent`, `global` or `multi-hop`) and its expected post ids, and reports post-level recall@10 and mean reciprocal rank for keyword only, semantic only, combined, and combined with re-ranking, each with and without summaries and broken down by question type, so model and ranking choices can be compared on the user's own data.

#### Scenario: Compare stages
- **WHEN** the user runs the evaluation script with 20 test questions
- **THEN** it prints recall@10 and MRR for each of the four stages, with and without summaries, overall and per question type

### Requirement: Every source represented
When a search has no `source` filter and more than one source is indexed, results SHALL be ranked by relevance across all sources. Each source's best result SHALL be included when it passes the relevance threshold (re-ranker score at least `search.source_min_score`, or, when results are not re-ranked, a rank within the first `search.candidates` fused candidates). If a source's best qualifying result would fall outside the requested `limit`, it SHALL take the place of the lowest-ranked result from a source with more than one result, so one source cannot crowd another out. A source with no qualifying result SHALL NOT be forced in.

#### Scenario: Both newsletters relevant
- **WHEN** a search for "on-call rotations" finds 15 strong matches in The Pragmatic Engineer and 2 relevant matches in the `main` newsletter, with `limit: 10`
- **THEN** the 10 results include at least the `main` newsletter's best match, and the rest are the best Pragmatic Engineer matches in relevance order

#### Scenario: Only one newsletter relevant
- **WHEN** a search for "Substack podcast transcript" has relevant matches only in The Pragmatic Engineer
- **THEN** all results come from The Pragmatic Engineer, and no weak `main` result is added

#### Scenario: Filtered search
- **WHEN** a search uses `source: ["main"]`
- **THEN** only results from `main` are returned, and the representation rule does not apply

