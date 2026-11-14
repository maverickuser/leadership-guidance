## Context

Today's search ranks and cuts **rows** (passages and summaries): keyword and semantic search each return 50 rows, fusion merges them, the first `rerank_top = 20` rows are re-ranked, and only those rows can be returned. The per-post cap (3 passages, 1 summary) runs after that cut. On the 36 eval questions this returns a median of 4 posts and leaves 34% of the relevant posts the combined stage found unscored (see the Re-rank Window Problem analysis, 14 Nov 2026).

Nothing measures the answers built from search. The retrieval judge (`eval_search.py --judge`) grades posts, not answers.

Measured for this design (14 Nov 2026, this Mac, Qwen3-Reranker-0.6B on MPS, rows of about 250 words):

| Rows re-ranked | Time |
|---|---|
| 20 | 3.6 s |
| 40 | 6.3 s |
| 60 | 9.4 s |

About 0.15 s per row plus about 0.8 s fixed. A whole search today takes a median of 5.0 s (p90 5.7 s), so the re-ranker is most of it, and the number of rows it scores is the main cost of this change.

## Goals / Non-Goals

**Goals:**
- Choose posts before the re-rank cut, so results cover as many relevant posts as the question has (up to the limit).
- Measure answers, not only retrieval, and record a baseline before the search change is switched on.
- Set every new default from measurement on the user's eval questions.

**Non-Goals:**
- Sub-question planning (step 3) and theme summaries (step 4): conditional on this change's results.
- Changing chunking, embeddings, the index schema, or the re-ranker model. No re-index.
- Making the answer evaluation reproduce Claude's exact `ask-coach` behaviour. It is a stable yardstick for comparing search versions, not a copy of the agent.

## Decisions

### 1. Group by post, then re-rank the best passages of the best posts
After fusion, rows are grouped by post. A post's score comes from its rows' fused scores. The best `rerank_posts` posts are kept; from each, its best `rerank_rows_per_post` passages (by fused order) and its summary (when the search includes summaries) go to the re-ranker. Rows of other posts are dropped when re-ranked, as today.
- **Why:** it is the step the literature calls passage-to-document aggregation, and it removes the measured loss directly, with no index change and no Gemini cost.
- **Alternatives:** a bigger `rerank_top` (more rows, same clustering: 60 rows ≈ 9.4 s and still few posts); diversifying before re-ranking without post scores (no ranking signal for posts); re-ranking whole posts (posts are too long for the re-ranker's input).

### 2. Post score: measured choice between "best row" and "sum of the top rows"
Two options are implemented and compared on the eval: `max` (the post's best fused score) and `sum_top_n` (the sum of its best 3 fused scores, which rewards posts with several matching sections). The default is the one with the better returned nDCG@10, with recall as the tie-break.
- **Why:** published results favour aggregation over the maximum for document ranking, but our rows are fused ranks, not learned scores, so it is measured rather than assumed.

### 3. Defaults chosen by a measured grid within a latency budget
`rerank_posts` ∈ {10, 15, 20} × `rerank_rows_per_post` ∈ {1, 2, 3}, with summaries. Rows scored ≈ posts × (rows + 1). The chosen default is the smallest configuration that meets the retrieval targets (distinct posts returned ≥ 8, recall@10 ≥ 0.84, nDCG@10 ≥ 0.89, local recall not lower) while the p90 search time stays within the budget agreed with the user. Example: 15 posts × (2 + 1) = 45 rows ≈ 7 s of re-ranking.
- **Needs the user:** the latency budget (see Open Questions).

### 4. Pick results across posts after re-ranking
A pure function replaces the slice after `diversify`: first each post's best re-ranked result, in score order, up to the limit; then, while slots remain, each post's next-best result in score order, still at most 3 passages and 1 summary per post.
- **Why:** with 10 relevant posts this returns 10 posts; with 3 it still fills the limit from those 3 (spec: "Few relevant posts").

### 5. Every-source rule moves to post level
Unchanged in effect: when re-ranked and no `source` filter, a newsletter with no kept post gets its best-scoring post added to the re-rank batch, and its best result is swapped in if it reaches `source_min_score`. The spec is corrected to say the rule is off when results are not re-ranked, which is what the code has done since the #38 review.

### 6. Shipped switched off, switched on after the baseline
Step 1 ships behind `search.selection: rows | posts`, default `rows` (today's behaviour), so the answer baseline can be recorded on the same code. A final PR sets the measured defaults and flips the default to `posts`. `rerank_top` keeps working for `rows`; it is retired when `rows` is removed in a later change, once `posts` has run for a while.

### 7. Answer evaluation: fixed search plan, then one answer
For each question: a summaries search and a passages search (limit 10 each), the way the `ask` instructions tell the agent to start a broad question, through the same `SearchService`. The answering model then writes one answer from the `ask` instructions and those results. No tool-calling loop.
- **Why:** the yardstick has to change only when search changes. A fixed plan removes the agent's own choices from the comparison, needs no tool-calling support in the Gemini client, and costs a predictable 2 searches per question.
- **Alternative:** an agent loop with Gemini function calling and up to 4 searches. Closer to real use, but noisier (different searches per run) and more work. Kept as a later option.

### 8. Models
- **Answer model:** Gemini Flash (`gemini-3.8-flash`, already pinned for summaries), setting `search.answers.model`. Your real agent is Claude; Flash stands in for it, so absolute scores are about Flash's answers, and the comparison between search versions is what matters.
- **Judge model:** setting `search.answers.judge_model`, default the same Flash model, with a fixed prompt and version. Using a different model than the answerer reduces self-grading bias; the setting allows it.

### 9. What the judge decides and what code decides
- **Code computes:** citations (links in the answer mapped to post ids through the post URLs), citation precision (cited posts that the searches returned), and post coverage (cited posts that are relevant ÷ relevant posts). These are deterministic.
- **The judge decides:** the answer's claims and whether each cited passage supports its claim (faithfulness), and the 0–3 overall grade. It returns JSON, checked like the existing judge's responses.

### 10. Storage and reuse
`<output_dir>/eval-answers/<label>.json`: per question, the searches run (result ids), the answer, the model and prompt versions, and the grades. Keys: a hash of the question, the instructions' version, the model and the result ids, so an unchanged run makes no Gemini calls. Labels (`--label baseline`, `--label posts-15x2`) let the report compare runs.

### 11. Cost
Measured from the archive: a result is about 250 words, so the 20 results per question give about 7K input tokens, plus about 2K of instructions, plus about 1K out; the judge reads about the same. About 36 × 2 × 10K ≈ 0.7M tokens per full run, roughly $0.50–$1 at the configured Flash rate; estimated and printed before each run (`--dry-run`), with a ceiling `search.answers.max_tokens_per_run` (default 1.5M) that needs `--yes` to pass.

## Risks / Trade-offs

- **Slower searches** → the latency budget caps the defaults; `rows` stays available until `posts` is proven.
- **A long weak post out-scoring a short strong one** under `sum_top_n` → decision 2 measures both.
- **LLM-judge noise** → fixed prompts and versions, saved grades, and the same answers re-graded give the same scores; differences under a few points are treated as noise with 36 questions.
- **Flash is not Claude** → scores are compared between search versions, not read as Claude's quality.
- **Small, partly weak eval set** (median 2 expected posts; several draft cross-newsletter lists rated weak) → post coverage also uses the retrieval judge's grades; the user reviews the six draft questions before the baseline run.

## Migration Plan

1. Answer evaluation lands; with the user's go-ahead, a `baseline` run on today's search (`selection: rows`).
2. Post selection lands switched off; the grid is measured offline on retrieval (query embeddings only, a few cents).
3. With the user's go-ahead, an answer run on the chosen configuration (`--label posts-…`).
4. The final PR sets the defaults and `selection: posts`. Rollback: set `search.selection: rows` in `config.yaml`.

## Decisions (made with the user, 14 Nov 2026)

- **Latency budget:** p90 search time up to about **8 s** (today 5.7 s). That allows about 40–45 re-ranked rows, for example 15 posts × (2 passages + summary).
- **Answer approach:** the **fixed search plan** (decision 7): one summaries search and one passages search per question, then one answer.
- **Judge model:** the **same Flash model** as the answerer (decision 8), as the retrieval judge does today.
