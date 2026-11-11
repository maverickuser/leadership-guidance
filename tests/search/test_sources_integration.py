"""Search over two newsletters on real Postgres: the source filter, names in results, the MCP
description and per-newsletter boilerplate."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from newsletter_archive.mcp_server import render_text, tool_description
from newsletter_archive.models import Manifest, ManifestEntry
from newsletter_archive.search import db
from newsletter_archive.search.indexer import Indexer, IndexOptions
from newsletter_archive.search.keyword import clear_category_cache
from newsletter_archive.search.service import SearchError, SearchRequest
from newsletter_archive.store import save_manifest

from ..conftest import make_source
from .archive import record, small_config
from .fakes import FakeEmbedder
from .test_indexer_integration import FakeSummarizer
from .test_retrieval_integration import FILLER, post
from .test_service_integration import service

pytestmark = pytest.mark.db

PE = "pragmatic-engineer"
REPEATED = "Thanks for reading this week's issue; forward it to a colleague who would enjoy it."


@pytest.fixture
def cfg(db_config):
    clear_category_cache()
    pe = make_source(
        db_config.output_dir, id=PE, name="The Pragmatic Engineer", base_url="https://pe.example.org"
    )
    return small_config(replace(db_config, sources=(db_config.sources[0], pe)))


def as_source(record, source_id):
    slug = record["id"].split("/", 1)[1]
    return record | {"id": f"{source_id}/{slug}", "source": source_id}


def write_sources(cfg, by_source):
    for source in cfg.sources:
        records = by_source.get(source.id, [])
        source.posts_dir.mkdir(parents=True, exist_ok=True)
        entries = {}
        for r in records:
            slug = r["id"].split("/", 1)[1]
            source.post_path(slug).write_text(json.dumps(r), encoding="utf-8")
            entries[slug] = ManifestEntry(url=r["url"], slug=slug, categories=r["categories"], status="ok")
        save_manifest(Manifest(entries=entries), source.manifest_path)


def build(cfg, by_source):
    write_sources(cfg, by_source)
    with db.connect(cfg, "writer") as conn:
        return Indexer(cfg, conn, lambda *_: FakeEmbedder(dim=8), lambda: FakeSummarizer()).run(
            IndexOptions()
        )


def two_newsletters():
    return {
        "main": [
            post("delegation", 1, [(None, FILLER + " Delegate outcomes to the team.")], day=1),
            post("hiring", 2, [(None, FILLER + " Every loop has a bar raiser.")], category="Hiring", day=2),
        ],
        PE: [
            as_source(
                post("on-call", 3, [(None, FILLER + " On-call rotations need clear ownership.")], day=3), PE
            ),
            as_source(
                post(
                    "delegation-at-scale",
                    4,
                    [(None, FILLER + " Delegate outcomes at scale.")],
                    category="Deepdives",
                    day=4,
                ),
                PE,
            ),
        ],
    }


def ask(svc, query, **kw):
    return svc.search(SearchRequest.create(query, **kw))


# spec: hybrid-search / Filters (Source filter)
def test_source_filter_and_names_in_results(cfg):
    build(cfg, two_newsletters())
    svc = service(cfg)
    both = ask(svc, "delegate outcomes", limit=10)
    assert {r.post.source for r in both.results} == {"main", PE}
    pe_only = ask(svc, "delegate outcomes", limit=10, source=PE)
    assert pe_only.results and {r.post.source for r in pe_only.results} == {PE}
    assert pe_only.filters["source"] == [PE]
    first = pe_only.results[0]
    assert first.post.source_name == "The Pragmatic Engineer" and first.post.id.startswith(f"{PE}/")
    assert ask(svc, "delegate outcomes", source=["MAIN", " main "]).filters["source"] == ["main"]
    # the readable result line names the newsletter first
    assert "(The Pragmatic Engineer; 2025-0" in render_text(pe_only)
    # summaries are filtered too
    sums = ask(svc, "delegate", kind="summaries", source="main", limit=10)
    assert sums.results and {r.post.source for r in sums.results} == {"main"}


# spec: hybrid-search / Filters (Unknown source)
def test_unknown_source_lists_the_valid_ones(cfg):
    build(cfg, two_newsletters())
    with pytest.raises(SearchError, match=f"unknown source nope; valid sources: main, {PE}"):
        ask(service(cfg), "team", source="nope")
    with pytest.raises(SearchError, match="source must be a newsletter id"):
        SearchRequest.create("team", source=[1])


# spec: mcp-server / Search tool (Source search): one line per newsletter
def test_overview_and_tool_description_list_each_newsletter(cfg):
    build(cfg, two_newsletters())
    overview = service(cfg).overview()
    assert [(s.id, s.name, s.posts) for s in overview.sources] == [
        ("main", "main", 2),
        (PE, "The Pragmatic Engineer", 2),
    ]
    text = tool_description(overview)
    assert "A personal archive of 4 posts from 2 newsletters" in text
    assert "- main (main): 2 posts, 2025-01-01 to 2025-02-01; categories: Hiring, Leadership" in text
    assert (
        f"- The Pragmatic Engineer ({PE}): 2 posts, 2025-03-01 to 2025-04-01; categories: Deepdives, Leadership"
        in text
    )
    assert "source (one or more newsletter ids" in text


# spec: search-indexing / Boilerplate removal, per newsletter
def distinct(slug, seed, extra, source="main", day=1):
    """A post whose own text is unique (random filler), with `extra` paragraphs appended."""
    r = record(slug, seed, intro=False, sizes=(80,))
    r["sections"][-1]["text"] += "".join(f"\n\n{p}" for p in extra)
    r["text"] = "\n\n".join(s["text"] for s in r["sections"])
    r["published_date"] = f"2025-0{day}-01"
    return as_source(r, source) if source != "main" else r


def test_boilerplate_is_found_per_newsletter(cfg):
    cfg = replace(cfg, search=replace(cfg.search, boilerplate_min_posts=3))
    shared, pe_twice = (
        REPEATED,
        "Paid subscribers get the full deep dive every Thursday, with all the charts.",
    )
    main = [distinct(f"m{i}", i, [shared]) for i in range(3)]
    pe = [distinct(f"p{i}", 10 + i, [shared, pe_twice] if i < 2 else [shared], PE, 2) for i in range(3)]
    report = build(cfg, {"main": main, PE: pe})
    assert not report.failed
    assert report.boilerplate_paragraphs == 6  # the shared line in all 6 posts; pe_twice is in only 2
    with db.connect(cfg, "reader") as conn:
        gen = db.active_generation(conn)
        bodies = dict(
            conn.execute(
                f"SELECT post_id, string_agg(body, ' ') FROM {gen.schema}.passages "
                "WHERE kind = 'passage' GROUP BY post_id"
            ).fetchall()
        )
        stored = conn.execute(f"SELECT cluster_id, post_count FROM {gen.schema}.boilerplate").fetchall()
    assert all("forward it to a colleague" not in body for body in bodies.values())
    assert all("full deep dive" in bodies[f"{PE}/p{i}"] for i in range(2))  # 2 posts: not boilerplate
    assert stored == [(1, 3)]  # one row for the line both newsletters repeat (each in 3 posts)


def test_a_newsletter_indexed_after_a_search_is_known_at_once(cfg):
    news = two_newsletters()
    build(cfg, {"main": news["main"]})
    svc = service(cfg)
    assert ask(svc, "delegate", source="main").results  # fills the cached list of newsletters
    build(cfg, news)  # the weekly run adds the second newsletter to the same index
    assert {r.post.source for r in ask(svc, "delegate", source=PE).results} == {PE}


def test_a_blank_source_is_an_error_not_a_search_of_everything():
    for blank in (" ", [" "], ["main", ""]):
        with pytest.raises(SearchError, match="not blank"):
            SearchRequest.create("team", source=blank)


def test_every_row_carries_its_source(cfg):
    build(cfg, two_newsletters())
    with db.connect(cfg, "reader") as conn:
        gen = db.active_generation(conn)
        posts = conn.execute(f"SELECT post_id, source FROM {gen.schema}.posts ORDER BY post_id").fetchall()
        passages = conn.execute(
            f"SELECT DISTINCT split_part(post_id, '/', 1) = source FROM {gen.schema}.passages"
        ).fetchall()
    assert [s for _, s in posts] == ["main", "main", PE, PE]
    assert passages == [(True,)]


# -- every newsletter represented (spec hybrid-search / Result content) ----------------------


class ScriptedReranker:
    """Scores 0.9 for the main newsletter's passages and `pe_score` for the other's (its post is
    titled "Post pe-1", and every passage's re-rank text starts with the title)."""

    def __init__(self, pe_score):
        self.pe_score = pe_score

    def score(self, query, documents):
        return [self.pe_score if d.startswith("Post pe-1") else 0.9 for d in documents]


def lopsided():
    """Eight posts in one newsletter and one in the other, all about delegating outcomes."""
    main = [distinct(f"m{i}", i, [f"Delegate outcomes, part {i}."]) for i in range(8)]
    pe = [distinct("pe-1", 20, ["Delegate outcomes omega."], PE, 2)]
    return {"main": main, PE: pe}


def sources_of(resp):
    return [r.post.source for r in resp.results]


# spec: hybrid-search / Result content (Both newsletters relevant)
def test_both_newsletters_shown_when_both_are_relevant(cfg):
    build(cfg, lopsided())
    resp = ask(service(cfg, reranker=ScriptedReranker(0.6)), "delegate outcomes", limit=5, kind="passages")
    assert len(resp.results) == 5 and sources_of(resp).count(PE) == 1
    pe_hit = next(r for r in resp.results if r.post.source == PE)
    assert pe_hit.score == 0.6 and pe_hit.rank == 5  # its own score, placed by rank: nothing disguised


# spec: hybrid-search / Result content (Only one newsletter relevant)
def test_a_weak_result_is_not_forced_in(cfg):
    build(cfg, lopsided())
    resp = ask(service(cfg, reranker=ScriptedReranker(0.1)), "delegate outcomes", limit=5, kind="passages")
    assert sources_of(resp) == ["main"] * 5


# spec: hybrid-search / Result content (Filtered search)
def test_a_source_filter_turns_the_rule_off(cfg):
    build(cfg, lopsided())
    resp = ask(
        service(cfg, reranker=ScriptedReranker(0.6)),
        "delegate outcomes",
        limit=5,
        kind="passages",
        source="main",
    )
    assert sources_of(resp) == ["main"] * 5


def test_without_the_reranker_the_rule_is_off(cfg, monkeypatch):
    import newsletter_archive.search.service as service_module

    def never(*a, **k):
        raise AssertionError("no re-ranker scores: nothing may be forced in")

    monkeypatch.setattr(service_module, "represent_sources", never)
    build(cfg, lopsided())
    resp = ask(service(cfg), "delegate outcomes", limit=3, kind="passages")
    assert not resp.reranked and len(resp.results) == 3


def test_a_newsletter_below_the_rerank_cut_still_gets_its_chance(cfg):
    cfg = replace(cfg, search=replace(cfg.search, rerank_top=2))
    build(cfg, lopsided())
    for pe_score, shown in ((0.6, True), (0.1, False)):
        trace: dict = {}
        svc = service(cfg, reranker=ScriptedReranker(pe_score))
        resp = svc.search(SearchRequest.create("delegate outcomes", limit=2, kind="passages"), trace=trace)
        combined = trace["combined"]
        assert not any(p.startswith(f"{PE}/") for p in combined[:2])  # below the re-rank cut
        assert (PE in sources_of(resp)) is shown and len(resp.results) == 2


def test_one_newsletter_only_is_unchanged(cfg):
    build(cfg, {"main": lopsided()["main"]})
    resp = ask(service(cfg, reranker=ScriptedReranker(0.6)), "delegate outcomes", limit=5, kind="passages")
    assert sources_of(resp) == ["main"] * 5
