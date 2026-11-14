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
