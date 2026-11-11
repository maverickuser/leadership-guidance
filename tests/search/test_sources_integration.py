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
from .archive import small_config
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
def test_boilerplate_is_found_per_newsletter(cfg):
    cfg = replace(cfg, search=replace(cfg.search, boilerplate_min_posts=3))
    main = [
        post(f"m{i}", i, [(None, FILLER + f" Point {i} about teams.\n\n{REPEATED}")], day=1) for i in range(3)
    ]
    pe = [
        as_source(post(f"p{i}", 10 + i, [(None, FILLER + f" Note {i}.\n\n{REPEATED}")], day=2), PE)
        for i in range(2)
    ]
    report = build(cfg, {"main": main, PE: pe})
    assert report.boilerplate_paragraphs == 3  # in 3 main posts; only 2 in the other newsletter
    with db.connect(cfg, "reader") as conn:
        gen = db.active_generation(conn)
        bodies = dict(
            conn.execute(
                f"SELECT post_id, string_agg(body, ' ') FROM {gen.schema}.passages WHERE kind = 'passage' GROUP BY post_id"
            ).fetchall()
        )
        stored = conn.execute(f"SELECT count(*) FROM {gen.schema}.boilerplate").fetchone()[0]
    assert all("forward it to a colleague" not in bodies[f"main/m{i}"] for i in range(3))
    assert all("forward it to a colleague" in bodies[f"{PE}/p{i}"] for i in range(2))
    assert stored >= 1


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
