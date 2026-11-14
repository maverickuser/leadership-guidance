"""`selection: posts` on real Postgres: the spec scenarios for choosing posts before re-ranking."""

from __future__ import annotations

from dataclasses import replace

import pytest

from newsletter_archive.search.rerank import FakeReranker
from newsletter_archive.search.service import SearchRequest

from .test_service_integration import service
from .test_sources_integration import PE, ScriptedReranker, build, distinct, lopsided

pytestmark = pytest.mark.db

PARTS = " ".join(f"Delegate outcomes to the team, step {i}, with clear ownership." for i in range(12))


def one_long_and_nine_short():
    long = distinct("long", 1, [PARTS] * 6)  # many passages that all match
    short = [
        distinct(f"s{i}", 10 + i, [f"Delegate outcomes with clear ownership, note {i}."]) for i in range(9)
    ]
    return {"main": [long, *short]}


@pytest.fixture
def cfg(db_config):
    from newsletter_archive.search.keyword import clear_category_cache

    from .archive import small_config

    clear_category_cache()
    return small_config(db_config)


def mode(cfg, selection, **kw):
    return replace(cfg, search=replace(cfg.search, selection=selection, **kw))


def ask(cfg, reranker, query="delegate outcomes with clear ownership", trace=None, **kw):
    return service(cfg, reranker=reranker).search(SearchRequest.create(query, **kw), trace=trace)


def posts_of(resp):
    return [r.post.id for r in resp.results]


# spec: hybrid-search / Re-ranking (One post with many matching passages)
def test_one_long_post_no_longer_fills_the_re_ranker(cfg):
    build(cfg, one_long_and_nine_short())
    rows_trace, posts_trace = {}, {}
    rows = ask(mode(cfg, "rows", rerank_top=10), FakeReranker(), trace=rows_trace, limit=10, kind="passages")
    posts = ask(
        mode(cfg, "posts", rerank_posts=10, rerank_rows_per_post=2), FakeReranker(), trace=posts_trace,
        limit=10, kind="passages",
    )  # fmt: skip
    batch_posts = {posts_trace["post_of"][p] for p in posts_trace["batch"]}
    assert len(batch_posts) == 10  # the re-ranker sees all 10 posts
    assert sum(1 for p in posts_trace["batch"] if posts_trace["post_of"][p] == "main/long") <= 2
    assert len(set(posts_of(posts))) == 10 > len(set(posts_of(rows)))


# spec: hybrid-search / Re-ranking (Relevant posts further down)
def test_a_post_whose_best_row_is_far_down_is_re_ranked(cfg):
    build(cfg, one_long_and_nine_short())
    trace = {}
    resp = ask(mode(cfg, "posts", rerank_posts=10), FakeReranker(), trace=trace, limit=10, kind="passages")
    combined_posts = [trace["post_of"][p] for p in trace["combined"]]
    far = [post for post in dict.fromkeys(combined_posts) if combined_posts.index(post) >= 10]
    assert far, "the fixture should put some posts' first row below place 10"
    assert set(far) <= {trace["post_of"][p] for p in trace["batch"]}
    assert set(far) <= set(posts_of(resp))


# spec: hybrid-search / Result content (Few relevant posts)
def test_few_posts_fill_the_limit_from_those_posts(cfg):
    build(cfg, {"main": [distinct(f"f{i}", i, [PARTS] * 4) for i in range(3)]})
    resp = ask(mode(cfg, "posts", rerank_rows_per_post=5), FakeReranker(), limit=10, kind="all")
    assert len(resp.results) == 10 and len(set(posts_of(resp))) == 3
    for post in set(posts_of(resp)):
        kinds = [r.kind for r in resp.results if r.post.id == post]
        assert kinds.count("passage") <= 3 and kinds.count("summary") <= 1


# spec: hybrid-search / Re-ranking (Re-ranker unavailable)
def test_without_the_re_ranker_the_kept_posts_come_in_combined_order(cfg):
    build(cfg, one_long_and_nine_short())
    trace = {}
    resp = ask(
        mode(cfg, "posts", rerank_posts=4), FakeReranker(fail=RuntimeError("down")), trace=trace, limit=10
    )
    assert not resp.reranked and any("not re-ranked" in n for n in resp.notes)
    assert {r.id for r in resp.results} <= set(trace["batch"])
    assert len(set(posts_of(resp))) == 4


# spec: hybrid-search / Every source represented, at post level
def test_a_missing_newsletter_gets_its_best_post_re_ranked(cfg):
    two = replace(cfg, sources=(cfg.sources[0], replace(cfg.sources[0], id=PE, name="The Pragmatic Engineer",
                  base_url="https://pe.example.org", root=cfg.output_dir / PE)))  # fmt: skip
    build(two, lopsided())
    for pe_score, shown in ((0.6, True), (0.1, False)):
        trace = {}
        resp = ask(
            mode(two, "posts", rerank_posts=2), ScriptedReranker(pe_score), "delegate outcomes",
            trace=trace, limit=5, kind="passages",
        )  # fmt: skip
        assert any(trace["post_of"][p].startswith(f"{PE}/") for p in trace["batch"])  # always scored
        assert (PE in {r.post.source for r in resp.results}) is shown  # kept only if it qualifies


def test_rows_mode_is_unchanged_by_the_new_settings(cfg):
    build(cfg, one_long_and_nine_short())
    a = ask(mode(cfg, "rows"), FakeReranker(), limit=10, kind="passages")
    b = ask(mode(cfg, "rows", rerank_posts=3, rerank_rows_per_post=1, post_score="sum_top3"), FakeReranker(),
            limit=10, kind="passages")  # fmt: skip
    assert [r.id for r in a.results] == [r.id for r in b.results]


def test_sum_top3_favours_posts_with_several_matching_parts(cfg):
    build(cfg, one_long_and_nine_short())
    trace = {}
    ask(mode(cfg, "posts", post_score="sum_top3", rerank_posts=1), FakeReranker(), trace=trace, limit=5,
        kind="passages")  # fmt: skip
    assert {trace["post_of"][p] for p in trace["batch"]} == {"main/long"}
