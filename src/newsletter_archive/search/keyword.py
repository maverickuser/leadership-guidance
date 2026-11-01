"""BM25 keyword search over passages and summaries (ParadeDB pg_search).

Any query text is safe: words are matched with ``|||`` (match any, run through the same English
stemmer as the index), so punctuation and words like AND / OR / NOT are just text; text in
double quotes is matched as a phrase with ``###``. Queries are accent-folded like the indexed
``search_*`` columns. Heading and title matches outrank body matches (``search.bm25_boosts``).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import psycopg
from psycopg import sql

from .text import fold, squash

# fmt: off
STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "do", "does", "for", "from", "had",
    "has", "have", "how", "i", "if", "in", "into", "is", "it", "its", "me", "my", "no", "not", "of",
    "on", "or", "our", "should", "so", "than", "that", "the", "their", "them", "then", "there",
    "these", "they", "this", "to", "was", "we", "were", "what", "when", "where", "which", "who",
    "why", "will", "with", "would", "you", "your", "can", "could", "about",
})
# fmt: on
_PHRASE_RE = re.compile(r'"([^"]+)"')
_WORD_RE = re.compile(r"[\w']+", re.UNICODE)


@dataclass(frozen=True)
class KeywordQuery:
    terms: str  # space-separated words to match (any of them)
    phrases: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.terms and not self.phrases


def build_keyword_query(text: str) -> KeywordQuery:
    """Words (stop words dropped unless nothing else is left) and quoted phrases, accent-folded."""
    folded = fold(text)
    phrases = tuple(p for p in (squash(m) for m in _PHRASE_RE.findall(folded)) if _WORD_RE.search(p))
    words = [w.strip("'") for w in _WORD_RE.findall(folded)]
    words = [w for w in words if w]
    content = [w for w in words if w.lower() not in STOP_WORDS]
    return KeywordQuery(" ".join(content or words), phrases)


@dataclass(frozen=True)
class Filters:
    categories: tuple[str, ...] = ()  # exact names (already resolved against the store)
    since: date | None = None
    until: date | None = None
    kind: str = "all"  # all | passages | summaries


KIND_VALUES = {"all": None, "passages": "passage", "summaries": "summary"}


def filter_sql(filters: Filters) -> tuple[sql.Composable, list[object]]:
    """SQL conditions (joined with AND, starting with AND) and their parameters."""
    parts: list[sql.Composable] = []
    params: list[object] = []
    if filters.categories:
        parts.append(sql.SQL("categories && %s"))
        params.append(list(filters.categories))
    if filters.since:
        parts.append(sql.SQL("published_date >= %s"))
        params.append(filters.since)
    if filters.until:
        parts.append(sql.SQL("published_date <= %s"))
        params.append(filters.until)
    kind = KIND_VALUES[filters.kind]
    if kind:
        parts.append(sql.SQL("kind = %s"))
        params.append(kind)
    clause = sql.SQL("").join(sql.SQL(" AND ") + p for p in parts)
    return clause, params


def _boost(value: float) -> sql.Composable:
    # The boost is part of a type modifier, so it can't be a query parameter; it comes from
    # config floats, checked and formatted here as a plain number.
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"BM25 boost must be a positive number, got {value!r}")
    return sql.SQL("::pdb.boost({})").format(sql.SQL(repr(number)))


def keyword_search(
    conn: psycopg.Connection,
    schema: str,
    query: KeywordQuery,
    filters: Filters,
    boosts: Mapping[str, float],
    limit: int,
) -> list[str]:
    """Passage ids, best BM25 score first (ties: passage id, for a stable order)."""
    if query.empty:
        return []
    matches: list[sql.Composable] = []
    params: list[object] = []
    if query.terms:
        for column, field in (
            ("search_heading", "heading"),
            ("search_title", "title"),
            ("search_body", "body"),
        ):
            matches.append(sql.SQL("{} ||| %s").format(sql.Identifier(column)) + _boost(boosts[field]))
            params.append(query.terms)
    for phrase in query.phrases:
        for column, field in (("search_title", "title"), ("search_body", "body")):
            matches.append(sql.SQL("{} ### %s").format(sql.Identifier(column)) + _boost(2 * boosts[field]))
            params.append(phrase)
    where, filter_params = filter_sql(filters)
    statement = sql.SQL(
        "SELECT passage_id FROM {schema}.passages WHERE ({matches}){where} "
        "ORDER BY pdb.score(passage_id) DESC, passage_id LIMIT %s"
    ).format(schema=sql.Identifier(schema), matches=sql.SQL(" OR ").join(matches), where=where)
    rows = conn.execute(statement, [*params, *filter_params, limit]).fetchall()
    return [r[0] for r in rows]


def resolve_categories(
    conn: psycopg.Connection, schema: str, wanted: Sequence[str]
) -> tuple[tuple[str, ...], list[str]]:
    """Match category names case-insensitively against the store: (resolved names, unknown)."""
    known = all_categories(conn, schema)
    by_lower = {c.lower(): c for c in known}
    resolved = tuple(by_lower[w.lower()] for w in wanted if w.lower() in by_lower)
    unknown = [w for w in wanted if w.lower() not in by_lower]
    return resolved, unknown


def all_categories(conn: psycopg.Connection, schema: str) -> list[str]:
    rows = conn.execute(
        sql.SQL("SELECT DISTINCT unnest(categories) AS c FROM {}.posts ORDER BY c").format(
            sql.Identifier(schema)
        )
    ).fetchall()
    return [r[0] for r in rows]
