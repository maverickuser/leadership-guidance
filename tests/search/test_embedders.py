"""Choosing the embedder per index version's model (openspec use-local-embeddings, lld §5)."""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from newsletter_archive.config import Config, SearchConfig
from newsletter_archive.search.embedders import LocalModels, embedder_for
from newsletter_archive.search.embeddings import LocalModelError

from .fakes import FakeEmbedder

LOCAL = "local:test-model"


def config(tmp_path, **search):
    return Config(
        sources=(), delay_seconds=2, max_retries=1, timeout_seconds=1, min_word_count=1,
        output_dir=tmp_path, user_agent="t", search=replace(SearchConfig(), local_batch_size=4, **search),
    )  # fmt: skip


class Loader:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def __call__(self, model, folder, dim, batch_size):
        self.calls.append((model, folder, dim, batch_size))
        if self.fail:
            raise self.fail
        return FakeEmbedder(model=model, dim=dim, batch_size=batch_size)


def test_local_models_load_once_from_their_folder(tmp_path):
    cfg = config(tmp_path)
    loader = Loader()
    make = embedder_for(cfg, for_queries=False, local=LocalModels(cfg, loader), gemini=lambda m, d: 1 / 0)
    first, second = make(LOCAL, 8), make(LOCAL, 8)
    assert first is second and first.model == LOCAL and first.batch_size == 4
    assert loader.calls == [(LOCAL, tmp_path / "models" / "test-model", 8, 4)]


def test_queries_embed_one_at_a_time(tmp_path):
    cfg = config(tmp_path)
    make = embedder_for(cfg, for_queries=True, local=LocalModels(cfg, Loader()))
    assert make(LOCAL, 8).batch_size == 1


# spec: search-indexing / Semantic index (Store searched with its own model)
def test_gemini_models_go_to_gemini_built_only_when_asked(tmp_path):
    cfg = config(tmp_path)
    built = []

    def gemini_factory(model, dim):
        built.append(model)
        return FakeEmbedder(model=model, dim=dim)

    loader = Loader()
    make = embedder_for(cfg, for_queries=True, local=LocalModels(cfg, loader), gemini=gemini_factory)
    make(LOCAL, 8)
    assert built == []
    assert make("gemini-embedding-2", 3072).model == "gemini-embedding-2"
    assert built == ["gemini-embedding-2"] and len(loader.calls) == 1


def test_a_failed_load_is_retried_next_time(tmp_path):
    cfg = config(tmp_path)
    loader = Loader(fail=LocalModelError("not found"))
    local = LocalModels(cfg, loader)
    for _ in range(2):
        with pytest.raises(LocalModelError):
            local.get(LOCAL, 8, 1)
    assert len(loader.calls) == 2


def test_preload_logs_a_failure_instead_of_raising(tmp_path, caplog):
    cfg = config(tmp_path)
    caplog.set_level(logging.WARNING, logger="newsletter_archive.search.embedders")
    LocalModels(cfg, Loader(fail=LocalModelError("folder missing"))).preload(LOCAL, 8).join(5)
    assert "local embedding model local:test-model unavailable: folder missing" in caplog.text


def test_preload_loads_in_the_background(tmp_path):
    cfg = config(tmp_path)
    loader = Loader()
    local = LocalModels(cfg, loader)
    local.preload(LOCAL, 8).join(5)
    assert local.get(LOCAL, 8, 1) is local.get(LOCAL, 8, 1) and len(loader.calls) == 1


def test_fakes_without_a_weights_hash_skip_the_check(tmp_path):
    cfg = config(tmp_path)
    assert LocalModels(cfg, Loader(), check="verify").get(LOCAL, 8, 1).model == LOCAL


# -- review fixes ------------------------------------------------------------------------------


def test_a_query_during_the_load_does_not_wait(tmp_path):
    import threading

    from newsletter_archive.search.embedders import LocalModelLoading

    cfg = config(tmp_path)
    started, release = threading.Event(), threading.Event()

    def slow_loader(model, folder, dim, batch):
        started.set()
        release.wait(5)
        return FakeEmbedder(model=model, dim=dim)

    local = LocalModels(cfg, slow_loader)
    thread = local.preload(LOCAL, 8)
    started.wait(5)
    with pytest.raises(LocalModelLoading, match="still loading"):
        local.get(LOCAL, 8, 1, wait=False)
    release.set()
    thread.join(5)
    assert local.get(LOCAL, 8, 1, wait=False).model == LOCAL


def test_refused_weights_are_not_reloaded_on_every_query(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    loader = Loader()
    local = LocalModels(cfg, loader, check="verify")

    def refuse(embedder):
        raise LocalModelError("the weights differ")

    monkeypatch.setattr(local, "_verify", refuse)
    for _ in range(3):
        with pytest.raises(LocalModelError, match="differ"):
            local.get(LOCAL, 8, 1, wait=False)
    assert len(loader.calls) == 1


def test_preload_live_falls_back_to_the_configured_model_without_a_database(tmp_path, monkeypatch):
    from newsletter_archive.search import db

    def down(*a, **k):
        raise OSError("database down")

    monkeypatch.setattr(db, "connect", down)
    loader = Loader()
    local_cfg = config(tmp_path, embedding_model=LOCAL, embedding_dimension=8)
    LocalModels(local_cfg, loader).preload_live().join(5)
    assert [c[0] for c in loader.calls] == [LOCAL]
    assert (
        LocalModels(config(tmp_path), Loader()).preload_live() is None
    )  # Gemini configured: nothing to load
