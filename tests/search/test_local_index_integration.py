"""Indexing and searching with a local model, end to end (openspec use-local-embeddings).

The real model is replaced by a fake loader; everything else is real: index_archive, the
weights record, versions, swap, rollback and the search service."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace

import pytest

from newsletter_archive.search import db, embedders
from newsletter_archive.search.embedders import LocalModels, embedder_for
from newsletter_archive.search.embeddings import LocalModelError
from newsletter_archive.search.indexer import IndexOptions, index_archive
from newsletter_archive.search.service import SearchRequest, SearchService

from .archive import record, small_config, write_archive
from .fakes import FakeEmbedder

pytestmark = pytest.mark.db

LOCAL = "local:test-model"


class HashedFake(FakeEmbedder):
    def __init__(self, sha="a" * 64, **kw):
        super().__init__(**kw)
        self.sha = sha

    def weights_sha256(self):
        return self.sha


@pytest.fixture
def cfg(db_config):
    base = small_config(db_config)
    s = replace(
        base.search,
        embedding_model=LOCAL,
        embedding_dimension=8,
        summaries=replace(base.search.summaries, enabled=False),
    )
    return replace(base, search=s)


@pytest.fixture
def fake_model(monkeypatch):
    """The weights hash the fake model reports; tests change ``state["sha"]``."""
    state = {"sha": "a" * 64}

    def load(model, folder, dim, batch_size):
        return HashedFake(sha=state["sha"], model=model, dim=dim, batch_size=batch_size)

    monkeypatch.setattr(embedders, "_load_local", load)
    return state


def clean(cfg):
    with db.connect(cfg, "writer") as conn:
        db.apply_migrations(conn)
        conn.execute("DELETE FROM public.embedding_models")


# spec: search-indexing / Semantic index (Local model needs no API key)
def test_index_with_the_local_model_needs_no_gemini_key(cfg, fake_model):
    clean(cfg)
    write_archive(cfg, [record(f"post-{i}", i) for i in range(3)])
    assert not cfg.gemini_api_key
    report = index_archive(cfg, IndexOptions())
    assert len(report.indexed) == 3
    with db.connect(cfg, "reader") as conn:
        assert db.active_generation(conn).embedding_model == LOCAL
        assert (
            conn.execute(
                "SELECT weights_sha256 FROM public.embedding_models WHERE name = %s", (LOCAL,)
            ).fetchone()[0]
            == "a" * 64
        )


def test_other_weights_under_the_same_name_are_refused(cfg, fake_model):
    clean(cfg)
    write_archive(cfg, [record("post-0", 0)])
    index_archive(cfg, IndexOptions())
    write_archive(cfg, [record("post-0", 0), record("post-1", 1)])
    fake_model["sha"] = "b" * 64
    with pytest.raises(LocalModelError, match="give new weights a new name"):
        index_archive(cfg, IndexOptions())


# spec: search-indexing / Spending ceiling (Local rebuild)
def test_dry_run_reports_local_embeddings_at_no_cost_and_no_ceiling(cfg, fake_model):
    clean(cfg)
    write_archive(cfg, [record(f"post-{i}", i) for i in range(3)])
    tiny = replace(cfg, search=replace(cfg.search, max_embed_tokens_per_run=1))
    lines = "\n".join(index_archive(tiny, IndexOptions(dry_run=True)).lines())
    assert "to embed locally:" in lines and "$0;" in lines and "estimated cost ~$0.00" in lines
    assert len(index_archive(tiny, IndexOptions()).indexed) == 3  # no spending ceiling for local


# spec: search-indexing / Local embedding model (Missing model folder)
def test_missing_model_folder_stops_indexing_with_the_setting_to_fix(cfg, tmp_path):
    clean(cfg)
    write_archive(cfg, [record("post-0", 0)])
    missing = replace(cfg, search=replace(cfg.search, local_model_path=str(tmp_path / "nowhere")))
    with pytest.raises(LocalModelError, match=r"search\.local_model_path"):
        index_archive(missing, IndexOptions())


def service(cfg, local):
    @contextmanager
    def connect():
        with db.connect(cfg, "reader") as conn:
            yield conn

    gemini_seen = []

    def gemini(model, dim):
        e = FakeEmbedder(model=model, dim=dim)
        gemini_seen.append(e)
        return e

    return SearchService(
        cfg, connect, embedder_for(cfg, for_queries=True, local=local, gemini=gemini)
    ), gemini_seen


# spec: search-indexing / Local embedding model (Search while the model is missing)
def test_search_falls_back_to_keywords_when_the_model_is_missing(cfg, fake_model, tmp_path):
    clean(cfg)
    write_archive(cfg, [record(f"post-{i}", i) for i in range(3)])
    index_archive(cfg, IndexOptions())
    from newsletter_archive.search.embeddings import check_model_folder

    def missing_loader(model, folder, dim, batch):
        check_model_folder(tmp_path / "nowhere", model)

    svc, _ = service(cfg, LocalModels(cfg, missing_loader, check="verify"))
    resp = svc.search(SearchRequest.create("post"))
    assert not resp.semantic and "not found" in " ".join(resp.notes)


def test_local_queries_are_verified_and_rollback_goes_back_to_gemini(cfg, fake_model, monkeypatch):
    """Build a Gemini-style version, then a local one; search each with its own model."""
    from newsletter_archive.config import Secret
    from newsletter_archive.search import gemini

    from ..conftest import TEST_GEMINI_KEY
    from .fakes import FakeSDK

    monkeypatch.setattr(gemini, "_default_sdk", lambda key, timeout: FakeSDK())
    clean(cfg)
    recs = [record(f"post-{i}", i) for i in range(3)]
    write_archive(cfg, recs)
    old = replace(
        cfg,
        gemini_api_key=Secret(TEST_GEMINI_KEY),  # rolling back catches the Gemini version up with Gemini
        search=replace(cfg.search, embedding_model="gemini-fake", embedding_dimension=8),
    )
    from newsletter_archive.search.indexer import Indexer

    from .test_indexer_integration import FakeSummarizer

    with db.connect(cfg, "writer") as conn:
        Indexer(old, conn, lambda m, d: FakeEmbedder(model=m, dim=d), lambda: FakeSummarizer()).run(
            IndexOptions()
        )
    index_archive(cfg, IndexOptions(refresh=True, force=True))  # local version, swapped in
    svc, gemini_seen = service(cfg, LocalModels(cfg, check="verify"))
    assert svc.search(SearchRequest.create("post")).semantic and gemini_seen == []
    index_archive(old, IndexOptions(rollback=True))
    svc._generation = None
    assert svc.search(SearchRequest.create("post")).semantic
    assert [e.model for e in gemini_seen] == ["gemini-fake"]
