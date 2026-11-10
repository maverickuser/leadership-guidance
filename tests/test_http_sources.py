"""Several sites in one run: a shared rate limiter, and each site's cookie kept to that site."""

from __future__ import annotations

import itertools

import pytest

from newsletter_archive.config import Secret
from newsletter_archive.errors import FetchError
from newsletter_archive.http import Client, RateLimiter

from .conftest import TEST_COOKIE, make_config
from .fakes import FakeClock, FakeTransport

A = "https://www.example.com"
B = "https://newsletter.example.org"
B_COOKIE = "substack.sid=TESTSECRET123"


@pytest.fixture
def clock():
    return FakeClock()


def two_clients(cfg, t, clock, limiter):
    a = Client(cfg, transport=t, sleep=clock.sleep, clock=clock.clock, limiter=limiter)
    b = Client(
        cfg,
        transport=t,
        sleep=clock.sleep,
        clock=clock.clock,
        limiter=limiter,
        base_url=B,
        cookie=Secret(B_COOKIE),
    )
    return a, b


# spec: newsletter-sources / Choosing sources (All sources); archive-cli / Custom delay
@pytest.mark.parametrize("delay", [2.0, 5.0])
def test_shared_limiter_spaces_requests_across_sites(tmp_path, clock, delay):
    cfg = make_config(tmp_path, delay_seconds=delay)
    t = FakeTransport(clock=clock, request_seconds=0.5)
    for url in (f"{A}/1/", f"{B}/p/1", f"{A}/2/", f"{B}/p/2"):
        t.add(url, 200)
    limiter = RateLimiter(cfg.delay_seconds, clock.sleep, clock.clock)
    a, b = two_clients(cfg, t, clock, limiter)
    a.get(f"{A}/1/")
    b.get(f"{B}/p/1")
    a.get(f"{A}/2/")
    b.get(f"{B}/p/2")
    starts = [c.at for c in t.calls]
    gaps = [later - (earlier + 0.5) for earlier, later in itertools.pairwise(starts)]
    assert gaps == [pytest.approx(delay)] * 3


def test_retries_on_one_site_count_against_the_shared_limiter(cfg, clock):
    t = FakeTransport(clock=clock).add(f"{A}/1/", 503, 200).add(f"{B}/p/1", 200)
    limiter = RateLimiter(cfg.delay_seconds, clock.sleep, clock.clock)
    a, b = two_clients(cfg, t, clock, limiter)
    a.get(f"{A}/1/")  # a 503, then a retry after backoff
    b.get(f"{B}/p/1")
    assert t.calls[2].at - t.calls[1].at >= cfg.delay_seconds


def test_separate_limiters_dont_wait_for_each_other(cfg, clock):
    t = FakeTransport(clock=clock).add(f"{A}/1/", 200).add(f"{B}/p/1", 200)
    a, b = two_clients(cfg, t, clock, None)  # each client makes its own limiter
    a.get(f"{A}/1/")
    b.get(f"{B}/p/1")
    assert clock.sleeps == []


# spec: archive-cli / Secret handling (Cookie stays with its source)
def test_each_cookie_goes_only_to_its_own_site(cfg, clock):
    t = FakeTransport(clock=clock).add(f"{A}/1/", 200).add(f"{B}/p/1", 200)
    limiter = RateLimiter(cfg.delay_seconds, clock.sleep, clock.clock)
    a, b = two_clients(cfg, t, clock, limiter)
    a.get(f"{A}/1/")
    b.get(f"{B}/p/1")
    b.get(f"{A}/1/")  # b asking for a's site gets no cookie at all
    assert t.calls[0].headers["Cookie"] == TEST_COOKIE
    assert t.calls[1].headers["Cookie"] == B_COOKIE
    assert "Cookie" not in t.calls[2].headers


def test_redirect_to_the_other_site_is_refused(cfg, clock):
    t = FakeTransport(clock=clock).add(f"{B}/p/1", ("redirect", f"{A}/1/"))
    _, b = two_clients(cfg, t, clock, None)
    with pytest.raises(FetchError, match="redirect to another site refused"):
        b.get(f"{B}/p/1")
    assert t.urls() == [f"{B}/p/1"]


def test_a_client_without_a_cookie_sends_none(cfg, clock):
    t = FakeTransport(clock=clock).add(f"{B}/p/1", 200)
    c = Client(cfg, transport=t, sleep=clock.sleep, clock=clock.clock, base_url=B, cookie=Secret(None))
    c.get(f"{B}/p/1")
    assert "Cookie" not in t.calls[0].headers


def test_limiter_first_request_does_not_wait(clock):
    limiter = RateLimiter(2.0, clock.sleep, clock.clock)
    limiter.wait()
    limiter.done()
    clock.now += 0.5
    limiter.wait()
    assert clock.sleeps == [1.5]
