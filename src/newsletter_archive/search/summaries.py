"""One summary per post, written by a Gemini text model at index time, plus its cache.

Summaries help search find the right posts for broad questions; passages stay the evidence
(``lld.md`` §5.10). A summary is regenerated only when the post's cleaned text, title, model,
thinking level or prompt version changes. The MCP server never calls a model: it only returns
stored summaries.

Two kinds of bad answer are handled differently:
- a broken response (empty, or stopped for any reason other than finishing: cut off, safety,
  recitation…) points at the service or the settings, so it counts toward the circuit breaker;
- a complete answer of the wrong length, or a refusal, is about that one post, so it is retried
  once with a firmer instruction and never stops the run.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Protocol

import psycopg

from .fingerprint import canonical_json
from .gemini import GeminiClient, GeminiError, GeminiResponseError
from .models import Section
from .text import estimate_tokens, word_count
from .versions import SUMMARY_PROMPT_VERSION

log = logging.getLogger("newsletter_archive.search.summaries")

MIN_WORDS = 60
MAX_WORDS = 200
SHORT_POST_MIN_WORDS = 15  # a short post gets a summary in proportion to its length
MAX_OUTPUT_TOKENS = 1024  # room for a 150-word answer plus any thinking the model does
ATTEMPTS = 2  # one retry on an unusable answer
RETRY_TEMPERATURE = 0.3  # the retry must differ from the first request to help
REFUSAL_OPENINGS = ("i can't", "i cannot", "i'm sorry", "i am sorry", "sorry,", "as an ai")
FINISHED = ("", "STOP")  # "" when the SDK reports no reason (and in fakes)

PROMPT = (
    "Summarise the newsletter post below in 100 to 150 words (fewer for a very short post), "
    "using only its content. Name its main argument, the frameworks or models it introduces "
    "(with their names), the concrete advice it gives, and who it is for. Write plain prose in "
    'the third person ("The post argues…"), with no title, no bullet points and no preamble.'
)


class SummaryError(GeminiError):
    """No usable summary for a post."""


class SummaryResponseError(GeminiResponseError, SummaryError):
    """A broken response (counts toward the circuit breaker)."""


class SummaryContentError(SummaryError):
    """A complete answer that doesn't fit (length, refusal). About this post only."""


def post_text(title: str, sections: Sequence[Section]) -> str:
    """What the model reads: the title and the cleaned sections with their headings."""
    parts = [f"Title: {title}"]
    for sec in sections:
        parts.append(f"## {sec.heading}\n\n{sec.text}" if sec.heading else sec.text)
    return "\n\n".join(parts)


def summary_key(model: str, thinking_level: str, title: str, sections: Sequence[Section]) -> str:
    payload = {
        "model": model,
        "thinking_level": thinking_level,
        "prompt_version": SUMMARY_PROMPT_VERSION,
        "title": title,
        "sections": [[s.heading, s.text] for s in sections],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def min_words_for(post_words: int) -> int:
    """60 words, or less for a post too short to summarise at that length."""
    return max(SHORT_POST_MIN_WORDS, min(MIN_WORDS, post_words // 2))


def check_summary(text: str, min_words: int = MIN_WORDS) -> str:
    """The cleaned summary, or SummaryContentError for a refusal or the wrong length."""
    cleaned = " ".join(text.split())
    if cleaned.lower().startswith(REFUSAL_OPENINGS):
        raise SummaryContentError("the model declined to summarise the post")
    words = word_count(cleaned)
    if not min_words <= words <= MAX_WORDS:
        raise SummaryContentError(f"summary has {words} words, expected {min_words}-{MAX_WORDS}")
    return cleaned


def _finish_reason(response: Any) -> str:
    candidates = getattr(response, "candidates", None) or []
    reason = getattr(candidates[0], "finish_reason", None) if candidates else None
    return str(getattr(reason, "name", reason) or "")


def check_response(response: Any) -> str:
    """The raw answer text, or SummaryResponseError if the response is broken."""
    reason = _finish_reason(response)
    if reason not in FINISHED:
        raise SummaryResponseError(f"the answer stopped early ({reason})")
    text = getattr(response, "text", None) or ""
    if not text.strip():
        raise SummaryResponseError("the model returned an empty answer")
    return str(text)


@dataclass(frozen=True)
class Summary:
    text: str
    input_tokens: int
    output_tokens: int


class Summarizer(Protocol):
    model: str
    thinking_level: str

    def summarize(self, title: str, sections: Sequence[Section]) -> Summary: ...


@dataclass
class GeminiSummarizer:
    """``thinking_level`` ("low" by default) keeps Gemini 3 models from spending the output
    budget on thinking (verified: at the default level thinking used 984 of 1,024 tokens and
    cut the summary to 32 words). An empty value sends no thinking setting."""

    client: GeminiClient
    model: str
    thinking_level: str = "low"

    def summarize(self, title: str, sections: Sequence[Section]) -> Summary:
        """Raises SummaryError after ``ATTEMPTS`` unusable answers; Gemini errors propagate."""
        text = post_text(title, sections)
        min_words = min_words_for(sum(word_count(s.text) for s in sections))
        tokens = estimate_tokens(PROMPT) + estimate_tokens(text) + MAX_OUTPUT_TOKENS
        problem = ""
        for attempt in range(1, ATTEMPTS + 1):
            answers: list[str] = []

            def keep(response: Any, answers: list[str] = answers) -> None:
                answers.append(check_response(response))

            note = (
                f" Your previous answer was unusable ({problem}); follow the length exactly."
                if problem
                else ""
            )
            request = partial(
                self._request,
                text=text,
                instruction=PROMPT + note,
                temperature=0.0 if attempt == 1 else RETRY_TEMPERATURE,
            )
            try:
                response = self.client.call(request, tokens, "summary", keep)
                summary = check_summary(answers[0], min_words)
            except (SummaryResponseError, SummaryContentError) as e:
                problem = str(e)
                log.info("summary attempt %s for %r unusable: %s", attempt, title[:60], problem)
                continue
            usage = getattr(response, "usage_metadata", None)
            return Summary(
                summary,
                int(getattr(usage, "prompt_token_count", 0) or 0),
                int(getattr(usage, "candidates_token_count", 0) or 0)
                + int(getattr(usage, "thoughts_token_count", 0) or 0),
            )
        raise SummaryError(f"no usable summary after {ATTEMPTS} attempts: {problem}")

    def _request(self, sdk: Any, text: str, instruction: str, temperature: float) -> Any:
        from google.genai import types

        thinking = (
            types.ThinkingConfig(thinking_level=types.ThinkingLevel(self.thinking_level.upper()))
            if self.thinking_level
            else None
        )
        return sdk.models.generate_content(
            model=self.model,
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=instruction,
                temperature=temperature,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                thinking_config=thinking,
            ),
        )


class SummaryCache:
    """``public.summary_cache``, keyed by ``summary_key``. Survives index rebuilds.
    Reads are read-only; ``touch`` marks rows used in one statement."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def get(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT summary FROM public.summary_cache WHERE summary_sha256 = %s", (key,)
        ).fetchone()
        return str(row[0]) if row else None

    def has_many(self, keys: Sequence[str]) -> set[str]:
        if not keys:
            return set()
        rows = self.conn.execute(
            "SELECT summary_sha256 FROM public.summary_cache WHERE summary_sha256 = ANY(%s)", (list(keys),)
        ).fetchall()
        return {r[0] for r in rows}

    def missing(
        self, model: str, thinking_level: str, posts: Sequence[tuple[str, Sequence[Section]]]
    ) -> tuple[int, int]:
        """(posts with no cached summary, estimated tokens to write them: input + output).
        One read-only query; identical posts count once."""
        by_key = {summary_key(model, thinking_level, t, s): (t, s) for t, s in posts}
        cached = self.has_many(sorted(by_key))
        todo = [post_text(t, s) for key, (t, s) in by_key.items() if key not in cached]
        return len(todo), sum(estimate_tokens(PROMPT) + estimate_tokens(t) + MAX_OUTPUT_TOKENS for t in todo)

    def touch(self, keys: Sequence[str]) -> None:
        if keys:
            self.conn.execute(
                "UPDATE public.summary_cache SET last_used_at = now() WHERE summary_sha256 = ANY(%s)",
                (list(keys),),
            )

    def put(self, key: str, model: str, summary: Summary) -> None:
        self.conn.execute(
            "INSERT INTO public.summary_cache "
            "(summary_sha256, model, prompt_version, summary, input_tokens, output_tokens) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (summary_sha256) DO UPDATE SET "
            "model = EXCLUDED.model, prompt_version = EXCLUDED.prompt_version, "
            "summary = EXCLUDED.summary, input_tokens = EXCLUDED.input_tokens, "
            "output_tokens = EXCLUDED.output_tokens, last_used_at = now()",
            (key, model, SUMMARY_PROMPT_VERSION, summary.text, summary.input_tokens, summary.output_tokens),
        )


@dataclass
class SummaryStats:
    requested: int = 0
    cache_hits: int = 0
    generated: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class CachedSummarizer:
    """Summarises through the cache: an unchanged post is never summarised twice. Call
    ``flush`` at the end of a run to mark the cache hits as used (one statement)."""

    summarizer: Summarizer
    cache: SummaryCache
    stats: SummaryStats = field(default_factory=SummaryStats)
    _hits: list[str] = field(default_factory=list)

    def key(self, title: str, sections: Sequence[Section]) -> str:
        return summary_key(self.summarizer.model, self.summarizer.thinking_level, title, sections)

    def summarize(self, title: str, sections: Sequence[Section]) -> tuple[str, str]:
        """(summary text, cache key)."""
        key = self.key(title, sections)
        self.stats.requested += 1
        cached = self.cache.get(key)
        if cached is not None:
            self.stats.cache_hits += 1
            self._hits.append(key)
            return cached, key
        summary = self.summarizer.summarize(title, sections)
        self.cache.put(key, self.summarizer.model, summary)
        self.stats.generated += 1
        self.stats.input_tokens += summary.input_tokens
        self.stats.output_tokens += summary.output_tokens
        return summary.text, key

    def flush(self) -> None:
        self.cache.touch(sorted(set(self._hits)))
        self._hits.clear()

    def missing_tokens(self, posts: Sequence[tuple[str, Sequence[Section]]]) -> int:
        """Estimated tokens (input + output) for posts with no cached summary. One query."""
        return self.cache.missing(self.summarizer.model, self.summarizer.thinking_level, posts)[1]
