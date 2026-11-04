"""Evaluation and the quality gate on real Postgres, with fake embedder and re-ranker."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import replace

import psycopg
import pytest

from newsletter_archive.search import db
from newsletter_archive.search.evaluate import (
    GateError,
    Question,
    eval_path,
    evaluate,
    gate_for,
    quality_gate,
    reader_service,
)
from newsletter_archive.search.generations import GenerationError, Generations
from newsletter_archive.search.indexer import Indexer, IndexOptions
from newsletter_archive.search.rerank import FakeReranker
from newsletter_archive.search.service import SearchService

from .fakes import FakeEmbedder
from .test_indexer_integration import FakeSummarizer
from .test_mcp_server import index, make_service, records

pytestmark = pytest.mark.db

QUESTIONS = [
    Question("bar-raiser", "local", '"bar raiser"', ("hiring",)),
    Question("promotion", "intent", "ask for a promotion with evidence", ("career",)),
    Question("broad", "global", "team", ("delegation", "hiring", "career")),
]


def indexer_factory(config, conn):
    def make():
        return Indexer(config, conn, lambda m, d: FakeEmbedder(model=m, dim=d), lambda: FakeSummarizer())

    return make


@pytest.fixture
def cfg(db_config):
    from .archive import small_config

    c = small_config(db_config)
    index(c, records())
    return c


def test_every_stage_is_scored_with_and_without_summaries(cfg):
    report = evaluate(make_service(cfg), QUESTIONS)
    assert report.generation == 1 and len(report.outcomes) == 6
    for summaries in (True, False):
        for stage in ("keyword", "semantic", "combined", "reranked"):
            assert report.score(stage, summaries) is not None
    bar = next(o for o in report.outcomes if o.question.id == "bar-raiser" and o.summaries)
    assert bar.scores["keyword"] == (1.0, 1.0) and bar.missing == ()
    assert report.score("keyword", True, "global")[0] == 1.0  # all 3 posts in the top 10
    assert report.score("returned", True) is not None and "returned" in "\n".join(report.lines())


def test_semantic_failure_leaves_those_stages_out(cfg):
    report = evaluate(make_service(cfg, embedder=RuntimeError("down")), QUESTIONS[:1], variants=(True,))
    [o] = report.outcomes
    assert set(o.scores) == {"keyword", "reranked", "returned"} and not o.semantic
    assert "warning: 1 searches ran without semantic search" in "\n".join(report.lines())


@pytest.fixture
def two_versions(cfg):
    """Generation 1 (fake-model, live) and a built candidate, generation 2 (other-model)."""
    other = replace(cfg, search=replace(cfg.search, embedding_model="other-model", embedding_dimension=4))
    with db.connect(cfg, "writer") as conn:
        Generations(other, conn, indexer_factory(other, conn)).refresh(IndexOptions(), swap=False)
        gens = {g.generation: g for g in db.list_generations(conn)}
    return gens[2], gens[1]


class _Flaky(FakeEmbedder):
    def __init__(self, model, dim, fails_on):
        super().__init__(model=model, dim=dim)
        self.fails_on = fails_on

    def embed(self, texts):
        if any(self.fails_on in t for t in texts):
            raise RuntimeError("Gemini down")
        return super().embed(texts)


def service_failing(cfg, model, fails_on=""):
    """Query embedding fails for ``model``: always, or only for queries containing ``fails_on``."""

    @contextmanager
    def connect():
        with db.connect(cfg, "reader") as conn:
            yield conn

    def factory(m, dim):
        if m == model and not fails_on:
            raise RuntimeError("model retired")
        return _Flaky(m, dim, fails_on if m == model else "\0never")

    return SearchService(cfg, connect, factory, FakeReranker())


def test_quality_gate_scores_two_versions(cfg, two_versions):
    candidate_points, live_points = quality_gate(make_service(cfg), QUESTIONS)(*two_versions)
    assert 0 <= candidate_points <= 100 and 0 <= live_points <= 100
    broken = quality_gate(make_service(cfg, embedder=RuntimeError("down")), QUESTIONS)
    with pytest.raises(GateError, match="failed for 3 questions on generation 2"):
        broken(*two_versions)


def test_gate_fails_when_the_candidate_cant_embed(cfg, two_versions):
    gate = quality_gate(service_failing(cfg, "other-model", fails_on="team"), QUESTIONS)
    with pytest.raises(GateError, match="failed for 1 questions on generation 2"):
        gate(*two_versions)


def test_gate_skips_the_comparison_when_the_live_model_is_gone(cfg, two_versions, caplog):
    gate = quality_gate(service_failing(cfg, "fake-model"), QUESTIONS)
    with caplog.at_level(logging.WARNING, "newsletter_archive.search.evaluate"):
        assert gate(*two_versions) is None
    assert "can't embed queries any more" in caplog.text


def test_gate_treats_a_partial_live_failure_as_an_outage(cfg, two_versions):
    # The first question fails; the rest must still be tried (no 30 s pause carried over).
    gate = quality_gate(service_failing(cfg, "fake-model", fails_on="bar raiser"), QUESTIONS)
    with pytest.raises(GateError, match="failed for 1 questions on generation 1"):
        gate(*two_versions)


def test_a_failing_gate_blocks_the_swap_unless_forced(cfg):
    other = replace(cfg, search=replace(cfg.search, embedding_model="other-model", embedding_dimension=4))

    def failing(candidate, active):
        raise GateError("quality check incomplete: Gemini down")

    with db.connect(cfg, "writer") as conn:
        factory = indexer_factory(other, conn)
        gens = Generations(other, conn, factory, gate=failing)
        with pytest.raises(GenerationError, match=r"Gemini down; not swapped \(use --force"):
            gens.refresh(IndexOptions())
        assert gens.swap(IndexOptions(), force=True).action == "swapped"


def test_a_database_error_in_the_gate_is_a_generation_error(cfg, caplog):
    other = replace(cfg, search=replace(cfg.search, embedding_model="other-model", embedding_dimension=4))

    def broken(candidate, active):
        raise psycopg.OperationalError("connection lost")

    with db.connect(cfg, "writer") as conn:
        gens = Generations(other, conn, indexer_factory(other, conn), gate=broken)
        with pytest.raises(GenerationError, match="connection lost; not swapped"):
            gens.refresh(IndexOptions())
        with caplog.at_level(logging.WARNING):
            assert gens.swap(IndexOptions(), force=True).action == "swapped"
    assert "swapping anyway (--force)" in caplog.text and "no quality comparison" not in caplog.text


def test_unknown_expected_posts_are_reported(cfg):
    report = evaluate(make_service(cfg), [Question("q", "local", "bar raiser", ("hiring", "gone"))])
    assert report.unknown_expected == {"q": ["gone"]}
    assert "q expects posts that aren't in the index: gone" in "\n".join(report.lines())


def test_reader_service_reuses_one_connection(cfg):
    svc = reader_service(cfg)
    with svc.connect() as first, svc.connect() as second:
        assert first is second
    first.close()
    with svc.connect() as third:
        assert third is not first and not third.closed
    third.close()


def test_gate_for_needs_the_question_file(cfg):
    assert gate_for(cfg) is None
    eval_path(cfg).write_text(
        "questions:\n  - {id: a, type: local, question: bar raiser, expected: [hiring]}\n", encoding="utf-8"
    )
    assert callable(gate_for(cfg))


def test_index_refresh_runs_the_gate_when_questions_exist(cfg, monkeypatch):
    from newsletter_archive.config import Secret
    from newsletter_archive.search import gemini
    from newsletter_archive.search.indexer import index_archive

    from ..conftest import TEST_GEMINI_KEY
    from .fakes import FakeSDK

    monkeypatch.setattr(gemini, "_default_sdk", lambda key, timeout: FakeSDK())
    eval_path(cfg).write_text(
        "questions:\n  - {id: a, type: local, question: bar raiser, expected: [hiring]}\n", encoding="utf-8"
    )
    keyed = replace(
        cfg,
        gemini_api_key=Secret(TEST_GEMINI_KEY),
        search=replace(cfg.search, embedding_model="other-model", embedding_dimension=4),
    )
    report = index_archive(keyed, IndexOptions(refresh=True, force=True))
    text = "\n".join(report.lines())
    assert "Quality gate: candidate recall@10" in text and "is now live" in text


def test_no_swap_refresh_ignores_a_broken_question_file(cfg, monkeypatch):
    from newsletter_archive.config import Secret
    from newsletter_archive.search import gemini
    from newsletter_archive.search.indexer import index_archive

    from ..conftest import TEST_GEMINI_KEY
    from .fakes import FakeSDK

    monkeypatch.setattr(gemini, "_default_sdk", lambda key, timeout: FakeSDK())
    eval_path(cfg).write_text("questions: [\n", encoding="utf-8")  # not valid YAML
    keyed = replace(
        cfg,
        gemini_api_key=Secret(TEST_GEMINI_KEY),
        search=replace(cfg.search, embedding_model="other-model", embedding_dimension=4),
    )
    report = index_archive(keyed, IndexOptions(refresh=True, no_swap=True, force=True))
    assert "is now live" not in "\n".join(report.lines())


def test_trace_records_each_stage(cfg):
    from newsletter_archive.search.service import SearchRequest

    trace = {}
    svc = make_service(cfg)
    svc.reranker = FakeReranker()
    resp = svc.search(SearchRequest.create("bar raiser"), trace=trace)
    assert set(trace) == {"keyword", "semantic", "combined", "reranked", "post_of"}
    assert trace["reranked"] and trace["post_of"][trace["keyword"][0]] == "hiring"
    assert resp.results[0].id in trace["reranked"]
