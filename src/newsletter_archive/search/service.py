"""The search pipeline behind the MCP tool (lld.md §6): validate → BM25 and query embedding
concurrently → vector search → RRF → re-rank → diversify → hydrate.

Degraded modes never fail a search: if the query can't be embedded (Gemini down, timeout,
quota, no key) results come from keyword search alone (``semantic: false``); if the re-ranker
is loading, missing, failing or slow they keep the combined order (``reranked: false``). Each
case adds a note. Invalid requests, a stopped database or a missing index raise SearchError
with a message the user can act on.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Protocol

import numpy as np
import psycopg
from psycopg import sql

from ..config import Config
from ..errors import ArchiveError
from . import db
from .embeddings import query_input
from .filters import KIND_VALUES, Filters
from .fusion import diversify, rrf
from .keyword import all_categories, build_keyword_query, keyword_search, resolve_categories
from .rerank import RerankerUnavailableError
from .vector import vector_search

log = logging.getLogger("newsletter_archive.search.service")

MAX_QUERY_CHARS = 1000
GENERATION_CACHE_SECONDS = 5.0
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="query-embed")


class SearchError(ArchiveError):
    """A search can't run: shown to the user as a tool error."""


class QueryEmbedder(Protocol):
    model: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list[np.ndarray]: ...


class Scorer(Protocol):
    def score(self, query: str, documents: Sequence[str]) -> list[float]: ...


# -- request ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchRequest:
    query: str
    limit: int
    categories: tuple[str, ...] = ()
    since: date | None = None
    until: date | None = None
    kind: str = "all"

    @classmethod
    def create(
        cls,
        query: object,
        limit: object = None,
        category: object = None,
        since: object = None,
        until: object = None,
        kind: object = "all",
        *,
        default_limit: int = 10,
        max_limit: int = 30,
    ) -> SearchRequest:
        """Validate raw tool arguments; SearchError says what is allowed."""
        if not isinstance(query, str) or not query.strip():
            raise SearchError("query must be a non-empty question or keywords")
        text = " ".join(query.split())
        if len(text) > MAX_QUERY_CHARS:
            raise SearchError(f"query is {len(text)} characters; the limit is {MAX_QUERY_CHARS}")
        if limit is None:
            n = default_limit
        elif isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= max_limit:
            raise SearchError(f"limit must be a whole number from 1 to {max_limit}")
        else:
            n = limit
        if category is None:
            cats: tuple[str, ...] = ()
        elif isinstance(category, str):
            cats = (category,)
        elif isinstance(category, list | tuple) and all(isinstance(c, str) for c in category):
            cats = tuple(category)
        else:
            raise SearchError("category must be a category name or a list of names")
        cats = tuple(c.strip() for c in cats if c.strip())
        start, end = _date("since", since), _date("until", until)
        if start and end and start > end:
            raise SearchError("since must be on or before until")
        if kind is None:
            kind = "all"
        if kind not in KIND_VALUES:
            raise SearchError(f"kind must be one of: {', '.join(KIND_VALUES)}")
        return cls(text, n, cats, start, end, str(kind))


def _date(name: str, value: object) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise SearchError(f"{name} must be a date written YYYY-MM-DD, e.g. 2025-03-31")


# -- response -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class PostRef:
    id: str
    title: str
    url: str
    published_date: str | None
    categories: list[str]


@dataclass(frozen=True)
class SearchResult:
    rank: int
    score: float
    found_by: str  # keyword | semantic | both
    kind: str  # passage | summary
    id: str
    heading: str | None
    text: str
    post: PostRef


@dataclass
class SearchResponse:
    query: str
    filters: dict[str, Any]
    semantic: bool
    reranked: bool
    results: list[SearchResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    generation: int = 0

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out.pop("generation")
        return out


@dataclass(frozen=True)
class _Row:
    passage_id: str
    post_id: str
    kind: str
    heading: str | None
    body: str
    post: PostRef

    def rerank_text(self) -> str:
        head = f"{self.post.title} — {self.heading}" if self.heading else self.post.title
        return f"{head}\n{self.body}"


# -- service ------------------------------------------------------------------------------------


class SearchService:
    """One per server. ``connect()`` yields a read-only connection (a pool in the MCP server);
    ``embedder_factory(model, dim)`` builds a query embedder for the active generation's model,
    or raises if it can't (e.g. no key); ``reranker`` may be None."""

    def __init__(
        self,
        config: Config,
        connect: Callable[[], AbstractContextManager[psycopg.Connection]],
        embedder_factory: Callable[[str, int], QueryEmbedder],
        reranker: Scorer | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.s = config.search
        self.connect = connect
        self.embedder_factory = embedder_factory
        self.reranker = reranker
        self.clock = clock
        self._generation: tuple[float, db.Generation] | None = None

    def search(self, request: SearchRequest) -> SearchResponse:
        try:
            with self.connect() as conn:
                return self._search(conn, request)
        except psycopg.OperationalError as e:
            raise SearchError(
                "lost the connection to the search database; start it with `docker compose up -d --wait db`"
            ) from e

    def categories(self) -> tuple[db.Generation, list[str]]:
        with self.connect() as conn:
            gen = self._active(conn)
            return gen, all_categories(conn, gen.schema)

    # -- pipeline -------------------------------------------------------------------------------

    def _search(self, conn: psycopg.Connection, req: SearchRequest) -> SearchResponse:
        gen = self._active(conn)
        notes: list[str] = []
        cats: tuple[str, ...] = ()
        if req.categories:
            cats, unknown = resolve_categories(conn, gen.schema, req.categories)
            if unknown:
                valid = ", ".join(all_categories(conn, gen.schema)) or "(none)"
                raise SearchError(f"unknown category {', '.join(unknown)}; valid categories: {valid}")
        filters = Filters(cats, req.since, req.until, req.kind)
        embedding = _pool.submit(self._embed_query, req.query, gen)
        keyword = keyword_search(
            conn, gen.schema, build_keyword_query(req.query), filters, self.s.bm25_boosts, self.s.candidates
        )
        lists: dict[str, list[str]] = {"keyword": keyword}
        semantic = True
        try:
            vector = embedding.result(timeout=self.s.query_timeout_seconds)
            lists["semantic"] = vector_search(conn, gen.schema, vector, filters, self.s.candidates)
        except FutureTimeout:
            semantic = False
            notes.append(
                f"semantic search skipped: embedding the query took over {self.s.query_timeout_seconds:g}s"
            )
        except Exception as e:
            semantic = False
            notes.append(f"semantic search skipped: {_reason(e)}")
            log.info("query embedding failed: %s", _reason(e))
        fused = rrf(lists)
        rows = self._hydrate(conn, gen.schema, [f.passage_id for f in fused])
        fused = [f for f in fused if f.passage_id in rows]
        order = [f.passage_id for f in fused]
        scores = {f.passage_id: f.score for f in fused}
        found = {f.passage_id: f.found_by for f in fused}
        reranked = False
        if order and self.reranker is not None:
            top = order[: self.s.rerank_top]
            try:
                new = self.reranker.score(req.query, [rows[p].rerank_text() for p in top])
                scores.update(zip(top, new, strict=True))
                order = sorted(top, key=lambda p: (-scores[p], top.index(p))) + order[len(top) :]
                reranked = True
            except RerankerUnavailableError as e:
                notes.append(f"results not re-ranked: {e}")
        elif order:
            notes.append("results not re-ranked: no re-ranker configured")
        kept = diversify(
            order,
            lambda p: rows[p].post_id,
            lambda p: rows[p].kind == "summary",
            self.s.max_per_post,
            1,
        )[: req.limit]
        results = [
            SearchResult(
                rank=i,
                score=round(float(scores[p]), 4),
                found_by="both" if len(found[p]) > 1 else found[p][0],
                kind=rows[p].kind,
                id=p,
                heading=rows[p].heading,
                text=rows[p].body,
                post=rows[p].post,
            )
            for i, p in enumerate(kept, start=1)
        ]
        filt = {
            "category": list(cats) or None,
            "since": req.since.isoformat() if req.since else None,
            "until": req.until.isoformat() if req.until else None,
            "kind": req.kind,
        }
        return SearchResponse(req.query, filt, semantic, reranked, results, notes, gen.generation)

    def _active(self, conn: psycopg.Connection) -> db.Generation:
        now = self.clock()
        if self._generation and now - self._generation[0] < GENERATION_CACHE_SECONDS:
            return self._generation[1]
        try:
            gen = db.active_generation(conn)
        except psycopg.errors.UndefinedTable:
            gen = None
        if gen is None:
            raise SearchError("there is no search index yet; run `archive index` first")
        self._generation = (now, gen)
        return gen

    def _embed_query(self, query: str, gen: db.Generation) -> np.ndarray:
        embedder = self.embedder_factory(gen.embedding_model, gen.embedding_dim)
        if (embedder.model, embedder.dim) != (gen.embedding_model, gen.embedding_dim):
            raise SearchError(f"query embedder uses {embedder.model}, the index {gen.embedding_model}")
        return embedder.embed([query_input(query)])[0]

    def _hydrate(self, conn: psycopg.Connection, schema: str, ids: list[str]) -> dict[str, _Row]:
        if not ids:
            return {}
        rows = conn.execute(
            sql.SQL(
                "SELECT x.passage_id, x.post_id, x.kind, x.heading, x.body, p.title, p.url, "
                "p.published_date, p.categories FROM {s}.passages x JOIN {s}.posts p USING (post_id) "
                "WHERE x.passage_id = ANY(%s)"
            ).format(s=sql.Identifier(schema)),
            (ids,),
        ).fetchall()
        out = {}
        for pid, post_id, kind, heading, body, title, url, published, categories in rows:
            published_str = published.isoformat() if isinstance(published, date) else published
            post = PostRef(post_id, title, url, published_str, list(categories or []))
            out[pid] = _Row(pid, post_id, kind, heading, body, post)
        return out


def _reason(e: BaseException) -> str:
    text = str(e) or type(e).__name__
    return text if len(text) <= 200 else text[:197] + "…"


def gemini_query_embedder(config: Config) -> Callable[[str, int], QueryEmbedder]:
    """Query embedders for the real Gemini API, one client per query: a past outage never
    leaves the circuit breaker open for later searches. Raises MissingGeminiKeyError without a
    key (the search then runs on keywords alone)."""
    from .embeddings import GeminiEmbedder
    from .gemini import GeminiClient

    s = config.search

    def make(model: str, dim: int) -> QueryEmbedder:
        client = GeminiClient(
            config.gemini_api_key,
            rpm=s.embed_rpm,
            tpm=s.embed_tpm,
            breaker_failures=1,
            timeout_seconds=s.query_timeout_seconds,
        )
        return GeminiEmbedder(client, model, dim, batch_size=1)

    return make
