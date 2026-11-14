"""Answer evaluation against a real indexed archive, with a fake answer model."""

from __future__ import annotations

import logging

import pytest

from newsletter_archive.errors import ArchiveError
from newsletter_archive.search.answers import answer_prompt, answer_questions, load_run, retrieve
from newsletter_archive.search.evaluate import Question

from .archive import small_config
from .test_mcp_server import index, make_service, records

pytestmark = pytest.mark.db

QUESTIONS = [
    Question("bar-raiser", "local", "what does a bar raiser do in a hiring loop?", ("main/hiring",)),
    Question("promotion", "intent", "how do I ask for a promotion?", ("main/career",)),
]


class FakeAnswerer:
    model = "fake-answer-model"
    thinking_level = "low"

    def __init__(self, fail_on=None):
        self.prompts = []
        self.fail_on = fail_on

    def answer(self, prompt):
        self.prompts.append(prompt)
        if self.fail_on and self.fail_on in prompt:
            raise ArchiveError("model down")
        return "See [Post hiring](https://www.example.com/hiring/) for the bar raiser."


@pytest.fixture
def cfg(db_config):
    c = small_config(db_config)
    index(c, records())
    return c


# spec: answer-evaluation / Generate answers for the eval questions (Answer one question)
def test_each_question_gets_one_answer_saved_with_its_searches(cfg):
    fake = FakeAnswerer()
    run = answer_questions(make_service(cfg), QUESTIONS, fake, cfg.output_dir, "baseline", 5)
    assert set(run.records) == {"bar-raiser", "promotion"} and len(fake.prompts) == 2
    r = run.records["bar-raiser"]
    assert r.answer and r.error is None and r.model == "fake-answer-model"
    assert r.result_ids and set(r.result_posts.values()) <= {"main/delegation", "main/hiring", "main/career"}
    assert r.cited.posts == ("main/hiring",)
    assert load_run(cfg.output_dir, "baseline").records["bar-raiser"].answer == r.answer


# spec: answer-evaluation / Generate answers (Searches use the live settings)
def test_the_plan_is_a_summaries_search_then_a_passages_search(cfg):
    got = retrieve(make_service(cfg), "bar raiser", 5)
    assert [{r.kind for r in resp.results} for resp in got.responses] == [{"summary"}, {"passage"}]
    prompt = answer_prompt("what does a bar raiser do?", got)
    assert prompt.startswith("Question: what does a bar raiser do?")  # the MCP `ask` text itself
    assert "Search results:" in prompt and "https://www.example.com/hiring/" in prompt


# spec: answer-evaluation / Saved answers and grades (Re-run without changes)
def test_a_second_run_reuses_every_answer_even_under_another_label(cfg):
    answer_questions(make_service(cfg), QUESTIONS, FakeAnswerer(), cfg.output_dir, "baseline", 5)
    again = FakeAnswerer()
    seen = []
    answer_questions(
        make_service(cfg),
        QUESTIONS,
        again,
        cfg.output_dir,
        "posts-test",
        5,
        lambda r, reused: seen.append(reused),
    )
    assert again.prompts == [] and seen == [True, True]
    assert load_run(cfg.output_dir, "posts-test").records["promotion"].question_id == "promotion"


# spec: answer-evaluation / Answer evaluation errors (One answer fails)
def test_one_failed_answer_is_recorded_and_the_run_goes_on(cfg, caplog):
    with caplog.at_level(logging.INFO):
        run = answer_questions(
            make_service(cfg),
            QUESTIONS,
            FakeAnswerer(fail_on="Question: how do I ask for a promotion"),
            cfg.output_dir,
            "baseline",
            5,
        )
    assert (
        run.records["promotion"].answer is None and run.records["promotion"].error == "answer: ArchiveError"
    )
    assert run.records["bar-raiser"].answer
    assert "bar raiser" not in caplog.text and "Post hiring" not in caplog.text  # no question or answer text


def test_a_failed_search_is_recorded_too(cfg):
    svc = make_service(cfg, embedder=None)

    def broken(*a, **k):
        raise ArchiveError("database down")

    svc.search = broken
    run = answer_questions(svc, QUESTIONS[:1], FakeAnswerer(), cfg.output_dir, "baseline", 5)
    assert (
        run.records["bar-raiser"].error == "search: ArchiveError" and run.records["bar-raiser"].answer is None
    )


def test_a_reused_answer_does_not_carry_the_other_runs_grade(cfg):
    from newsletter_archive.search.answers import load_run, save_run

    answer_questions(make_service(cfg), QUESTIONS, FakeAnswerer(), cfg.output_dir, "baseline", 5)
    base = load_run(cfg.output_dir, "baseline")
    for r in base.records.values():
        r.grade = {"overall": 3, "grade_key": "old"}
    save_run(cfg.output_dir, base)
    again = answer_questions(make_service(cfg), QUESTIONS, FakeAnswerer(), cfg.output_dir, "posts-test", 5)
    assert all(r.answer and r.grade is None for r in again.records.values())


def test_an_unusable_question_or_unexpected_error_is_recorded_not_raised(cfg):
    long = Question("too-long", "local", "x " * 2500, ())  # over the ask prompt's 4,000 characters
    run = answer_questions(
        make_service(cfg), [long, QUESTIONS[0]], FakeAnswerer(), cfg.output_dir, "baseline", 5
    )
    assert run.records["too-long"].answer is None and run.records["too-long"].error  # recorded, not raised
    assert run.records["bar-raiser"].answer

    svc = make_service(cfg)

    def broken(*a, **k):
        raise RuntimeError("unexpected")

    svc.search = broken
    run = answer_questions(svc, QUESTIONS[:1], FakeAnswerer(), cfg.output_dir, "other", 5)
    assert run.records["bar-raiser"].error == "search: RuntimeError"


# -- the whole run (PR 3) -------------------------------------------------------------------------


def keyed(cfg):
    from dataclasses import replace

    from newsletter_archive.config import Secret

    return replace(cfg, gemini_api_key=Secret("AIza-test-key-not-real"))


def run_with_fakes(cfg, label="baseline", **kw):
    from newsletter_archive.search.answers import run_answers

    from .test_answers import FakeJudge

    lines = []
    calls = {"service": 0}

    def service():
        calls["service"] += 1
        return make_service(cfg)

    code = run_answers(
        cfg,
        QUESTIONS,
        label,
        dry_run=kw.get("dry_run", False),
        yes=kw.get("yes", False),
        service_factory=service,
        answerer_factory=lambda: FakeAnswerer(),
        judge_factory=lambda: FakeJudge(),
        judge_grades={"bar-raiser": {"main/hiring": 3, "main/career": 2}},
        print_line=lines.append,
    )
    return code, "\n".join(lines), calls["service"]


def test_a_dry_run_needs_no_key(cfg):
    code, out, services = run_with_fakes(cfg, dry_run=True)
    assert code == 0 and "Estimate:" in out and services == 0


def test_a_reused_answer_gets_this_runs_results_and_no_old_error(cfg):
    from newsletter_archive.search.answers import load_run, save_run

    answer_questions(make_service(cfg), QUESTIONS, FakeAnswerer(), cfg.output_dir, "baseline", 5)
    base = load_run(cfg.output_dir, "baseline")
    for r in base.records.values():
        r.results_text, r.error = "", "grade: OldError"  # as saved by an older run
    save_run(cfg.output_dir, base)
    again = answer_questions(make_service(cfg), QUESTIONS, FakeAnswerer(), cfg.output_dir, "posts-test", 5)
    assert all(r.results_text and r.error is None for r in again.records.values())


# spec: answer-evaluation / Cost shown before spending (Dry run)
def test_dry_run_shows_the_cost_and_searches_nothing(cfg):
    code, out, services = run_with_fakes(keyed(cfg), dry_run=True)
    assert code == 0 and "Estimate: 2 answers and 2 grades" in out and "Dry run" in out
    assert services == 0 and not (cfg.output_dir / "eval-answers").exists()


# spec: answer-evaluation / Cost shown before spending (Over the ceiling)
def test_over_the_ceiling_needs_yes(cfg):
    from dataclasses import replace

    small = replace(
        keyed(cfg), search=replace(cfg.search, answers=replace(cfg.search.answers, max_tokens_per_run=10))
    )
    code, out, services = run_with_fakes(small)
    assert code == 1 and "re-run with --yes" in out and services == 0
    code, out, services = run_with_fakes(small, yes=True)
    assert code == 0 and services == 1


# spec: answer-evaluation / Answer evaluation errors (No key)
def test_no_key_stops_before_any_search(cfg):
    from dataclasses import replace

    from newsletter_archive.config import Secret

    code, out, services = run_with_fakes(replace(cfg, gemini_api_key=Secret(None)))
    assert code == 1 and "GEMINI_API_KEY is not set; add it to .env" in out and services == 0


# spec: answer-evaluation / Grade each answer; Saved answers and grades (Compare two runs)
def test_a_full_run_answers_grades_and_reports(cfg):
    from newsletter_archive.search.answers import compare_lines, load_run

    code, out, _ = run_with_fakes(keyed(cfg))
    assert code == 0 and "label baseline, 2 questions (2 answered, 2 graded)" in out
    run = load_run(cfg.output_dir, "baseline")
    g = run.records["bar-raiser"].grade
    assert g["overall"] == 3 and g["citation_precision"] == 1.0 and g["post_coverage"] == 0.5
    code, out, _ = run_with_fakes(keyed(cfg), label="posts-test")
    assert code == 0
    lines = "\n".join(compare_lines(run, load_run(cfg.output_dir, "posts-test"), QUESTIONS))
    assert "+0.00" in lines  # same search, same answers: no change


def test_no_index_yet_stops_with_what_to_do(db_config):
    from newsletter_archive.search import db

    with db.connect(db_config, "writer") as conn:
        db.apply_migrations(conn)
        conn.commit()
    code, out, services = run_with_fakes(keyed(small_config(db_config)))
    assert code == 1 and "run `archive index` first" in out and services == 0


def test_the_real_models_share_one_client(cfg):
    from newsletter_archive.search.answers import GeminiAnswerer, GeminiAnswerJudge, gemini_factories

    answerer_f, judge_f = gemini_factories(keyed(cfg))
    a, j = answerer_f(), judge_f()
    assert isinstance(a, GeminiAnswerer) and isinstance(j, GeminiAnswerJudge) and a.client is j.client
    assert (a.model, j.model) == (cfg.search.answers.model, cfg.search.answers.judge_model)
