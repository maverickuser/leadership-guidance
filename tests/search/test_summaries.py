import pytest

from newsletter_archive.search import summaries
from newsletter_archive.search.gemini import CircuitOpenError
from newsletter_archive.search.models import Section
from newsletter_archive.search.summaries import (
    PROMPT,
    RETRY_TEMPERATURE,
    GeminiSummarizer,
    SummaryContentError,
    SummaryError,
    SummaryResponseError,
    check_response,
    check_summary,
    min_words_for,
    post_text,
    summary_key,
)

from .fakes import GOOD_SUMMARY, FakeSDK, generate_response
from .test_gemini import make_client

SECTIONS = [Section(None, "Intro text."), Section("Why", "Body text.")]
LONG_SECTIONS = [Section(None, "word " * 400)]


def test_post_text_and_key():
    assert post_text("T", SECTIONS) == "Title: T\n\nIntro text.\n\n## Why\n\nBody text."
    key = summary_key("m", "low", "T", SECTIONS)
    assert key == summary_key("m", "low", "T", list(SECTIONS))
    variants = {
        key,
        summary_key("m2", "low", "T", SECTIONS),
        summary_key("m", "high", "T", SECTIONS),
        summary_key("m", "low", "T2", SECTIONS),
        summary_key("m", "low", "T", SECTIONS[:1]),
    }
    assert len(variants) == 5


def test_prompt_version_changes_the_key(monkeypatch):
    before = summary_key("m", "low", "T", SECTIONS)
    monkeypatch.setattr(summaries, "SUMMARY_PROMPT_VERSION", 99)
    assert summary_key("m", "low", "T", SECTIONS) != before


def test_short_posts_get_a_lower_minimum():
    assert min_words_for(2000) == 60 and min_words_for(80) == 40 and min_words_for(10) == 15


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("I can't help with that request " * 5, "declined"),
        ("Too short.", "2 words"),
        ("word " * 250, "250 words"),
    ],
)
def test_unusable_content(text, message):
    with pytest.raises(SummaryContentError, match=message):
        check_summary(text)


def test_broken_responses():
    for response, message in (
        (generate_response(GOOD_SUMMARY, finish="MAX_TOKENS"), "stopped early \\(MAX_TOKENS\\)"),
        (generate_response(GOOD_SUMMARY, finish="SAFETY"), "SAFETY"),
        (generate_response("   "), "empty"),
    ):
        with pytest.raises(SummaryResponseError, match=message):
            check_response(response)
    assert check_response(generate_response(GOOD_SUMMARY, finish="")) == GOOD_SUMMARY
    assert check_summary("  " + GOOD_SUMMARY.replace(" ", "\n ", 3)) == GOOD_SUMMARY


def test_request_settings_and_usage():
    sdk = FakeSDK()
    client, _, _ = make_client(sdk)
    result = GeminiSummarizer(client, "gemini-3.8-flash").summarize("T", LONG_SECTIONS)
    call = sdk.calls[0]
    assert call["model"] == "gemini-3.8-flash" and call["contents"].startswith("Title: T")
    cfg = call["config"]
    assert cfg.system_instruction == PROMPT and cfg.temperature == 0.0
    assert cfg.thinking_config.thinking_level.lower() == "low"
    assert result.text == GOOD_SUMMARY and (result.input_tokens, result.output_tokens) == (100, 55)
    GeminiSummarizer(client, "older-model", thinking_level="").summarize("T", LONG_SECTIONS)
    assert sdk.calls[1]["config"].thinking_config is None


def test_retry_changes_the_request():
    sdk = FakeSDK(answers=[generate_response("Too short.")])
    client, _, _ = make_client(sdk)
    assert GeminiSummarizer(client, "m").summarize("T", LONG_SECTIONS).text == GOOD_SUMMARY
    retry = sdk.calls[1]["config"]
    assert retry.temperature == RETRY_TEMPERATURE
    assert "previous answer was unusable (summary has 2 words" in retry.system_instruction


def test_short_post_summary_is_accepted():
    answer = " ".join(["Short post summary has six words."] * 6)  # 36 words
    sdk = FakeSDK(answers=[generate_response(answer)])
    client, _, _ = make_client(sdk)
    short = [Section(None, "word " * 60)]  # a 60-word post: minimum 30 words
    assert GeminiSummarizer(client, "m").summarize("T", short).text == answer
    assert len(sdk.calls) == 1


def test_gives_up_after_two_unusable_answers():
    sdk = FakeSDK(answers=[generate_response("Too short."), generate_response(GOOD_SUMMARY, "MAX_TOKENS")])
    client, _, _ = make_client(sdk)
    with pytest.raises(SummaryError, match="after 2 attempts: the answer stopped early"):
        GeminiSummarizer(client, "m").summarize("T", LONG_SECTIONS)


def test_content_problems_never_open_the_breaker():
    sdk = FakeSDK(answers=[generate_response("Too short.")] * 6)
    client, _, _ = make_client(sdk, breaker_failures=3)
    summarizer = GeminiSummarizer(client, "m")
    for title in ("A", "B", "C"):
        with pytest.raises(SummaryError):
            summarizer.summarize(title, LONG_SECTIONS)
    assert not client.circuit_open


def test_broken_responses_open_the_breaker():
    sdk = FakeSDK(answers=[generate_response(GOOD_SUMMARY, "MAX_TOKENS")] * 4)
    client, _, _ = make_client(sdk, breaker_failures=3)
    summarizer = GeminiSummarizer(client, "m")
    with pytest.raises(SummaryError):
        summarizer.summarize("A", LONG_SECTIONS)
    with pytest.raises(CircuitOpenError):
        summarizer.summarize("B", LONG_SECTIONS)
    assert len(sdk.calls) == 3
