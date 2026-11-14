## MODIFIED Requirements

### Requirement: Re-ranking
The combined candidates SHALL be grouped by post before re-ranking, and each post SHALL get a score from its candidates' fused scores. The re-ranker SHALL score the best passages of the best-scoring posts: up to `search.rerank_posts` posts, and up to `search.rerank_rows_per_post` passages from each (plus the post's summary when the search includes summaries), so that the re-ranker sees many posts rather than many passages of a few posts. Candidates of posts that are not kept SHALL NOT be returned when results are re-ranked. Results SHALL be ordered by the re-ranker's score. If the re-ranker is unavailable, the system SHALL return the kept posts' candidates in combined order and mark the response as not re-ranked.

#### Scenario: Re-ranker reorders
- **WHEN** the combined list puts a loosely related passage first and a directly relevant one fifth
- **THEN** after re-ranking the directly relevant passage ranks above the loosely related one

#### Scenario: Re-ranker unavailable
- **WHEN** the re-ranker fails
- **THEN** results are returned in combined order with `reranked: false`

#### Scenario: One post with many matching passages
- **WHEN** one post's passages fill the first 20 places of the combined list and 9 other relevant posts follow with 1 to 3 passages each
- **THEN** the re-ranker scores passages from all 10 posts, and at most `search.rerank_rows_per_post` passages from the first post

#### Scenario: Relevant posts further down
- **WHEN** a relevant post's best passage is at place 40 of the combined list, and its post is among the `search.rerank_posts` best-scoring posts
- **THEN** that passage is re-ranked and can be returned

### Requirement: Result content
Each result SHALL contain: rank, relevance score, kind (`passage` or `summary`), id, text, section heading (null for summaries), post id, source, post title, post URL, published date, categories, and which methods found it (`keyword`, `semantic` or both). The number of results SHALL default to 10 and be capped at 30. Results SHALL be chosen across posts: each post's best result first, in score order, then further results from the same posts while slots remain. Results SHALL include at most 3 passages and 1 summary from the same post, so one long post does not crowd out others. When fewer posts are relevant than the number of results asked for, the remaining slots SHALL be filled with further passages of those posts rather than left empty. Without a `source` filter, results SHALL be ranked by relevance across all sources, and every source that has a relevant match SHALL appear in the results (see "Every source represented").

#### Scenario: Broad question
- **WHEN** the query is "recurring advice on hiring across posts" and 10 posts are about hiring
- **THEN** results include summaries of several of those posts, each marked `kind: "summary"` with its post URL and source

#### Scenario: Diverse results
- **WHEN** 6 of the top 10 re-ranked passages come from the same post
- **THEN** only its best 3 are returned and the remaining slots go to other posts

#### Scenario: Ten relevant posts
- **WHEN** a search with `limit: 10` has 10 or more relevant posts among the re-ranked candidates
- **THEN** the 10 results come from 10 different posts, each post's best result, in score order

#### Scenario: Few relevant posts
- **WHEN** a search with `limit: 10` re-ranks candidates from only 3 posts
- **THEN** it returns up to 3 passages and 1 summary from each, in score order, so up to 10 results from 3 posts

### Requirement: Search quality evaluation
The project SHALL include a script that reads a local, git-ignored file of test questions, each tagged with a type (`local`, `intent`, `global` or `multi-hop`) and its expected post ids, and reports post-level recall@10 and mean reciprocal rank for keyword only, semantic only, combined, and combined with re-ranking, each with and without summaries and broken down by question type, so model and ranking choices can be compared on the user's own data. The report SHALL also give the median number of distinct posts in the returned results and the share of expected posts that were in the combined top 10 posts but were not re-ranked.

#### Scenario: Compare stages
- **WHEN** the user runs the evaluation script with 20 test questions
- **THEN** it prints recall@10 and MRR for each of the four stages, with and without summaries, overall and per question type

#### Scenario: Breadth of results
- **WHEN** the evaluation finishes
- **THEN** the report shows the median distinct posts returned per question and the share of expected posts the combined stage found but the re-ranker never scored

### Requirement: Every source represented
When a search has no `source` filter, more than one source is indexed, and results are re-ranked, results SHALL be ranked by relevance across all sources. Each source's best result SHALL be included when its re-ranker score is at least `search.source_min_score`; a source with no post among those kept for re-ranking SHALL have its best-scoring post re-ranked as well, so it can qualify. If a source's best qualifying result would fall outside the requested `limit`, it SHALL take the place of the lowest-ranked result from a source with more than one result, so one source cannot crowd another out. A source with no qualifying result SHALL NOT be forced in. When results are not re-ranked, this rule SHALL NOT apply, because fused ranks do not say whether a result is relevant.

#### Scenario: Both newsletters relevant
- **WHEN** a search for "on-call rotations" finds 15 strong matches in The Pragmatic Engineer and 2 relevant matches in the `scarletink` newsletter, with `limit: 10`
- **THEN** the 10 results include at least the `scarletink` newsletter's best match, and the rest are the best Pragmatic Engineer matches in relevance order

#### Scenario: Only one newsletter relevant
- **WHEN** a search for "Substack podcast transcript" has relevant matches only in The Pragmatic Engineer
- **THEN** all results come from The Pragmatic Engineer, and no weak `scarletink` result is added

#### Scenario: Filtered search
- **WHEN** a search uses `source: ["scarletink"]`
- **THEN** only results from `scarletink` are returned, and the representation rule does not apply

#### Scenario: Not re-ranked
- **WHEN** the re-ranker is unavailable for a search over both newsletters
- **THEN** results keep their combined order and no result is swapped in for representation
