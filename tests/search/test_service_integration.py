"""The search service end to end on real Postgres, with fake embedder and re-ranker."""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import replace

import psycopg
import pytest

from newsletter_archive.search import db
from newsletter_archive.search.indexer import Indexer, IndexOptions
from newsletter_archive.search.rerank import FakeReranker, Reranking
from newsletter_archive.search.service import SearchError, SearchRequest, SearchService

from .archive import small_config, write_archive
from .fakes import FakeEmbedder
from .test_indexer_integration import FakeSummarizer
from .test_retrieval_integration import FILLER, post

pytestmark = pytest.mark.db

LONG = " ".join(f"Delegation step {i} needs clear ownership and trust in the team." for i in range(40))


@pytest.fixture
def cfg(db_config):
    return small_config(db_config)


def build(cfg, records=None):
    records = records or [
        post("delegation", 1, [("Delegation basics", FILLER + " Delegate outcomes, not tasks.")], day=1),
        post("hiring", 2, [(None, FILLER + " Every loop has a bar raiser.")], category="Hiring", day=2),
        post("career", 3, [(None, FILLER + " Ask for a promotion with evidence.")], category="Career", day=3),
        post("long", 4, [(f"Part {i}", LONG) for i in range(6)], day=4),
    ]
    write_archive(cfg, records)
    with db.connect(cfg, "writer") as conn:
        Indexer(cfg, conn, lambda *_: FakeEmbedder(dim=8), lambda: FakeSummarizer()).run(IndexOptions())


def service(cfg, embedder=None, reranker=None, **kw):
    @contextmanager
    def connect():
        with db.connect(cfg, "reader") as conn:
            yield conn

    def factory(model, dim):
        if isinstance(embedder, Exception):
            raise embedder
        return embedder or FakeEmbedder(model=model, dim=dim)

    return SearchService(cfg, connect, factory, reranker, **kw)


def ask(svc, query, **kw):
    return svc.search(SearchRequest.create(query, **kw))


def posts(resp):
    return [r.post.id for r in resp.results]


def test_no_index_yet(cfg):
    with db.connect(cfg, "writer") as conn:
        db.apply_migrations(conn)
    with pytest.raises(SearchError, match="run `archive index`"):
        ask(service(cfg), "anything")


def test_no_database_tables_yet(cfg):
    with pytest.raises(SearchError, match="run `archive index`"):
        ask(service(cfg), "anything")


def test_hybrid_results_have_every_field(cfg):
    build(cfg)
    resp = ask(service(cfg, reranker=FakeReranker()), '"bar raiser"', limit=5)
    assert resp.semantic and resp.reranked and resp.notes == []
    top = resp.results[0]
    assert top.post.id == "hiring" and top.rank == 1 and top.found_by in ("keyword", "both")
    assert top.post.url and top.post.published_date == "2025-02-01" and top.post.categories == ["Hiring"]
    d = resp.to_dict()
    assert set(d) == {"query", "filters", "semantic", "reranked", "results", "notes"}
    assert d["filters"] == {"category": None, "since": None, "until": None, "kind": "all"}
    assert {r["found_by"] for r in d["results"]} <= {"keyword", "semantic", "both"}
    assert len(resp.results) == 5


def test_keyword_only_when_the_query_cannot_be_embedded(cfg):
    build(cfg)
    resp = ask(service(cfg, embedder=RuntimeError("GEMINI_API_KEY is not set")), "bar raiser")
    assert not resp.semantic and "semantic search skipped: GEMINI_API_KEY is not set" in resp.notes[0]
    assert posts(resp)[0] == "hiring" and all(r.found_by == "keyword" for r in resp.results)
    assert ask(service(cfg, embedder=RuntimeError("down")), "zzqqxx").results == []  # no match: empty


def test_slow_embedding_times_out(cfg):
    build(cfg)

    class Slow(FakeEmbedder):
        def embed(self, texts):
            time.sleep(0.5)
            return super().embed(texts)

    fast = replace(cfg, search=replace(cfg.search, query_timeout_seconds=0.05))
    resp = ask(service(fast, embedder=Slow(dim=8)), "bar raiser")
    assert not resp.semantic and "took over 0.05s" in resp.notes[0]


def test_wrong_embedding_model_is_a_semantic_failure(cfg):
    build(cfg)
    resp = ask(service(cfg, embedder=FakeEmbedder(model="other", dim=8)), "bar raiser")
    assert not resp.semantic and "query embedder uses other" in resp.notes[0]


def test_reranker_reorders_and_falls_back(cfg):
    build(cfg)

    class LovesCareer:
        def score(self, query, documents):
            return [1.0 if "promotion" in d else 0.0 for d in documents]

    resp = ask(service(cfg, reranker=LovesCareer()), "bar raiser")
    assert resp.reranked and posts(resp)[0] == "career" and resp.results[0].score == 1.0
    broken = Reranking(lambda: FakeReranker(fail=RuntimeError("mps error")), 5)
    broken.wait_loaded(5)
    resp = ask(service(cfg, reranker=broken), "bar raiser")
    assert not resp.reranked and "re-ranker failed" in resp.notes[0] and posts(resp)[0] == "hiring"
    resp = ask(service(cfg), "bar raiser")
    assert not resp.reranked and resp.notes == ["results not re-ranked: no re-ranker configured"]


def test_at_most_three_passages_and_one_summary_per_post(cfg):
    build(cfg)
    resp = ask(service(cfg, reranker=FakeReranker()), "delegation ownership trust", limit=30)
    from collections import Counter

    passages = Counter(r.post.id for r in resp.results if r.kind == "passage")
    summaries = Counter(r.post.id for r in resp.results if r.kind == "summary")
    assert passages["long"] == 3 and max(summaries.values()) == 1
    assert len({r.id for r in resp.results}) == len(resp.results)


def test_filters_and_unknown_category(cfg):
    build(cfg)
    svc = service(cfg)
    resp = ask(svc, "team", category=["hiring"], limit=30)
    assert set(posts(resp)) == {"hiring"} and resp.filters["category"] == ["Hiring"]
    assert all(r.kind == "summary" and r.heading is None for r in ask(svc, "team", kind="summaries").results)
    early = ask(svc, "team", until="2025-02-28", limit=30)
    assert set(posts(early)) == {"delegation", "hiring"}
    with pytest.raises(
        SearchError, match="unknown category Nope; valid categories: Career, Hiring, Leadership"
    ):
        ask(svc, "team", category="Nope")
    gen, cats = svc.categories()
    assert gen.generation == 1 and cats == ["Career", "Hiring", "Leadership"]


def test_lost_database_connection(cfg):
    @contextmanager
    def connect():
        raise psycopg.OperationalError("server closed the connection")
        yield

    svc = SearchService(cfg, connect, lambda m, d: FakeEmbedder(model=m, dim=d))
    with pytest.raises(SearchError, match="docker compose up -d --wait db"):
        ask(svc, "q")


def test_active_generation_is_cached_briefly(cfg):
    build(cfg)
    now = [0.0]
    svc = service(cfg, clock=lambda: now[0])
    assert ask(svc, "team").generation == 1
    with db.connect(cfg, "writer") as conn:  # pretend the index vanished
        conn.execute("UPDATE generations SET status = 'retired'")
    assert ask(svc, "team").generation == 1  # cached
    now[0] += 6
    with pytest.raises(SearchError, match="no search index"):
        ask(svc, "team")


def test_gemini_query_embedder(cfg, monkeypatch):
    from newsletter_archive.config import Secret
    from newsletter_archive.search import gemini
    from newsletter_archive.search.service import gemini_query_embedder

    from ..conftest import TEST_GEMINI_KEY
    from .fakes import FakeSDK

    build(cfg)
    sdk = FakeSDK()
    monkeypatch.setattr(gemini, "_default_sdk", lambda key, timeout: sdk)
    keyed = replace(cfg, gemini_api_key=Secret(TEST_GEMINI_KEY))
    svc = service(keyed)
    svc.embedder_factory = gemini_query_embedder(keyed)
    resp = ask(svc, "bar raiser")
    assert resp.semantic and len(sdk.calls) == 1
    assert "task: search result | query: bar raiser" in str(sdk.calls[0])
    svc.embedder_factory = gemini_query_embedder(cfg)  # no key
    resp = ask(svc, "bar raiser")
    assert not resp.semantic and "GEMINI_API_KEY" in resp.notes[0]


# -- review fixes ------------------------------------------------------------------------------


def test_a_vector_query_error_is_an_error_not_semantic_skipped(cfg):
    build(cfg)
    wrong_size = FakeEmbedder(model="fake-model", dim=4)  # the index holds 8-dimension vectors
    wrong_size.dim = 8  # claims the right size, returns the wrong one
    wrong_size.embed = lambda texts: FakeEmbedder(dim=4).embed(texts)
    with pytest.raises(psycopg.Error):
        ask(service(cfg, embedder=wrong_size), "bar raiser")


def test_semantic_search_pauses_after_a_failure(cfg):
    from newsletter_archive.search.service import SEMANTIC_PAUSE_SECONDS

    build(cfg)
    calls, now = [], [0.0]

    class Down(FakeEmbedder):
        def embed(self, texts):
            calls.append(1)
            raise RuntimeError("503 unavailable")

    svc = service(cfg, embedder=Down(dim=8), clock=lambda: now[0])
    assert "503 unavailable" in ask(svc, "bar raiser").notes[0] and calls == [1]
    paused = ask(svc, "bar raiser")
    assert not paused.semantic and "paused for 30s" in paused.notes[0] and calls == [1]
    now[0] += SEMANTIC_PAUSE_SECONDS + 1
    ask(svc, "bar raiser")
    assert calls == [1, 1]


def test_any_reranker_error_degrades_instead_of_failing(cfg):
    build(cfg)

    class Raises:
        def score(self, query, documents):
            raise RuntimeError("MPS out of memory")

    class Short:
        def score(self, query, documents):
            return [1.0]

    resp = ask(service(cfg, reranker=Raises()), "bar raiser")
    assert not resp.reranked and "MPS out of memory" in resp.notes[0] and resp.results
    resp = ask(service(cfg, reranker=Short()), "bar raiser")
    assert not resp.reranked and "wrong number of scores" in resp.notes[0]


def test_reranked_results_are_all_reranked_even_past_rerank_top(cfg):
    build(cfg)
    small = replace(cfg, search=replace(cfg.search, rerank_top=2))
    seen = []

    class Count(FakeReranker):
        def score(self, query, documents):
            seen.append(len(documents))
            return [1.0 - i / 100 for i in range(len(documents))]

    resp = ask(service(small, reranker=Count()), "team", limit=6)
    assert seen == [6] and len(resp.results) <= 6
    scores = [r.score for r in resp.results]
    assert scores == sorted(scores, reverse=True) and min(scores) > 0.9


def test_index_dropped_under_a_cached_generation_retries(cfg):
    from newsletter_archive.search.generations import Generations

    build(cfg)
    svc = service(cfg)
    assert ask(svc, "team").generation == 1
    with db.connect(cfg, "writer") as conn:
        factory = lambda: Indexer(cfg, conn, lambda *_: FakeEmbedder(dim=8), lambda: FakeSummarizer())  # noqa: E731
        Generations(cfg, conn, factory).refresh(IndexOptions())
        conn.execute("DROP SCHEMA idx_g1 CASCADE")
        conn.execute("DELETE FROM generations WHERE generation = 1")
    assert ask(svc, "team").generation == 2  # the cached 1 was forgotten and the search retried


def test_categories_maps_a_lost_database(cfg):
    @contextmanager
    def connect():
        raise psycopg.OperationalError("server closed the connection")
        yield

    svc = SearchService(cfg, connect, lambda m, d: FakeEmbedder(model=m, dim=d))
    with pytest.raises(SearchError, match="docker compose up -d --wait db"):
        svc.categories()


def test_gemini_query_embedder_shares_one_client(cfg, monkeypatch):
    from newsletter_archive.config import Secret
    from newsletter_archive.search import gemini
    from newsletter_archive.search.service import gemini_query_embedder

    from ..conftest import TEST_GEMINI_KEY
    from .fakes import FakeSDK

    made = []
    monkeypatch.setattr(gemini, "_default_sdk", lambda key, timeout: made.append(timeout) or FakeSDK())
    make = gemini_query_embedder(replace(cfg, gemini_api_key=Secret(TEST_GEMINI_KEY)))
    a, b = make("fake-model", 8), make("fake-model", 8)
    a.embed(["x"]), b.embed(["y"])
    assert a.client is b.client and a.client.retry_delays == () and made == [cfg.search.query_timeout_seconds]
