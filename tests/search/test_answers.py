"""Answer evaluation, the parts that need no database: citations, metrics, labels, the file."""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from newsletter_archive.search.answers import (
    ANSWER_PROMPT_VERSION,
    AnswerError,
    AnswerRecord,
    AnswerRun,
    Citations,
    answer_key,
    citation_precision,
    citations,
    load_run,
    normalize_url,
    post_coverage,
    relevant_posts,
    save_run,
    saved_answers,
)
from newsletter_archive.search.evaluate import Question

URLS = {
    "https://www.example.com/hiring": "main/hiring",
    "https://www.example.com/career": "main/career",
}


def test_citations_map_links_to_retrieved_posts():
    answer = (
        "See [Hiring](https://WWW.example.com/hiring/?utm_source=x) and [again](https://www.example.com/hiring#a). "
        "Also [Career](https://www.example.com/career), [Elsewhere](https://other.example.org/p/1) and a bare "
        "https://www.example.com/career mention."
    )
    c = citations(answer, URLS)
    assert c.posts == ("main/hiring", "main/career")  # each post once, in order of first citation
    assert c.unknown == ("https://other.example.org/p/1",)
    assert c.total == 3 and citation_precision(c) == pytest.approx(2 / 3)


def test_metrics_without_citations_or_relevant_posts():
    empty = citations("No links at all.", URLS)
    assert empty == Citations((), ()) and citation_precision(empty) is None
    assert post_coverage(empty, []) is None
    c = Citations(("main/hiring",), ())
    assert post_coverage(c, ["main/hiring", "main/career", "main/x"]) == pytest.approx(1 / 3)


# spec: answer-evaluation / Grade each answer (Narrow answer)
def test_narrow_answer_coverage_is_3_of_8():
    relevant = [f"main/p{i}" for i in range(8)]
    c = Citations(("main/p0", "main/p3", "main/p5", "main/other"), ())
    assert post_coverage(c, relevant) == 3 / 8


def test_relevant_posts_are_expected_plus_judge_grades_2_and_3():
    q = Question("q", "global", "?", ("main/a",))
    assert relevant_posts(q, {"main/a": 0, "main/b": 2, "main/c": 3, "main/d": 1}) == {
        "main/a",
        "main/b",
        "main/c",
    }


segment = st.text("abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=12)


@given(segment, segment, st.booleans(), st.booleans(), st.sampled_from(["", "?r=1", "?utm_source=a&b=2"]))
def test_normalize_url_ignores_case_slash_query_and_fragment(host, path, upper, slash, query):
    base = f"https://{host}.example.com/p/{path}"
    variant = base.upper().replace("/P/", "/p/").replace(path.upper(), path) if upper else base
    variant += ("/" if slash else "") + query + "#frag"
    assert normalize_url(variant) == normalize_url(base) == f"https://{host}.example.com/p/{path}"


def test_answer_key_changes_with_what_the_answer_depends_on():
    k = answer_key("q", "m", "low", ["b", "a"])
    assert k == answer_key("q", "m", "low", ["a", "b"])  # result order doesn't matter
    assert len({k, answer_key("q2", "m", "low", ["a", "b"]), answer_key("q", "m2", "low", ["a", "b"]),
                answer_key("q", "m", "high", ["a", "b"]), answer_key("q", "m", "low", ["a"])}) == 5  # fmt: skip


def record(qid, key="k1", answer="text"):
    return AnswerRecord(
        qid, "question?", key, "m", ANSWER_PROMPT_VERSION, ["r1"], {"r1": "main/a"}, {}, answer
    )


# spec: answer-evaluation / Saved answers and grades
def test_runs_are_saved_in_the_data_folder_and_reused_across_labels(tmp_path):
    run = AnswerRun("baseline", {"q1": record("q1"), "q2": record("q2", "k2", None)})
    save_run(tmp_path, run)
    path = tmp_path / "eval-answers" / "baseline.json"
    data = json.loads(path.read_text())
    assert data["schema_version"] == 1 and len(data["records"]) == 2
    assert load_run(tmp_path, "baseline").records["q1"].answer == "text"
    assert set(saved_answers(tmp_path)) == {"k1"}  # unanswered records are not reusable
    assert load_run(tmp_path, "other").records == {}
    assert saved_answers(tmp_path / "nowhere") == {}


@pytest.mark.parametrize("label", ["", "Baseline", "../x", "a b", "x" * 65])
def test_bad_labels_are_refused(tmp_path, label):
    with pytest.raises(AnswerError, match="label"):
        load_run(tmp_path, label)


@pytest.mark.parametrize(
    "content", ["{ broken", '{"schema_version": 9, "records": []}', '{"schema_version": 1}']
)
def test_an_unreadable_answers_file_says_what_to_do(tmp_path, content):
    (tmp_path / "eval-answers").mkdir()
    (tmp_path / "eval-answers" / "baseline.json").write_text(content)
    with pytest.raises(AnswerError, match="move it aside and run again"):
        load_run(tmp_path, "baseline")


def test_gemini_answerer_sends_the_prompt_with_the_answer_instruction():
    from newsletter_archive.search.answers import ANSWER_INSTRUCTION, GeminiAnswerer
    from newsletter_archive.search.summaries import SummaryResponseError

    from .fakes import FakeSDK, generate_response
    from .test_gemini import make_client

    sdk = FakeSDK(answers=[generate_response("An answer [P](https://x.example/p).")])
    answerer = GeminiAnswerer(make_client(sdk)[0], "answer-model", "low")
    assert answerer.answer("Question: why?") == "An answer [P](https://x.example/p)."
    call = sdk.calls[0]
    assert call["model"] == "answer-model" and call["contents"] == "Question: why?"
    assert call["config"].system_instruction == ANSWER_INSTRUCTION and call["config"].temperature == 0.0
    cut = FakeSDK(answers=[generate_response("half", finish="MAX_TOKENS")] * 3)
    with pytest.raises(SummaryResponseError, match="stopped early"):
        GeminiAnswerer(make_client(cut)[0], "answer-model").answer("Question: why?")


@pytest.mark.parametrize(
    "answer, url",
    [
        ("[t](https://x.example/p/b_(c))", "https://x.example/p/b_(c)"),
        ("[t](<https://x.example/p/a>)", "https://x.example/p/a"),
        ('[t](https://x.example/p/a "a title")', "https://x.example/p/a"),
        ("[t]( https://x.example/p/a )", "https://x.example/p/a"),
        ("[[x] y](https://x.example/p/a)", "https://x.example/p/a"),
        ("see [t][1].\n\n[1]: https://x.example/p/a", "https://x.example/p/a"),
        ("[t](https://x.example:443/p/a)", "https://x.example/p/a"),
    ],
)
def test_every_markdown_link_form_counts(answer, url):
    from newsletter_archive.search.answers import link_urls

    found = link_urls(answer)
    assert len(found) == 1 and normalize_url(found[0]) == normalize_url(url)
    assert citations(answer, {normalize_url(url): "main/a"}).posts == ("main/a",)


def test_links_that_are_not_web_links_are_ignored():
    from newsletter_archive.search.answers import link_urls

    assert link_urls("[mail](mailto:a@b.c) [rel](/p/a) [unclosed](https://x.example/p/a") == [
        "https://x.example/p/a"
    ]
    assert link_urls("text](https://x.example/p/x) no opening bracket but still a target") == [
        "https://x.example/p/x"
    ]


def test_a_stray_or_broken_file_does_not_stop_reuse_from_other_runs(tmp_path):
    save_run(tmp_path, AnswerRun("baseline", {"q1": record("q1")}))
    (tmp_path / "eval-answers" / "My Notes.json").write_text("{}")
    (tmp_path / "eval-answers" / "broken.json").write_text("[]")
    assert set(saved_answers(tmp_path)) == {"k1"}
    with pytest.raises(AnswerError, match="move it aside"):
        load_run(tmp_path, "broken")  # a JSON list, not a run


# -- the answer judge, estimate and report (PR 3) -------------------------------------------------


def test_parse_judgment_checks_the_json():
    from newsletter_archive.search.answers import parse_judgment
    from newsletter_archive.search.gemini import GeminiResponseError

    good = '{"claims": [{"claim": "A", "post_id": "main/a", "supported": true}, {"claim": "B", "post_id": "x/zz", "supported": false}], "overall": 2}'
    j = parse_judgment(good, ["main/a"])
    assert j == {
        "claims": [
            {"claim": "A", "post_id": "main/a", "supported": True},
            {"claim": "B", "post_id": "", "supported": False},  # a post the answer wasn't given
        ],
        "overall": 2,
    }
    assert parse_judgment("```json\n" + good + "\n```", ["main/a"])["overall"] == 2
    for bad in ["nope", '{"claims": [], "overall": 4}', '{"claims": [], "overall": true}',
                '{"claims": [{"claim": "A", "supported": "yes"}], "overall": 1}', '{"overall": 1}', "[]"]:  # fmt: skip
        with pytest.raises(GeminiResponseError, match="unusable judgment"):
            parse_judgment(bad, ["main/a"])


def test_grade_record_scores():
    from newsletter_archive.search.answers import grade_record

    r = record("q1")
    r.answer = "See [A](https://x.example/a) and [Z](https://elsewhere.example/z)."
    r.url_to_post = {"https://x.example/a": "main/a"}
    judgment = {
        "claims": [
            {"claim": "1", "post_id": "main/a", "supported": True},
            {"claim": "2", "post_id": "main/a", "supported": False},
            {"claim": "3", "post_id": "", "supported": True},  # uncited: counts as unsupported
        ],
        "overall": 2,
    }
    g = grade_record(r, judgment, {"main/a", "main/b"}, "judge-m")
    assert g["faithfulness"] == pytest.approx(1 / 3) and g["citation_precision"] == 0.5
    assert g["post_coverage"] == 0.5 and g["overall"] == 2 and g["judge_model"] == "judge-m"
    assert grade_record(r, {"claims": [], "overall": 0}, set(), "m")["faithfulness"] is None


class FakeJudge:
    model = "fake-judge"

    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def judge(self, question, answer, results_text, post_ids):
        self.calls.append(question)
        if self.fail_on and self.fail_on == question:
            from newsletter_archive.errors import ArchiveError

            raise ArchiveError("judge down")
        return {
            "claims": [{"claim": "c", "post_id": post_ids[0] if post_ids else "", "supported": True}],
            "overall": 3,
        }


def test_grading_reuses_grades_and_records_failures(tmp_path):
    from newsletter_archive.search.answers import grade_answers

    r1, r2 = record("q1"), record("q2", "k2", "other text")
    r2.question = "second?"
    run = AnswerRun("baseline", {"q1": r1, "q2": r2})
    judge = FakeJudge(fail_on="second?")
    grade_answers(run, judge, lambda qid: {"main/a"}, tmp_path)
    assert run.records["q1"].grade["overall"] == 3 and run.records["q2"].error == "grade: ArchiveError"
    assert len(judge.calls) == 2
    again = FakeJudge()
    grade_answers(run, again, lambda qid: {"main/a"}, tmp_path)
    assert again.calls == ["second?"]  # q1's grade is current; only the failed one is tried
    other = AnswerRun("posts", {"q1": record("q1")})
    third = FakeJudge()
    grade_answers(other, third, lambda qid: set(), tmp_path)
    assert third.calls == [] and other.records["q1"].grade["overall"] == 3  # reused from baseline
    assert other.records["q1"].grade["post_coverage"] is None  # coverage recomputed for this run


def test_gemini_answer_judge_asks_for_json_and_retries_once():
    from newsletter_archive.search.answers import AnswerError, GeminiAnswerJudge

    from .fakes import FakeSDK, generate_response
    from .test_gemini import make_client

    good = '{"claims": [{"claim": "c", "post_id": "main/a", "supported": true}], "overall": 3}'
    sdk = FakeSDK(answers=[generate_response("garbage"), generate_response(good)])
    j = GeminiAnswerJudge(make_client(sdk)[0], "judge-model").judge("Q?", "answer", "[r1] ...", ["main/a"])
    assert j["overall"] == 3 and len(sdk.calls) == 2
    first, second = sdk.calls
    assert first["config"].response_mime_type == "application/json" and first["config"].temperature == 0.0
    assert "unusable" in second["config"].system_instruction and second["config"].temperature > 0
    bad = FakeSDK(answers=[generate_response("garbage"), generate_response("still garbage")])
    with pytest.raises(AnswerError, match="no usable judgment after 2 attempts"):
        GeminiAnswerJudge(make_client(bad)[0], "judge-model").judge("Q?", "a", "", ["main/a"])


def test_estimate_is_an_upper_bound_with_cost():
    from newsletter_archive.search.answers import estimate

    qs = [Question(f"q{i}", "local", "a question?", ()) for i in range(36)]
    est = estimate(qs, 10, 300, 0.75)
    assert est.questions == 36 and 36 * 2 * (2 * 10 * 300) < est.tokens < 36 * 40_000
    assert est.usd == pytest.approx(est.tokens / 1_000_000 * 0.75)
    assert "36 answers and 36 grades" in est.line() and f"~${est.usd:.2f}" in est.line()


def test_report_and_compare():
    from newsletter_archive.search.answers import compare_lines, report_lines

    qs = [Question("a", "local", "?", ()), Question("b", "global", "?", ()), Question("c", "intent", "?", ())]
    g = {"faithfulness": 1.0, "citation_precision": 1.0, "post_coverage": 0.5, "overall": 3}
    ra, rb = record("a"), record("b")
    ra.grade, rb.grade = dict(g), {**g, "post_coverage": None, "overall": 1}
    failed = record("c", "k3", None)
    failed.error = "answer: GeminiUnavailableError"
    run = AnswerRun("baseline", {"a": ra, "b": rb, "c": failed})
    text = "\n".join(report_lines(run, qs, "flash"))
    assert "label baseline, 3 questions (2 answered, 2 graded), flash" in text
    assert "all" in text and "global" in text and "failed: c (answer: GeminiUnavailableError)" in text
    assert "n/a" in text  # intent has no graded answer
    assert "not run: z" in "\n".join(report_lines(run, [*qs, Question("z", "local", "?", ())], "flash"))
    better = AnswerRun("posts", {"a": record("a"), "b": record("b")})
    better.records["a"].grade = {**g, "overall": 3}
    better.records["b"].grade = {**g, "overall": 3}
    lines = "\n".join(compare_lines(run, better, qs))
    assert "baseline" in lines and "posts" in lines and "+1.00" in lines  # overall 2.0 -> 3.0
