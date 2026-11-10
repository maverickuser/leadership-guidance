"""LLM judge: pool, grading, saved grades and the review (fake Gemini, no database)."""

from __future__ import annotations

import json

import pytest

from newsletter_archive.search import judge
from newsletter_archive.search.evaluate import Outcome, Question, Report
from newsletter_archive.search.gemini import GeminiResponseError
from newsletter_archive.search.judge import (
    Candidate,
    GeminiGrader,
    JudgeError,
    Judgment,
    load_judgments,
    ndcg_at,
    parse_grades,
    pool,
    render,
    review,
    save_judgments,
)

from .fakes import FakeSDK, generate_response
from .test_gemini import make_client

Q1 = Question("q1", "local", "What is a bar raiser?", ("hiring",))
Q2 = Question("q2", "global", "Career advice?", ("career", "gone"))


def outcome(q, summaries, rankings, hits=()):
    return Outcome(q, summaries, 1.0, True, True, {}, (), rankings, hits)


def report():
    return Report(
        [
            outcome(
                Q1, True, {"keyword": ("hiring", "career"), "returned": ("hiring",)}, ("hiring#0", "career#1")
            ),
            outcome(Q1, False, {"semantic": ("delegation",)}, ("delegation#0", "hiring#0")),
            outcome(Q2, True, {"combined": ("career",), "returned": ("delegation", "career")}),
        ],
        unknown_expected={"q2": ["gone"]},
    )


def test_pool_joins_stages_variants_and_expected_without_ranking_order():
    pools = pool(report(), [Q1, Q2])
    assert set(pools["q1"]) == {"hiring", "career", "delegation"}
    assert set(pools["q2"]) == {"career", "delegation"}  # "gone" is not in the index
    assert pools == pool(report(), [Q1, Q2])  # a fixed order, not the search order
    assert judge.hits_by_post(report())["q1"] == ["hiring#0", "career#1", "delegation#0"]


def cand(pid):
    return Candidate(pid, f"Title {pid}", f"Summary {pid}", (f"Passage {pid}",))


def test_render_shows_each_post():
    text = render("Why?", [cand("a"), Candidate("b", "B", "", ())])
    assert text.startswith("Question: Why?")
    assert '<post id="a">\nTitle: Title a\nSummary: Summary a\nPassage: Passage a\n</post>' in text
    assert '<post id="b">\nTitle: B\n</post>' in text


@pytest.mark.parametrize(
    "text, problem",
    [
        ("nope", "not JSON"),
        ('{"a": 1}', "not a list"),
        ('[{"post_id": "a", "grade": 3}]', "left out or misgraded 1"),
        ('[{"post_id": "a", "grade": 3}, {"post_id": "b", "grade": 7}]', "misgraded 1"),
        ('[{"post_id": "a", "grade": true}, "x", {"post_id": "b", "grade": 1}]', "misgraded 1"),
    ],
)
def test_parse_grades_rejects_incomplete_answers(text, problem):
    with pytest.raises(GeminiResponseError, match=problem):
        parse_grades(text, ["a", "b"])


def test_parse_grades_accepts_fenced_json_and_ignores_extra_posts():
    text = '```json\n[{"post_id": "a", "grade": 3}, {"post_id": "b", "grade": 0}, {"post_id": "z", "grade": 2}]\n```'
    assert parse_grades(text, ["a", "b"]) == {"a": 3, "b": 0}


def answer(grades):
    return generate_response(json.dumps([{"post_id": p, "grade": g} for p, g in grades.items()]))


def test_gemini_grader_asks_for_json_and_retries_once():
    sdk = FakeSDK(answers=[generate_response("garbage"), answer({"a": 2, "b": 0})])
    grader = GeminiGrader(make_client(sdk)[0], "judge-model")
    assert grader.grade("Why?", [cand("a"), cand("b")]) == {"a": 2, "b": 0}
    first = sdk.calls[0]
    assert first["model"] == "judge-model" and first["contents"].startswith("Question: Why?")
    assert first["config"].response_mime_type == "application/json" and first["config"].temperature == 0.0
    assert first["config"].thinking_config.thinking_level.value == "LOW"


def test_gemini_grader_gives_up_after_two_bad_answers():
    sdk = FakeSDK(answers=[generate_response("x"), generate_response("[]")])
    with pytest.raises(JudgeError, match="no usable grades after 2 attempts"):
        GeminiGrader(make_client(sdk)[0], "m", thinking_level="").grade("Why?", [cand("a")])
    assert sdk.calls[0]["config"].thinking_config is None


def test_judgments_round_trip_and_bad_files(tmp_path):
    path = tmp_path / "j.yaml"
    assert load_judgments(path) == {}
    saved = {"q1": Judgment("What?", "m", judge.JUDGE_PROMPT_VERSION, {"b": 1, "a": 3})}
    save_judgments(path, saved)
    assert path.read_text().startswith("# LLM relevance grades")
    assert load_judgments(path) == saved
    for bad in ("questions: [\n", "questions: {q1: {model: m}}\n", "questions: [1]\n"):
        path.write_text(bad)
        with pytest.raises(JudgeError, match="unreadable"):
            load_judgments(path)


class FakeGrader:
    model = "m"

    def __init__(self, grades):
        self.grades, self.asked = grades, []

    def grade(self, question, candidates):
        self.asked.append([c.post_id for c in candidates])
        return {c.post_id: self.grades.get(c.post_id, 0) for c in candidates}


def test_judge_grades_only_new_posts_and_regrades_changed_questions(monkeypatch):
    monkeypatch.setattr(judge, "BATCH", 2)
    grader = FakeGrader({"hiring": 3, "career": 2})
    saves = []
    seen = []
    judgments = {
        "q1": Judgment(Q1.question, "m", judge.JUDGE_PROMPT_VERSION, {"hiring": 3}),
        "q2": Judgment("an older wording", "m", judge.JUDGE_PROMPT_VERSION, {"career": 0}),
    }

    def candidates(posts, hits):
        return {p: cand(p) for p in posts}

    graded = judge.judge(
        report(), [Q1, Q2], judgments, candidates, grader, saves.append, lambda q, n: seen.append((q.id, n))
    )
    assert graded == 4 and seen == [("q1", 2), ("q2", 2)] and len(saves) == 2
    assert sorted(grader.asked[0]) == ["career", "delegation"]  # q1: hiring already graded
    assert judgments["q2"].question == Q2.question and judgments["q2"].grades == {
        "career": 2,
        "delegation": 0,
    }
    assert judge.judge(report(), [Q1, Q2], judgments, candidates, grader) == 0  # nothing left to grade


def test_judge_skips_posts_without_content():
    grader = FakeGrader({})
    judgments = {}
    graded = judge.judge(report(), [Q2], judgments, lambda posts, hits: {}, grader)
    assert graded == 0 and grader.asked == [] and judgments["q2"].grades == {}


def test_ndcg():
    grades = {"a": 3, "b": 2, "c": 0}
    assert ndcg_at(["a", "b"], grades) == pytest.approx(1.0)
    assert 0 < ndcg_at(["b", "a"], grades) < 1 and ndcg_at(["x", "c"], grades) == 0.0
    assert ndcg_at(["a"], {"a": 0}) is None


def test_review_suggests_additions_and_doubts():
    judgments = {
        "q1": Judgment(Q1.question, "m", 1, {"hiring": 1, "career": 3, "delegation": 2}),
        "q2": Judgment(Q2.question, "m", 1, {"career": 3, "delegation": 0}),
    }
    verdict = review(report(), [Q1, Q2], judgments, "m")
    r1, r2 = verdict.reviews
    assert r1.add == [("career", 3), ("delegation", 2)] and r1.doubtful == [("hiring", 1)]
    assert r2.add == [] and r2.doubtful == [] and r2.ungraded == ["gone"]
    assert verdict.agreement == 0.5  # career yes, hiring no
    assert set(verdict.ndcg) == {"keyword", "combined", "returned"}  # with-summaries stages that ran
    text = "\n".join(verdict.lines())
    assert "q1: career (3), delegation (2)" in text and "q1: hiring (1)" in text and "50%" in text
    d = verdict.to_dict()
    assert d["questions"]["q1"]["add"] == {"career": 3, "delegation": 2} and d["agreement"] == 0.5


def test_review_when_the_judge_agrees_or_has_nothing():
    judgments = {"q1": Judgment(Q1.question, "m", 1, {"hiring": 3})}
    text = "\n".join(
        review(Report([outcome(Q1, True, {"returned": ("hiring",)})]), [Q1], judgments, "m").lines()
    )
    assert "agrees with every expected list" in text and "returned 1.00" in text
    empty = review(Report(), [Q1], {}, "m")
    assert empty.agreement is None and empty.ndcg == {} and empty.reviews[0].ungraded == ["hiring"]


def test_the_retry_differs_from_the_first_request():
    sdk = FakeSDK(answers=[answer({"a": 1}), answer({"a": 1, "b": 2})])
    GeminiGrader(make_client(sdk)[0], "m").grade("Why?", [cand("a"), cand("b")])
    first, second = (c["config"] for c in sdk.calls)
    assert first.temperature == 0.0 and second.temperature > 0
    assert "previous answer was unusable" in second.system_instruction
    assert "previous answer" not in first.system_instruction
    assert first.max_output_tokens >= 8192  # room for thinking even with no thinking setting


def test_grades_are_saved_after_each_batch_so_a_failure_keeps_them(monkeypatch):
    monkeypatch.setattr(judge, "BATCH", 1)

    class FailsSecond(FakeGrader):
        def grade(self, question, candidates):
            if len(self.asked) == 1:
                raise JudgeError("bad answers")
            return super().grade(question, candidates)

    saved = []
    judgments = {}
    with pytest.raises(JudgeError):
        judge.judge(
            report(),
            [Q1],
            judgments,
            lambda posts, hits: {p: cand(p) for p in posts},
            FailsSecond({}),
            lambda j: saved.append({q: dict(x.grades) for q, x in j.items()}),
        )
    assert len(saved) == 1 and len(saved[0]["q1"]) == 1


def test_review_ignores_grades_for_posts_no_longer_in_the_pool():
    judgments = {"q1": Judgment(Q1.question, "m", 1, {"hiring": 3, "removed-post": 3})}
    verdict = review(report(), [Q1], judgments, "m")
    assert verdict.reviews[0].add == [] and verdict.reviews[0].graded == 1


def test_no_grades_is_not_reported_as_agreement():
    text = "\n".join(review(report(), [Q1], {}, "m").lines())
    assert "no grades yet" in text and "agrees" not in text
