from datetime import date

import pytest

from newsletter_archive.search import keyword
from newsletter_archive.search.filters import Filters, filter_sql
from newsletter_archive.search.fusion import diversify, rrf
from newsletter_archive.search.keyword import build_keyword_query


def test_keyword_query_words_phrases_and_folding():
    q = build_keyword_query('How do I give feedback to a "bar raiser" at the café?')
    assert q.terms == "give feedback bar raiser cafe" and q.phrases == ("bar raiser",)
    assert build_keyword_query("what is it").terms == "what is it"  # only stop words: keep them
    assert build_keyword_query("   ").empty and build_keyword_query("?!*").empty
    assert build_keyword_query("don't 'quote'").terms == "quote"  # split like the index: don + t
    assert build_keyword_query('"!!"').phrases == ()


# spec: hybrid-search / Safe keyword queries / Punctuation and operators
def test_operators_are_plain_words():
    q = build_keyword_query('what\'s "bar raiser" AND (hiring)?')
    assert (
        q.phrases == ("bar raiser",) and q.terms == "bar raiser hiring"
    )  # "AND" is just the stop word "and"


def test_filter_sql():
    _clause, params = filter_sql(Filters(("Leadership",), date(2025, 1, 1), date(2025, 12, 31), "summaries"))
    assert params == [["Leadership"], date(2025, 1, 1), date(2025, 12, 31), "summary"]
    assert filter_sql(Filters())[1] == []


# spec: hybrid-search / Hybrid retrieval: found by both ranks above found by one
def test_rrf_rewards_agreement_and_is_deterministic():
    fused = rrf({"keyword": ["a", "b", "c"], "semantic": ["c", "d", "a"]})
    assert [f.passage_id for f in fused][:2] == ["a", "c"]
    by_id = {f.passage_id: f for f in fused}
    assert by_id["a"].found_by == ("keyword", "semantic") and by_id["b"].found_by == ("keyword",)
    assert by_id["d"].best_rank == 2
    assert rrf({"keyword": ["x", "y"], "semantic": ["y", "x"]})[0].passage_id == "x"  # tie: id
    assert rrf({}) == []


# spec: hybrid-search / Result content / Diverse results
def test_diversify_caps_passages_and_summaries_per_post():
    ranked = ["p#0", "p#1", "p#summary", "p#2", "p#3", "q#0", "p#4"]
    out = diversify(ranked, lambda x: x.split("#")[0], lambda x: x.endswith("#summary"))
    assert out == ["p#0", "p#1", "p#summary", "p#2", "q#0"]


@pytest.mark.parametrize("bad", [0, -1.0, float("inf"), float("nan")])
def test_boost_must_be_a_positive_number(bad):
    with pytest.raises(ValueError, match="positive number"):
        keyword._boost(bad)


def test_contractions_and_typographic_quotes():
    q = build_keyword_query("What’s the best way to delegate? It’s about “bar raiser” you’re told")
    assert q.terms == "best way delegate bar raiser told" and q.phrases == ("bar raiser",)
    assert build_keyword_query("‘single’ quotes").terms == "single quotes"


def test_kind_must_be_known():
    with pytest.raises(ValueError, match="kind must be one of all, passages, summaries"):
        Filters(kind="passage")


def test_rrf_counts_a_repeated_id_once_at_its_first_rank():
    fused = {f.passage_id: f for f in rrf({"keyword": ["a", "b", "a"], "semantic": ["b"]})}
    assert fused["a"].found_by == ("keyword",) and fused["a"].score == pytest.approx(1 / 61)
    assert fused["b"].found_by == ("keyword", "semantic")
    assert [f.passage_id for f in rrf({"keyword": ["a", "a", "c"]})] == ["a", "c"]
    assert {f.passage_id: f.best_rank for f in rrf({"keyword": ["a", "a", "c"]})}["c"] == 3
