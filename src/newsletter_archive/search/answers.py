"""Answer evaluation (openspec improve-search-post-selection, lld.md §5).

For each eval question the archive is searched with a fixed plan (one summaries search, one
passages search, through the same SearchService the MCP server uses), and a model writes one
answer from the ``ask`` prompt's instructions and those results. The answers are saved per
labelled run in ``<output_dir>/eval-answers/<label>.json`` and reused when nothing they depend
on has changed, so comparing two search versions only pays for what differs.

What the answer cites is worked out here, from its Markdown links: citation precision (cited
posts that the searches returned) and post coverage (relevant posts the answer drew on). An
LLM judge adds faithfulness and an overall grade (PR 3).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from ..errors import ArchiveError
from ..store import write_json_atomic
from .evaluate import Question
from .service import SearchRequest, SearchResponse, SearchService

log = logging.getLogger("newsletter_archive.search.answers")

ANSWER_PROMPT_VERSION = 1  # bump when the prompt below changes: every answer is then made again
ANSWERS_DIR = "eval-answers"
STORE_VERSION = 1
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
LINK_RE = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")

ANSWER_INSTRUCTION = """You are answering from a personal newsletter archive. The archive has
already been searched for you: the results below are the only material you may use, and you
cannot search again. Follow the instructions after the question exactly, except where they tell
you to search: the searching is done. Link each post you draw on by its title and URL, as
Markdown links, using the URLs shown in the results."""


class AnswerError(ArchiveError):
    """The answer evaluation can't go on (bad label, unreadable answers file)."""


# -- the plan and the prompt ---------------------------------------------------------------------


@dataclass(frozen=True)
class Retrieved:
    """The results the fixed plan gave one question."""

    responses: tuple[SearchResponse, ...]

    @property
    def result_ids(self) -> list[str]:
        return [r.id for resp in self.responses for r in resp.results]

    @property
    def post_of(self) -> dict[str, str]:
        return {r.id: r.post.id for resp in self.responses for r in resp.results}

    @property
    def url_to_post(self) -> dict[str, str]:
        return {normalize_url(r.post.url): r.post.id for resp in self.responses for r in resp.results}


def retrieve(service: SearchService, question: str, results_per_search: int) -> Retrieved:
    """The fixed plan: a summaries search to find the posts, then a passages search for evidence,
    the way the ``ask`` instructions start a broad question."""
    return Retrieved(
        tuple(
            service.search(SearchRequest.create(question, results_per_search, kind=kind))
            for kind in ("summaries", "passages")
        )
    )


def render_results(retrieved: Retrieved) -> str:
    blocks = []
    for resp in retrieved.responses:
        for r in resp.results:
            where = f"{r.post.title} — {r.heading}" if r.heading else r.post.title
            meta = " · ".join(x for x in (r.post.source_name or "", r.post.published_date or "") if x)
            blocks.append(f"[{r.id}] {r.kind} · {where} · {meta} · {r.post.url}\n{r.text}")
    return "\n\n".join(blocks) if blocks else "(no results)"


def answer_prompt(question: str, retrieved: Retrieved) -> str:
    """The ``ask`` prompt's own text (question and instructions), then the search results."""
    from ..mcp_server import ask_message  # the MCP module pulls in the SDK; only load it here

    return f"{ask_message(question)}\n\nSearch results:\n\n{render_results(retrieved)}"


# -- citations (computed, not judged) -------------------------------------------------------------


def normalize_url(url: str) -> str:
    """Scheme and host lowercased; query, fragment and trailing slash dropped."""
    parts = urlsplit(url.strip())
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


@dataclass(frozen=True)
class Citations:
    posts: tuple[str, ...]  # cited posts that the searches returned, in order of first citation
    unknown: tuple[str, ...]  # cited URLs that no search result has

    @property
    def total(self) -> int:
        return len(self.posts) + len(self.unknown)


def citations(answer: str, url_to_post: Mapping[str, str]) -> Citations:
    """The posts an answer links to. Each distinct post or unknown URL counts once."""
    posts: dict[str, None] = {}
    unknown: dict[str, None] = {}
    for url in LINK_RE.findall(answer):
        key = normalize_url(url)
        if key in url_to_post:
            posts.setdefault(url_to_post[key], None)
        else:
            unknown.setdefault(key, None)
    return Citations(tuple(posts), tuple(unknown))


def citation_precision(c: Citations) -> float | None:
    """Cited posts that the searches returned ÷ everything cited; None when nothing is cited."""
    return len(c.posts) / c.total if c.total else None


def post_coverage(c: Citations, relevant: Iterable[str]) -> float | None:
    """Relevant posts the answer cites ÷ relevant posts; None when the question has none."""
    wanted = set(relevant)
    return len(wanted & set(c.posts)) / len(wanted) if wanted else None


def relevant_posts(question: Question, judge_grades: Mapping[str, int]) -> set[str]:
    """The question's expected posts plus the posts the retrieval judge graded 2 or 3."""
    return set(question.expected) | {p for p, g in judge_grades.items() if g >= 2}


# -- records and the answers file -----------------------------------------------------------------


def _sha(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def answer_key(question: str, model: str, thinking_level: str, result_ids: Iterable[str]) -> str:
    return _sha(question, ANSWER_PROMPT_VERSION, model, thinking_level, sorted(result_ids))


@dataclass
class AnswerRecord:
    question_id: str
    question: str
    answer_key: str
    model: str
    prompt_version: int
    result_ids: list[str]
    result_posts: dict[str, str]  # result id -> post id
    url_to_post: dict[str, str]
    answer: str | None = None
    error: str | None = None
    grade: dict[str, Any] | None = None  # filled by the answer judge (PR 3)

    @property
    def cited(self) -> Citations:
        return citations(self.answer or "", self.url_to_post)


@dataclass
class AnswerRun:
    label: str
    records: dict[str, AnswerRecord] = field(default_factory=dict)  # question id -> record


def check_label(label: str) -> str:
    if not LABEL_RE.match(label):
        raise AnswerError(
            f"label {label!r}: use lowercase letters, digits, '.', '_' and '-' (for example baseline)"
        )
    return label


def answers_dir(output_dir: Path) -> Path:
    return output_dir / ANSWERS_DIR


def run_path(output_dir: Path, label: str) -> Path:
    return answers_dir(output_dir) / f"{check_label(label)}.json"


def load_run(output_dir: Path, label: str) -> AnswerRun:
    """A saved run, or an empty one."""
    path = run_path(output_dir, label)
    if not path.exists():
        return AnswerRun(label)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != STORE_VERSION:
            raise ValueError(f"unknown schema_version {data.get('schema_version')!r}")
        records = {r["question_id"]: AnswerRecord(**r) for r in data["records"]}
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise AnswerError(f"{path} can't be read ({e}); move it aside and run again") from None
    return AnswerRun(label, records)


def save_run(output_dir: Path, run: AnswerRun) -> None:
    write_json_atomic(
        run_path(output_dir, run.label),
        {
            "schema_version": STORE_VERSION,
            "label": run.label,
            "records": [asdict(r) for r in run.records.values()],
        },
    )


def saved_answers(output_dir: Path) -> dict[str, AnswerRecord]:
    """Every saved answer in every run, by answer key, so another label can reuse it."""
    out: dict[str, AnswerRecord] = {}
    folder = answers_dir(output_dir)
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.json")):
        for r in load_run(output_dir, path.stem).records.values():
            if r.answer is not None:
                out.setdefault(r.answer_key, r)
    return out


# -- answering ------------------------------------------------------------------------------------


class Answerer(Protocol):
    model: str
    thinking_level: str

    def answer(self, prompt: str) -> str: ...


def answer_questions(
    service: SearchService,
    questions: Sequence[Question],
    answerer: Answerer,
    output_dir: Path,
    label: str,
    results_per_search: int,
    on_question: Callable[[AnswerRecord, bool], None] | None = None,
) -> AnswerRun:
    """Answer every question (reusing a saved answer when its key matches) and save the run
    after each question, so a stopped run keeps what it made. ``on_question(record, reused)``."""
    run = load_run(output_dir, check_label(label))
    reusable = saved_answers(output_dir)
    for q in questions:
        try:
            retrieved = retrieve(service, q.question, results_per_search)
        except ArchiveError as e:
            record = AnswerRecord(q.id, q.question, "", answerer.model, ANSWER_PROMPT_VERSION, [], {}, {})
            record.error = f"search: {type(e).__name__}"
            run.records[q.id] = record
            save_run(output_dir, run)
            log.info("answer %s: search failed (%s)", q.id, type(e).__name__)
            if on_question:
                on_question(record, False)
            continue
        key = answer_key(q.question, answerer.model, answerer.thinking_level, retrieved.result_ids)
        old = reusable.get(key)
        if old is not None:
            record = AnswerRecord(**{**asdict(old), "question_id": q.id})
            reused = True
        else:
            record = AnswerRecord(
                q.id,
                q.question,
                key,
                answerer.model,
                ANSWER_PROMPT_VERSION,
                retrieved.result_ids,
                retrieved.post_of,
                retrieved.url_to_post,
            )
            try:
                record.answer = answerer.answer(answer_prompt(q.question, retrieved))
            except ArchiveError as e:
                record.error = f"answer: {type(e).__name__}"
            reused = False
            if record.answer is not None:
                reusable[key] = record
        run.records[q.id] = record
        save_run(output_dir, run)
        log.info("answer %s: %s", q.id, "reused" if reused else ("ok" if record.answer else record.error))
        if on_question:
            on_question(record, reused)
    return run


@dataclass
class GeminiAnswerer:
    """Writes one answer with Gemini (the client handles rate limits, retries, the breaker)."""

    client: Any  # GeminiClient
    model: str
    thinking_level: str = "low"
    max_output_tokens: int = 2048

    def answer(self, prompt: str) -> str:
        from .summaries import check_response
        from .text import estimate_tokens

        out: list[str] = []

        def keep(response: Any) -> None:
            out.append(check_response(response))

        tokens = estimate_tokens(ANSWER_INSTRUCTION) + estimate_tokens(prompt) + self.max_output_tokens
        self.client.call(self._request(prompt), tokens, "answer", keep)
        return out[0]

    def _request(self, prompt: str) -> Callable[[Any], Any]:
        from .summaries import thinking_config

        def call(sdk: Any) -> Any:
            from google.genai import types

            return sdk.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=ANSWER_INSTRUCTION,
                    temperature=0.0,
                    max_output_tokens=self.max_output_tokens,
                    thinking_config=thinking_config(self.thinking_level),
                ),
            )

        return call


__all__ = [
    "ANSWER_PROMPT_VERSION",
    "AnswerError",
    "AnswerRecord",
    "AnswerRun",
    "Answerer",
    "Citations",
    "GeminiAnswerer",
    "Retrieved",
    "answer_key",
    "answer_prompt",
    "answer_questions",
    "citation_precision",
    "citations",
    "load_run",
    "normalize_url",
    "post_coverage",
    "relevant_posts",
    "retrieve",
    "save_run",
    "saved_answers",
]
