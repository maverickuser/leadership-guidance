"""Index versions: build, catch-up, gate, swap, rollback (spec search-indexing / Safe index updates)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from newsletter_archive.search import db
from newsletter_archive.search.gemini import GeminiUnavailableError
from newsletter_archive.search.generations import GenerationError, Generations
from newsletter_archive.search.indexer import Indexer, IndexOptions

from .archive import record, small_config, write_archive
from .fakes import FakeEmbedder
from .test_indexer_integration import FakeSummarizer, FlakyEmbedder

pytestmark = pytest.mark.db


@pytest.fixture
def cfg(db_config):
    return small_config(db_config)


@pytest.fixture
def conn(cfg):
    with db.connect(cfg, "writer") as c:
        yield c


def other_model(cfg):
    return replace(cfg, search=replace(cfg.search, embedding_model="other-model", embedding_dimension=4))


def gens(cfg, conn, embedder=None, summarizer=None, gate=None):
    embedder = embedder or FakeEmbedder(model=cfg.search.embedding_model, dim=cfg.search.embedding_dimension)
    summarizer = summarizer or FakeSummarizer()
    factory = lambda: Indexer(cfg, conn, lambda: embedder, lambda: summarizer)  # noqa: E731
    kw = {"gate": gate} if gate else {}
    return Generations(cfg, conn, factory, **kw), embedder, summarizer


def first_build(cfg, conn, n=4):
    recs = [record(f"post-{i}", i) for i in range(n)]
    write_archive(cfg, recs)
    Indexer(cfg, conn, lambda: FakeEmbedder(dim=8), lambda: FakeSummarizer()).run(IndexOptions())
    return recs


def statuses(conn):
    return {g.generation: g.status for g in db.list_generations(conn)}


def count(conn, schema):
    return conn.execute(f"SELECT count(*) FROM {schema}.posts").fetchone()[0]


def test_refresh_builds_beside_the_live_one_and_swaps(cfg, conn):
    first_build(cfg, conn)
    new = other_model(cfg)
    g, _, summ = gens(new, conn)
    result = g.refresh(IndexOptions())
    assert result.action == "swapped" and result.generation == 2 and result.previous == 1
    assert statuses(conn) == {1: "retired", 2: "active"}
    assert db.active_generation(conn).embedding_model == "other-model" and count(conn, "idx_g2") == 4
    assert summ.calls == []  # summaries reused from the cache: only embeddings were redone
    assert "Generation 2 is now live (previous: 1)" in result.summary()


# spec: Safe index updates / Catch-up before switching
def test_no_swap_then_swap_catches_up_first(cfg, conn):
    recs = first_build(cfg, conn)
    new = other_model(cfg)
    g, *_ = gens(new, conn)
    built = g.refresh(IndexOptions(), swap=False)
    assert built.action == "built" and statuses(conn) == {1: "active", 2: "ready"}
    write_archive(cfg, [*recs, record("post-new", 50)])  # a week of new posts, indexed into gen 1
    Indexer(cfg, conn, lambda: FakeEmbedder(dim=8), lambda: FakeSummarizer()).run(IndexOptions())
    assert count(conn, "idx_g1") == 5 and count(conn, "idx_g2") == 4
    result = g.swap(IndexOptions())
    assert result.action == "swapped" and count(conn, "idx_g2") == 5 and statuses(conn)[2] == "active"


# spec: Safe index updates / Rollback
def test_rollback_restores_and_catches_up(cfg, conn):
    recs = first_build(cfg, conn)
    new = other_model(cfg)
    gens(new, conn)[0].refresh(IndexOptions())
    write_archive(cfg, [*recs, record("post-new", 50)])
    g, *_ = gens(cfg, conn)  # config back on the old model
    result = g.rollback(IndexOptions())
    assert result.action == "rolled-back" and statuses(conn) == {1: "active", 2: "retired"}
    assert count(conn, "idx_g1") == 5  # includes the post added since the swap


def test_quality_gate_blocks_a_worse_generation_unless_forced(cfg, conn):
    first_build(cfg, conn)
    new = other_model(cfg)
    worse = lambda cand, live: (70.0, 80.0)  # noqa: E731
    g, *_ = gens(new, conn, gate=worse)
    with pytest.raises(GenerationError, match="more than 2 points worse"):
        g.refresh(IndexOptions())
    assert statuses(conn) == {1: "active", 2: "ready"}  # built, kept, not live
    result = g.swap(IndexOptions(), force=True)
    assert result.gate == (70.0, 80.0) and statuses(conn)[2] == "active"


def test_gate_within_tolerance_swaps(cfg, conn):
    first_build(cfg, conn)
    g, *_ = gens(other_model(cfg), conn, gate=lambda c, live: (79.0, 80.0))
    assert g.refresh(IndexOptions()).action == "swapped"


def test_only_the_newest_retired_generation_is_kept(cfg, conn):
    first_build(cfg, conn)
    gens(other_model(cfg), conn)[0].refresh(IndexOptions())
    gens(cfg, conn)[0].refresh(IndexOptions())
    assert statuses(conn) == {2: "retired", 3: "active"}
    schemas = {r[0] for r in conn.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'idx_g%'")}
    assert schemas == {"idx_g2", "idx_g3"}


def test_incomplete_build_never_goes_live_and_is_cleaned_up(cfg, conn):
    first_build(cfg, conn)
    new = other_model(cfg)
    flaky = FlakyEmbedder("Post post-2", GeminiUnavailableError("down"), model="other-model", dim=4)
    g, *_ = gens(new, conn, embedder=flaky)
    with pytest.raises(GenerationError, match="incomplete"):
        g.refresh(IndexOptions())
    assert statuses(conn) == {1: "active", 2: "building"}
    g, *_ = gens(new, conn)
    g.refresh(IndexOptions())
    # the half-built 2 was dropped (schema and row), so the rebuild reuses the number
    assert statuses(conn) == {1: "retired", 2: "active"}
    assert conn.execute("SELECT count(*) FROM idx_g2.posts").fetchone() == (4,)


def test_catch_up_failure_blocks_the_swap(cfg, conn):
    recs = first_build(cfg, conn)
    new = other_model(cfg)
    gens(new, conn)[0].refresh(IndexOptions(), swap=False)
    write_archive(cfg, [*recs, record("post-new", 50)])
    flaky = FlakyEmbedder("Post post-new", GeminiUnavailableError("down"), model="other-model", dim=4)
    g, *_ = gens(new, conn, embedder=flaky)
    with pytest.raises(GenerationError, match="couldn't be fully caught up"):
        g.swap(IndexOptions())
    assert statuses(conn) == {1: "active", 2: "ready"}


def test_nothing_to_swap_or_roll_back_and_model_mismatch(cfg, conn):
    first_build(cfg, conn)
    g, *_ = gens(cfg, conn)
    with pytest.raises(GenerationError, match="no generation is ready"):
        g.swap(IndexOptions())
    with pytest.raises(GenerationError, match="no previous generation"):
        g.rollback(IndexOptions())
    gens(other_model(cfg), conn)[0].refresh(IndexOptions())
    with pytest.raises(GenerationError, match=r"set search\.embedding_model"):
        gens(other_model(cfg), conn)[0].rollback(IndexOptions())  # gen 1 uses fake-model


def test_dry_run_refresh_creates_nothing(cfg, conn):
    first_build(cfg, conn)
    result = gens(other_model(cfg), conn)[0].refresh(IndexOptions(dry_run=True))
    assert result.report.status == "dry-run" and statuses(conn) == {1: "active"}


def test_index_archive_routes_generation_commands(cfg, monkeypatch):
    from newsletter_archive.config import Secret
    from newsletter_archive.search import gemini
    from newsletter_archive.search.indexer import index_archive

    from ..conftest import TEST_GEMINI_KEY
    from .fakes import FakeSDK

    monkeypatch.setattr(gemini, "_default_sdk", lambda key, timeout: FakeSDK())
    write_archive(cfg, [record(f"post-{i}", i) for i in range(2)])
    keyed = replace(cfg, gemini_api_key=Secret(TEST_GEMINI_KEY))
    index_archive(keyed, IndexOptions())
    new = replace(other_model(keyed))
    built = index_archive(new, IndexOptions(refresh=True, no_swap=True))
    assert "Generation 2 built (not live yet)" in built.lines()[-2]
    assert "Generation 2 is now live" in "\n".join(index_archive(new, IndexOptions(swap=True)).lines())
    back = index_archive(keyed, IndexOptions(rollback=True))
    assert "Generation 1 is live again" in "\n".join(back.lines())
