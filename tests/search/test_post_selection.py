"""Post-level selection (pure): grouping fused rows by post, the re-rank batch, picking across posts."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from newsletter_archive.search.fusion import (
    Fused,
    PostGroup,
    group_by_post,
    rerank_batch,
    select_across_posts,
)


def post_of(pid: str) -> str:
    return pid.split("#")[0]


def is_summary(pid: str) -> bool:
    return pid.endswith("#summary")


def fused(*items: tuple[str, float]) -> list[Fused]:
    return [Fused(pid, score, ("keyword",), i) for i, (pid, score) in enumerate(items, start=1)]


def test_group_by_post_max_and_sum_top3():
    rows = fused(("a#1", 0.9), ("b#1", 0.8), ("b#2", 0.7), ("b#3", 0.6), ("a#summary", 0.5), ("c#1", 0.4))
    by_max = group_by_post(rows, post_of, is_summary, "max")
    assert [g.post_id for g in by_max] == ["a", "b", "c"]
    a = by_max[0]
    assert a == PostGroup("a", 0.9, ("a#1",), "a#summary", 1)
    by_sum = group_by_post(rows, post_of, is_summary, "sum_top3")
    assert [g.post_id for g in by_sum] == ["b", "a", "c"]  # b: 0.8+0.7+0.6 beats a: 0.9+0.5
    assert by_sum[0].score == pytest.approx(2.1) and by_sum[0].passages == ("b#1", "b#2", "b#3")


def test_ties_go_to_the_post_whose_best_row_comes_first():
    rows = fused(("z#1", 0.5), ("a#1", 0.5))
    assert [g.post_id for g in group_by_post(rows, post_of, is_summary)] == ["z", "a"]
    with pytest.raises(ValueError, match="unknown post score"):
        group_by_post(rows, post_of, is_summary, "mean")


def test_rerank_batch_takes_the_best_passages_and_the_summary():
    groups = group_by_post(
        fused(("a#1", 0.9), ("a#2", 0.8), ("a#3", 0.7), ("a#summary", 0.6), ("b#summary", 0.5)),
        post_of,
        is_summary,
    )
    assert rerank_batch(groups, 2, with_summaries=True) == ["a#1", "a#2", "a#summary", "b#summary"]
    assert rerank_batch(groups, 1, with_summaries=False) == ["a#1"]


# spec: hybrid-search / Result content (Ten relevant posts)
def test_ten_relevant_posts_give_ten_different_posts():
    ranked = [f"p{i}#{n}" for i in range(10) for n in range(3)]  # each post's 3 rows together
    picked = select_across_posts(ranked, post_of, is_summary)[:10]
    assert [post_of(p) for p in picked] == [f"p{i}" for i in range(10)]


# spec: hybrid-search / Result content (Few relevant posts)
def test_few_posts_still_fill_the_limit_within_the_caps():
    ranked = [f"{p}#{n}" for p in "abc" for n in range(5)] + ["a#summary", "b#summary", "c#summary"]
    picked = select_across_posts(ranked, post_of, is_summary)[:10]
    assert len(picked) == 10 and {post_of(p) for p in picked} == {"a", "b", "c"}
    full = select_across_posts(ranked, post_of, is_summary)
    for post in "abc":
        mine = [p for p in full if post_of(p) == post]
        assert sum(not is_summary(p) for p in mine) == 3 and sum(is_summary(p) for p in mine) == 1


# spec: hybrid-search / Result content (Diverse results)
def test_one_post_with_six_of_the_top_ten_gives_up_its_extra_places():
    ranked = ["a#1", "a#2", "b#1", "a#3", "a#4", "c#1", "a#5", "a#6", "d#1", "e#1"]
    picked = select_across_posts(ranked, post_of, is_summary)
    assert picked[:5] == ["a#1", "b#1", "c#1", "d#1", "e#1"]  # each post's best first, in rank order
    assert [p for p in picked if post_of(p) == "a"] == ["a#1", "a#2", "a#3"]


ids = st.lists(
    st.tuples(st.sampled_from("abcdefg"), st.sampled_from(["1", "2", "3", "4", "5", "summary"])).map(
        "#".join
    ),
    unique=True,
    max_size=30,
)


@given(ids, st.integers(1, 3), st.integers(0, 2))
def test_selection_properties(ranked, max_passages, max_summaries):
    picked = select_across_posts(ranked, post_of, is_summary, max_passages, max_summaries)
    assert len(picked) == len(set(picked)) and set(picked) <= set(ranked)
    for post in {post_of(p) for p in ranked}:
        mine = [p for p in picked if post_of(p) == post]
        assert sum(not is_summary(p) for p in mine) <= max_passages
        assert sum(is_summary(p) for p in mine) <= max_summaries
    # every post's first pick comes before any post's second pick
    seen: dict[str, int] = {}
    rounds = []
    for p in picked:
        seen[post_of(p)] = seen.get(post_of(p), 0) + 1
        rounds.append(seen[post_of(p)])
    assert rounds == sorted(rounds)


@given(st.lists(st.tuples(st.sampled_from("abcde"), st.integers(1, 9), st.floats(0, 1)), max_size=25))
def test_grouping_properties(raw):
    items = list({f"{p}#{n}": s for p, n, s in raw}.items())
    groups = group_by_post(fused(*items), post_of, is_summary, "sum_top3")
    assert sorted(g.post_id for g in groups) == sorted({post_of(pid) for pid, _ in items})
    assert [g.score for g in groups] == sorted((g.score for g in groups), reverse=True)
    assert sum(len(g.passages) for g in groups) == len(items)
