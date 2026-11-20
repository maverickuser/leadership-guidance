## MODIFIED Requirements

### Requirement: Hybrid retrieval
For each query the system SHALL run BM25 keyword search and semantic search over all passages and post summaries, take the top `search.candidates` (default 100) from each, and combine them into one candidate list with reciprocal-rank fusion weighted by `search.fusion_weights` (default keyword 0.25, semantic 2.0), so that passages found by both methods rank above passages found by only one at similar positions. Combining SHALL be deterministic: the same query against the same store always gives the same order.

#### Scenario: Semantic match without shared words
- **WHEN** the query is "how to handle an underperforming engineer" and a post discusses "coaching a struggling team member" without those words
- **THEN** that post's passage is among the candidates

#### Scenario: Exact term match
- **WHEN** the query is "bar raiser"
- **THEN** passages containing the exact phrase are among the candidates even if their meaning vectors are not the closest

#### Scenario: Keyword matches do not crowd out meaning matches
- **WHEN** a post matches the query's meaning at semantic rank 8 and ten other passages share common words with the query
- **THEN** that post stays among the first 10 posts of the combined list
