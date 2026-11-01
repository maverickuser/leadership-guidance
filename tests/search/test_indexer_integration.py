"""archive index against a real database with fake Gemini (spec search-indexing)."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from newsletter_archive.search import db
from newsletter_archive.search.gemini import CircuitOpenError, GeminiUnavailableError
from newsletter_archive.search.indexer import (
    Indexer,
    IndexOptions,
    ModelChangedError,
    SpendingCeilingError,
)
from newsletter_archive.search.summaries import Summary, SummaryError

from .archive import record, remove_post, small_config, write_archive
from .fakes import FakeEmbedder

pytestmark = pytest.mark.db


class FakeSummarizer:
    model = "fake-summary-model"
    thinking_level = "low"

    def __init__(self, fail: set[str] = frozenset()) -> None:
        self.calls: list[str] = []
        self.fail = set(fail)

    def summarize(self, title, sections):
        self.calls.append(title)
        if title in self.fail:
            raise SummaryError("no usable summary")
        return Summary(f"Summary of {title}: the post argues things.", 100, 30)


class FlakyEmbedder(FakeEmbedder):
    """Raises ``error`` when asked to embed text containing ``trigger``."""

    def __init__(self, trigger: str, error: Exception, **kw) -> None:
        super().__init__(**kw)
        self.trigger, self.error = trigger, error

    def embed(self, texts):
        if any(self.trigger in t for t in texts):
            self.batches.append(list(texts))
            raise self.error
        return super().embed(texts)


@pytest.fixture
def cfg(db_config):
    return small_config(db_config)


@pytest.fixture
def conn(cfg):
    with db.connect(cfg, "writer") as c:
        yield c


def make(cfg, conn, embedder=None, summarizer=None, posts=None):
    embedder = embedder or FakeEmbedder(dim=8)
    summarizer = summarizer if summarizer is not None else FakeSummarizer()
    lines = []
    indexer = Indexer(cfg, conn, lambda: embedder, lambda: summarizer, lambda *a: lines.append(a))
    return indexer, embedder, summarizer, lines


def run(cfg, conn, options=None, **kw):
    indexer, emb, summ, lines = make(cfg, conn, **kw)
    return indexer.run(options or IndexOptions()), emb, summ, lines


def rows(conn, sql, *args):
    return conn.execute(sql, args).fetchall()


def archive(cfg, n=6, **kw):
    recs = [record(f"post-{i}", i, **kw) for i in range(n)]
    write_archive(cfg, recs)
    return recs


def test_first_build_then_nothing_to_do(cfg, conn):
    archive(cfg)
    report, _emb, _summ, _ = run(cfg, conn)
    assert report.mode == "build" and report.generation == 1 and len(report.indexed) == 6
    assert report.status == "ok" and report.summaries == 6 and report.boilerplate_paragraphs == 6
    assert db.active_generation(conn).generation == 1
    kinds = dict(rows(conn, "SELECT kind, count(*) FROM idx_g1.passages GROUP BY kind"))
    assert kinds["summary"] == 6 and kinds["passage"] == report.passages
    intro = rows(conn, "SELECT count(*) FROM idx_g1.passages WHERE body LIKE %s", "%example letter%")
    assert intro == [(0,)]  # boilerplate never indexed
    assert rows(conn, "SELECT count(*) FROM idx_g1.boilerplate") == [(1,)]
    run_row = rows(conn, "SELECT mode, status, counts->>'indexed' FROM index_runs")
    assert run_row == [("build", "ok", "6")]

    # spec: Change detection / Nothing changed
    again, emb2, summ2, _ = run(cfg, conn)
    assert again.mode == "incremental" and again.indexed == [] and again.unchanged == 6
    assert emb2.batches == [] and summ2.calls == [] and again.embedded == 0


def test_folded_search_columns_and_halfvec(cfg, conn):
    recs = [record("cafe-post", 1, title="Résumé tips for the café")]
    write_archive(cfg, recs)
    run(cfg, conn)
    title, search_title = rows(conn, "SELECT title, search_title FROM idx_g1.passages LIMIT 1")[0]
    assert title.startswith("Résumé") and search_title.startswith("Resume tips for the cafe")
    hit = rows(conn, "SELECT count(*) FROM idx_g1.passages WHERE search_title ||| 'resume'")
    assert hit[0][0] > 0


# spec: Change detection / Title corrected
def test_title_change_reindexes_only_that_post(cfg, conn):
    recs = archive(cfg)
    run(cfg, conn)
    recs[2]["title"] = "A corrected title"
    write_archive(cfg, recs)
    report, _emb, summ, _ = run(cfg, conn)
    assert report.indexed == ["post-2"] and summ.calls == ["A corrected title"]
    assert rows(conn, "SELECT DISTINCT title FROM idx_g1.passages WHERE post_id = 'post-2'") == [
        ("A corrected title",)
    ]


# spec: Change detection / Chunk setting changed
def test_chunk_setting_change_resplits_but_reuses_embeddings(cfg, conn):
    archive(cfg, n=3, sizes=(40, 40))  # short sections: chunks don't change at 400 words
    run(cfg, conn)
    bigger = replace(cfg, search=replace(cfg.search, chunk_words=400, chunk_overlap_words=60))
    report, emb, _summ, _ = run(bigger, conn)
    assert len(report.indexed) == 3
    assert emb.batches == [] and report.cache_hits > 0  # same passage text: served from the cache


# spec: Change detection / Model changed
def test_model_change_needs_a_new_generation(cfg, conn):
    archive(cfg, n=2)
    run(cfg, conn)
    other = replace(cfg, search=replace(cfg.search, embedding_model="other-model"))
    with pytest.raises(ModelChangedError, match="archive index --refresh"):
        run(other, conn)


def test_removed_and_guarded(cfg, conn):
    archive(cfg, n=8)
    run(cfg, conn)
    remove_post(cfg, "post-7")
    report, *_ = run(cfg, conn)
    assert report.removed == ["post-7"]
    assert rows(conn, "SELECT count(*) FROM idx_g1.posts") == [(7,)]
    # spec: Deletion guard / Truncated manifest
    write_archive(cfg, [record("post-0", 0)])
    report, *_ = run(cfg, conn)
    assert report.removed == [] and len(report.guarded) == 6
    assert rows(conn, "SELECT count(*) FROM idx_g1.posts") == [(7,)]
    report, *_ = run(cfg, conn, IndexOptions(allow_mass_delete=True))
    assert len(report.removed) == 6


# spec: Index summary and dry run / Dry run
def test_dry_run_changes_nothing(cfg, conn):
    archive(cfg, n=2)
    report, emb, summ, lines = run(cfg, conn, IndexOptions(dry_run=True))
    assert report.status == "dry-run" and emb.batches == [] and summ.calls == []
    assert report.estimate.passages > 0 and report.estimate.summaries == 2 and report.estimate.cost_usd > 0
    assert sorted(line[0] for line in lines) == ["post-0", "post-1"]
    assert db.list_generations(conn) == [] and rows(conn, "SELECT count(*) FROM index_runs") == [(0,)]


# spec: Spending ceiling / Large rebuild
def test_spending_ceiling(cfg, conn):
    archive(cfg, n=2)
    tight = replace(cfg, search=replace(cfg.search, max_embed_tokens_per_run=10))
    with pytest.raises(SpendingCeilingError, match="--yes"):
        run(tight, conn)
    assert db.list_generations(conn) == []
    report, *_ = run(tight, conn, IndexOptions(yes=True))
    assert len(report.indexed) == 2


# spec: Post summaries / Summary failure does not block passages
def test_summary_failure_keeps_passages_and_retries(cfg, conn):
    archive(cfg, n=3)
    report, *_ = run(cfg, conn, summarizer=FakeSummarizer(fail={"Post post-1"}))
    assert len(report.indexed) == 3 and list(report.summaries_failed) == ["post-1"]
    assert report.status == "partial"
    assert rows(conn, "SELECT summary_sha256 IS NULL FROM idx_g1.posts WHERE post_id = 'post-1'") == [(True,)]
    assert rows(conn, "SELECT stage FROM index_failures") == [("summarize",)]
    report, _emb, summ, _ = run(cfg, conn)
    assert summ.calls == ["Post post-1"] and report.summaries == 1 and report.indexed == []
    assert rows(conn, "SELECT count(*) FROM idx_g1.passages WHERE kind = 'summary'") == [(3,)]
    assert rows(conn, "SELECT count(*) FROM index_failures") == [(0,)]


# spec: Post summaries / Summaries disabled
def test_summaries_disabled(cfg, conn):
    archive(cfg, n=2)
    off = replace(cfg, search=replace(cfg.search, summaries=replace(cfg.search.summaries, enabled=False)))
    report, _emb, summ, _ = run(off, conn)
    assert summ.calls == [] and report.summaries == 0
    assert rows(conn, "SELECT count(*) FROM idx_g1.passages WHERE kind = 'summary'") == [(0,)]


# spec: Runs, locking and failure tracking / Retry next run; Freshness / Stuck post
def test_embedding_failure_is_per_post_retried_and_reported_stuck(cfg, conn):
    archive(cfg, n=3)
    flaky = FlakyEmbedder("Post post-1", GeminiUnavailableError("still failing"), dim=8)
    report, *_ = run(cfg, conn, embedder=flaky)
    assert sorted(report.indexed) == ["post-0", "post-2"] and "post-1" in report.failed
    assert report.unindexed[0][0] == "post-1" and report.stuck == []
    report, *_ = run(cfg, conn, embedder=FlakyEmbedder("Post post-1", GeminiUnavailableError("x"), dim=8))
    assert report.stuck == ["post-1"]
    assert rows(conn, "SELECT attempts FROM index_failures WHERE stage = 'embed'") == [(2,)]
    report, *_ = run(cfg, conn)  # healthy again
    assert report.indexed == ["post-1"] and report.stuck == [] and report.unindexed == []


def test_three_failures_then_skipped_until_retry_failed(cfg, conn):
    archive(cfg, n=2)
    for _ in range(3):
        run(cfg, conn, embedder=FlakyEmbedder("Post post-1", GeminiUnavailableError("x"), dim=8))
    report, *_ = run(cfg, conn)
    assert report.skipped == ["post-1"]
    report, *_ = run(cfg, conn, IndexOptions(retry_failed=True))
    assert report.indexed == ["post-1"]


# spec: Embedding API use / Outage
def test_circuit_breaker_stops_all_further_calls(cfg, conn):
    archive(cfg, n=4)
    flaky = FlakyEmbedder("Post post-1", CircuitOpenError(3), dim=8)
    report, emb, *_ = run(cfg, conn, embedder=flaky)
    assert report.indexed == ["post-0"] and sorted(report.failed) == ["post-1", "post-2", "post-3"]
    assert "stopped for this run" in report.failed["post-3"]
    assert len(emb.batches) == 2  # post-0, then post-1 raised; nothing sent for post-2/3


def test_bad_post_file_is_recorded_and_not_removed(cfg, conn):
    recs = archive(cfg, n=3)
    run(cfg, conn)
    cfg.post_path("post-1").write_text("{not json", encoding="utf-8")
    report, *_ = run(cfg, conn)
    assert "post-1" in report.failed and report.failed["post-1"].startswith("load:")
    assert report.removed == [] and rows(conn, "SELECT count(*) FROM idx_g1.posts") == [(3,)]
    assert recs  # the other posts are untouched


def test_limit_indexes_some_and_keeps_the_rest(cfg, conn):
    archive(cfg, n=5)
    report, *_ = run(cfg, conn, IndexOptions(limit=2))
    assert report.indexed == ["post-0", "post-1"] and report.removed == []
    report, *_ = run(cfg, conn)
    assert sorted(report.indexed) == ["post-2", "post-3", "post-4"] and report.unchanged == 2


def test_stale_runs_aborted_and_concurrent_runs_refused(cfg, conn):
    archive(cfg, n=1)
    db.apply_migrations(conn)
    conn.execute("INSERT INTO index_runs (mode) VALUES ('incremental')")
    run(cfg, conn)
    assert rows(conn, "SELECT status FROM index_runs ORDER BY run_id") == [("aborted",), ("ok",)]
    with db.connect(cfg, "writer") as other, db.index_lock(other):
        from newsletter_archive.errors import IndexBusyError

        with pytest.raises(IndexBusyError):
            run(cfg, conn)


def test_cache_gc_drops_old_unreferenced_rows(cfg, conn):
    archive(cfg, n=1)
    run(cfg, conn)
    conn.execute(
        "INSERT INTO embedding_cache (embed_sha256, model, dim, embedding, last_used_at) "
        "VALUES ('orphan', 'fake-model', 8, '[1,0,0,0,0,0,0,0]', now() - interval '200 days')"
    )
    conn.execute(
        "INSERT INTO summary_cache (summary_sha256, model, prompt_version, summary, last_used_at) "
        "VALUES ('orphan', 'm', 1, 's', now() - interval '200 days')"
    )
    conn.execute("UPDATE embedding_cache SET last_used_at = now() - interval '200 days'")
    run(cfg, conn)
    assert rows(conn, "SELECT count(*) FROM embedding_cache WHERE embed_sha256 = 'orphan'") == [(0,)]
    assert rows(conn, "SELECT count(*) FROM summary_cache WHERE summary_sha256 = 'orphan'") == [(0,)]
    assert rows(conn, "SELECT count(*) FROM embedding_cache")[0][0] > 0  # referenced rows stay


def test_report_lines(cfg, conn):
    archive(cfg, n=2)
    report, *_ = run(cfg, conn, summarizer=FakeSummarizer(fail={"Post post-0"}))
    text = "\n".join(report.lines())
    assert "Index summary (generation 1, build): indexed=2" in text
    assert "Summary missing: post-0" in text and "Index status: partial" in text
    assert json.loads(json.dumps(report.counts()))["indexed"] == 2


# --- edge and failure paths ---------------------------------------------------------------------


def test_index_archive_with_the_real_client_classes(cfg, monkeypatch):
    from newsletter_archive.config import Secret
    from newsletter_archive.search import gemini
    from newsletter_archive.search.indexer import index_archive

    from ..conftest import TEST_GEMINI_KEY
    from .fakes import FakeSDK

    archive(cfg, n=2)
    seen = []
    monkeypatch.setattr(gemini, "_default_sdk", lambda key, timeout: seen.append(timeout) or FakeSDK())
    report = index_archive(replace(cfg, gemini_api_key=Secret(TEST_GEMINI_KEY)), IndexOptions())
    assert len(report.indexed) == 2 and report.summaries == 2 and seen == [60.0, 60.0]
    with pytest.raises(gemini.MissingGeminiKeyError):
        index_archive(cfg, IndexOptions())
    assert index_archive(cfg, IndexOptions(dry_run=True)).status == "dry-run"  # no key needed


def test_resumes_an_unfinished_first_build(cfg, conn):
    archive(cfg, n=2)
    db.apply_migrations(conn)
    gen = db.create_generation(conn, "fake-model", 8)
    report, *_ = run(cfg, conn)
    assert report.generation == gen.generation and db.active_generation(conn).generation == gen.generation


def test_dry_run_lists_every_kind_of_change(cfg, conn):
    recs = archive(cfg, n=8)
    run(cfg, conn)
    recs[0]["title"] = "Changed"
    write_archive(cfg, recs[:7])
    conn.execute("UPDATE idx_g1.posts SET summary_sha256 = NULL WHERE post_id = 'post-3'")
    _report, _, _, lines = run(cfg, conn, IndexOptions(dry_run=True))
    assert {(slug, status) for slug, _, status, _ in lines} == {
        ("post-0", "would index"),
        ("post-3", "would summarise"),
        ("post-7", "would remove"),
    }


def test_heavy_boilerplate_loss_is_reported(cfg, conn):
    write_archive(cfg, [record(f"post-{i}", i, sizes=()) for i in range(6)])
    report, *_ = run(cfg, conn)
    assert len(report.heavy_loss) == 6
    assert "Warning: boilerplate filter removed over 20% of post-0" in "\n".join(report.lines())


def test_summary_ceiling(cfg, conn):
    archive(cfg, n=2)
    tight = replace(
        cfg, search=replace(cfg.search, summaries=replace(cfg.search.summaries, max_tokens_per_run=10))
    )
    with pytest.raises(SpendingCeilingError, match="summaries"):
        run(tight, conn)


@pytest.mark.parametrize("where", ["summarizer", "embedder"])
def test_auth_errors_stop_the_run_and_mark_it_failed(cfg, conn, where):
    from newsletter_archive.search.gemini import GeminiAuthError

    archive(cfg, n=2)

    class AuthFail(FakeSummarizer):
        def summarize(self, title, sections):
            raise GeminiAuthError(401)

    kw = {"summarizer": AuthFail()} if where == "summarizer" else {
        "embedder": FlakyEmbedder("Post", GeminiAuthError(403), dim=8)
    }  # fmt: skip
    with pytest.raises(GeminiAuthError):
        run(cfg, conn, **kw)
    assert rows(conn, "SELECT status, error LIKE 'GeminiAuthError%%' FROM index_runs") == [("failed", True)]


def test_summary_only_post_whose_summary_fails_again(cfg, conn):
    archive(cfg, n=2)
    run(cfg, conn, summarizer=FakeSummarizer(fail={"Post post-1"}))
    report, emb, *_ = run(cfg, conn, summarizer=FakeSummarizer(fail={"Post post-1"}))
    assert list(report.summaries_failed) == ["post-1"] and emb.batches == []
    assert rows(conn, "SELECT attempts FROM index_failures") == [(2,)]


def test_summary_only_post_when_embedding_fails(cfg, conn):
    archive(cfg, n=2)
    run(cfg, conn, summarizer=FakeSummarizer(fail={"Post post-1"}))
    flaky = FlakyEmbedder("Summary", GeminiUnavailableError("down"), dim=8)
    report, *_ = run(cfg, conn, embedder=flaky)
    assert list(report.summaries_failed) == ["post-1"] and report.failed == {}


def test_oversized_input_fails_only_that_post(cfg, conn):
    write_archive(cfg, [record("post-0", 0), record("post-1", 1, title="T" * 7500)])
    report, *_ = run(cfg, conn)
    assert report.indexed == ["post-0"] and report.failed["post-1"].startswith("chunk:")


def test_write_errors_fail_only_that_post(cfg, conn, monkeypatch):
    import psycopg

    archive(cfg, n=2)
    indexer, *_ = make(cfg, conn)
    real = indexer._write_post

    def write(gen, p, *a):
        if p.post.id == "post-1":
            raise psycopg.errors.DataError("bad row")
        return real(gen, p, *a)

    monkeypatch.setattr(indexer, "_write_post", write)
    report = indexer.run(IndexOptions())
    assert report.indexed == ["post-0"] and report.failed["post-1"].startswith("write: DataError")


def test_empty_archive_builds_an_empty_generation(cfg, conn):
    write_archive(cfg, [])
    report, *_ = run(cfg, conn)
    assert report.status == "ok" and db.active_generation(conn) is not None


def test_verify_catches_mismatches(cfg, conn):
    from newsletter_archive.search.indexer import IndexReport, VerifyError

    archive(cfg, n=2)
    run(cfg, conn)
    indexer, *_ = make(cfg, conn)
    gen = db.active_generation(conn)
    loaded = indexer._load(None, IndexReport(), record=False)
    conn.execute("UPDATE idx_g1.posts SET fingerprint = 'stale' WHERE post_id = 'post-0'")
    with pytest.raises(VerifyError, match="don't match the archive"):
        indexer._verify(gen, loaded, IndexReport())
    conn.execute(
        "UPDATE idx_g1.posts SET fingerprint = %s WHERE post_id = 'post-0'",
        (loaded.prepared["post-0"].fingerprint,),
    )
    conn.execute("DELETE FROM idx_g1.passages WHERE post_id = 'post-0' AND kind = 'passage'")
    with pytest.raises(VerifyError, match="no passages"):
        indexer._verify(gen, loaded, IndexReport())


def test_run_bookkeeping_survives_a_closed_or_broken_connection(cfg, conn):
    from newsletter_archive.search.indexer import IndexReport

    indexer, *_ = make(cfg, conn)
    db.apply_migrations(conn)
    run_id = indexer._start_run("incremental", 1)
    indexer._finish_run(run_id, IndexReport(), None)
    conn.execute("DROP TABLE index_runs")
    indexer._finish_run(run_id, IndexReport(), None)  # logs a warning, no crash
    conn.close()
    indexer._finish_run(run_id, IndexReport(), None)


def test_report_lines_cover_every_section():
    from newsletter_archive.search.indexer import IndexReport, utc_age

    report = IndexReport(
        guarded=[f"p{i}" for i in range(12)],
        failed={"a": "embed: down"},
        unindexed=[("b", None), ("c", "2026-10-01T00:00:00Z")],
        stuck=["b"],
    )
    text = "\n".join(report.lines())
    assert "Deletion guard stopped 12 removals" in text and "…" in text
    assert "Failed: a: embed: down" in text and "Not indexed yet: b (fetched unknown)" in text
    assert "days ago" in text and "Stuck: b" in text
    assert utc_age("not a date") == "not a date"
    assert utc_age("2999-01-01T00:00:00Z").endswith("hours ago")
