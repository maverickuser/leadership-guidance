"""LLM relevance judge for the evaluation questions (``python scripts/eval_search.py --judge``).

For each question, the posts that any search stage ranked in its top 10, plus the expected
ones, form a pool. Gemini grades each pooled post 0-3 from its title, summary and the passages
search found for the question. Grades are saved in ``<output_dir>/eval-judgments.yaml``
(personal, never committed) and reused: only new posts or changed questions are graded again,
so a run is cheap and the numbers stay stable.

The grades never decide a swap (the quality gate keeps using the expected lists). They:
- suggest posts to add to a question's ``expected`` list (graded 3, or 2, but not listed);
- flag listed posts the judge finds weak (graded 0 or 1), for you to look at again;
- give a graded score per stage, nDCG@10, that rewards the best posts ranking first.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Protocol

import psycopg
import yaml
from psycopg import sql

from ..config import Config
from ..errors import ArchiveError
from ..store import write_text_atomic
from . import db
from .evaluate import RETURNED, STAGES, Question, Report
from .gemini import GeminiClient, GeminiResponseError
from .summaries import RETRY_TEMPERATURE, check_response, thinking_config
from .text import estimate_tokens

log = logging.getLogger("newsletter_archive.search.judge")

JUDGE_FILE = "eval-judgments.yaml"
JUDGE_PROMPT_VERSION = 1  # bump when PROMPT changes: every grade is then made again
POOL_DEPTH = 10  # posts taken from each stage's ranking
BATCH = 12  # posts graded per request
PASSAGES_PER_POST = 2
TEXT_CHARS = 1500  # of each summary and passage shown
MAX_OUTPUT_TOKENS = 8192  # room for the grades plus thinking, even with no thinking setting
ATTEMPTS = 2
GRADES = (0, 1, 2, 3)
K = 10

PROMPT = (
    "You grade search results from a personal archive of newsletter posts about leadership, "
    "management and careers. For the question, grade how useful each post is for answering "
    "it, from what is shown of the post:\n"
    "3 = essential: it directly answers the question or is a main source for it\n"
    "2 = relevant: it answers part of the question or adds useful evidence\n"
    "1 = related: same general topic, but it doesn't help answer the question\n"
    "0 = not relevant\n"
    "Grade each post on its own merits; don't favour longer posts or the order they appear in. "
    'Answer with JSON only: a list with one {"post_id": ..., "grade": ...} for every post.'
)


class JudgeError(ArchiveError):
    """The judge couldn't grade a question (bad answers, or the judgments file is broken)."""


# -- the pool ------------------------------------------------------------------------------------


def pool(report: Report, questions: Sequence[Question]) -> dict[str, list[str]]:
    """Question id -> posts to grade: each stage's top 10 (both variants) plus the expected
    posts that are in the index. Ordered by a hash, so the judge sees no ranking."""
    unknown = report.unknown_expected
    out: dict[str, set[str]] = {q.id: set(q.expected) - set(unknown.get(q.id, ())) for q in questions}
    for o in report.outcomes:
        for ranking in o.rankings.values() if o.question.id in out else ():
            out[o.question.id].update(ranking[:POOL_DEPTH])
    return {qid: sorted(posts, key=partial(_shuffle_key, qid)) for qid, posts in out.items()}


def _shuffle_key(qid: str, post: str) -> str:
    return hashlib.sha256(f"{qid}\0{post}".encode()).hexdigest()


def hits_by_post(report: Report) -> dict[str, list[str]]:
    """Question id -> passage ids search found for it, best first (to show the judge)."""
    out: dict[str, list[str]] = {}
    for o in report.outcomes:
        seen = out.setdefault(o.question.id, [])
        seen.extend(h for h in o.hits if h not in seen)
    return out


@dataclass(frozen=True)
class Candidate:
    post_id: str
    title: str
    summary: str
    passages: tuple[str, ...]


def load_candidates(
    conn: psycopg.Connection, schema: str, posts: Iterable[str], hits: Sequence[str]
) -> dict[str, Candidate]:
    """Title, summary and up to PASSAGES_PER_POST passages per post: the ones search found
    for this question first, else the post's opening passages."""
    wanted = list(dict.fromkeys(posts))
    query = sql.SQL(
        "WITH found AS (SELECT id, ord FROM unnest(%(hits)s::text[]) WITH ORDINALITY AS f(id, ord)), "
        "ranked AS (SELECT x.post_id, x.kind, left(x.body, %(chars)s) AS body, row_number() OVER ("
        "  PARTITION BY x.post_id, x.kind ORDER BY f.ord NULLS LAST, x.n) AS rn "
        "  FROM {s}.passages x LEFT JOIN found f ON f.id = x.passage_id WHERE x.post_id = ANY(%(posts)s)) "
        "SELECT p.post_id, p.title, r.kind, r.body FROM {s}.posts p "
        "LEFT JOIN ranked r ON r.post_id = p.post_id AND r.rn <= %(keep)s "
        "WHERE p.post_id = ANY(%(posts)s) ORDER BY p.post_id, r.kind, r.rn"
    ).format(s=sql.Identifier(schema))
    params = {"hits": list(hits), "chars": TEXT_CHARS, "posts": wanted, "keep": PASSAGES_PER_POST}
    titles: dict[str, str] = {}
    summaries: dict[str, str] = {}
    passages: dict[str, list[str]] = {}
    for post_id, title, kind, body in conn.execute(query, params).fetchall():
        titles[post_id] = title
        if kind == "summary":
            summaries[post_id] = body
        elif kind is not None:
            passages.setdefault(post_id, []).append(body)
    return {
        p: Candidate(p, titles[p], summaries.get(p, ""), tuple(passages.get(p, ())))
        for p in wanted
        if p in titles
    }


# -- grading -------------------------------------------------------------------------------------


class Grader(Protocol):
    model: str

    def grade(self, question: str, candidates: Sequence[Candidate]) -> dict[str, int]: ...


def render(question: str, candidates: Sequence[Candidate]) -> str:
    parts = [f"Question: {question}", ""]
    for c in candidates:
        parts.append(f'<post id="{c.post_id}">')
        parts.append(f"Title: {c.title}")
        if c.summary:
            parts.append(f"Summary: {c.summary}")
        parts.extend(f"Passage: {p}" for p in c.passages)
        parts += ["</post>", ""]
    return "\n".join(parts)


def parse_grades(text: str, ids: Sequence[str]) -> dict[str, int]:
    """{post id: grade} for exactly ``ids``, or GeminiResponseError."""
    raw = text.strip()
    if raw.startswith("```"):  # a fenced answer despite the JSON setting
        raw = raw.strip("`").removeprefix("json").strip()
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        raise GeminiResponseError("the judge's answer is not JSON") from None
    if not isinstance(items, list):
        raise GeminiResponseError("the judge's answer is not a list")
    grades: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        pid, grade = item.get("post_id"), item.get("grade")
        if (
            isinstance(pid, str)
            and pid in ids
            and isinstance(grade, int)
            and not isinstance(grade, bool)
            and grade in GRADES
        ):
            grades[pid] = grade
    missing = [p for p in ids if p not in grades]
    if missing:
        raise GeminiResponseError(f"the judge left out or misgraded {len(missing)} posts")
    return grades


@dataclass
class GeminiGrader:
    client: GeminiClient
    model: str
    thinking_level: str = "low"

    def grade(self, question: str, candidates: Sequence[Candidate]) -> dict[str, int]:
        """Grades for every candidate; one retry on an unusable answer, then JudgeError. The
        retry differs from the first request (a note and a little temperature), or it would
        most likely get the same answer."""
        text = render(question, candidates)
        ids = [c.post_id for c in candidates]
        tokens = estimate_tokens(PROMPT) + estimate_tokens(text) + MAX_OUTPUT_TOKENS
        problem = ""
        for attempt in range(1, ATTEMPTS + 1):
            out: list[dict[str, int]] = []

            def keep(response: Any, out: list[dict[str, int]] = out) -> None:
                out.append(parse_grades(check_response(response), ids))

            note = (
                f" Your previous answer was unusable ({problem}): grade every post id exactly once."
                if problem
                else ""
            )
            request = partial(
                self._request,
                text=text,
                instruction=PROMPT + note,
                temperature=RETRY_TEMPERATURE if problem else 0.0,
            )
            try:
                self.client.call(request, tokens, "judge", keep)
                return out[0]
            except GeminiResponseError as e:
                problem = str(e)
                log.info("judge attempt %s unusable: %s", attempt, problem)
        raise JudgeError(f"no usable grades after {ATTEMPTS} attempts: {problem}")

    def _request(self, sdk: Any, text: str, instruction: str, temperature: float) -> Any:
        from google.genai import types

        schema = types.Schema(
            type=types.Type.ARRAY,
            items=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "post_id": types.Schema(type=types.Type.STRING),
                    "grade": types.Schema(type=types.Type.INTEGER),
                },
                required=["post_id", "grade"],
            ),
        )
        return sdk.models.generate_content(
            model=self.model,
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=instruction,
                temperature=temperature,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                thinking_config=thinking_config(self.thinking_level),
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )


# -- the judgments file --------------------------------------------------------------------------


@dataclass
class Judgment:
    question: str
    model: str
    prompt_version: int
    grades: dict[str, int] = field(default_factory=dict)

    def fits(self, question: str, model: str) -> bool:
        return (self.question, self.model, self.prompt_version) == (question, model, JUDGE_PROMPT_VERSION)


def load_judgments(path: Path) -> dict[str, Judgment]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return {
            qid: Judgment(
                str(j["question"]),
                str(j["model"]),
                int(j["prompt_version"]),
                {str(p): int(g) for p, g in (j.get("grades") or {}).items() if int(g) in GRADES},
            )
            for qid, j in (data.get("questions") or {}).items()
        }
    except (yaml.YAMLError, AttributeError, KeyError, TypeError, ValueError) as e:
        raise JudgeError(
            f"{path.name} is unreadable ({type(e).__name__}); delete it to grade again"
        ) from None


def save_judgments(path: Path, judgments: Mapping[str, Judgment]) -> None:
    data = {
        "version": 1,
        "questions": {
            qid: {
                "question": j.question,
                "model": j.model,
                "prompt_version": j.prompt_version,
                "grades": dict(sorted(j.grades.items())),
            }
            for qid, j in sorted(judgments.items())
        },
    }
    header = "# LLM relevance grades for eval-queries.yaml (0-3). Written by eval_search.py --judge.\n"
    write_text_atomic(path, header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def judge(
    report: Report,
    questions: Sequence[Question],
    judgments: dict[str, Judgment],
    candidates_for: Callable[[Sequence[str], Sequence[str]], dict[str, Candidate]],
    grader: Grader,
    save: Callable[[dict[str, Judgment]], None] = lambda j: None,
    on_question: Callable[[Question, int], None] | None = None,
) -> int:
    """Grade every pooled post not graded yet (all of them for a changed question). Saves
    after each batch, so an interrupted run keeps what it paid for. Returns posts graded."""
    pools, hits = pool(report, questions), hits_by_post(report)
    graded = 0
    for q in questions:
        current = judgments.get(q.id)
        if current is None or not current.fits(q.question, grader.model):
            current = judgments[q.id] = Judgment(q.question, grader.model, JUDGE_PROMPT_VERSION)
        todo = [p for p in pools.get(q.id, []) if p not in current.grades]
        if not todo:
            continue
        found = candidates_for(todo, hits.get(q.id, []))
        batch = [found[p] for p in todo if p in found]
        for start in range(0, len(batch), BATCH):
            part = batch[start : start + BATCH]
            current.grades.update(grader.grade(q.question, part))
            graded += len(part)
            save(judgments)
        if on_question:
            on_question(q, len(batch))
    return graded


# -- results -------------------------------------------------------------------------------------


def ndcg_at(ranking: Sequence[str], grades: Mapping[str, int], k: int = K) -> float | None:
    """nDCG@k with gain 2^grade - 1; ungraded posts count as 0. None if no post scores."""
    ideal = sorted(grades.values(), reverse=True)[:k]
    idcg = sum((2.0**g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    if idcg == 0:
        return None
    dcg = sum((2.0 ** grades.get(p, 0) - 1) / math.log2(i + 2) for i, p in enumerate(ranking[:k]))
    return dcg / idcg


@dataclass
class QuestionReview:
    question: Question
    graded: int  # grades used for this question (posts in the current pool)
    add: list[tuple[str, int]]  # (post, grade) graded 2-3 but not expected, best first
    doubtful: list[tuple[str, int]]  # expected but graded 0-1
    ungraded: list[str]  # expected posts without a grade (not in the index, or not graded yet)


@dataclass
class JudgeReport:
    reviews: list[QuestionReview]
    ndcg: dict[str, float]  # stage -> mean nDCG@10 (with summaries)
    agreement: float | None  # share of expected posts graded 2 or 3
    model: str
    graded_now: int = 0

    def lines(self) -> list[str]:
        out = ["", f"LLM judge ({self.model}): {self.graded_now} new grades this run"]
        if self.ndcg:
            out.append("nDCG@10 (with summaries, graded 0-3): " + "  ".join(
                f"{s} {v:.2f}" for s, v in self.ndcg.items()))  # fmt: skip
        if self.agreement is not None:
            out.append(f"expected posts the judge rates relevant (2-3): {100 * self.agreement:.0f}%")
        adds = [r for r in self.reviews if r.add]
        if adds:
            out += ["", "posts to consider adding to `expected` (grade):"]
            for r in adds:
                out.append(f"  {r.question.id}: " + ", ".join(f"{p} ({g})" for p, g in r.add))
        doubts = [r for r in self.reviews if r.doubtful]
        if doubts:
            out += ["", "expected posts the judge rates weak (grade), worth a second look:"]
            for r in doubts:
                out.append(f"  {r.question.id}: " + ", ".join(f"{p} ({g})" for p, g in r.doubtful))
        if not any(r.graded for r in self.reviews):
            out.append("no grades yet: nothing to compare with the expected lists")
        elif not adds and not doubts:
            out.append("the judge agrees with every expected list")
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "ndcg_at_10": self.ndcg,
            "agreement": self.agreement,
            "questions": {
                r.question.id: {"add": dict(r.add), "doubtful": dict(r.doubtful), "ungraded": r.ungraded}
                for r in self.reviews
            },
        }


def review(
    report: Report, questions: Sequence[Question], judgments: Mapping[str, Judgment], model: str
) -> JudgeReport:
    """Compare the grades with the expected lists. Only posts in this run's pool count: a
    post graded in an earlier run that is no longer indexed is neither suggested nor scored."""
    pools = pool(report, questions)

    def grades_for(qid: str) -> dict[str, int]:
        saved = judgments[qid].grades if qid in judgments else {}
        pooled = set(pools.get(qid, ()))
        return {p: g for p, g in saved.items() if p in pooled}

    graded = {q.id: grades_for(q.id) for q in questions}
    reviews = []
    agree = total = 0
    for q in questions:
        grades = graded[q.id]
        expected = set(q.expected)
        add = sorted(
            ((p, g) for p, g in grades.items() if g >= 2 and p not in expected), key=lambda x: (-x[1], x[0])
        )
        doubtful = [(p, grades[p]) for p in q.expected if p in grades and grades[p] <= 1]
        ungraded = [p for p in q.expected if p not in grades]
        judged = [p for p in q.expected if p in grades]
        agree += sum(1 for p in judged if grades[p] >= 2)
        total += len(judged)
        reviews.append(QuestionReview(q, len(grades), add, doubtful, ungraded))
    ndcg: dict[str, float] = {}
    for stage in (*STAGES, RETURNED):
        values = []
        for o in report.outcomes:
            if o.summaries and stage in o.rankings and o.question.id in graded:
                v = ndcg_at(o.rankings[stage], graded[o.question.id])
                if v is not None:
                    values.append(v)
        if values:
            ndcg[stage] = sum(values) / len(values)
    return JudgeReport(reviews, ndcg, agree / total if total else None, model)


def run(
    config: Config, report: Report, questions: Sequence[Question], generation: db.Generation
) -> JudgeReport:
    """Grade the pooled posts with Gemini (reusing saved grades) and review the expected lists.
    A failure partway keeps the grades made so far (they are saved per batch)."""
    import sys

    s = config.search
    client = GeminiClient(
        config.gemini_api_key,
        rpm=s.summary_rpm,
        tpm=s.summary_tpm,
        breaker_failures=s.circuit_breaker_failures,
        timeout_seconds=s.gemini_timeout_seconds,
    )
    grader = GeminiGrader(client, s.summaries.model, s.summaries.thinking_level)
    path = config.output_dir / JUDGE_FILE
    judgments = load_judgments(path)
    with db.connect(config, "reader") as conn:

        def candidates(posts: Sequence[str], hits: Sequence[str]) -> dict[str, Candidate]:
            return load_candidates(conn, generation.schema, posts, hits)

        def progress(q: Question, n: int) -> None:
            print(f"  judged {q.id:<24} {n} posts", file=sys.stderr)

        graded = judge(
            report, questions, judgments, candidates, grader, lambda j: save_judgments(path, j), progress
        )
    verdict = review(report, questions, judgments, grader.model)
    verdict.graded_now = graded
    return verdict
