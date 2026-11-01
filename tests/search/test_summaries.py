import pytest

from newsletter_archive.search import summaries
from newsletter_archive.search.gemini import CircuitOpenError
from newsletter_archive.search.models import Section
from newsletter_archive.search.summaries import (
    PROMPT,
    GeminiSummarizer,
    SummaryError,
    SummaryResponseError,
    check_response,
    check_summary,
    post_text,
    summary_key,
)

from .fakes import GOOD_SUMMARY, FakeSDK, generate_response
from .test_gemini import make_client

SECTIONS = [Section(None, "Intro text."), Section("Why", "Body text.")]


def test_post_text_and_key():
    assert post_text("T", SECTIONS) == "Title: T\n\nIntro text.\n\n## Why\n\nBody text."
    key = summary_key("m", "T", SECTIONS)
    assert key == summary_key("m", "T", list(SECTIONS))
    assert len({key, summary_key("m2", "T", SECTIONS), summary_key("m", "T2", SECTIONS),
                summary_key("m", "T", SECTIONS[:1])}) == 4  # fmt: skip


def test_prompt_version_changes_the_key(monkeypatch):
    before = summary_key("m", "T", SECTIONS)
    monkeypatch.setattr(summaries, "SUMMARY_PROMPT_VERSION", 99)
    assert summary_key("m", "T", SECTIONS) != before


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("   ", "empty"),
        ("I can't help with that request " * 5, "declined"),
        ("Too short.", "1 words|2 words"),
        ("word " * 250, "250 words"),
    ],
)
def test_unusable_summaries(text, message):
    with pytest.raises(SummaryResponseError, match=message):
        check_summary(text)


def test_good_summary_is_squashed():
    assert check_summary("  " + GOOD_SUMMARY.replace(" ", "\n ", 3)) == GOOD_SUMMARY
    with pytest.raises(SummaryResponseError, match="cut off"):
        check_response(generate_response(GOOD_SUMMARY, finish="MAX_TOKENS"))
    assert check_response(generate_response(GOOD_SUMMARY, finish="")) == GOOD_SUMMARY


def test_request_settings_and_usage():
    sdk = FakeSDK()
    client, _, _ = make_client(sdk)
    result = GeminiSummarizer(client, "gemini-3.8-flash").summarize("T", SECTIONS)
    call = sdk.calls[0]
    assert call["model"] == "gemini-3.8-flash" and call["contents"].startswith("Title: T")
    cfg = call["config"]
    assert cfg.system_instruction == PROMPT and cfg.temperature == 0.0
    assert cfg.thinking_config.thinking_level.lower() == "low"
    assert result.text == GOOD_SUMMARY and (result.input_tokens, result.output_tokens) == (100, 55)
    GeminiSummarizer(client, "older-model", thinking_level="").summarize("T", SECTIONS)
    assert sdk.calls[1]["config"].thinking_config is None


def test_one_retry_on_an_unusable_answer():
    sdk = FakeSDK(answers=[generate_response("Too short.")])
    client, _, _ = make_client(sdk)
    assert GeminiSummarizer(client, "m").summarize("T", SECTIONS).text == GOOD_SUMMARY
    assert len(sdk.calls) == 2


def test_gives_up_after_two_unusable_answers():
    sdk = FakeSDK(answers=[generate_response("Too short."), generate_response(GOOD_SUMMARY, "MAX_TOKENS")])
    client, _, _ = make_client(sdk)
    with pytest.raises(SummaryError, match="after 2 attempts: the answer was cut off"):
        GeminiSummarizer(client, "m").summarize("T", SECTIONS)


def test_repeated_bad_answers_open_the_breaker():
    sdk = FakeSDK(answers=[generate_response("Too short.")] * 4)
    client, _, _ = make_client(sdk, breaker_failures=3)
    summarizer = GeminiSummarizer(client, "m")
    with pytest.raises(SummaryError):
        summarizer.summarize("T", SECTIONS)
    with pytest.raises(CircuitOpenError):
        summarizer.summarize("T2", SECTIONS)
    assert len(sdk.calls) == 3
