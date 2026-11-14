# Low-level design: post-level selection and answer evaluation

Builds on `design.md` (approved 14 Nov 2026: p90 budget about 8 s, fixed search plan, Flash judges). Code paths are the current `main` (after #40).

## 1. Settings (`config.py`)

New fields on `SearchConfig`, all settable under `search:` in `config.yaml`:

| Setting | Type | Default | Valid | Meaning |
|---|---|---|---|---|
| `selection` | str | `rows` | `rows`, `posts` | `rows` = today's behaviour; `posts` = this change. Flipped to `posts` by the last PR |
| `post_score` | str | `max` | `max`, `sum_top3` | How a post's score comes from its rows' fused scores (§2) |
| `rerank_posts` | int | 15 | 1–50 | Posts kept for re-ranking (`posts` mode) |
| `rerank_rows_per_post` | int | 2 | 1–5 | Best passages per kept post sent to the re-ranker; the summary is extra |
| `rerank_top` | int | 20 | as today | Used only by `rows` mode |

`rerank_posts` and `rerank_rows_per_post` defaults are provisional until §8; the last PR sets them from the measured grid. Validation: `rerank_posts × (rerank_rows_per_post + 1) ≤ 2 × candidates` ("search.rerank_posts × (rows_per_post + 1) can't exceed the combined candidates (2 × search.candidates)").

New nested section `search.answers` (`AnswersConfig`, parsed like `search.summaries`):

| Setting | Default | Meaning |
|---|---|---|
| `model` | `gemini-3.8-flash` | Writes the answers |
| `judge_model` | `gemini-3.8-flash` | Grades the answers |
| `thinking_level` | `low` | As for summaries |
| `results_per_search` | 10 | Results per search in the fixed plan |
| `max_tokens_per_run` | 1,500,000 | Ceiling; over it needs `--yes` |

Cost uses the existing `summary_usd_per_million_tokens` (same Flash rate).

## 2. Post grouping and selection (`search/fusion.py`, pure)

```python
@dataclass(frozen=True)
class PostGroup:
    post_id: str
    score: float                 # from the post's fused scores (post_score)
    passages: tuple[str, ...]    # row ids, best fused first
    summary: str | None          # the post's summary row id, if fused
    best_rank: int               # best fused rank of any of its rows (tie-break)

def group_by_post(fused: Sequence[Fused], post_of, is_summary, mode: str) -> list[PostGroup]
    # mode "max": score = max row score; "sum_top3": sum of the 3 best row scores.
    # Order: score desc, then best_rank, then post_id (deterministic).

def rerank_batch(groups: Sequence[PostGroup], rows_per_post: int, with_summaries: bool) -> list[str]
    # For each group in order: its first `rows_per_post` passages, then its summary if
    # with_summaries and present. Keeps the groups' order; no duplicates.

def select_across_posts(ranked: Sequence[str], post_of, is_summary,
                        max_passages: int, max_summaries: int) -> list[str]
    # Round 1: each post's best item, in `ranked` order. Round 2, 3, ...: each post's next-best
    # item, in `ranked` order, while the post is under its caps. Returns every allowed item in
    # pick order (not cut to a limit), so represent_sources can still look below the limit.
```

`diversify` stays for `rows` mode. `represent_sources` is unchanged and runs on the output of `select_across_posts`.

## 3. Search flow (`search/service.py`, `_search`)

Unchanged up to `fused` and `rows = self._hydrate(...)`. Then, when `selection == "posts"`:

1. `groups = group_by_post(fused, post_of, is_summary, s.post_score)`; with `kind=passages` summaries are already filtered out by SQL, with `kind=summaries` every group has only its summary.
2. `kept = groups[:s.rerank_posts]`. For each represented source (no `source` filter, more than one source) with no kept group: append its best group (the every-source rule at post level).
3. `batch = rerank_batch(kept, s.rerank_rows_per_post, with_summaries=req.kind != "passages")`.
4. Re-rank `batch` (same error handling as today). On success, `order = batch sorted by re-ranker score` (stable). On failure or no re-ranker, `order = batch` in batch order (kept posts' rows, combined order) and `reranked = False`.
5. `picked = select_across_posts(order, post_of, is_summary, s.max_per_post, 1)`.
6. If re-ranked and more than one represented source: `kept_ids = represent_sources(picked, req.limit, …)` as today; otherwise `kept_ids = picked[:req.limit]`.

`rows` mode is today's code, untouched. Trace (`trace=` dict for the eval) gains `batch` (the row ids sent to the re-ranker) in both modes (`rows` mode: the first `rerank_top` rows plus extras).

Time: rows scored = `len(batch)`. With 15 × (2 + 1) = 45 it is about 7.2 s of re-ranking (0.15 s per row + 0.8 s, measured); the grid (§8) checks the whole search's p90 ≤ 8 s.

## 4. Eval report additions (`search/evaluate.py`)

Per outcome (with summaries): `returned_posts` (distinct posts in the results) and `unscored` (expected posts in the combined top 10 posts whose rows are all outside `batch`). Report lines:

```
breadth: median 4 distinct posts returned (limit 10)
not re-ranked: 34% of expected posts found by the combined stage were never scored
```

Both go into `--json`. New CLI overrides for the grid: `--selection`, `--post-score`, `--rerank-posts`, `--rows-per-post` (next to the existing `--rerank-top`).

## 5. Answer evaluation (`search/answers.py`, new)

### 5.1 Plan and prompt
For each question: `summaries = service.search(SearchRequest.create(q, n, kind="summaries"))`, `passages = service.search(SearchRequest.create(q, n, kind="passages"))`, `n = results_per_search`. The prompt is `ask_message(question)` (the MCP `ask` text, reused unchanged) followed by `Search results:` and each result rendered as:

```
[<id>] <kind> · <post title> — <heading> · <source name> · <date> · <post URL>
<text>
```

The instruction tells the model the search has already been run and these are its only results. `ANSWER_PROMPT_VERSION = 1`.

### 5.2 Types

```python
@dataclass
class AnswerRecord:
    question_id: str; question: str; label: str
    result_ids: list[str]; result_posts: dict[str, str]   # row id -> post id, from both searches
    answer: str | None; answer_key: str; model: str; prompt_version: int
    grade: Grade | None; grade_key: str | None; error: str | None

@dataclass
class Grade:
    claims: list[Claim]            # from the judge
    faithfulness: float | None     # supported claims / claims (uncited claims count as unsupported)
    citation_precision: float | None   # code: cited posts that the searches returned / cited posts
    post_coverage: float | None        # code: cited relevant posts / relevant posts
    overall: int                   # judge, 0-3
    judge_model: str; judge_version: int
```

### 5.3 Citations (code)
Markdown links `[text](url)` in the answer are mapped to post ids through the post URLs in the results (normalized: scheme and host lowercased, trailing slash and query dropped). A link to a URL that no result has counts as cited-but-not-retrieved. Bare post titles without a link are not counted as citations.

### 5.4 Judge (`GeminiAnswerJudge`)
Input: the question, the answer, and the results it was given. Output (JSON schema, checked like the retrieval judge, one retry): `claims: [{claim: str, post_id: str | "", supported: bool}]`, `overall: 0..3`. The judge marks `supported` only when the cited post's given passages say it. `ANSWER_JUDGE_VERSION = 1`.

### 5.5 Relevant posts
`expected` from the eval file ∪ posts graded 2–3 in `eval-judgments.yaml` (the retrieval judge). Post coverage uses that set; if it is empty for a question, coverage is `None` for it.

### 5.6 Storage and reuse
File: `<output_dir>/eval-answers/<label>.json` (`schema_version: 1`), atomic write after each question. Keys:
- `answer_key = sha256(question, ANSWER_PROMPT_VERSION, model, thinking_level, sorted result ids)`.
- `grade_key = sha256(answer, ANSWER_JUDGE_VERSION, judge_model)`.

Before calling Gemini, all label files are searched for a record with the same key, so an answer made for `baseline` is reused by another label when its results are identical. `--compare A B` prints both runs' scores and the difference.

### 5.7 Cost and errors
- **Estimate before any Gemini call** (no search either, since searching embeds the query): per question, `estimate_tokens(instructions + question) + 2 × n × avg_row_tokens + 1,500` output for the answer, and the same plus the answer for the grade. `avg_row_tokens` comes from one SQL query on the active generation. It is an upper bound: reused records cost nothing. Printed always; `--dry-run` stops there; over `max_tokens_per_run` without `--yes` stops with "re-run with --yes".
- **Missing `GEMINI_API_KEY`:** stops before any search: "GEMINI_API_KEY is not set; add it to .env".
- **One question fails** (search error, Gemini error, unusable judge JSON after the retry): `error` is saved for it, the run continues, and the report lists it.
- **Logging:** question ids, counts, timings and error class names only; never question text, answer text, passages or keys.

### 5.8 Report

```
Answer evaluation: label baseline, 36 questions (35 answered, 35 graded), gemini-3.8-flash
              faithfulness  citation precision  post coverage  overall (0-3)
all                  0.87               0.95           0.31           2.1
local / intent / global / multi-hop: …
failed: x-aws-outage (GeminiUnavailableError)
```

### 5.9 CLI
`scripts/eval_search.py --answers [--label NAME] [--dry-run] [--yes]` and `--compare A B`. `--answers` runs instead of the retrieval report (a separate run, so each costs what it says).

## 6. Secrets and data
Answers, results and grades contain archive text, so they live only in `<output_dir>/eval-answers/` (git-ignored data folder). No new secret. `check_safety.py` unchanged.

## 7. Tests

| Area | Tests |
|---|---|
| `fusion` | `group_by_post` both modes, order and tie-breaks; `rerank_batch` caps and summaries; `select_across_posts` round-robin, caps, "ten relevant posts" and "few relevant posts"; `hypothesis`: no duplicates, caps hold, every post's best item comes before any post's second item |
| `service` (`db`) | `posts` mode: the spec scenarios "one post with many matching passages" and "relevant posts further down"; re-ranker failure returns kept posts in combined order; every-source rule adds a missing newsletter's best post; `rows` mode unchanged (existing tests) |
| `config` | new settings, ranges, the batch-size check, `search.answers` parsing |
| `evaluate` | breadth and not-re-ranked lines and JSON; the new overrides |
| `answers` | fake answerer and judge: plan runs 2 searches; prompt contains the `ask` text and the results; citations parsing (links, unknown URLs, no links; `hypothesis` on URL normalizing); metrics maths; reuse across runs and labels (second run makes no calls); estimate, `--dry-run`, ceiling and `--yes`; missing key stops before search; one failure continues; `--compare`; logs carry no answer or question text |
| CLI | `--answers` and `--compare` end to end with fakes |

Real Gemini runs stay manual, with the user's go-ahead (§8).

## 8. Measurement and acceptance (live, each with the user's go-ahead)

1. **Baseline answers** (`selection: rows`, `--label baseline`): dry run shows the cost first (about 0.7M tokens, under $1).
2. **Grid on retrieval** (query embeddings only, a few cents): `selection: posts`, `post_score` ∈ {max, sum_top3} × `rerank_posts` ∈ {10, 15, 20} × `rerank_rows_per_post` ∈ {1, 2, 3}, run as the 18 cheapest-first combinations that fit the p90 budget. Pick: the smallest batch that meets distinct posts ≥ 8, recall@10 ≥ 0.84, nDCG@10 ≥ 0.89 (judge grades), local recall ≥ 0.91, p90 ≤ 8 s. If none meets all, the best nDCG within the budget, reported as such.
3. **Answers on the chosen configuration** (`--label posts-<score>-<posts>x<rows>`), `--compare baseline …`.
4. Last PR: the measured defaults, `selection: posts`, the measurements in the PR and in `config.py` comments.
