"""BM25 keyword search over passages and summaries (ParadeDB pg_search).

Any query text is safe: words are matched with ``|||`` (match any, run through the same English
stemmer as the index), so punctuation and words like AND / OR / NOT are just text; text in
double quotes (straight or typographic) is matched as a phrase with ``###``. Queries are
accent-folded like the indexed ``search_*`` columns. Heading and title matches outrank body
matches (``search.bm25_boosts``).

Words are split the way the index tokenizer splits them (at apostrophes too), so "what's" is
"what" + "s", and both fragments are dropped as stop words rather than matching every
possessive in the archive.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import psycopg
from psycopg import sql

from .filters import Filters, filter_sql
from .text import fold, squash

# fmt: off
STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "do", "does", "for", "from", "had",
    "has", "have", "how", "i", "if", "in", "into", "is", "it", "its", "me", "my", "no", "not", "of",
    "on", "or", "our", "should", "so", "than", "that", "the", "their", "them", "then", "there",
    "these", "they", "this", "to", "was", "we", "were", "what", "when", "where", "which", "who",
    "why", "will", "with", "would", "you", "your", "can", "could", "about",
    # contraction fragments, as the tokenizer splits them ("what's", "don't", "you're"…)
    "s", "t", "d", "m", "ll", "re", "ve", "don", "doesn", "didn", "isn", "aren", "wasn", "weren",
    "won", "wouldn", "shouldn", "couldn", "haven", "hasn", "hadn", "ain",
})
# fmt: on
_QUOTES = str.maketrans(
    {"“": '"', "”": '"', "„": '"', "«": '"', "»": '"', "‘": "'", "’": "'", "‛": "'", "′": "'"}
)  # fmt: skip
_PHRASE_RE = re.compile(r'"([^"]+)"')
_WORD_RE = re.compile(r"\w+", re.UNICODE)
CATEGORY_CACHE_SECONDS = 60.0


@dataclass(frozen=True)
class KeywordQuery:
    terms: str  # space-separated words to match (any of them)
    phrases: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.terms and not self.phrases


def build_keyword_query(text: str) -> KeywordQuery:
    """Words (stop words dropped unless nothing else is left) and quoted phrases, accent-folded."""
    folded = fold(text).translate(_QUOTES)
    phrases = tuple(p for p in (squash(m) for m in _PHRASE_RE.findall(folded)) if _WORD_RE.search(p))
    words = _WORD_RE.findall(folded)
    content = [w for w in words if w.lower() not in STOP_WORDS]
    return KeywordQuery(" ".join(content or words), phrases)


def _boost(value: float) -> sql.Composable:
    # The boost is part of a type modifier, so it can't be a query parameter; it comes from
    # config floats, checked and formatted here as a plain number.
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"BM25 boost must be a positive number, got {value!r}")
    return sql.SQL("::pdb.boost({})").format(sql.SQL(repr(number)))


_FIELDS = (("search_heading", "heading"), ("search_title", "title"), ("search_body", "body"))


def keyword_search(
    conn: psycopg.Connection,
    schema: str,
    query: KeywordQuery,
    filters: Filters,
    boosts: Mapping[str, float],
    limit: int,
) -> list[str]:
    """Passage ids, best BM25 score first (equal scores: passage id, for a stable order).

    SQL orders by score alone so pg_search can use its top-N path; equal scores are put in
    passage-id order here."""
    if query.empty:
        return []
    matches: list[sql.Composable] = []
    params: list[object] = []
    if query.terms:
        for column, field in _FIELDS:
            matches.append(sql.SQL("{} ||| %s").format(sql.Identifier(column)) + _boost(boosts[field]))
            params.append(query.terms)
    for phrase in query.phrases:
        for column, field in _FIELDS:
            matches.append(sql.SQL("{} ### %s").format(sql.Identifier(column)) + _boost(2 * boosts[field]))
            params.append(phrase)
    where, filter_params = filter_sql(filters)
    statement = sql.SQL(
        "SELECT passage_id, pdb.score(passage_id) FROM {schema}.passages WHERE ({matches}){where} "
        "ORDER BY pdb.score(passage_id) DESC LIMIT %s"
    ).format(schema=sql.Identifier(schema), matches=sql.SQL(" OR ").join(matches), where=where)
    rows = conn.execute(statement, [*params, *filter_params, limit]).fetchall()
    return [r[0] for r in sorted(rows, key=lambda r: (-float(r[1]), r[0]))]


def resolve_categories(
    conn: psycopg.Connection, schema: str, wanted: Sequence[str]
) -> tuple[tuple[str, ...], list[str]]:
    """Match category names case-insensitively against the store: (every stored spelling of
    the wanted names, names not found)."""
    by_lower: dict[str, list[str]] = {}
    for c in all_categories(conn, schema):
        by_lower.setdefault(c.lower(), []).append(c)
    resolved = tuple(dict.fromkeys(c for w in wanted for c in by_lower.get(w.lower(), [])))
    unknown = [w for w in wanted if w.lower() not in by_lower]
    return resolved, unknown


_category_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}


def all_categories(
    conn: psycopg.Connection, schema: str, clock: Callable[[], float] = time.monotonic
) -> list[str]:
    """Every category in the generation, sorted. Cached per database and generation for
    ``CATEGORY_CACHE_SECONDS``: categories change only when the weekly run adds posts."""
    now = clock()
    key = (conn.info.dbname, schema)
    hit = _category_cache.get(key)
    if hit and now - hit[0] < CATEGORY_CACHE_SECONDS:
        return list(hit[1])
    rows = conn.execute(
        sql.SQL("SELECT DISTINCT unnest(categories) AS c FROM {}.posts ORDER BY c").format(
            sql.Identifier(schema)
        )
    ).fetchall()
    found = [r[0] for r in rows]
    _category_cache[key] = (now, found)
    return list(found)


_source_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}


def all_sources(
    conn: psycopg.Connection, schema: str, clock: Callable[[], float] = time.monotonic
) -> list[str]:
    """Every newsletter id in the generation, sorted; cached like ``all_categories``. Rows from
    before the move to per-newsletter folders (no source) are left out."""
    now = clock()
    key = (conn.info.dbname, schema)
    hit = _source_cache.get(key)
    if hit and now - hit[0] < CATEGORY_CACHE_SECONDS:
        return list(hit[1])
    rows = conn.execute(
        sql.SQL("SELECT DISTINCT source FROM {}.posts WHERE source IS NOT NULL ORDER BY source").format(
            sql.Identifier(schema)
        )
    ).fetchall()
    found = [r[0] for r in rows]
    _source_cache[key] = (now, found)
    return list(found)


def clear_category_cache() -> None:
    """Forget the cached category and newsletter lists."""
    _category_cache.clear()
    _source_cache.clear()
