"""represent_sources: every newsletter with a qualifying result gets a place (pure)."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from newsletter_archive.search.fusion import represent_sources


def src(pid: str) -> str:
    return pid.split("/")[0]


def run(ranked, limit, ok=lambda p: True, sources=("a", "b")):
    return represent_sources(ranked, limit, src, ok, sources)


def test_missing_newsletter_gets_its_best_result_in_place_of_the_lowest_repeat():
    ranked = ["a/1", "a/2", "a/3", "b/1", "b/2"]
    assert run(ranked, 3) == ["a/1", "a/2", "b/1"]  # kept in original order; b/1 shows its own rank


def test_already_represented_or_nothing_qualifies_changes_nothing():
    assert run(["a/1", "b/1", "a/2", "b/2"], 2) == ["a/1", "b/1"]
    assert run(["a/1", "a/2", "b/1"], 2, ok=lambda p: False) == ["a/1", "a/2"]
    assert run(["a/1", "a/2"], 2) == ["a/1", "a/2"]  # b has no results at all


def test_no_victim_when_every_newsletter_in_the_top_has_one_result():
    ranked = ["a/1", "b/1", "c/1", "c/2"]
    assert run(ranked, 2, sources=("a", "b", "c")) == ["a/1", "b/1"]


def test_three_newsletters_each_get_a_place():
    ranked = ["a/1", "a/2", "a/3", "a/4", "b/1", "c/1"]
    assert run(ranked, 4, sources=("a", "b", "c")) == ["a/1", "a/2", "b/1", "c/1"]


ids = st.lists(
    st.tuples(st.sampled_from("abc"), st.integers(0, 30)).map(lambda t: f"{t[0]}/{t[1]}"),
    unique=True,
    max_size=25,
)


@given(ids, st.integers(1, 12), st.sets(st.sampled_from(["a/1", "b/1", "c/1", "b/2", "c/5"])))
def test_properties(ranked, limit, bad):
    def ok(p):
        return p not in bad

    out = represent_sources(ranked, limit, src, ok, ("a", "b", "c"))
    top = ranked[:limit]
    assert len(out) == len(top) and len(set(out)) == len(out)  # same size, no duplicates
    assert out == sorted(out, key=ranked.index)  # original order kept
    for p in set(out) - set(top):  # anything swapped in qualifies and was missing from the top
        assert ok(p) and src(p) not in {src(q) for q in top}
    for s in {src(p) for p in top}:  # a newsletter in the top never loses its last place
        assert any(src(p) == s for p in out)
