## 0. Approvals

- [x] 0.1 Proposal approved 2026-11-14
- [x] 0.2 Specs approved 2026-11-14 (hybrid-search delta, new answer-evaluation)
- [x] 0.3 Design approved 2026-11-14 (p90 budget about 8 s, fixed search plan, Flash judges)
- [x] 0.4 Low-level design approved 2026-11-14
- [x] 0.5 PR plan approved 2026-11-14
- [x] 0.6 Design PR (proposal, specs, design, lld, tasks) opened as [0/6]

## 1. Settings (PR 1)

- [x] 1.1 `SearchConfig`: `selection` (rows | posts, default rows), `post_score` (max | sum_top3), `rerank_posts` (15, 1–50), `rerank_rows_per_post` (2, 1–5), with validation and the batch-size check against `2 × candidates` (lld §1)
- [x] 1.2 `AnswersConfig` under `search.answers` (model, judge_model, thinking_level, results_per_search, max_tokens_per_run), parsed like `search.summaries`
- [x] 1.3 Tests: defaults, ranges, unknown values, the batch-size error, nested parsing
- [x] 1.4 README search-settings table rows (marked as not active until `selection: posts`)

## 2. Answer generation and storage (PR 2)

- [ ] 2.1 `search/answers.py`: the fixed plan (summaries + passages search), the prompt from `ask_message` plus rendered results, `AnswerRecord`, `answer_key` (lld §5.1, 5.2, 5.6)
- [ ] 2.2 Citations from the answer's links, URL normalizing, cited-but-not-retrieved; citation precision and post coverage maths; relevant posts = expected ∪ judge grades 2–3 (lld §5.3, 5.5)
- [ ] 2.3 `eval-answers/<label>.json` atomic save after each question, reuse across runs and labels
- [ ] 2.4 `GeminiAnswerer` (thin adapter, `pragma: no cover` only on the SDK call) and a fake for tests
- [ ] 2.5 Tests incl. `hypothesis` on URL normalizing; logs carry no question or answer text

## 3. Answer judge, report and CLI (PR 3)

- [ ] 3.1 `GeminiAnswerJudge`: JSON claims + overall grade, one retry, `grade_key`, faithfulness maths (lld §5.4)
- [ ] 3.2 Cost estimate before any search or Gemini call, `--dry-run`, ceiling with `--yes`, missing-key stop, per-question failures (lld §5.7)
- [ ] 3.3 Report per question type, `--compare A B`, `--answers` and `--label` in `scripts/eval_search.py` (lld §5.8, 5.9)
- [ ] 3.4 README: the answer evaluation, its cost, where results are saved
- [ ] 3.5 Tests end to end with the fakes
- [ ] 3.6 Live, with the user's go-ahead: dry run, then `--answers --label baseline` on today's search; record the scores in the PR

## 4. Post-level selection (PR 4)

- [ ] 4.1 `fusion.py`: `PostGroup`, `group_by_post` (max, sum_top3), `rerank_batch`, `select_across_posts` (lld §2)
- [ ] 4.2 `service._search` `posts` mode: keep posts, add a missing newsletter's best post, re-rank the batch, pick across posts, then `represent_sources`; `rows` mode untouched; `batch` in the trace (lld §3)
- [ ] 4.3 Unit tests incl. `hypothesis` properties; `db` tests for the spec scenarios (one post with many matching passages, relevant posts further down, ten relevant posts, few relevant posts, re-ranker failure, every newsletter represented)

## 5. Breadth in the eval report and the grid (PR 5)

- [ ] 5.1 `evaluate.py`: distinct posts returned and expected posts never re-ranked, in the report and `--json` (lld §4)
- [ ] 5.2 Overrides `--selection`, `--post-score`, `--rerank-posts`, `--rows-per-post`
- [ ] 5.3 Tests for the report lines, JSON and overrides
- [ ] 5.4 Live, with the user's go-ahead: the retrieval grid (lld §8.2), with latency; record the table in the PR and choose the configuration

## 6. Defaults and acceptance (PR 6)

- [ ] 6.1 Set `post_score`, `rerank_posts`, `rerank_rows_per_post` to the measured choice, with the measurement in `config.py` comments; default `selection: posts`
- [ ] 6.2 Live, with the user's go-ahead: `--answers --label posts-<choice>` and `--compare baseline posts-<choice>`; record both in the PR
- [ ] 6.3 README: how results are chosen now, the new defaults, how to switch back (`selection: rows`)
- [ ] 6.4 After merge: sync the delta specs into `openspec/specs/` and archive this change

## Every PR (definition of done)

- All CI jobs green: `test` (3.11, 3.12), `integration`, `coverage` (combined, branch, ≥ 95%), `lint`, `safety`
- Tests ship with the code; new modules ≥ 95% coverage
- `/code-review high` run on a lower model (Sonnet), findings fixed in a "Fix review findings on the <area>" commit
- PR template filled, tasks ticked in the same PR
- The user merges (or asks Claude to)
