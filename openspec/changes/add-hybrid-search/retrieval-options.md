# Retrieval approach options

Status: **decided 2026-11-01: A + C (+ H)**. Specs and `lld.md` v3 updated. Contextual retrieval deferred to eval; no `get_post` tool for now; summaries use a Gemini Flash-class model on the same key.

## 1. Our data and questions

| Fact | Value | Why it matters |
|---|---|---|
| Corpus | 252 posts, ~730k words, ~3,600 passages, ~1M tokens | Small: any indexing method is affordable; whole corpus does not fit one prompt well |
| Authors / domain | 2 authors, one domain (leadership and management advice) | Shared vocabulary; few distinct entities |
| Content type | Opinion and advice essays, stories, frameworks | **Concept-dense, entity-poor.** Few people, companies or facts linked across posts |
| Structure | Title, sections, 9 categories, dates | Free hierarchy (post → section → passage) without an LLM |
| Growth | ~1 post a week | Incremental updates must be cheap |
| Consumer | Calling agent through MCP; it writes the answer and can search several times | The agent already does multi-step retrieval |

Expected question types:

| Type | Example | Share (guess) |
|---|---|---|
| Local / specific | "What does the author say about skip-level meetings?" | High |
| Intent / paraphrase | "How do I handle an underperforming senior engineer?" | High |
| Global / thematic | "What are the recurring themes on hiring across all posts?" | Medium |
| Multi-hop / relational | "Which frameworks from the promotion posts also appear in the feedback posts?" | Low |
| Temporal | "How did the view on remote work change since 2022?" | Low |

## 2. What the research says

| Source | Finding | Meaning for us |
|---|---|---|
| RAG vs. GraphRAG (Han et al., arXiv 2502.11371) | Plain RAG wins on single-hop and detail questions; GraphRAG wins on multi-hop; for summaries RAG keeps detail, GraphRAG gives more diverse views. They are complementary. | Our main question types favour plain RAG |
| GraphRAG-Bench, "When to use graphs in RAG" (Xiang et al., arXiv 2506.05690) | Graphs help complex reasoning and summarisation; on simple fact retrieval GraphRAG often trails vanilla RAG and adds redundancy | A graph pays off only for reasoning-heavy, entity-linked corpora |
| HippoRAG 2 (Gutiérrez et al., arXiv 2502.14802) | Graph-augmented methods (RAPTOR, GraphRAG, LightRAG, HippoRAG 1) drop below a strong dense retriever on basic factual tasks; HippoRAG 2 closes the gap | A strong embedder + re-ranker is a hard baseline to beat |
| GraphRAG, "From Local to Global" (Edge et al., arXiv 2404.16130) | Entity graph + community summaries beat naive RAG on global "sensemaking" questions over ~1M-token corpora | Global questions are the one place we might lack coverage |
| LazyGraphRAG (Microsoft Research blog, Nov 2024) | Indexing at vector-RAG cost (~0.1% of GraphRAG); comparable global-query quality, much lower query cost | Full GraphRAG indexing cost is hard to justify; lighter variants exist |
| RAPTOR (Sarthi et al., arXiv 2401.18059, ICLR 2024) | Tree of cluster summaries; retrieving across levels gave +20 points absolute on QuALITY with GPT-4 | Summary layers help thematic questions without a graph |
| LaRA (Li et al., arXiv 2502.09977, ICML 2025) | No winner between long context and RAG; depends on model, length and task | Hand the agent whole posts when needed, but keep retrieval |
| Contextual Retrieval (Anthropic, 2024) | Adding LLM-written chunk context cut failed retrievals 49% with BM25, 67% with re-ranking | Cheap gain for passages that lose meaning out of context |
| BEIR (Thakur et al., arXiv 2104.08663) | BM25 is a strong zero-shot baseline; hybrid + re-rank is robust | Supports keeping BM25 in the mix |

## 3. Options

### A. Hybrid BM25 + dense + re-rank (current LLD v2)
- **Pros:** strongest on local and intent questions; deterministic; cheap (~$0.15 to embed everything); simple incremental updates; already designed.
- **Cons:** passage-level only, so "across all posts" questions need many searches; passages lose post context.
- **Cost / complexity:** low / medium (already planned).

### B. A + contextual retrieval
- **What:** an LLM writes 1–2 sentences per passage placing it in its post; prepended before BM25 and embedding.
- **Pros:** proven gain (35–67% fewer failed retrievals); fixes pronoun-heavy and story passages; no query-time change.
- **Cons:** ~3,600 LLM calls once (~1M tokens input with caching; under $1 with a Flash-class model); whole posts go to the LLM provider; re-run on chunker change.
- **Cost / complexity:** low / low.

### C. A + post summary layer (RAPTOR-lite) ← part of preferred
- **What:** one LLM summary per post (key ideas, frameworks, advice), stored as an extra searchable unit with `level = post` in the same BM25 and vector indexes. Optional later: category or cluster summaries.
- **Pros:** answers global and "which posts talk about X" questions with one search; gives the agent a map before drilling into passages; 252 LLM calls only; fits the existing schema and fingerprint (summary regenerated only when the post changes).
- **Cons:** summaries can drift from source (mitigate: always return the source post id, keep passages as the evidence); one more stored kind; LLM text sent off-machine.
- **Cost / complexity:** very low ($0.10–0.50 once) / low.

### D. Full knowledge graph (Microsoft GraphRAG)
- **What:** LLM extracts entities and relations from every passage, builds communities (Leiden), writes community summaries; global and local graph search.
- **Pros:** best known approach for corpus-wide sensemaking; explicit links between concepts.
- **Cons:** our posts are entity-poor, so extracted "entities" are fuzzy concepts ("trust", "feedback") and the graph gets noisy; heavy indexing (multiple LLM passes over 1M tokens); incremental updates need community recompute; new storage (graph) and query modes; research shows it loses on our dominant question types.
- **Cost / complexity:** high / high.

### E. Light graph (LightRAG, HippoRAG 2, LazyGraphRAG)
- **What:** entity/phrase graph linked to passages, used to expand retrieval (Personalized PageRank) or built at query time.
- **Pros:** cheaper than D; HippoRAG 2 keeps dense-retrieval quality while adding associative recall.
- **Cons:** same entity-poor problem; extra moving parts; immature libraries; gain over A + C unproven for single-domain essays.
- **Cost / complexity:** medium / medium–high.

### F. Long context: retrieve posts, read whole posts
- **What:** search returns posts; the agent reads full post text (needs a `get_post` tool).
- **Pros:** no chunk boundaries; the agent sees full argument; trivial to build.
- **Cons:** more tokens per answer; only a few posts fit per turn; "lost in the middle" risk; needs a second MCP tool (scope says search only).
- **Cost / complexity:** low / low.

### G. Learned sparse or late interaction (SPLADE, ColBERT)
- **Pros:** strong first-stage quality.
- **Cons:** local model + custom index; overlaps with BM25 + Gemini + re-ranker we already have; little gain at 3,600 passages.
- **Cost / complexity:** medium / medium.

### H. Agentic / iterative retrieval
- **What:** the agent rewrites the query, searches several times, filters by category or date.
- **Pros:** already free: the calling agent (Claude Code etc.) does this with our `search` tool.
- **Cons:** none to build; quality depends on the agent. Good tool description and filters help.

## 4. Fit matrix

| Option | Local | Intent | Global | Multi-hop | Build cost | Upkeep | Fit |
|---|---|---|---|---|---|---|---|
| A Hybrid + re-rank | ●●● | ●●● | ●○○ | ●○○ | Low | Low | Core |
| B + Contextual | ●●● | ●●● | ●○○ | ●○○ | Low | Low | Good add-on |
| A + C Post summaries | ●●● | ●●● | ●●● | ●●○ | Very low | Low | **Best add-on** |
| D GraphRAG | ●○○ | ●●○ | ●●● | ●●● | High | High | Poor |
| E Light graph | ●●○ | ●●○ | ●●○ | ●●● | Medium | Medium | Defer |
| F Whole posts | ●●○ | ●●○ | ●●○ | ●●○ | Low | Low | Later tool |
| G SPLADE/ColBERT | ●●● | ●●● | ●○○ | ●○○ | Medium | Medium | Not needed |
| H Agentic | ●●○ | ●●○ | ●●○ | ●●○ | None | None | Free |

## 5. Preferred approach

**A + C (+ H for free), with B and D/E gated by evaluation.**

1. Keep hybrid BM25 + dense + re-rank as the core (no change to LLD v2).
2. Add a post summary layer: one summary per post, indexed as `level = post` beside passages. Search returns both levels; the result marks each item's level. Fingerprint covers the summary prompt version.
3. Rely on the calling agent for iteration; write a clear tool description that suggests searching for post summaries first on broad questions.
4. Build the evaluation set with questions tagged local / intent / global / multi-hop.
5. Add contextual retrieval (B) only if eval shows passages failing for lack of context.
6. Revisit a graph (E, then D) only if multi-hop questions score badly after steps 1–5.

**Why not a knowledge graph now:** our blogs are advice essays with few named entities, research shows graphs lose on the detail and intent questions that dominate here, the corpus is small enough that summaries cover the global case, and a graph would add the most cost and upkeep to the weekly pipeline.

## 6. What C adds to the LLD (if chosen)

- Schema: `idx_gN.passages.level` (`passage` | `post`), or a sibling `post_summaries` table in the same indexes.
- Config: `search.summaries.enabled`, `search.summaries.model` (e.g. a Gemini Flash model), `prompt_version`.
- Indexer: a `summarise` step after cleaning; summaries cached by post fingerprint + prompt version; same pacing, retries, spending ceiling.
- Result: `level` field; post-level items carry the post URL and no passage text beyond the summary.
- Secrets: same `GEMINI_API_KEY`; no new key.

## 7. Decisions needed

1. Approve A + C as the design? (or A only, or another mix)
2. Summary model: Gemini Flash-class (same key) or none.
3. Contextual retrieval: defer to eval (recommended) or build now.
4. A `get_post` tool (F): keep search only for now (recommended) or add.
