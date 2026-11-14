"""Answer evaluation (openspec improve-search-post-selection, lld.md §5).

For each eval question the archive is searched with a fixed plan (one summaries search, one
passages search, through the same SearchService the MCP server uses), and a model writes one
answer from the ``ask`` prompt's instructions and those results. The answers are saved per
labelled run in ``<output_dir>/eval-answers/<label>.json`` and reused when nothing they depend
on has changed, so comparing two search versions only pays for what differs.

What the answer cites is worked out here, from its Markdown links: citation precision (cited
posts that the searches returned) and post coverage (relevant posts the answer drew on). An
LLM judge adds faithfulness and an overall grade.
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
ANSWER_JUDGE_VERSION = 1  # bump when JUDGE_PROMPT changes: every grade is then made again
JUDGE_ATTEMPTS = 2
ANSWER_OUTPUT_TOKENS = 4096  # a teaching post plus thinking
JUDGE_OUTPUT_TOKENS = 8192  # the claims list plus thinking
ANSWERS_DIR = "eval-answers"
STORE_VERSION = 1
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
REF_DEF_RE = re.compile(r"^[ \t]*\[[^\]\n]+\]:[ \t]*<?(https?://[^\s>]+)>?", re.M)

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
    """Scheme and host lowercased, default port dropped; query, fragment and trailing slash
    dropped (post URLs in the index never carry a query: discovery normalizes them)."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if parts.port and (scheme, parts.port) not in (("http", 80), ("https", 443)):
        host += f":{parts.port}"
    return f"{scheme}://{host}{parts.path.rstrip('/')}"


def link_urls(text: str) -> list[str]:
    """Every link target in Markdown text, in order: inline links ``[t](url)``, also with
    ``<url>``, a title, spaces or balanced parentheses in the URL, and reference definitions
    ``[label]: url``. Bare URLs are not links."""
    urls: list[str] = []
    i = 0
    while (j := text.find("](", i)) != -1:
        k = j + 2
        while k < len(text) and text[k] in " \t":
            k += 1
        if text.startswith("<", k):
            end = text.find(">", k)
            url = text[k + 1 : end] if end != -1 else ""
        else:
            depth, start = 0, k
            while k < len(text) and not text[k].isspace():
                if text[k] == "(":
                    depth += 1
                elif text[k] == ")":
                    if depth == 0:
                        break
                    depth -= 1
                k += 1
            url = text[start:k]
        if url.startswith(("http://", "https://")):
            urls.append(url)
        i = j + 2
    urls += REF_DEF_RE.findall(text)
    return urls


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
    for url in link_urls(answer):
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
    grade: dict[str, Any] | None = None  # filled by the answer judge
    results_text: str = ""  # the results as the model saw them, so the judge sees the same

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
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
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
        try:
            run = load_run(output_dir, path.stem)
        except AnswerError as e:  # another label's broken file, or a stray file: skip it
            log.warning("not reusing answers from %s: %s", path.name, e)
            continue
        for r in run.records.values():
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
        except Exception as e:  # one question's failure never stops the run; the type is recorded
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
            # The answer is reused; its grade is not (it belongs to the other run and may be
            # out of date), so the judge decides that separately.
            record = AnswerRecord(**{**asdict(old), "question_id": q.id, "grade": None})
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
                results_text=render_results(retrieved),
            )
            try:
                record.answer = answerer.answer(answer_prompt(q.question, retrieved))
            except Exception as e:  # a model error, or an unusable question for the ask text
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
    max_output_tokens: int = ANSWER_OUTPUT_TOKENS

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


# -- the answer judge -----------------------------------------------------------------------------

JUDGE_PROMPT = """You check an answer written from a personal archive of newsletter posts against
the search results it was written from. You are given the question, the results (each starts
with [result id], its post's title and URL) and the answer.

1. List every claim the answer makes about what the posts say or advise, one short sentence
   each. For each claim give the post_id of the result it cites (the post the answer links for
   that claim, "" when it cites none) and supported: true only when that post's results shown
   here actually say it.
2. Grade the whole answer for the question: 3 = answers it well from the archive, 2 = useful
   but missing something the results had, 1 = weak or mostly general, 0 = wrong or unusable.

Reply with JSON only: {"claims": [{"claim": "...", "post_id": "...", "supported": true}],
"overall": 0}."""


class AnswerJudge(Protocol):
    model: str

    def judge(self, question: str, answer: str, results_text: str, post_ids: Sequence[str]) -> dict[str, Any]:
        """{"claims": [{"claim", "post_id", "supported"}], "overall": 0-3}."""
        ...


def parse_judgment(text: str, post_ids: Sequence[str]) -> dict[str, Any]:
    """The judge's JSON, checked; GeminiResponseError when it can't be used."""
    from .gemini import GeminiResponseError

    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(raw)
        claims = data["claims"]
        overall = data["overall"]
        if not isinstance(claims, list) or isinstance(overall, bool) or overall not in (0, 1, 2, 3):
            raise ValueError("bad claims or overall")
        allowed = set(post_ids) | {""}
        out = []
        for c in claims:
            if not isinstance(c.get("supported"), bool) or not isinstance(c.get("claim"), str):
                raise ValueError("bad claim")
            pid = c.get("post_id") or ""
            out.append(
                {"claim": c["claim"], "post_id": pid if pid in allowed else "", "supported": c["supported"]}
            )
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise GeminiResponseError(f"unusable judgment ({e})") from None
    return {"claims": out, "overall": int(overall)}


def grade_key(answer: str, judge_model: str) -> str:
    return _sha(answer, ANSWER_JUDGE_VERSION, judge_model)


def grade_record(
    record: AnswerRecord, judgment: dict[str, Any], relevant: Iterable[str], judge_model: str
) -> dict[str, Any]:
    """The grade saved with a record: the judge's claims and grade plus the computed scores."""
    claims = judgment["claims"]
    cited = record.cited
    return {
        "grade_key": grade_key(record.answer or "", judge_model),
        "judge_model": judge_model,
        "judge_version": ANSWER_JUDGE_VERSION,
        "claims": claims,
        "faithfulness": sum(1 for c in claims if c["supported"] and c["post_id"]) / len(claims)
        if claims
        else None,
        "citation_precision": citation_precision(cited),
        "post_coverage": post_coverage(cited, relevant),
        "overall": judgment["overall"],
    }


def grade_answers(
    run: AnswerRun,
    judge: AnswerJudge,
    relevant_of: Callable[[str], set[str]],
    output_dir: Path,
    on_question: Callable[[AnswerRecord, bool], None] | None = None,
) -> AnswerRun:
    """Grade every answered record whose saved grade is missing or out of date (a different
    answer, judge model or judge version); save after each. ``on_question(record, reused)``."""
    reusable = {
        r.grade["grade_key"]: r.grade
        for r in saved_answers(output_dir).values()
        if r.grade and r.grade.get("judge_version") == ANSWER_JUDGE_VERSION
    }
    for record in run.records.values():
        if record.answer is None:
            continue
        key = grade_key(record.answer, judge.model)
        relevant = relevant_of(record.question_id)
        if record.grade and record.grade.get("grade_key") == key:
            reused = True
        elif key in reusable:
            record.grade = {**reusable[key], "post_coverage": post_coverage(record.cited, relevant)}
            reused = True
        else:
            try:
                judgment = judge.judge(
                    record.question,
                    record.answer,
                    record.results_text,
                    sorted(set(record.result_posts.values())),
                )
            except ArchiveError as e:
                record.error = f"grade: {type(e).__name__}"
                save_run(output_dir, run)
                log.info("grade %s: failed (%s)", record.question_id, type(e).__name__)
                if on_question:
                    on_question(record, False)
                continue
            record.grade = grade_record(record, judgment, relevant, judge.model)
            reusable[key] = record.grade
            reused = False
        save_run(output_dir, run)
        log.info("grade %s: %s", record.question_id, "reused" if reused else "ok")
        if on_question:
            on_question(record, reused)
    return run


@dataclass
class GeminiAnswerJudge:
    client: Any  # GeminiClient
    model: str
    thinking_level: str = "low"

    def judge(self, question: str, answer: str, results_text: str, post_ids: Sequence[str]) -> dict[str, Any]:
        """One retry on an unusable judgment (with a note and a little temperature), then the error."""
        from .gemini import GeminiResponseError
        from .summaries import RETRY_TEMPERATURE, check_response
        from .text import estimate_tokens

        text = f"Question: {question}\n\nSearch results:\n\n{results_text}\n\nAnswer:\n{answer}"
        tokens = estimate_tokens(JUDGE_PROMPT) + estimate_tokens(text) + JUDGE_OUTPUT_TOKENS
        problem = ""
        for _ in range(JUDGE_ATTEMPTS):
            out: list[dict[str, Any]] = []

            def keep(response: Any, out: list[dict[str, Any]] = out) -> None:
                out.append(parse_judgment(check_response(response), post_ids))

            note = (
                f" Your previous reply was unusable ({problem}): reply with the JSON only." if problem else ""
            )
            request = self._request(text, JUDGE_PROMPT + note, RETRY_TEMPERATURE if problem else 0.0)
            try:
                self.client.call(request, tokens, "answer-judge", keep)
                return out[0]
            except GeminiResponseError as e:
                problem = str(e)
        raise AnswerError(f"no usable judgment after {JUDGE_ATTEMPTS} attempts: {problem}")

    def _request(self, text: str, instruction: str, temperature: float) -> Callable[[Any], Any]:
        from .summaries import thinking_config

        def call(sdk: Any) -> Any:
            from google.genai import types

            return sdk.models.generate_content(
                model=self.model,
                contents=text,
                config=types.GenerateContentConfig(
                    system_instruction=instruction,
                    temperature=temperature,
                    max_output_tokens=JUDGE_OUTPUT_TOKENS,
                    thinking_config=thinking_config(self.thinking_level),
                    response_mime_type="application/json",
                ),
            )

        return call


# -- cost -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Estimate:
    questions: int
    tokens: int
    usd: float

    def line(self) -> str:
        return (
            f"Estimate: {self.questions} answers and {self.questions} grades, "
            f"at most ~{self.tokens:,} tokens, ~${self.usd:.2f} "
            "(saved answers and grades are reused and cost nothing)"
        )


def estimate(
    questions: Sequence[Question], results_per_search: int, avg_row_tokens: int, usd_per_million: float
) -> Estimate:
    """An upper bound made before any search or model call (a search embeds the query): every
    question answered and graded, with two full searches' results each."""
    from .text import estimate_tokens

    instructions = estimate_tokens(ANSWER_INSTRUCTION) + 1500  # the ask text is about this long
    results = 2 * results_per_search * avg_row_tokens
    total = 0
    for q in questions:
        answer = instructions + estimate_tokens(q.question) + results + ANSWER_OUTPUT_TOKENS
        grade = estimate_tokens(JUDGE_PROMPT) + results + ANSWER_OUTPUT_TOKENS + JUDGE_OUTPUT_TOKENS
        total += answer + grade
    return Estimate(len(questions), total, total / 1_000_000 * usd_per_million)


# -- the report -----------------------------------------------------------------------------------

METRICS = ("faithfulness", "citation_precision", "post_coverage", "overall")


def _mean(values: Iterable[float | None]) -> float | None:
    got = [v for v in values if v is not None]
    return sum(got) / len(got) if got else None


def scores(
    run: AnswerRun, questions: Sequence[Question], qtype: str | None = None
) -> dict[str, float | None]:
    ids = {q.id for q in questions if qtype is None or q.type == qtype}
    graded = [r.grade for qid, r in run.records.items() if qid in ids and r.grade]
    return {m: _mean(g.get(m) for g in graded) for m in METRICS}


def _cell(v: float | None, width: int = 11) -> str:
    return f"{v:>{width}.2f}" if v is not None else f"{'n/a':>{width}}"


def report_lines(run: AnswerRun, questions: Sequence[Question], model: str) -> list[str]:
    answered = sum(1 for q in questions if (r := run.records.get(q.id)) and r.answer)
    graded = sum(1 for q in questions if (r := run.records.get(q.id)) and r.grade)
    out = [
        f"Answer evaluation: label {run.label}, {len(questions)} questions "
        f"({answered} answered, {graded} graded), {model}",
        "",
        f"{'':<10} {'faithful':>11} {'citations':>11} {'coverage':>11} {'overall':>11}",
    ]
    from .evaluate import TYPES

    for qtype in (None, *TYPES):
        if qtype and not any(q.type == qtype for q in questions):
            continue
        s = scores(run, questions, qtype)
        out.append(f"{qtype or 'all':<10} " + " ".join(_cell(s[m]) for m in METRICS))
    failed = [f"{q.id} ({r.error})" for q in questions if (r := run.records.get(q.id)) and r.error]
    missing = [q.id for q in questions if q.id not in run.records]
    if failed:
        out.append("failed: " + ", ".join(failed))
    if missing:
        out.append("not run: " + ", ".join(missing))
    return out


def compare_lines(a: AnswerRun, b: AnswerRun, questions: Sequence[Question]) -> list[str]:
    sa, sb = scores(a, questions), scores(b, questions)
    out = [f"{'':<20} {a.label:>14} {b.label:>14} {'change':>9}"]
    for m in METRICS:
        va, vb = sa[m], sb[m]
        diff = f"{vb - va:>+9.2f}" if va is not None and vb is not None else f"{'n/a':>9}"
        out.append(f"{m:<20} {_cell(va, 14)} {_cell(vb, 14)} {diff}")
    return out


# -- running it ----------------------------------------------------------------------------------


def average_row_tokens(conn: Any, schema: str) -> int:
    """The mean size of a search result in the live index, for the estimate."""
    from psycopg import sql

    from .text import CHARS_PER_TOKEN

    row = conn.execute(
        sql.SQL("SELECT coalesce(avg(length(title) + length(body)), 0) FROM {}.passages").format(
            sql.Identifier(schema)
        )
    ).fetchone()
    return max(1, int(float(row[0]) / CHARS_PER_TOKEN) + 30)  # plus the result's header line


def run_answers(
    config: Any,
    questions: Sequence[Question],
    label: str,
    *,
    dry_run: bool,
    yes: bool,
    service_factory: Callable[[], SearchService],
    answerer_factory: Callable[[], Answerer],
    judge_factory: Callable[[], AnswerJudge],
    judge_grades: Mapping[str, Mapping[str, int]],
    print_line: Callable[[str], None] = print,
) -> int:
    """The `eval_search.py --answers` run: check the key, estimate, then answer, grade and report.
    Returns the exit code (0 ok, 1 stopped before spending or a run error)."""
    from . import db

    s = config.search
    check_label(label)
    if not config.gemini_api_key:
        print_line("Error: GEMINI_API_KEY is not set; add it to .env")
        return 1
    with db.connect(config, "reader") as conn:
        gen = db.active_generation(conn)
        if gen is None:
            print_line("Error: there is no search index yet; run `archive index` first")
            return 1
        avg = average_row_tokens(conn, gen.schema)
    est = estimate(questions, s.answers.results_per_search, avg, s.summary_usd_per_million_tokens)
    print_line(est.line())
    if dry_run:
        print_line("Dry run: nothing was searched, answered or graded")
        return 0
    if est.tokens > s.answers.max_tokens_per_run and not yes:
        print_line(
            f"Over the per-run limit (search.answers.max_tokens_per_run = {s.answers.max_tokens_per_run:,}); "
            "re-run with --yes to go ahead"
        )
        return 1
    by_id = {q.id: q for q in questions}

    def relevant_of(qid: str) -> set[str]:
        return relevant_posts(by_id[qid], judge_grades.get(qid, {}))

    answerer = answerer_factory()
    run = answer_questions(
        service_factory(), questions, answerer, config.output_dir, label, s.answers.results_per_search
    )
    run = grade_answers(run, judge_factory(), relevant_of, config.output_dir)
    for line in report_lines(run, questions, answerer.model):
        print_line(line)
    return 0


def gemini_factories(config: Any) -> tuple[Callable[[], Answerer], Callable[[], AnswerJudge]]:
    """The real answer model and judge, sharing one rate-limited client."""
    from .gemini import GeminiClient

    s = config.search
    shared: list[GeminiClient] = []

    def client() -> GeminiClient:
        if not shared:
            shared.append(
                GeminiClient(
                    config.gemini_api_key,
                    rpm=s.summary_rpm,
                    tpm=s.summary_tpm,
                    breaker_failures=s.circuit_breaker_failures,
                    timeout_seconds=s.gemini_timeout_seconds,
                )
            )
        return shared[0]

    a = s.answers
    return (
        lambda: GeminiAnswerer(client(), a.model, a.thinking_level),
        lambda: GeminiAnswerJudge(client(), a.judge_model, a.thinking_level),
    )


__all__ = [
    "ANSWER_JUDGE_VERSION",
    "ANSWER_PROMPT_VERSION",
    "AnswerError",
    "AnswerJudge",
    "AnswerRecord",
    "AnswerRun",
    "Answerer",
    "Citations",
    "Estimate",
    "GeminiAnswerJudge",
    "GeminiAnswerer",
    "Retrieved",
    "answer_key",
    "answer_prompt",
    "answer_questions",
    "citation_precision",
    "citations",
    "compare_lines",
    "estimate",
    "grade_answers",
    "load_run",
    "normalize_url",
    "parse_judgment",
    "post_coverage",
    "relevant_posts",
    "report_lines",
    "retrieve",
    "run_answers",
    "save_run",
    "saved_answers",
]
