"""Keyword and vector search against a real index built with fake embeddings."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from newsletter_archive.search import db
from newsletter_archive.search.embeddings import passage_input
from newsletter_archive.search.filters import Filters
from newsletter_archive.search.indexer import Indexer, IndexOptions
from newsletter_archive.search.keyword import (
    all_categories,
    build_keyword_query,
    keyword_search,
    resolve_categories,
)
from newsletter_archive.search.vector import vector_search

from .archive import record, small_config, write_archive
from .fakes import FakeEmbedder, fake_vector
from .test_indexer_integration import FakeSummarizer

pytestmark = pytest.mark.db
BOOSTS = {"heading": 2.0, "title": 1.5, "body": 1.0}


def post(slug, seed, sections, title=None, category="Leadership", day=1):
    r = record(slug, seed, intro=False)
    r["title"] = title or f"Post {slug}"
    r["sections"] = [{"heading": h, "level": 2 if h else 0, "text": t} for h, t in sections]
    r["text"] = "\n\n".join(t for _, t in sections)
    r["categories"] = [category]
    r["published_date"] = f"2025-0{day}-01"
    return r


FILLER = " ".join(["Teams grow when people learn together and share what they know."] * 6)


@pytest.fixture
def store(db_config):
    cfg = small_config(db_config)
    records = [
        post("heading-hit", 1, [("Delegation basics", FILLER)], category="Leadership", day=1),
        post("body-hit", 2, [("Other things", FILLER + " Delegating work builds trust over time.")], day=2),
        post(
            "phrase-hit",
            3,
            [(None, FILLER + " Every loop has a bar raiser who can veto.")],
            category="Hiring",
            day=3,
        ),
        post(
            "cafe",
            4,
            [(None, FILLER + " Notes from the café about résumé reviews.")],
            title="Café notes",
            day=4,
        ),
        post("unrelated", 5, [(None, FILLER)], category="Career", day=5),
    ]
    write_archive(cfg, records)
    with db.connect(cfg, "writer") as conn:
        Indexer(cfg, conn, lambda *_: FakeEmbedder(dim=8), lambda: FakeSummarizer()).run(IndexOptions())
        yield cfg, conn, db.active_generation(conn).schema


def ids(results):
    return [r.split("#")[0] for r in results]


# spec: search-indexing / Keyword index / Stemming; Heading outranks body
def test_stemming_and_heading_boost(store):
    _, conn, schema = store
    hits = keyword_search(conn, schema, build_keyword_query("delegate"), Filters(kind="passages"), BOOSTS, 10)
    assert ids(hits)[:2] == ["heading-hit", "body-hit"]


# spec: hybrid-search / Hybrid retrieval / Exact term match
def test_phrase_and_accents(store):
    _, conn, schema = store
    hits = keyword_search(conn, schema, build_keyword_query('"bar raiser"'), Filters(), BOOSTS, 10)
    assert ids(hits)[0] == "phrase-hit"
    assert (
        ids(keyword_search(conn, schema, build_keyword_query("resume"), Filters(), BOOSTS, 10))[0] == "cafe"
    )
    assert ids(keyword_search(conn, schema, build_keyword_query("cafe"), Filters(), BOOSTS, 10))[0] == "cafe"


def test_odd_queries_never_error(store):
    _, conn, schema = store
    for q in ['what\'s "bar raiser" AND (hiring)?', "OR NOT *", "~^[]{}", "", '"unclosed']:
        keyword_search(conn, schema, build_keyword_query(q), Filters(), BOOSTS, 5)


# spec: hybrid-search / Filters
def test_filters(store):
    _, conn, schema = store
    q = build_keyword_query("teams learn")
    hiring = keyword_search(conn, schema, q, Filters(categories=("Hiring",)), BOOSTS, 20)
    assert set(ids(hiring)) == {"phrase-hit"}
    recent = keyword_search(conn, schema, q, Filters(since=date(2025, 4, 1)), BOOSTS, 20)
    assert set(ids(recent)) == {"cafe", "unrelated"}
    early = keyword_search(conn, schema, q, Filters(until=date(2025, 1, 31)), BOOSTS, 20)
    assert set(ids(early)) == {"heading-hit"}
    summaries = keyword_search(
        conn, schema, build_keyword_query("summary"), Filters(kind="summaries"), BOOSTS, 20
    )
    assert summaries and all(s.endswith("#summary") for s in summaries)


def test_categories(store):
    _, conn, schema = store
    assert all_categories(conn, schema) == ["Career", "Hiring", "Leadership"]
    assert resolve_categories(conn, schema, ["leadership", "HIRING", "Nope"]) == (
        ("Leadership", "Hiring"),
        ["Nope"],
    )


def test_vector_search_nearest_first_with_filters(store):
    _cfg, conn, schema = store
    row = conn.execute(
        f"SELECT passage_id, title, heading, body FROM {schema}.passages WHERE post_id = 'cafe' AND kind = 'passage'"
    ).fetchone()
    v = np.asarray(fake_vector(passage_input(row[1], row[2], row[3]), 8), dtype=np.float32)
    v /= np.linalg.norm(v)
    assert vector_search(conn, schema, v, Filters(), 3)[0] == row[0]
    hiring = vector_search(conn, schema, v, Filters(categories=("Hiring",)), 10)
    assert hiring and all(h.startswith("phrase-hit") for h in hiring)


def small_store(db_config, records):
    cfg = small_config(db_config)
    write_archive(cfg, records)
    conn = db.connect(cfg, "writer")
    Indexer(cfg, conn, lambda *_: FakeEmbedder(dim=8), lambda: FakeSummarizer()).run(IndexOptions())
    return cfg, conn, db.active_generation(conn).schema


def test_vector_search_works_on_a_fresh_reader_connection(store):
    cfg, _conn, schema = store
    v = np.ones(8, dtype=np.float32) / np.sqrt(8)
    with db.connect(cfg, "reader") as reader:  # nothing else has registered pgvector here
        assert len(vector_search(reader, schema, v, Filters(), 3)) == 3
        assert len(vector_search(reader, schema, v, Filters(), 3)) == 3  # registered once


def test_phrase_in_a_heading_outranks_the_same_phrase_in_a_body(db_config):
    _, conn, schema = small_store(
        db_config,
        [
            post("in-body", 1, [("Other things", FILLER + " Every loop has a bar raiser.")]),
            post("in-heading", 2, [("Bar raiser", FILLER)]),
        ],
    )
    with conn:
        hits = keyword_search(conn, schema, build_keyword_query('"bar raiser"'), Filters(), BOOSTS, 10)
        assert ids(hits)[:2] == ["in-heading", "in-body"]


def test_categories_differing_only_by_case_are_all_kept(db_config):
    _, conn, schema = small_store(
        db_config,
        [
            post("upper", 1, [(None, FILLER)], category="Leadership"),
            post("lower", 2, [(None, FILLER)], category="leadership"),
            post("other", 3, [(None, FILLER)], category="Career"),
        ],
    )
    with conn:
        resolved, unknown = resolve_categories(conn, schema, ["LEADERSHIP"])
        assert sorted(resolved) == ["Leadership", "leadership"] and unknown == []
        hits = keyword_search(
            conn,
            schema,
            build_keyword_query("teams"),
            Filters(categories=resolved, kind="passages"),
            BOOSTS,
            20,
        )
        assert set(ids(hits)) == {"upper", "lower"}


def test_category_list_is_cached_briefly(store):
    from newsletter_archive.search import keyword as kw

    _, conn, schema = store
    kw.clear_category_cache()
    now = [100.0]
    assert "Brand new" not in kw.all_categories(conn, schema, clock=lambda: now[0])
    conn.execute(
        f"UPDATE {schema}.posts SET categories = categories || 'Brand new'::text WHERE post_id = 'cafe'"
    )
    now[0] += 10
    assert "Brand new" not in kw.all_categories(conn, schema, clock=lambda: now[0])  # cached
    now[0] += kw.CATEGORY_CACHE_SECONDS
    assert "Brand new" in kw.all_categories(conn, schema, clock=lambda: now[0])
