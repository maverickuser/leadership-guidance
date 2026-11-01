import httpx
import pytest

from newsletter_archive.config import Secret
from newsletter_archive.search import gemini
from newsletter_archive.search.gemini import (
    CircuitOpenError,
    GeminiAuthError,
    GeminiClient,
    GeminiRequestError,
    GeminiUnavailableError,
    MissingGeminiKeyError,
    TokenBucket,
)

from ..conftest import TEST_GEMINI_KEY
from .fakes import FakeSDK, api_error


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(sdk, clock=None, **kw):
    clock = clock or Clock()
    seen = {}

    def factory(key, timeout):
        seen["key"], seen["timeout"] = key, timeout
        return sdk

    client = GeminiClient(
        Secret(TEST_GEMINI_KEY),
        rpm=kw.pop("rpm", 1000),
        tpm=kw.pop("tpm", 1_000_000),
        sdk_factory=factory,
        sleep=clock.sleep,
        clock=clock,
        jitter=lambda: 0.0,
        **kw,
    )
    return client, clock, seen


def embed_call(sdk):
    return sdk.models.embed_content(model="m", contents=[], config=None)


def test_token_bucket_waits_for_capacity():
    clock = Clock()
    bucket = TokenBucket(60, clock=clock, sleep=clock.sleep)  # 1 per second
    assert bucket.take(60) == 0
    assert bucket.take(1) == pytest.approx(1.0)
    assert bucket.take(120) == pytest.approx(60.0)  # bigger than the bucket: waits for a full one
    with pytest.raises(ValueError):
        TokenBucket(0)


def test_pacing_by_requests_and_tokens():
    sdk = FakeSDK()
    client, clock, _ = make_client(sdk, rpm=2, tpm=100)
    for _ in range(3):
        client.call(embed_call, tokens=10)
    assert clock.sleeps and client.stats.waited_seconds == pytest.approx(30.0)  # 3rd request waits
    client.call(embed_call, tokens=100)
    assert client.stats.tokens == 130


def test_sdk_built_lazily_with_the_key():
    sdk = FakeSDK()
    client, _, seen = make_client(sdk)
    assert seen == {}
    client.call(embed_call, 1)
    assert seen == {"key": TEST_GEMINI_KEY, "timeout": gemini.DEFAULT_TIMEOUT_SECONDS}


def test_missing_key():
    with pytest.raises(MissingGeminiKeyError, match="GEMINI_API_KEY is not set"):
        GeminiClient(Secret(None), rpm=1, tpm=1)


# spec: search-indexing / Embedding API use / Rate limited
def test_rate_limit_is_retried_with_backoff():
    sdk = FakeSDK([api_error(429), api_error(503)])
    client, clock, _ = make_client(sdk)
    client.call(embed_call, 1)
    assert clock.sleeps == [2, 4] and client.stats.retries == 2 and len(sdk.calls) == 3


def test_retry_after_is_honoured_and_capped():
    sdk = FakeSDK(
        [api_error(429, retry_after="7"), api_error(429, retry_after="9999"), api_error(429, "soon")]
    )
    client, clock, _ = make_client(sdk)
    client.call(embed_call, 1)
    assert clock.sleeps == [7.0, gemini.MAX_RETRY_AFTER_SECONDS, 8]


def test_network_errors_are_retried():
    sdk = FakeSDK([httpx.ConnectError("down"), httpx.ReadTimeout("slow"), TimeoutError()])
    client, clock, _ = make_client(sdk)
    client.call(embed_call, 1)
    assert len(clock.sleeps) == 3


@pytest.mark.parametrize("code", [401, 403])
def test_auth_errors_are_not_retried(code):
    sdk = FakeSDK([api_error(code)])
    client, clock, _ = make_client(sdk)
    with pytest.raises(GeminiAuthError, match=f"HTTP {code}") as exc:
        client.call(embed_call, 1)
    assert clock.sleeps == [] and TEST_GEMINI_KEY not in str(exc.value)


def test_invalid_requests_are_not_retried_and_dont_trip_the_breaker():
    sdk = FakeSDK([api_error(400)] * 5)
    client, clock, _ = make_client(sdk, breaker_failures=3)
    for _ in range(5):
        with pytest.raises(GeminiRequestError, match="HTTP 400: fake 400"):
            client.call(embed_call, 1, "embedding")
    assert clock.sleeps == [] and not client.circuit_open


def test_programming_errors_propagate_unchanged():
    client, _, _ = make_client(FakeSDK([ValueError("bug")]))
    with pytest.raises(ValueError, match="bug"):
        client.call(embed_call, 1)
    assert not client.circuit_open and client.stats.failures == 0


def test_unusable_responses_count_toward_the_breaker():
    client, _, _ = make_client(FakeSDK(), breaker_failures=2)

    def bad(_result):
        raise gemini.GeminiResponseError("bad shape")

    for _ in range(2):
        with pytest.raises(gemini.GeminiResponseError):
            client.call(embed_call, 1, check=bad)
    with pytest.raises(CircuitOpenError):
        client.call(embed_call, 1)


# spec: search-indexing / Embedding API use / Outage
def test_circuit_breaker_opens_after_three_failed_calls():
    sdk = FakeSDK([api_error(500)] * 15)
    client, _, _ = make_client(sdk, breaker_failures=3)
    for _ in range(3):
        with pytest.raises(GeminiUnavailableError, match="after 4 retries"):
            client.call(embed_call, 1)
    assert client.circuit_open
    calls_before = len(sdk.calls)
    with pytest.raises(CircuitOpenError, match="after 3 failed calls"):
        client.call(embed_call, 1)
    assert len(sdk.calls) == calls_before  # no more API calls


def test_success_resets_the_breaker():
    sdk = FakeSDK([api_error(500)] * 10)
    client, _, _ = make_client(sdk, breaker_failures=3)
    for _ in range(2):
        with pytest.raises(GeminiUnavailableError):
            client.call(embed_call, 1)
    client.call(embed_call, 1)
    assert not client.circuit_open and client.stats.failures == 2


def test_status_helpers():
    assert gemini._status(ValueError()) is None
    assert gemini._retry_after(ValueError()) is None
    err = api_error(500)
    err.code = None
    assert gemini._status(err) is None and not gemini.is_retryable(ValueError())
