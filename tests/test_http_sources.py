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


def test_retries_on_one_site_count_against_the_shared_limiter(tmp_path, clock):
    # delay 5 is longer than the 2 s backoff, so only the shared limiter can explain b's wait
    cfg = make_config(tmp_path, delay_seconds=5.0)
    t = FakeTransport(clock=clock, request_seconds=0.5).add(f"{A}/1/", 503, 200).add(f"{B}/p/1", 200)
    limiter = RateLimiter(cfg.delay_seconds, clock.sleep, clock.clock)
    a, b = two_clients(cfg, t, clock, limiter)
    a.get(f"{A}/1/")  # a 503, then a retry
    b.get(f"{B}/p/1")
    retry_end = t.calls[1].at + 0.5
    assert t.calls[2].at - retry_end == pytest.approx(5.0)


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


def test_limiter_waits_only_for_the_rest_of_the_gap(clock):
    limiter = RateLimiter(2.0, clock.sleep, clock.clock)
    limiter.wait()
    limiter.done()
    clock.now += 0.5
    limiter.wait()
    assert clock.sleeps == [1.5]


def test_another_sites_client_never_gets_the_configs_cookie(cfg, clock):
    # cfg carries the main site's cookie; a client for site B built without one must send none
    t = FakeTransport(clock=clock).add(f"{B}/p/1", 200)
    c = Client(cfg, transport=t, sleep=clock.sleep, clock=clock.clock, base_url=B)
    c.get(f"{B}/p/1")
    assert "Cookie" not in t.calls[0].headers and not c.has_cookie


def test_fetch_and_discover_use_the_clients_site(cfg, clock):
    from newsletter_archive.discover import run_discover
    from newsletter_archive.fetch import run_fetch
    from newsletter_archive.models import Manifest

    from .sitebuilder import ghost_site

    t = FakeTransport(clock=clock)
    ghost_site(t, B, ["a"])
    c = Client(cfg, transport=t, sleep=clock.sleep, clock=clock.clock, base_url=B, cookie=Secret(B_COOKIE))
    m = Manifest()
    run_discover(c, cfg, m)
    run_fetch(c, cfg, m)
    assert t.urls()[0] == f"{B}/sitemap.xml" and f"{B}/a/" in t.urls()
    assert all(u.startswith(B) for u in t.urls())
    assert all(call.headers["Cookie"] == B_COOKIE for call in t.calls)
