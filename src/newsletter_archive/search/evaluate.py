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

import statistics
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import yaml

from ..config import Config
from ..errors import ArchiveError
from . import db
from .service import Scorer, SearchRequest, SearchService

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
            f"no evaluation file at {path}; see openspec/changes/add-hybrid-search/lld.md §13"
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


@dataclass
class Report:
    outcomes: list[Outcome] = field(default_factory=list)
    generation: int | None = None
    model: str = ""
    rerank_top: int = 0

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

    def best_stage(self) -> str:
        return next((s for s in reversed(STAGES) if self.score(s, True) is not None), "keyword")

    def needs_contextual_retrieval(self) -> bool | None:
        """lld §13: True when local or intent recall@10 (final stage, with summaries) is below
        0.8; None when there are no such questions."""
        stage = self.best_stage()
        found = [self.score(stage, True, t) for t in ("local", "intent")]
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
            p90 = seconds[min(len(seconds) - 1, int(0.9 * len(seconds)))]
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
            "questions": [
                {
                    "id": o.question.id,
                    "summaries": o.summaries,
                    "seconds": round(o.seconds, 2),
                    "scores": {st: {"recall_at_10": r, "rr": m} for st, (r, m) in o.scores.items()},
                    "missing": list(o.missing),
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
    for q in questions:
        for summaries in variants:
            request = SearchRequest.create(q.question, K, kind="all" if summaries else "passages")
            trace: dict[str, Any] = {}
            started = clock()
            resp = service.search(request, generation=generation, trace=trace)
            seconds = clock() - started
            report.generation = resp.generation
            post_of = trace["post_of"]
            # A stage is scored only if it really ran: semantic and combined need the query
            # embedding (combined alone would just repeat keyword), reranked needs the re-ranker.
            ran = {
                "keyword": True,
                "semantic": resp.semantic,
                "combined": resp.semantic,
                "reranked": resp.reranked,
            }
            scores = {}
            for stage in STAGES:
                if ran[stage]:
                    ranking = post_ranking(trace[stage], post_of)
                    scores[stage] = (recall_at(ranking, q.expected), reciprocal_rank(ranking, q.expected))
            final = post_ranking([r.id for r in resp.results], post_of)
            scores[RETURNED] = (recall_at(final, q.expected), reciprocal_rank(final, q.expected))
            missing = tuple(e for e in q.expected if e not in final[:K])
            outcome = Outcome(q, summaries, seconds, resp.semantic, resp.reranked, scores, missing)
            report.outcomes.append(outcome)
            if on_question:
                on_question(outcome)
    return report


class GateError(ArchiveError):
    """The quality gate couldn't score both versions fairly (e.g. Gemini failed)."""


def quality_gate(
    service: SearchService, questions: Sequence[Question]
) -> Callable[[db.Generation, db.Generation], tuple[float, float] | None]:
    """A gate for ``Generations``: (candidate, live) → their combined-stage recall@10 with
    summaries, in points. The combined stage isolates the embedding model (what a new version
    changes) and needs no re-ranker. Raises GateError if semantic search failed for any
    question, since keyword-only scores would make the comparison meaningless."""

    def gate(candidate: db.Generation, active: db.Generation) -> tuple[float, float] | None:
        points = []
        for gen in (candidate, active):
            report = evaluate(service, questions, generation=gen, variants=(True,))
            if any(not o.semantic for o in report.outcomes):
                raise GateError(
                    f"quality check incomplete: semantic search failed for generation {gen.generation}; "
                    "run again when Gemini is reachable, or use --force"
                )
            sc = report.score("combined", True)
            points.append(100 * sc[0] if sc else 0.0)
        return points[0], points[1]

    return gate


# -- wiring ---------------------------------------------------------------------------------------

EVAL_FILE = "eval-queries.yaml"


def eval_path(config: Config) -> Path:
    return config.output_dir / EVAL_FILE


def reader_service(config: Config, reranker: Scorer | None = None) -> SearchService:
    """A search service on direct reader connections with the real Gemini query embedder."""
    from contextlib import contextmanager

    from .service import gemini_query_embedder

    @contextmanager
    def connect() -> Iterator[psycopg.Connection]:
        with db.connect(config, "reader") as conn:
            yield conn

    return SearchService(config, connect, gemini_query_embedder(config), reranker)


def gate_for(config: Config) -> Callable[[db.Generation, db.Generation], tuple[float, float] | None] | None:
    """The quality gate when the evaluation file exists, else None (swaps then warn)."""
    if not eval_path(config).exists():
        return None
    return quality_gate(reader_service(config), load_questions(eval_path(config)))


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - real Gemini, model and database
    import argparse
    import json
    import sys
    from dataclasses import replace

    from ..config import load_config
    from .rerank import Reranking, qwen_loader

    parser = argparse.ArgumentParser(description="Evaluate search quality on your own questions.")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--file", type=Path, help=f"questions file (default: <output_dir>/{EVAL_FILE})")
    parser.add_argument("--generation", type=int, help="score this index version instead of the live one")
    parser.add_argument("--rerank-top", type=int, help="override search.rerank_top")
    parser.add_argument("--no-rerank", action="store_true", help="skip the re-ranker")
    parser.add_argument("--passages-only", action="store_true", help="skip the with-summaries variant")
    parser.add_argument("--json", type=Path, help="also write the full report as JSON")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config, args.config.parent / ".env")
        if args.rerank_top:
            config = replace(config, search=replace(config.search, rerank_top=args.rerank_top))
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

        variants = (False,) if args.passages_only else (True, False)
        report = evaluate(service, questions, generation=generation, variants=variants, on_question=progress)
        report.model = generation.embedding_model
    except ArchiveError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print("\n".join(report.lines()))
    if args.json:
        args.json.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    return 0
