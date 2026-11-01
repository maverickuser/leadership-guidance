"""Shared Gemini API client: pacing, retries and a circuit breaker for every call we make.

- Pacing: a token bucket per limit (requests and tokens per minute) waits before a call
  would exceed the key's quota.
- Retries: rate limits (429), server errors (5xx), timeouts and connection errors are retried
  after 2, 4, 8, 16 s (with jitter, or the server's Retry-After). Authentication and invalid
  requests are not retried.
- Circuit breaker: after ``breaker_failures`` calls in a row fail even after retries, no more
  calls are made in this run (CircuitOpenError), so an outage can't burn time or money.

The API key is held in a Secret and handed only to the SDK; it never appears in messages.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx
from google.genai import errors as genai_errors

from ..config import GEMINI_KEY_ENV, Secret
from ..errors import ArchiveError

log = logging.getLogger("newsletter_archive.search.gemini")

T = TypeVar("T")

RETRY_DELAYS_SECONDS: tuple[float, ...] = (2, 4, 8, 16)
MAX_RETRY_AFTER_SECONDS = 60.0
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
AUTH_STATUS = frozenset({401, 403})


class GeminiError(ArchiveError):
    """A Gemini call failed. Messages never include the API key."""


class MissingGeminiKeyError(GeminiError):
    def __init__(self) -> None:
        super().__init__(f"{GEMINI_KEY_ENV} is not set. Add your Gemini API key to .env (see .env.example).")


class GeminiAuthError(GeminiError):
    def __init__(self, status: int) -> None:
        super().__init__(f"The Gemini API rejected the key (HTTP {status}); check {GEMINI_KEY_ENV} in .env.")


class GeminiRequestError(GeminiError):
    """The request itself is invalid (HTTP 400/404…); retrying won't help."""


class GeminiUnavailableError(GeminiError):
    """Still failing after every retry."""


class CircuitOpenError(GeminiError):
    def __init__(self, failures: int) -> None:
        super().__init__(f"Gemini calls stopped for this run after {failures} failed calls in a row.")


class TokenBucket:
    """Allows ``rate`` units per ``period`` seconds, refilled continuously."""

    def __init__(
        self,
        rate: float,
        period: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.capacity = float(rate)
        self.refill_per_second = rate / period
        self.tokens = float(rate)
        self.clock = clock
        self.sleep = sleep
        self.updated = clock()

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.refill_per_second)
        self.updated = now

    def take(self, amount: float = 1.0) -> float:
        """Wait until ``amount`` units are available, then use them. Returns seconds waited.
        A request bigger than the whole bucket waits for a full bucket and goes through."""
        amount = min(float(amount), self.capacity)
        waited = 0.0
        self._refill()
        while self.tokens < amount:
            wait = (amount - self.tokens) / self.refill_per_second
            self.sleep(wait)
            waited += wait
            self._refill()
        self.tokens -= amount
        return waited


@dataclass
class CallStats:
    calls: int = 0
    retries: int = 0
    failures: int = 0
    tokens: int = 0
    waited_seconds: float = 0.0


def _status(error: BaseException) -> int | None:
    if isinstance(error, genai_errors.APIError):
        return int(error.code) if error.code is not None else None
    return None


def _retry_after(error: BaseException) -> float | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        value = float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return None
    return max(0.0, min(value, MAX_RETRY_AFTER_SECONDS))


def is_retryable(error: BaseException) -> bool:
    status = _status(error)
    if status is not None:
        return status in RETRYABLE_STATUS
    return isinstance(error, httpx.TimeoutException | httpx.TransportError | TimeoutError | ConnectionError)


def _describe(error: BaseException) -> str:
    status = _status(error)
    message = getattr(error, "message", None) or type(error).__name__
    return f"HTTP {status}: {message}" if status else str(message)


@dataclass
class GeminiClient:
    """One per run. ``sdk_factory`` builds the google-genai client (tests pass a fake)."""

    api_key: Secret
    rpm: int
    tpm: int
    breaker_failures: int = 3
    sdk_factory: Callable[[str], Any] | None = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    jitter: Callable[[], float] = random.random
    stats: CallStats = field(default_factory=CallStats)

    def __post_init__(self) -> None:
        if not self.api_key:
            raise MissingGeminiKeyError()
        self._requests = TokenBucket(self.rpm, clock=self.clock, sleep=self.sleep)
        self._tokens = TokenBucket(self.tpm, clock=self.clock, sleep=self.sleep)
        self._consecutive_failures = 0
        self._sdk: Any = None

    @property
    def sdk(self) -> Any:
        if self._sdk is None:
            factory = self.sdk_factory or _default_sdk
            self._sdk = factory(self.api_key.reveal())
        return self._sdk

    @property
    def circuit_open(self) -> bool:
        return self._consecutive_failures >= self.breaker_failures

    def call(self, fn: Callable[[Any], T], tokens: int, what: str = "call") -> T:
        """Run ``fn(sdk)`` with pacing, retries and the circuit breaker.

        Raises:
            CircuitOpenError: too many calls in a row have failed in this run.
            GeminiAuthError, GeminiRequestError: not retried.
            GeminiUnavailableError: still failing after every retry.
        """
        if self.circuit_open:
            raise CircuitOpenError(self._consecutive_failures)
        attempt = 0
        while True:
            self.stats.waited_seconds += self._requests.take(1) + self._tokens.take(tokens)
            self.stats.calls += 1
            try:
                result = fn(self.sdk)
            except Exception as e:
                status = _status(e)
                if status in AUTH_STATUS:
                    raise GeminiAuthError(status or 0) from None
                if not is_retryable(e):
                    self._fail()
                    raise GeminiRequestError(f"Gemini {what} failed: {_describe(e)}") from None
                if attempt >= len(RETRY_DELAYS_SECONDS):
                    self._fail()
                    raise GeminiUnavailableError(
                        f"Gemini {what} still failing after {attempt} retries: {_describe(e)}"
                    ) from None
                delay = _retry_after(e)
                if delay is None:
                    delay = RETRY_DELAYS_SECONDS[attempt] * (1 + 0.25 * self.jitter())
                log.info("Gemini %s failed (%s); retrying in %.1fs", what, _describe(e), delay)
                self.stats.retries += 1
                self.sleep(delay)
                attempt += 1
                continue
            self._consecutive_failures = 0
            self.stats.tokens += tokens
            return result

    def _fail(self) -> None:
        self.stats.failures += 1
        self._consecutive_failures += 1
        if self.circuit_open:
            log.warning(
                "Gemini circuit breaker open after %s failed calls in a row", self._consecutive_failures
            )


def _default_sdk(api_key: str) -> Any:  # pragma: no cover - the real SDK; smoke-tested with -m api
    from google import genai

    return genai.Client(api_key=api_key)
