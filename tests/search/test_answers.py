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
