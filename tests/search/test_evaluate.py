"""Evaluation: question file, metrics and report (no database)."""

from __future__ import annotations

import pytest

from newsletter_archive.search.evaluate import (
    EvalFileError,
    Outcome,
    Question,
    Report,
    load_questions,
    memoized,
    post_ranking,
    recall_at,
    reciprocal_rank,
)

GOOD = """questions:
  - id: a
    type: local
    question: What is the trust equation?
    expected: [trust, trust]
  - id: b
    type: global
    question: Hiring advice?
    expected: [h1, h2]
"""


def test_load_questions(tmp_path):
    path = tmp_path / "q.yaml"
    path.write_text(GOOD)
    qs = load_questions(path)
    assert qs == [
        Question("a", "local", "What is the trust equation?", ("trust",)),
        Question("b", "global", "Hiring advice?", ("h1", "h2")),
    ]


@pytest.mark.parametrize(
    "text, message",
    [
        ("questions: [\n", "not valid YAML"),
        ("questions: []\n", "non-empty `questions:`"),
        ("- just a list\n", "non-empty `questions:`"),
        ("questions:\n  - nope\n", "must be a mapping"),
        ("questions:\n  - {type: local, question: q, expected: [x]}\n", "needs an id"),
        ("questions:\n  - {id: a, type: odd, question: q, expected: [x]}\n", "type must be one of"),
        ("questions:\n  - {id: a, type: local, expected: [x]}\n", "needs a question"),
        ("questions:\n  - {id: a, type: local, question: q, expected: []}\n", "non-empty list of post ids"),
        (
            "questions:\n  - {id: a, type: local, question: " + "x" * 1001 + ", expected: [x]}\n",
            "over 1000 characters",
        ),
        (
            "questions:\n  - {id: a, type: local, question: q, expected: [x]}\n  - {id: a, type: local, question: q, expected: [x]}\n",
            "duplicate id",
        ),
    ],
)
def test_bad_question_files(tmp_path, text, message):
    path = tmp_path / "q.yaml"
    path.write_text(text)
    with pytest.raises(EvalFileError, match=message):
        load_questions(path)


def test_missing_file(tmp_path):
    with pytest.raises(EvalFileError, match="no evaluation file"):
        load_questions(tmp_path / "nope.yaml")


def test_metrics_are_post_level():
    post_of = {"a#0": "a", "a#1": "a", "b#summary": "b", "c#0": "c"}
    ranking = post_ranking(["a#1", "a#0", "x#9", "b#summary", "c#0"], post_of)
    assert ranking == ["a", "b", "c"]
    assert recall_at(ranking, ["b", "z"]) == 0.5 and recall_at(ranking, ["c"], k=2) == 0.0
    assert reciprocal_rank(ranking, ["c", "b"]) == 0.5 and reciprocal_rank(ranking, ["z"]) == 0.0


def outcome(qid, qtype, summaries, scores, missing=(), semantic=True, reranked=True, seconds=1.0):
    return Outcome(Question(qid, qtype, "q", ("p",)), summaries, seconds, semantic, reranked, scores, missing)


def test_report_tables_rules_and_json():
    full = {"keyword": (0.5, 0.5), "semantic": (1.0, 1.0), "combined": (1.0, 0.5), "reranked": (1.0, 1.0)}
    report = Report(
        [
            outcome("q1", "local", True, full),
            outcome(
                "q2",
                "intent",
                True,
                {**full, "combined": (0.5, 0.5), "reranked": (0.5, 0.25)},
                missing=("p",),
                seconds=3.0,
            ),
            outcome("q1", "local", False, {"keyword": (0.0, 0.0)}, semantic=False, reranked=False),
        ],
        generation=2,
        model="m",
        rerank_top=20,
        unknown_expected={"q2": ["gone"]},
    )
    assert report.score("reranked", True) == (0.75, 0.625, 2)
    assert report.score("reranked", False) is None and report.score("keyword", True, "intent") == (
        0.5,
        0.5,
        1,
    )
    assert report.best_stage() == "reranked" and report.needs_contextual_retrieval() is True
    text = "\n".join(report.lines())
    assert "generation 2 (m), rerank_top 20" in text and "reranked" in text and "n/a" in text
    assert "latency per search: median 1.0s, p90 3.0s" in text
    assert "warning: 1 searches ran without semantic search or re-ranking" in text
    assert "contextual retrieval: needed" in text and "q2 (intent): p" in text
    assert "warning: q2 expects posts that aren't in the index: gone" in text
    d = report.to_dict()
    assert d["overall"]["with_summaries"]["reranked"] == {"recall_at_10": 0.75, "mrr": 0.625, "n": 2}
    assert d["overall"]["passages_only"]["semantic"] is None and d["contextual_retrieval_needed"] is True
    assert d["questions"][1]["missing"] == ["p"] and d["unknown_expected"] == {"q2": ["gone"]}


def test_contextual_rule_looks_at_retrieval_not_reranking():
    scores = {"keyword": (0.5, 0.5), "semantic": (1.0, 1.0), "combined": (1.0, 1.0), "reranked": (0.5, 0.5)}
    assert Report([outcome("a", "local", True, scores)]).needs_contextual_retrieval() is False
    keyword_only = {"keyword": (0.2, 0.2)}  # semantic search never ran: nothing to judge
    assert Report([outcome("a", "local", True, keyword_only)]).needs_contextual_retrieval() is None


def test_p90_is_the_nearest_rank():
    report = Report([outcome(f"q{i}", "local", True, {}, seconds=float(i)) for i in range(1, 11)])
    assert "median 5.5s, p90 9.0s" in "\n".join(report.lines())


def test_memoized_embedder_embeds_each_text_once_per_model():
    from .fakes import FakeEmbedder

    made = []

    def factory(model, dim):
        made.append(FakeEmbedder(model=model, dim=dim))
        return made[-1]

    memo = memoized(factory)
    first = memo("m", 4).embed(["a", "b"])
    again = memo("m", 4).embed(["b", "a"])
    other = memo("n", 4).embed(["a"])
    assert [b for e in made for b in e.batches] == [["a", "b"], ["a"]]
    assert (first[0] == again[1]).all() and memo("m", 4).model == "m" and len(other) == 1


def test_contextual_rule_passes_and_handles_no_local_questions():
    good = {"keyword": (1.0, 1.0), "combined": (1.0, 1.0)}
    assert Report([outcome("a", "local", True, good)]).needs_contextual_retrieval() is False
    assert Report([outcome("a", "global", True, good)]).needs_contextual_retrieval() is None
    assert "contextual retrieval" not in "\n".join(Report([outcome("a", "global", True, good)]).lines())
    assert Report().best_stage() == "keyword"


def cross(qid, expected, returned, expected_scores=(), summaries=True):
    from newsletter_archive.search.evaluate import RETURNED

    return Outcome(
        Question(qid, "global", "q", tuple(expected)),
        summaries,
        1.0,
        True,
        True,
        {},
        (),
        {RETURNED: tuple(returned)},
        (),
        tuple(expected_scores),
    )


# lld §9.4: the eval reports source coverage and suggests source_min_score
def test_source_coverage_and_suggested_threshold():
    report = Report(
        [
            cross("both-shown", ["main/a", "pe/b"], ["main/a", "pe/b"], [0.91, 0.42]),
            cross("pe-missing", ["main/a", "pe/c"], ["main/a", "main/x"], [0.88]),
            cross("one-newsletter", ["main/a"], ["main/a"], [0.97]),  # not a cross-newsletter question
            cross("passages-only", ["main/a", "pe/b"], [], summaries=False),  # summaries variant only
        ]
    )
    assert report.source_coverage() == (0.5, 2)
    assert report.suggested_source_min_score() == 0.40  # 0.42 rounded down to 0.05
    text = "\n".join(report.lines())
    assert "source coverage: 0.50 of 2 cross-newsletter questions" in text
    assert "source_min_score suggested by this run: 0.40" in text


def test_no_cross_newsletter_questions_or_reranking_reports_nothing():
    report = Report([cross("q", ["main/a"], ["main/a"])])
    assert report.source_coverage() is None and report.suggested_source_min_score() is None
    text = "\n".join(report.lines())
    assert "source coverage" not in text and "suggested" not in text
