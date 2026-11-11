"""The platform interface: Ghost behaves exactly as before, and each stage uses its platform."""

from __future__ import annotations

import json

import pytest

import newsletter_archive.platforms.ghost as ghost_platform
from newsletter_archive.discover import CategoryCrawl, post_urls_from_sitemap, run_discover
from newsletter_archive.errors import ConfigError, NotSignedInError
from newsletter_archive.extract import extract_post
from newsletter_archive.fetch import fetch_url, run_fetch
from newsletter_archive.http import Client
from newsletter_archive.models import Extracted, Manifest, ManifestEntry, Section
from newsletter_archive.platforms import PLATFORM_NAMES, get_platform
from newsletter_archive.store import load_manifest, run_extract, write_text_atomic

from .conftest import fixture_text
from .fakes import FakeClock, FakeTransport
from .sitebuilder import ghost_site, post_page, sitemap_urlset

BASE = "https://www.example.com"


@pytest.fixture
def clock():
    return FakeClock()


def client(cfg, t, clock):
    return Client(cfg, transport=t, sleep=clock.sleep, clock=clock.clock)


# -- registry -----------------------------------------------------------------


def test_ghost_is_registered_once():
    assert "ghost" in PLATFORM_NAMES
    assert get_platform("ghost").name == "ghost"
    assert get_platform("ghost") is get_platform("ghost")


def test_unknown_platform_lists_the_valid_ones():
    with pytest.raises(ConfigError, match="unknown platform 'wordpress'; use one of: ghost"):
        get_platform("wordpress")


# -- Ghost gives exactly today's results ----------------------------------------


def test_ghost_post_urls_are_todays():
    urls = [f"{BASE}/a/", f"{BASE}/tag/x/", f"{BASE}/a?utm_source=x", "https://other.example/b/"]
    assert get_platform("ghost").post_urls(urls, BASE) == post_urls_from_sitemap(urls, BASE) == [f"{BASE}/a"]


def test_ghost_request_url_is_todays():
    entry = ManifestEntry(url="https://example.com/foo", slug="foo")
    assert get_platform("ghost").request_url(entry.url, BASE) == fetch_url(entry, BASE) == f"{BASE}/foo/"


def test_ghost_signed_in_marker():
    ghost = get_platform("ghost")
    assert ghost.signed_in(post_page("a", signed_in=True))
    assert not ghost.signed_in(post_page("a", signed_in=False))


def test_ghost_extract_is_todays():
    html = fixture_text("post-signed-in.html")
    url = f"{BASE}/stop-trying-harder-change-your-environment-instead"
    assert get_platform("ghost").extract(html, url, (), 300) == extract_post(html, url, ())


def test_ghost_categories_come_from_the_category_crawl(monkeypatch):
    crawl = CategoryCrawl(post_categories={f"{BASE}/a": ["Career Growth"]}, names=["Career Growth"])
    monkeypatch.setattr(ghost_platform, "crawl_categories", lambda c, base: crawl)
    assert get_platform("ghost").discover_categories(object(), BASE) is crawl  # type: ignore[arg-type]


# -- each stage uses the platform it's given --------------------------------------


class FakePlatform:
    """Every post on the sitemap counts; no category crawl; categories and sign-in from the page."""

    name = "fake"

    def __init__(self, signed_in: bool = True, categories: list[str] | None = None, paywalled: bool = False):
        self._signed_in = signed_in
        self._categories = categories
        self._paywalled = paywalled

    def post_urls(self, sitemap_urls, base_url):
        return [u.rstrip("/") for u in sitemap_urls]

    def discover_categories(self, client, base_url):
        return None

    def request_url(self, url, base_url):
        return url + "?fake=1"

    def signed_in(self, html):
        return self._signed_in

    def extract(self, html, page_url, boilerplate_prefixes, min_word_count):
        text = "word " * 400
        return Extracted(
            title="T",
            subtitle=None,
            author="A",
            published_date="2026-01-02",
            summary="S",
            sections=[Section(heading=None, level=0, text=text.strip())],
            text=text.strip(),
            links=[],
            word_count=400,
            categories=self._categories,
            paywalled=self._paywalled,
        )


def test_discover_without_a_category_crawl(cfg, clock):
    t = FakeTransport(clock=clock).add(f"{BASE}/sitemap.xml", (200, sitemap_urlset(BASE, ["a", "b"])))
    m = Manifest()
    result = run_discover(client(cfg, t, clock), cfg, m, platform=FakePlatform())
    assert t.urls() == [f"{BASE}/sitemap.xml"]  # no /categories/ request
    assert result.categories == 0 and [e.categories for e in m.ordered()] == [[], []]


def test_discover_defaults_to_ghost(cfg, clock):
    t = FakeTransport(clock=clock)
    ghost_site(t, BASE, ["a", "b"])
    m = Manifest()
    run_discover(client(cfg, t, clock), cfg, m)
    assert f"{BASE}/categories/" in t.urls()
    assert m.entries["a"].categories == ["Career Growth"]


def test_fetch_uses_the_platform_url_and_sign_in_check(cfg, clock):
    m = Manifest(entries={"a": ManifestEntry(url=f"{BASE}/a", slug="a")})
    t = FakeTransport(clock=clock).add(f"{BASE}/a?fake=1", (200, "<html>no ghost marker</html>"))
    result = run_fetch(client(cfg, t, clock), cfg, m, platform=FakePlatform())
    assert result.fetched == 1 and t.urls() == [f"{BASE}/a?fake=1"]

    m2 = Manifest(entries={"b": ManifestEntry(url=f"{BASE}/b", slug="b")})
    t.add(f"{BASE}/b?fake=1", (200, "<html>page</html>"))
    with pytest.raises(NotSignedInError):
        run_fetch(client(cfg, t, clock), cfg, m2, platform=FakePlatform(signed_in=False))
    assert not cfg.only_source().raw_path("b").exists()


def test_dry_run_fetch_reports_the_platform_url(cfg):
    m = Manifest(entries={"a": ManifestEntry(url=f"{BASE}/a", slug="a")})
    calls = []
    run_fetch(None, cfg, m, dry_run=True, on_post=lambda *a: calls.append(a), platform=FakePlatform())  # type: ignore[arg-type]
    assert calls == [("a", "fetch", "would-fetch", f"{BASE}/a?fake=1")]


def seed(cfg, slug, categories):
    m = Manifest(entries={slug: ManifestEntry(url=f"{BASE}/{slug}", slug=slug, categories=categories)})
    write_text_atomic(cfg.only_source().raw_path(slug), "<html>saved page</html>")
    return m


def test_page_categories_replace_the_manifests(cfg):
    m = seed(cfg, "a", ["Old"])
    run_extract(cfg, m, platform=FakePlatform(categories=["The Pulse"]))
    assert json.loads(cfg.only_source().post_path("a").read_text(encoding="utf-8"))["categories"] == [
        "The Pulse"
    ]
    assert m.entries["a"].categories == ["The Pulse"]
    assert load_manifest(cfg.only_source().manifest_path).entries["a"].categories == ["The Pulse"]


def test_no_page_categories_keeps_the_manifests(cfg):
    m = seed(cfg, "a", ["Career Growth"])
    run_extract(cfg, m, platform=FakePlatform(categories=None))
    assert m.entries["a"].categories == ["Career Growth"]
    assert json.loads(cfg.only_source().post_path("a").read_text(encoding="utf-8"))["categories"] == [
        "Career Growth"
    ]


def test_rediscovery_keeps_categories_read_from_pages(cfg, clock):
    t = FakeTransport(clock=clock).add(f"{BASE}/sitemap.xml", (200, sitemap_urlset(BASE, ["a", "b"])))
    m = Manifest()
    run_discover(client(cfg, t, clock), cfg, m, platform=FakePlatform())
    m.entries["a"].categories = ["The Pulse"]  # as extraction sets it
    t.routes[f"{BASE}/sitemap.xml"] = [(200, sitemap_urlset(BASE, ["a", "b", "c"]))]  # the next sitemap
    run_discover(client(cfg, t, clock), cfg, m, platform=FakePlatform())
    assert [(e.slug, e.categories) for e in m.ordered()] == [("a", ["The Pulse"]), ("b", []), ("c", [])]
    assert load_manifest(cfg.only_source().manifest_path).entries["a"].categories == ["The Pulse"]


def test_status_change_alone_rewrites_the_post(cfg):
    m = seed(cfg, "a", [])
    run_extract(cfg, m, platform=FakePlatform())
    assert json.loads(cfg.only_source().post_path("a").read_text(encoding="utf-8"))["status"] == "ok"
    result = run_extract(cfg, m, platform=FakePlatform(paywalled=True))
    assert result.written == 1
    assert json.loads(cfg.only_source().post_path("a").read_text(encoding="utf-8"))["status"] == "partial"
    assert m.entries["a"].status == "partial"
