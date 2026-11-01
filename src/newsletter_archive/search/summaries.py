"""One summary per post, written by a Gemini text model at index time, plus its cache.

Summaries help search find the right posts for broad questions; passages stay the evidence
(``lld.md`` §5.10). A summary is regenerated only when the post's cleaned text, title, model or
prompt version changes. The MCP server never calls a model: it only returns stored summaries.
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
MAX_OUTPUT_TOKENS = 1024  # room for a 150-word answer plus any thinking the model does
ATTEMPTS = 2  # one retry on an unusable answer
REFUSAL_OPENINGS = ("i can't", "i cannot", "i'm sorry", "i am sorry", "sorry,", "as an ai")

PROMPT = (
    "Summarise the newsletter post below in 100 to 150 words, using only its content. "
    "Name its main argument, the frameworks or models it introduces (with their names), "
    "the concrete advice it gives, and who it is for. Write plain prose in the third person "
    '("The post argues…"), with no title, no bullet points and no preamble.'
)


class SummaryError(GeminiError):
    """No usable summary for a post."""


class SummaryResponseError(GeminiResponseError, SummaryError):
    """One answer was unusable (counts toward the circuit breaker)."""


def post_text(title: str, sections: Sequence[Section]) -> str:
    """What the model reads: the title and the cleaned sections with their headings."""
    parts = [f"Title: {title}"]
    for sec in sections:
        parts.append(f"## {sec.heading}\n\n{sec.text}" if sec.heading else sec.text)
    return "\n\n".join(parts)


def summary_key(model: str, title: str, sections: Sequence[Section]) -> str:
    payload = {
        "model": model,
        "prompt_version": SUMMARY_PROMPT_VERSION,
        "title": title,
        "sections": [[s.heading, s.text] for s in sections],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def check_summary(text: str) -> str:
    """The cleaned summary, or SummaryError if it is empty, a refusal or the wrong length."""
    cleaned = " ".join(text.split())
    words = word_count(cleaned)
    if not cleaned:
        raise SummaryResponseError("the model returned an empty summary")
    if cleaned.lower().startswith(REFUSAL_OPENINGS):
        raise SummaryResponseError("the model declined to summarise the post")
    if not MIN_WORDS <= words <= MAX_WORDS:
        raise SummaryResponseError(f"summary has {words} words, expected {MIN_WORDS}-{MAX_WORDS}")
    return cleaned


def _finish_reason(response: Any) -> str:
    candidates = getattr(response, "candidates", None) or []
    reason = getattr(candidates[0], "finish_reason", None) if candidates else None
    return str(getattr(reason, "name", reason) or "")


def check_response(response: Any) -> str:
    """The usable summary text of a response; a cut-off answer is unusable."""
    if _finish_reason(response) == "MAX_TOKENS":
        raise SummaryResponseError("the answer was cut off at the output limit (thinking may have used it)")
    return check_summary(getattr(response, "text", None) or "")


@dataclass(frozen=True)
class Summary:
    text: str
    input_tokens: int
    output_tokens: int


class Summarizer(Protocol):
    model: str

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
        tokens = estimate_tokens(PROMPT) + estimate_tokens(text) + MAX_OUTPUT_TOKENS
        problem = ""
        for attempt in range(1, ATTEMPTS + 1):
            try:
                response = self.client.call(
                    partial(self._request, text=text), tokens, "summary", check_response
                )
            except SummaryResponseError as e:
                problem = str(e)
                log.info("summary attempt %s for %r unusable: %s", attempt, title[:60], problem)
                continue
            summary = check_response(response)
            usage = getattr(response, "usage_metadata", None)
            return Summary(
                summary,
                int(getattr(usage, "prompt_token_count", 0) or 0),
                int(getattr(usage, "candidates_token_count", 0) or 0)
                + int(getattr(usage, "thoughts_token_count", 0) or 0),
            )
        raise SummaryError(f"no usable summary after {ATTEMPTS} attempts: {problem}")

    def _request(self, sdk: Any, text: str) -> Any:
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
                system_instruction=PROMPT,
                temperature=0.0,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                thinking_config=thinking,
            ),
        )


class SummaryCache:
    """``public.summary_cache``, keyed by ``summary_key``. Survives index rebuilds."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def get(self, key: str) -> str | None:
        row = self.conn.execute(
            "UPDATE public.summary_cache SET last_used_at = now() "
            "WHERE summary_sha256 = %s RETURNING summary",
            (key,),
        ).fetchone()
        return str(row[0]) if row else None

    def has(self, key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM public.summary_cache WHERE summary_sha256 = %s", (key,)
        ).fetchone()
        return row is not None

    def put(self, key: str, model: str, summary: Summary) -> None:
        self.conn.execute(
            "INSERT INTO public.summary_cache "
            "(summary_sha256, model, prompt_version, summary, input_tokens, output_tokens) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (summary_sha256) DO UPDATE "
            "SET summary = EXCLUDED.summary, last_used_at = now()",
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
    """Summarises through the cache: an unchanged post is never summarised twice."""

    summarizer: Summarizer
    cache: SummaryCache
    stats: SummaryStats = field(default_factory=SummaryStats)

    def summarize(self, title: str, sections: Sequence[Section]) -> str:
        key = summary_key(self.summarizer.model, title, sections)
        self.stats.requested += 1
        cached = self.cache.get(key)
        if cached is not None:
            self.stats.cache_hits += 1
            return cached
        summary = self.summarizer.summarize(title, sections)
        self.cache.put(key, self.summarizer.model, summary)
        self.stats.generated += 1
        self.stats.input_tokens += summary.input_tokens
        self.stats.output_tokens += summary.output_tokens
        return summary.text

    def missing_tokens(self, posts: Sequence[tuple[str, Sequence[Section]]]) -> int:
        """Estimated tokens (input + output) for posts with no cached summary."""
        total = 0
        for title, sections in posts:
            if not self.cache.has(summary_key(self.summarizer.model, title, sections)):
                total += (
                    estimate_tokens(PROMPT) + estimate_tokens(post_text(title, sections)) + MAX_OUTPUT_TOKENS
                )
        return total
