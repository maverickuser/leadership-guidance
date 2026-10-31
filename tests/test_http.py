import logging

import pytest

from newsletter_archive.config import Secret
from newsletter_archive.errors import FetchError
from newsletter_archive.http import Client, RequestsTransport, TransportConnectionError, TransportTimeout

from .conftest import TEST_COOKIE, make_config
from .fakes import FakeClock, FakeTransport

BASE = "https://www.example.com"


def client(cfg, transport, clock):
    return Client(cfg, transport=transport, sleep=clock.sleep, clock=clock.clock)


@pytest.fixture
def clock():
    return FakeClock()


def test_returns_body_and_sends_headers(cfg, clock):
    t = FakeTransport(clock=clock).add(f"{BASE}/a/", (200, "hello"))
    resp = client(cfg, t, clock).get(f"{BASE}/a/")
    assert (resp.status, resp.text, resp.url) == (200, "hello", f"{BASE}/a/")
    headers = t.calls[0].headers
    assert headers["Cookie"] == TEST_COOKIE
    assert headers["User-Agent"] == "test-agent/1.0"


# spec: post-fetching / Transient server error
def test_retries_5xx_with_backoff(cfg, clock):
    t = FakeTransport(clock=clock).add(f"{BASE}/a/", 503, 503, (200, "ok"))
    assert client(cfg, t, clock).get(f"{BASE}/a/").text == "ok"
    assert clock.sleeps == [2, 4]
    assert len(t.calls) == 3


@pytest.mark.parametrize("exc", [TransportTimeout, TransportConnectionError])
def test_retries_timeouts_and_connection_errors(cfg, clock, exc):
    t = FakeTransport(clock=clock).add(f"{BASE}/a/", exc, (200, "ok"))
    assert client(cfg, t, clock).get(f"{BASE}/a/").status == 200
    assert clock.sleeps == [2]


# spec: post-fetching / Retries exhausted
def test_gives_up_after_max_retries(cfg, clock):
    t = FakeTransport(clock=clock).add(f"{BASE}/a/", TransportConnectionError)
    with pytest.raises(FetchError) as e:
        client(cfg, t, clock).get(f"{BASE}/a/")
    assert e.value.attempts == 4
    assert "connection error" in e.value.reason and "after 4 attempts" in e.value.reason
    assert clock.sleeps == [2, 4, 8]


# spec: post-fetching / Client error
def test_no_retry_on_4xx(cfg, clock):
    t = FakeTransport(clock=clock).add(f"{BASE}/a/", 404)
    with pytest.raises(FetchError) as e:
        client(cfg, t, clock).get(f"{BASE}/a/")
    assert e.value.status == 404 and e.value.reason == "HTTP 404"
    assert len(t.calls) == 1 and clock.sleeps == []


def test_max_retries_zero(tmp_path, clock):
    cfg = make_config(tmp_path, max_retries=0)
    t = FakeTransport(clock=clock).add(f"{BASE}/a/", 500)
    with pytest.raises(FetchError, match="after 1 attempts"):
        client(cfg, t, clock).get(f"{BASE}/a/")
    assert len(t.calls) == 1


# spec: post-fetching / Consecutive requests; archive-cli / Custom delay
@pytest.mark.parametrize("delay", [2.0, 5.0])
def test_requests_are_spaced_from_end_of_previous(tmp_path, clock, delay):
    cfg = make_config(tmp_path, delay_seconds=delay)
    t = FakeTransport(clock=clock, request_seconds=0.5).add(f"{BASE}/a/", 200).add(f"{BASE}/b/", 200)
    c = client(cfg, t, clock)
    c.get(f"{BASE}/a/")
    c.get(f"{BASE}/b/")
    first_end = t.calls[0].at + 0.5
    assert t.calls[1].at - first_end >= delay
    assert t.calls[1].at - first_end == pytest.approx(delay)


def test_first_request_does_not_wait(cfg, clock):
    t = FakeTransport(clock=clock).add(f"{BASE}/a/", 200)
    client(cfg, t, clock).get(f"{BASE}/a/")
    assert clock.sleeps == []


def test_spacing_also_applies_after_errors(cfg, clock):
    t = FakeTransport(clock=clock).add(f"{BASE}/a/", 404).add(f"{BASE}/b/", 200)
    c = client(cfg, t, clock)
    with pytest.raises(FetchError):
        c.get(f"{BASE}/a/")
    c.get(f"{BASE}/b/")
    assert clock.sleeps == [2]


# spec: post-fetching / Fetch canonical URLs without extra redirects
def test_same_host_redirect_followed_in_one_slot(cfg, clock):
    t = (
        FakeTransport(clock=clock)
        .add(f"{BASE}/t/career-growth", ("redirect", "/tag/career-growth/"))
        .add(f"{BASE}/tag/career-growth/", (200, "listing"))
    )
    c = client(cfg, t, clock)
    resp = c.get(f"{BASE}/t/career-growth")
    assert resp.url == f"{BASE}/tag/career-growth/" and resp.text == "listing"
    assert clock.sleeps == []  # both hops in the same slot
    assert c.request_count == 2


def test_redirect_to_other_site_refused_and_never_requested(cfg, clock):
    t = FakeTransport(clock=clock).add(f"{BASE}/a/", ("redirect", "https://evil.example/steal"))
    with pytest.raises(FetchError, match="another site"):
        client(cfg, t, clock).get(f"{BASE}/a/")
    assert t.urls() == [f"{BASE}/a/"]


@pytest.mark.parametrize("target", ["http://www.example.com/a/", "https://www.example.com:8443/a/"])
def test_redirect_to_other_scheme_or_port_refused(cfg, clock, target):
    t = FakeTransport(clock=clock).add(f"{BASE}/a/", ("redirect", target))
    with pytest.raises(FetchError):
        client(cfg, t, clock).get(f"{BASE}/a/")


def test_too_many_redirects(cfg, clock):
    t = FakeTransport(clock=clock).add(f"{BASE}/loop/", ("redirect", "/loop/"))
    with pytest.raises(FetchError, match="too many redirects"):
        client(cfg, t, clock).get(f"{BASE}/loop/")


def test_cookie_only_sent_to_base_origin(tmp_path, clock):
    cfg = make_config(tmp_path, base_url="http://127.0.0.1:8001")
    t = FakeTransport(clock=clock).add("http://127.0.0.1:8001/a/", 200).add("http://127.0.0.1:9999/a/", 200)
    c = client(cfg, t, clock)
    c.get("http://127.0.0.1:8001/a/")
    c.get("http://127.0.0.1:9999/a/")
    assert "Cookie" in t.calls[0].headers
    assert "Cookie" not in t.calls[1].headers


def test_no_cookie_header_when_cookie_empty(tmp_path, clock):
    cfg = make_config(tmp_path, cookie=Secret(""))
    t = FakeTransport(clock=clock).add(f"{BASE}/sitemap.xml", 200)
    client(cfg, t, clock).get(f"{BASE}/sitemap.xml")
    assert "Cookie" not in t.calls[0].headers
    assert "User-Agent" in t.calls[0].headers


def test_debug_log_never_contains_headers(cfg, clock, caplog):
    caplog.set_level(logging.DEBUG, logger="newsletter_archive")
    t = FakeTransport(clock=clock).add(f"{BASE}/a/", 503, (200, "ok"))
    client(cfg, t, clock).get(f"{BASE}/a/")
    assert "GET https://www.example.com/a/ -> 200" in caplog.text
    assert "TESTSECRET123" not in caplog.text and "Cookie" not in caplog.text


def test_requests_transport_refuses_to_store_cookies():
    # Set-Cookie from responses is refused (checked end to end in the integration tests)
    t = RequestsTransport()
    assert t.session.trust_env is False
    assert t.session.cookies.get_policy().allowed_domains() == ()
