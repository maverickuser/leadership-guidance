## Why

A search asked for 10 results returns results from a median of **4 posts**, and a third of the relevant posts the first stage already found are thrown away before the re-ranker sees them. Measured on 14 Nov 2026 with the 36 eval questions on the live index (837 posts, 2 newsletters):

- Every step ranks **rows** (passages and summaries), not posts. The re-ranker scores only the first **20 rows** of the fused ranking (`search.rerank_top`), and only scored rows can be returned. A relevant post has many rows that rank close together (median 14 passages per Scarlet Ink post, 22 per Pragmatic Engineer post), so those 20 rows hold a median of 4 posts. The per-post cap (3 passages, 1 summary) runs after that cut.
- Of 203 relevant posts in the combined top 10, **34% are never scored** (outside the window); 7% are lost to re-ranker scoring or the cap.
- Post-level recall@10 falls from **0.84** (combined) to **0.71** (returned), and the judge's graded nDCG@10 from **0.89** to **0.70**. Broad and paraphrased questions lose most (global 0.69 → 0.49, intent 0.83 → 0.69); quick lookups barely (local 0.97 → 0.91).

The archive is used to write answers that draw on many posts (`ask-coach`), so an answer built from 4 posts misses most of what the archive says. Nothing measures the answers themselves today, so the cost of this, and the gain from fixing it, is only inferred from retrieval numbers.

This change does steps 1 and 2 of the Search Upgrade Plan: fix the cut, and start measuring answers. Steps 3 (sub-question planning) and 4 (theme summaries) are conditional on the results and are not part of this change.

## What Changes

- **Post-level selection before re-ranking (step 1):**
  - Group the fused rows by post and give each post a score from its rows.
  - Keep the best-scoring posts (a configurable number, `search.rerank_posts`), not the first 20 rows.
  - Send each kept post's best passages (and its summary when the search includes summaries) to the re-ranker, so the re-ranker sees many posts with a few rows each.
  - After re-ranking, choose the results across posts: each post's best result first, then further results from the same posts while slots remain, still at most 3 passages and 1 summary per post.
  - `search.rerank_top` is replaced by the post-based settings; the old name is accepted with a message saying what replaced it.
  - The every-source rule keeps working on top of this; its spec is corrected to match the code (the rule is off when results are not re-ranked, as changed in the review of #38).
  - Defaults are set by measurement on the eval, not guessed.
- **Answer-level evaluation (step 2):**
  - A new evaluation mode generates an answer for each eval question the way `ask-coach` answers it: the same instructions, searches through the same search service, Gemini writing the answer.
  - An LLM judge scores each answer: whether each claim is supported by the passage it cites (faithfulness), whether citations point at real retrieved posts (citation precision), how many of the question's relevant posts the answer draws on (post coverage), and an overall grade.
  - Answers and grades are saved in the user's data folder and reused, like the existing judge grades, so a re-run only pays for what changed.
  - The cost is estimated and shown before any Gemini call; the run needs a yes when it is over the per-run ceiling.
  - A **baseline is recorded on today's search** before the step 1 change is switched on, so step 1 is judged on answers as well as retrieval.
- **Eval report:** gains "distinct posts returned" and "relevant posts never re-ranked", the two numbers that show this problem directly.

## Capabilities

### New Capabilities
- `answer-evaluation`: generating answers for the eval questions the way `ask-coach` does and grading them with an LLM judge for faithfulness, citation precision, post coverage and an overall grade; saved and reused grades; cost shown first.

### Modified Capabilities
- `hybrid-search`:
  - **Re-ranking:** the re-ranker scores the best passages of the best posts, chosen at post level, instead of the first 20 rows.
  - **Result content:** results are chosen across posts (each post's best result first), keeping the 3-passage and 1-summary cap.
  - **Every source represented:** corrected to say the rule is off when results are not re-ranked.
  - **Search quality evaluation:** the report adds distinct posts returned and relevant posts never re-ranked.

## Impact

- **Code:** `search/service.py` (the cut and the re-rank batch), `search/fusion.py` (post aggregation and selection, pure functions), `config.py` (new settings, `rerank_top` retired), `search/evaluate.py` (report additions), a new `search/answers.py` with the answer generator and answer judge, `scripts/eval_search.py` (new mode).
- **No index or schema change:** rows, embeddings and the database stay as they are; no re-index and no Gemini cost from step 1.
- **Search time:** the re-ranker may score more rows than today's 20 (about 0.2 s each on this Mac); the budget is set from measurement.
- **Gemini cost:** only step 2, per eval run (about 36 answers and 36 judge calls on Flash, estimated before running).
- **Defaults:** the new search settings change the results of every search once shipped; they are switched on only after the baseline answer scores are recorded.
- **Docs:** README search settings and the eval section.
