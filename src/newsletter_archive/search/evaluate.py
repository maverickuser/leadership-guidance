"""Search quality evaluation on the user's own questions (``scripts/eval_search.py``, lld §13).

Questions live in ``{output_dir}/eval-queries.yaml`` (personal, never committed), each tagged
``local`` (a fact in one post), ``intent`` (paraphrased), ``global`` (broad, many posts) or
``multi-hop`` (needs several posts), with the post ids that should be found.

Each question runs through the real search pipeline once with summaries (``kind: all``) and
once without (``kind: passages``). One run yields all four stages: keyword only, semantic only,
combined (RRF) and combined + re-ranked, plus ``returned``: the final results the agent
receives (re-ranked, at most ``max_per_post`` passages per post, 10 results). Scores are post
level: a post counts as found when any of its passages or its summary is ranked. Reported per
stage, with and without summaries, overall and per question type:
- recall@10: the share of a question's expected posts among its first 10 distinct posts;
- MRR: 1 / the rank of the first expected post (0 if none in the ranking).

The same machinery is the quality gate for index versions: it compares a candidate version's
combined-stage recall@10 (with summaries) with the live one's, in points (0-100).
"""

from __future__ import annotations

import logging
import math
import statistics
import time
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import psycopg
import yaml
from psycopg import sql

from ..config import Config
from ..errors import ArchiveError
from . import db
from .service import MAX_QUERY_CHARS, QueryEmbedder, Scorer, SearchRequest, SearchService

log = logging.getLogger("newsletter_archive.search.evaluate")

STAGES = ("keyword", "semantic", "combined", "reranked")
RETURNED = "returned"  # the final results the agent receives (after the per-post cap and limit)
ROWS = (*STAGES, RETURNED)
TYPES = ("local", "intent", "global", "multi-hop")
K = 10
CONTEXT_RULE_RECALL = 0.8  # lld §13: add contextual retrieval if local/intent recall@10 is below this


class EvalFileError(ArchiveError):
    """The evaluation file is missing or malformed."""


@dataclass(frozen=True)
class Question:
    id: str
    type: str
    question: str
    expected: tuple[str, ...]


def load_questions(path: Path) -> list[Question]:
    if not path.exists():
        raise EvalFileError(
            f"no evaluation file at {path}; "
            "see openspec/changes/archive/2026-11-10-add-hybrid-search/lld.md §13"
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise EvalFileError(f"{path.name} is not valid YAML: {e}") from None
    raw = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        raise EvalFileError(f"{path.name} needs a non-empty `questions:` list")
    out: list[Question] = []
    seen: set[str] = set()
    for i, q in enumerate(raw, start=1):
        where = f"{path.name} question {i}"
        if not isinstance(q, dict):
            raise EvalFileError(f"{where} must be a mapping")
        qid, qtype, text, expected = q.get("id"), q.get("type"), q.get("question"), q.get("expected")
        if not isinstance(qid, str) or not qid.strip():
            raise EvalFileError(f"{where} needs an id")
        if qid in seen:
            raise EvalFileError(f"{where}: duplicate id {qid!r}")
        if qtype not in TYPES:
            raise EvalFileError(f"{where} ({qid}): type must be one of {', '.join(TYPES)}")
        if not isinstance(text, str) or not text.strip():
            raise EvalFileError(f"{where} ({qid}) needs a question")
        if len(text.strip()) > MAX_QUERY_CHARS:
            raise EvalFileError(f"{where} ({qid}): the question is over {MAX_QUERY_CHARS} characters")
        if not isinstance(expected, list) or not expected or not all(isinstance(e, str) for e in expected):
            raise EvalFileError(f"{where} ({qid}): expected must be a non-empty list of post ids")
        seen.add(qid)
        out.append(Question(qid, str(qtype), text.strip(), tuple(dict.fromkeys(expected))))
    return out


# -- metrics -------------------------------------------------------------------------------------


def post_ranking(passages: Iterable[str], post_of: dict[str, str]) -> list[str]:
    """Distinct posts in the order their first passage or summary appears."""
    return list(dict.fromkeys(post_of[p] for p in passages if p in post_of))


def recall_at(ranking: Sequence[str], expected: Sequence[str], k: int = K) -> float:
    return len(set(ranking[:k]) & set(expected)) / len(expected)


def reciprocal_rank(ranking: Sequence[str], expected: Sequence[str]) -> float:
    wanted = set(expected)
    return next((1.0 / i for i, post in enumerate(ranking, start=1) if post in wanted), 0.0)


# -- running -------------------------------------------------------------------------------------


@dataclass
class Outcome:
    """One question in one variant (with or without summaries)."""

    question: Question
    summaries: bool
    seconds: float
    semantic: bool
    reranked: bool
    scores: dict[str, tuple[float, float]]  # stage -> (recall@10, reciprocal rank); missing stages left out
    missing: tuple[str, ...]  # expected posts not in the final top 10
    rankings: dict[str, tuple[str, ...]] = field(default_factory=dict)  # stage -> posts, best first
    hits: tuple[str, ...] = ()  # passage and summary ids search found, best first
    expected_scores: tuple[float, ...] = ()  # re-ranker scores of returned results from expected posts
    returned_posts: int = 0  # distinct posts in the returned results
    found_by_combined: tuple[str, ...] = ()  # expected posts among the combined stage's top 10 posts
    unscored: tuple[str, ...] = ()  # of those, the ones with no row in the re-rank batch


def source_of(post_id: str) -> str:
    """The newsletter of a post id (`<source>/<slug>`); "" for ids from before the move."""
    return post_id.split("/", 1)[0] if "/" in post_id else ""


def _sources(post_ids: Iterable[str]) -> set[str]:
    """The newsletters among post ids, leaving out ids from before the move."""
    return {s for s in map(source_of, post_ids) if s}


@dataclass
class Report:
    outcomes: list[Outcome] = field(default_factory=list)
    generation: int | None = None
    model: str = ""
    rerank_top: int = 0
    unknown_expected: dict[str, list[str]] = field(default_factory=dict)  # question id -> post ids

    def outcomes_for(self, summaries: bool, qtype: str | None = None) -> list[Outcome]:
        return [
            o
            for o in self.outcomes
            if o.summaries == summaries and (qtype is None or o.question.type == qtype)
        ]

    def score(self, stage: str, summaries: bool, qtype: str | None = None) -> tuple[float, float, int] | None:
        """(mean recall@10, MRR, questions scored), or None when no question has that stage."""
        rows = [o.scores[stage] for o in self.outcomes_for(summaries, qtype) if stage in o.scores]
        if not rows:
            return None
        return statistics.fmean(r for r, _ in rows), statistics.fmean(m for _, m in rows), len(rows)

    def source_coverage(self) -> tuple[float, int] | None:
        """(share, questions) over questions whose expected posts span several newsletters: the
        share where every one of those newsletters appears in the returned top 10 (with
        summaries). None when there are no such questions."""
        cross = self._cross_newsletter()
        if not cross:
            return None
        covered = [_sources(o.question.expected) <= _sources(o.rankings.get(RETURNED, ())[:K]) for o in cross]
        return sum(covered) / len(cross), len(cross)

    def _cross_newsletter(self) -> list[Outcome]:
        return [o for o in self.outcomes_for(True) if len(_sources(o.question.expected)) > 1]

    def breadth(self) -> float | None:
        """The median number of distinct posts returned per question (with summaries)."""
        counts = [o.returned_posts for o in self.outcomes_for(True)]
        return statistics.median(counts) if counts else None

    def unscored_share(self) -> float | None:
        """Of the expected posts the combined stage put in its top 10, the share that had no row
        in the re-rank batch (re-ranked searches with summaries only)."""
        rows = [o for o in self.outcomes_for(True) if o.reranked]
        found = sum(len(o.found_by_combined) for o in rows)
        return sum(len(o.unscored) for o in rows) / found if found else None

    def suggested_source_min_score(self) -> float | None:
        """lld §9.3: the lowest re-ranker score among returned results from expected posts in
        the cross-newsletter questions, rounded down to 0.05; None when nothing was re-ranked.
        Measure with `search.source_min_score: 0`, so no expected result is held back by the
        current value."""
        scores = [s for o in self._cross_newsletter() for s in o.expected_scores]
        return math.floor(min(scores) * 20) / 20 if scores else None

    def best_stage(self) -> str:
        return next((s for s in reversed(STAGES) if self.score(s, True) is not None), "keyword")

    def needs_contextual_retrieval(self) -> bool | None:
        """lld §13: True when local or intent recall@10 at the combined (retrieval) stage, with
        summaries, is below 0.8; None when there are no such questions or semantic search never
        ran. Re-ranking is left out: the rule is about what retrieval finds."""
        found = [self.score("combined", True, t) for t in ("local", "intent")]
        recalls = [f[0] for f in found if f is not None]
        return None if not recalls else min(recalls) < CONTEXT_RULE_RECALL

    def lines(self) -> list[str]:
        n = len({o.question.id for o in self.outcomes})
        out = [
            f"Search evaluation: {n} questions, generation {self.generation} ({self.model}), "
            f"rerank_top {self.rerank_top}, post-level recall@{K} and MRR",
            "",
            f"{'stage':<10} {'with summaries':>22} {'passages only':>22}",
            f"{'':<10} {'recall@10':>11} {'MRR':>10} {'recall@10':>11} {'MRR':>10}",
        ]
        for stage in ROWS:
            cells = []
            for summaries in (True, False):
                sc = self.score(stage, summaries)
                cells.append(f"{sc[0]:>11.2f} {sc[1]:>10.2f}" if sc else f"{'n/a':>11} {'n/a':>10}")
            out.append(f"{stage:<10} {cells[0]} {cells[1]}")
        types = [t for t in TYPES if self.outcomes_for(True, t)]
        out += ["", f"recall@10 by question type (with summaries): {'  '.join(f'{t:>9}' for t in types)}"]
        for stage in ROWS:
            cells = []
            for t in types:
                sc = self.score(stage, True, t)
                cells.append(f"{sc[0]:>9.2f}" if sc else f"{'n/a':>9}")
            out.append(f"  {stage:<10}{'':<34}{'  '.join(cells)}")
        seconds = sorted(o.seconds for o in self.outcomes)
        if seconds:
            p90 = seconds[max(0, math.ceil(0.9 * len(seconds)) - 1)]
            out += ["", f"latency per search: median {statistics.median(seconds):.1f}s, p90 {p90:.1f}s"]
        degraded = [o for o in self.outcomes if not o.semantic or not o.reranked]
        if degraded:
            out.append(
                f"warning: {len(degraded)} searches ran without semantic search or re-ranking; "
                "their stages are missing from the table"
            )
        verdict = self.needs_contextual_retrieval()
        if verdict is not None:
            out.append(
                "contextual retrieval: "
                + (
                    "needed (local or intent recall@10 below 0.8)"
                    if verdict
                    else "not needed (local and intent ≥ 0.8)"
                )
            )
        breadth = self.breadth()
        if breadth is not None:
            out.append(f"breadth: median {breadth:g} distinct posts returned (limit {K})")
        unscored = self.unscored_share()
        if unscored is not None:
            out.append(
                f"not re-ranked: {unscored:.0%} of expected posts found by the combined stage "
                "were never scored"
            )
        coverage = self.source_coverage()
        if coverage is not None:
            out.append(
                f"source coverage: {coverage[0]:.2f} of {coverage[1]} cross-newsletter questions "
                "show every expected newsletter in the top 10"
            )
        suggested = self.suggested_source_min_score()
        if suggested is not None:
            out.append(
                f"source_min_score suggested by this run: {suggested:.2f} (lowest expected-result score)"
            )
        for qid, ids in self.unknown_expected.items():
            out.append(f"warning: {qid} expects posts that aren't in the index: {', '.join(ids)}")
        misses = [o for o in self.outcomes_for(True) if o.missing]
        if misses:
            out += ["", f"questions missing expected posts in the final top {K} (with summaries):"]
            out += [f"  {o.question.id} ({o.question.type}): {', '.join(o.missing)}" for o in misses]
        return out

    def to_dict(self) -> dict[str, Any]:
        def cell(sc: tuple[float, float, int] | None) -> dict[str, float] | None:
            return {"recall_at_10": round(sc[0], 4), "mrr": round(sc[1], 4), "n": sc[2]} if sc else None

        return {
            "generation": self.generation,
            "model": self.model,
            "rerank_top": self.rerank_top,
            "overall": {
                ("with_summaries" if s else "passages_only"): {st: cell(self.score(st, s)) for st in ROWS}
                for s in (True, False)
            },
            "by_type": {t: {st: cell(self.score(st, True, t)) for st in ROWS} for t in TYPES},
            "contextual_retrieval_needed": self.needs_contextual_retrieval(),
            "breadth_median_posts": self.breadth(),
            "unscored_share": self.unscored_share(),
            "unknown_expected": self.unknown_expected,
            "questions": [
                {
                    "id": o.question.id,
                    "summaries": o.summaries,
                    "seconds": round(o.seconds, 2),
                    "scores": {st: {"recall_at_10": r, "rr": m} for st, (r, m) in o.scores.items()},
                    "missing": list(o.missing),
                    "returned_posts": o.returned_posts,
                    "unscored": list(o.unscored),
                }
                for o in self.outcomes
            ],
        }


def evaluate(
    service: SearchService,
    questions: Sequence[Question],
    *,
    generation: db.Generation | None = None,
    variants: Sequence[bool] = (True, False),
    on_question: Callable[[Outcome], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Report:
    """Run every question in each variant (summaries on / off) through the service."""
    report = Report(rerank_top=service.s.rerank_top)
    known: set[str] | None = None
    for q in questions:
        for summaries in variants:
            request = SearchRequest.create(q.question, K, kind="all" if summaries else "passages")
            trace: dict[str, Any] = {}
            service.resume_semantic()  # score each question on its own, not on an earlier failure
            started = clock()
            resp = service.search(request, generation=generation, trace=trace)
            seconds = clock() - started
            report.generation = resp.generation
            if known is None:
                known = _known_posts(service, generation or _generation_of(service, resp.generation))
            unknown = [e for e in q.expected if e not in known]
            if unknown:
                report.unknown_expected[q.id] = unknown
            post_of = trace["post_of"]
            # A stage is scored only if it really ran: semantic and combined need the query
            # embedding (combined alone would just repeat keyword), reranked needs the re-ranker.
            ran = {
                "keyword": True,
                "semantic": resp.semantic,
                "combined": resp.semantic,
                "reranked": resp.reranked,
            }
            scores, rankings = {}, {}
            for stage in STAGES:
                if ran[stage]:
                    ranking = post_ranking(trace[stage], post_of)
                    rankings[stage] = tuple(ranking)
                    scores[stage] = (recall_at(ranking, q.expected), reciprocal_rank(ranking, q.expected))
            final = post_ranking([r.id for r in resp.results], post_of)
            rankings[RETURNED] = tuple(final)
            scores[RETURNED] = (recall_at(final, q.expected), reciprocal_rank(final, q.expected))
            missing = tuple(e for e in q.expected if e not in final[:K])
            order = [r.id for r in resp.results] + [
                h for st in reversed(STAGES) if ran[st] for h in trace[st]
            ]
            hits = tuple(dict.fromkeys(order))
            expected_scores = (
                tuple(r.score for r in resp.results if r.post.id in q.expected) if resp.reranked else ()
            )
            combined_top = post_ranking(trace["combined"], post_of)[:K]
            found = tuple(e for e in q.expected if e in combined_top)
            batch_posts = {post_of[p] for p in trace.get("batch", []) if p in post_of}
            unscored = tuple(e for e in found if e not in batch_posts) if resp.reranked else ()
            outcome = Outcome(
                q,
                summaries,
                seconds,
                resp.semantic,
                resp.reranked,
                scores,
                missing,
                rankings,
                hits,
                expected_scores,
                len({r.post.id for r in resp.results}),
                found,
                unscored,
            )
            report.outcomes.append(outcome)
            if on_question:
                on_question(outcome)
    return report


def _generation_of(service: SearchService, number: int) -> db.Generation:
    with service.connect() as conn:
        return next(g for g in db.list_generations(conn) if g.generation == number)


def _known_posts(service: SearchService, generation: db.Generation) -> set[str]:
    with service.connect() as conn:
        rows = conn.execute(
            sql.SQL("SELECT post_id FROM {}.posts").format(sql.Identifier(generation.schema))
        ).fetchall()
    return {r[0] for r in rows}


class GateError(ArchiveError):
    """The quality gate couldn't score both versions fairly (e.g. Gemini failed)."""


def quality_gate(
    service: SearchService, questions: Sequence[Question]
) -> Callable[[db.Generation, db.Generation], tuple[float, float] | None]:
    """A gate for ``Generations``: (candidate, live) → their combined-stage recall@10 with
    summaries, in points. The combined stage isolates the embedding model (what a new version
    changes) and needs no re-ranker.

    The candidate is scored first. If semantic search fails for any of its questions, the check
    can't run (GateError: try again, or --force). If the candidate scores fine but the live
    version can't be embedded at all, its model is most likely retired (a common reason to
    refresh): there is nothing to compare with, so the gate returns None and the swap goes
    ahead with a warning. A partial failure on the live version is treated as an outage."""

    def score(gen: db.Generation) -> tuple[float, int]:
        report = evaluate(service, questions, generation=gen, variants=(True,))
        failed = sum(1 for o in report.outcomes if not o.semantic)
        sc = report.score("combined", True)
        return (100 * sc[0] if sc else 0.0), failed

    def gate(candidate: db.Generation, active: db.Generation) -> tuple[float, float] | None:
        candidate_points, failed = score(candidate)
        if failed:
            raise GateError(
                f"quality check incomplete: semantic search failed for {failed} questions on generation "
                f"{candidate.generation}; run again when Gemini is reachable"
            )
        live_points, failed = score(active)
        if failed == len(questions):
            log.warning(
                "the live generation %s (%s) can't embed queries any more (model retired?): "
                "no quality comparison; candidate recall@10 %.1f",
                active.generation,
                active.embedding_model,
                candidate_points,
            )
            return None
        if failed:
            raise GateError(
                f"quality check incomplete: semantic search failed for {failed} questions on generation "
                f"{active.generation}; run again when Gemini is reachable"
            )
        return candidate_points, live_points

    return gate


# -- wiring ---------------------------------------------------------------------------------------

EVAL_FILE = "eval-queries.yaml"


def eval_path(config: Config) -> Path:
    return config.output_dir / EVAL_FILE


class _MemoEmbedder:
    """Remembers query vectors: each question runs in two variants (kind doesn't change the
    embedding), and the gate scores two versions that may share a model."""

    def __init__(self, inner: QueryEmbedder, memo: dict[tuple[str, int, str], np.ndarray]) -> None:
        self.inner, self.memo = inner, memo
        self.model, self.dim = inner.model, inner.dim

    def embed(self, texts: Sequence[str]) -> list[np.ndarray]:
        missing = [t for t in texts if (self.model, self.dim, t) not in self.memo]
        if missing:
            for t, v in zip(missing, self.inner.embed(missing), strict=True):
                self.memo[(self.model, self.dim, t)] = v
        return [self.memo[(self.model, self.dim, t)] for t in texts]


def memoized(factory: Callable[[str, int], QueryEmbedder]) -> Callable[[str, int], QueryEmbedder]:
    memo: dict[tuple[str, int, str], np.ndarray] = {}
    return lambda model, dim: _MemoEmbedder(factory(model, dim), memo)


def reader_service(config: Config, reranker: Scorer | None = None) -> SearchService:
    """A search service for evaluation: one reused reader connection (single-threaded; reopened
    if it drops) and the real Gemini query embedder with each query embedded once."""
    from contextlib import nullcontext

    from .service import gemini_query_embedder

    held: list[psycopg.Connection] = []

    def connect() -> AbstractContextManager[psycopg.Connection]:
        if not held or held[0].closed or held[0].broken:
            held[:] = [db.connect(config, "reader")]
        return nullcontext(held[0])

    return SearchService(config, connect, memoized(gemini_query_embedder(config)), reranker)


def gate_for(config: Config) -> Callable[[db.Generation, db.Generation], tuple[float, float] | None] | None:
    """The quality gate when the evaluation file exists, else None (swaps then warn)."""
    if not eval_path(config).exists():
        return None
    return quality_gate(reader_service(config), load_questions(eval_path(config)))


def variants_for(passages_only: bool, summaries_only: bool) -> tuple[bool, ...]:
    """Which variants to run: with summaries (True), passages only (False), or both."""
    if passages_only and summaries_only:
        raise EvalFileError("use --passages-only or --summaries-only, not both")
    return (False,) if passages_only else (True,) if summaries_only else (True, False)


def with_search_overrides(config: Config, args: Any) -> Config:
    """The config with the command line's search overrides applied and checked."""
    from dataclasses import replace

    changes = {
        name: value
        for name, value in (
            ("rerank_top", getattr(args, "rerank_top", None)),
            ("selection", getattr(args, "selection", None)),
            ("post_score", getattr(args, "post_score", None)),
            ("rerank_posts", getattr(args, "rerank_posts", None)),
            ("rerank_rows_per_post", getattr(args, "rows_per_post", None)),
        )
        if value is not None
    }
    return config.with_overrides(search=replace(config.search, **changes)) if changes else config


def _answers_main(args: Any) -> int:  # pragma: no cover - real Gemini, model and database
    """`--answers` and `--compare` (openspec improve-search-post-selection, lld §5.9)."""
    import sys

    from ..config import load_config
    from . import answers
    from .judge import JUDGE_FILE, load_judgments
    from .rerank import Reranking, qwen_loader

    try:
        config = with_search_overrides(load_config(args.config, args.config.parent / ".env"), args)
        questions = load_questions(args.file or eval_path(config))
        if args.compare:
            if args.answers:
                raise EvalFileError("use --answers or --compare, not both")
            for label in args.compare:
                if not answers.run_exists(config.output_dir, label):
                    raise EvalFileError(f"no saved answer run {label!r}; run --answers --label {label} first")
            a, b = (answers.load_run(config.output_dir, label) for label in args.compare)
            print("\n".join(answers.compare_lines(a, b, questions)))
            return 0
        judgments = (
            load_judgments(config.output_dir / JUDGE_FILE)
            if (config.output_dir / JUDGE_FILE).exists()
            else {}
        )

        def service() -> SearchService:
            reranking = Reranking(
                qwen_loader(config.search.reranker_model, config.output_dir),
                config.search.rerank_timeout_seconds,
            )
            reranking.wait_loaded(300)
            print(f"re-ranker: {reranking.status}", file=sys.stderr)
            return reader_service(config, reranking)

        answerer, judge_ = answers.gemini_factories(config)
        return answers.run_answers(
            config,
            questions,
            args.label,
            dry_run=args.dry_run,
            yes=args.yes,
            service_factory=service,
            answerer_factory=answerer,
            judge_factory=judge_,
            judge_grades={qid: j.grades for qid, j in judgments.items()},
        )
    except (ArchiveError, psycopg.Error) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - real Gemini, model and database
    import argparse
    import json
    import sys

    from ..config import load_config
    from .rerank import Reranking, qwen_loader

    parser = argparse.ArgumentParser(description="Evaluate search quality on your own questions.")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--file", type=Path, help=f"questions file (default: <output_dir>/{EVAL_FILE})")
    parser.add_argument("--generation", type=int, help="score this index version instead of the live one")
    parser.add_argument("--rerank-top", type=int, help="override search.rerank_top")
    parser.add_argument("--selection", choices=("rows", "posts"), help="override search.selection")
    parser.add_argument("--post-score", choices=("max", "sum_top3"), help="override search.post_score")
    parser.add_argument("--rerank-posts", type=int, help="override search.rerank_posts")
    parser.add_argument("--rows-per-post", type=int, help="override search.rerank_rows_per_post")
    parser.add_argument("--no-rerank", action="store_true", help="skip the re-ranker")
    parser.add_argument("--passages-only", action="store_true", help="skip the with-summaries variant")
    parser.add_argument("--summaries-only", action="store_true", help="skip the passages-only variant")
    parser.add_argument("--json", type=Path, help="also write the full report as JSON")
    parser.add_argument(
        "--judge",
        action="store_true",
        help="also grade the results with Gemini and suggest changes to the expected lists",
    )
    parser.add_argument(
        "--answers",
        action="store_true",
        help="instead: write an answer per question and grade it with Gemini (shows the cost first)",
    )
    parser.add_argument("--label", default="baseline", help="name of this answer run (default: baseline)")
    parser.add_argument(
        "--dry-run", action="store_true", help="with --answers: show the cost, change nothing"
    )
    parser.add_argument("--yes", action="store_true", help="with --answers: go ahead over the per-run limit")
    parser.add_argument(
        "--compare", nargs=2, metavar=("LABEL_A", "LABEL_B"), help="compare two saved answer runs and exit"
    )
    args = parser.parse_args(argv)
    if args.answers or args.compare:
        return _answers_main(args)

    try:
        config = load_config(args.config, args.config.parent / ".env")
        config = with_search_overrides(config, args)
        questions = load_questions(args.file or eval_path(config))
        reranker = None
        if not args.no_rerank:
            reranking = Reranking(
                qwen_loader(config.search.reranker_model, config.output_dir),
                config.search.rerank_timeout_seconds,
            )
            reranking.wait_loaded(300)
            print(f"re-ranker: {reranking.status}", file=sys.stderr)
            reranker = reranking
        service = reader_service(config, reranker)
        generation = None
        with db.connect(config, "reader") as conn:
            gens = {g.generation: g for g in db.list_generations(conn)}
            generation = gens.get(args.generation) if args.generation else db.active_generation(conn)
        if generation is None:
            raise EvalFileError("no such index version (see `archive index`)")

        def progress(o: Outcome) -> None:
            r = o.scores.get("reranked") or o.scores.get("combined") or o.scores.get("keyword")
            print(f"  {o.question.id:<24} {'summaries' if o.summaries else 'passages':<9} "
                  f"recall@10 {r[0] if r else 0:.2f}  {o.seconds:.1f}s", file=sys.stderr)  # fmt: skip

        variants = variants_for(args.passages_only, args.summaries_only)
        report = evaluate(service, questions, generation=generation, variants=variants, on_question=progress)
        report.model = generation.embedding_model
    except ArchiveError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print("\n".join(report.lines()))
    out = report.to_dict()
    status = 0
    if args.judge:  # advisory: a judge failure never hides the report above
        from . import judge

        try:
            verdict = judge.run(config, report, questions, generation)
        except ArchiveError as e:
            print(f"\nLLM judge failed (grades so far are saved): {e}", file=sys.stderr)
            status = 1
        else:
            print("\n".join(verdict.lines()))
            out["judge"] = verdict.to_dict()
    if args.json:
        args.json.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return status
