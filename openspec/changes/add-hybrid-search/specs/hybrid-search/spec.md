## ADDED Requirements

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
- **THEN** the search runs without error, matching the phrase "bar raiser" and the words "what's" and "hiring"

### Requirement: Re-ranking
The combined candidates SHALL be re-scored by a re-ranking model that reads the query together with each candidate's text, and results SHALL be ordered by the re-ranker's score. If the re-ranker is unavailable, the system SHALL return the combined order and mark the response as not re-ranked.

#### Scenario: Re-ranker reorders
- **WHEN** the combined list puts a loosely related passage first and a directly relevant one fifth
- **THEN** after re-ranking the directly relevant passage ranks above the loosely related one

#### Scenario: Re-ranker unavailable
- **WHEN** the re-ranker fails
- **THEN** results are returned in combined order with `reranked: false`

### Requirement: Filters
Search SHALL accept optional filters `category` (one or more category names, case-insensitive), `since` and `until` (ISO dates, inclusive), and `kind` (`all`, the default; `passages`; or `summaries`). Filters SHALL be applied before results are limited, so up to the requested number of matching results is still returned. An unknown category or invalid date SHALL be rejected with an error that lists the valid categories or the expected date format.

#### Scenario: Category filter
- **WHEN** a search uses `category: ["Leadership"]`
- **THEN** every result belongs to a post with the category "Leadership"

#### Scenario: Summaries only
- **WHEN** a search uses `kind: "summaries"`
- **THEN** every result is a post summary

#### Scenario: Unknown category
- **WHEN** a search uses a category that does not exist
- **THEN** it is rejected with an error listing the valid categories

### Requirement: Result content
Each result SHALL contain: rank, relevance score, kind (`passage` or `summary`), id, text, section heading (null for summaries), post id, post title, post URL, published date, categories, and which methods found it (`keyword`, `semantic` or both). The number of results SHALL default to 10 and be capped at 30. Results SHALL include at most 3 passages and 1 summary from the same post, so one long post does not crowd out others.

#### Scenario: Broad question
- **WHEN** the query is "recurring advice on hiring across posts" and 10 posts are about hiring
- **THEN** results include summaries of several of those posts, each marked `kind: "summary"` with its post URL

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
