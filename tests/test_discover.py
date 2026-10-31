import json

import pytest

from scarlet_archive.discover import (
    category_slugs,
    collect_sitemap_urls,
    crawl_categories,
    is_post_url,
    merge_manifest,
    normalize_url,
    post_urls_from_sitemap,
    run_discover,
    slug_from_url,
)
from scarlet_archive.errors import ArchiveError
from scarlet_archive.http import Client
from scarlet_archive.models import Manifest, ManifestEntry
from scarlet_archive.store import load_manifest, save_manifest

from .conftest import fixture_text
from .fakes import FakeClock, FakeTransport
from .sitebuilder import categories_page, sitemap_index, sitemap_urlset, tag_page

BASE = "https://www.scarletink.com"


@pytest.fixture
def clock():
    return FakeClock()


def make_client(cfg, t, clock):
    return Client(cfg, transport=t, sleep=clock.sleep, clock=clock.clock)


# -- sitemap -----------------------------------------------------------------

# spec: post-discovery / Ghost sitemap index
def test_ghost_index_reads_only_posts_sitemap(cfg, clock):
    t = (
        FakeTransport(clock=clock)
        .add(f"{BASE}/sitemap.xml", (200, fixture_text("sitemap-index.xml")))
        .add(f"{BASE}/sitemap-posts.xml", (200, fixture_text("sitemap-posts.xml")))
    )
    urls = collect_sitemap_urls(make_client(cfg, t, clock), f"{BASE}/sitemap.xml")
    assert t.urls() == [f"{BASE}/sitemap.xml", f"{BASE}/sitemap-posts.xml"]
    assert len(urls) == 6
    assert all(u.startswith(BASE) and u.endswith("/") for u in urls)
    assert not any("storage.ghost.io" in u for u in urls)  # image:loc ignored


# spec: post-discovery / Nested sitemap index
def test_nested_index_followed_without_loops(cfg, clock):
    t = FakeTransport(clock=clock)
    for name in ["sitemap-nested.xml", "sitemap-nested-child.xml", "sitemap-nested-leaf-a.xml", "sitemap-nested-leaf-b.xml"]:
        t.add(f"{BASE}/{name}", (200, fixture_text(name)))
    urls = collect_sitemap_urls(make_client(cfg, t, clock), f"{BASE}/sitemap-nested.xml")
    assert sorted(urls) == [f"{BASE}/post-a/", f"{BASE}/post-b/", f"{BASE}/post-c/"]
    assert t.urls().count(f"{BASE}/sitemap-nested.xml") == 1


# spec: post-discovery / Flat sitemap
def test_flat_urlset(cfg, clock):
    t = FakeTransport(clock=clock).add(f"{BASE}/sitemap.xml", (200, sitemap_urlset(BASE, ["a", "b"])))
    assert collect_sitemap_urls(make_client(cfg, t, clock), f"{BASE}/sitemap.xml") == [f"{BASE}/a/", f"{BASE}/b/"]


def test_bad_sitemap_xml_raises(cfg, clock):
    t = FakeTransport(clock=clock).add(f"{BASE}/sitemap.xml", (200, "<html>not xml"))
    with pytest.raises(ArchiveError, match="could not parse sitemap"):
        collect_sitemap_urls(make_client(cfg, t, clock), f"{BASE}/sitemap.xml")


# spec: post-discovery / Mixed URLs in the sitemap; Duplicate with tracking params
def test_filter_and_dedupe_mixed_sitemap(cfg, clock):
    t = FakeTransport(clock=clock).add(f"{BASE}/sitemap.xml", (200, fixture_text("sitemap-flat-mixed.xml")))
    urls = collect_sitemap_urls(make_client(cfg, t, clock), f"{BASE}/sitemap.xml")
    assert post_urls_from_sitemap(urls, BASE) == [f"{BASE}/why-promotions-stall", f"{BASE}/foo"]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://www.scarletink.com/foo/?utm_source=x#top", "https://www.scarletink.com/foo"),
        ("https://www.scarletink.com/foo", "https://www.scarletink.com/foo"),
        ("HTTPS://WWW.ScarletInk.com/foo/", "https://www.scarletink.com/foo"),
        ("https://www.scarletink.com/foo/?ref=nl&b=2&a=1", "https://www.scarletink.com/foo?a=1&b=2"),
        ("https://www.scarletink.com/foo/?UTM_Medium=e&fbclid=z", "https://www.scarletink.com/foo"),
        ("http://127.0.0.1:8001/foo/", "http://127.0.0.1:8001/foo"),
    ],
)
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    "url, ok",
    [
        ("https://www.scarletink.com/why-promotions-stall", True),
        ("https://scarletink.com/why-promotions-stall", True),
        ("https://www.scarletink.com/tag/career-growth", False),
        ("https://www.scarletink.com/author/david", False),
        ("https://www.scarletink.com/categories", False),
        ("https://www.scarletink.com/about", False),
        ("https://www.scarletink.com", False),
        ("https://www.scarletink.com/a/b", False),
        ("https://other.example/post", False),
    ],
)
def test_is_post_url(url, ok):
    assert is_post_url(url, BASE) is ok


# spec: post-discovery / Slug from URL
def test_slug_from_url():
    assert slug_from_url("https://www.scarletink.com/why-promotions-stall/") == "why-promotions-stall"
    assert slug_from_url("https://www.scarletink.com/Caf%C3%A9_Notes") == "caf-notes"
    with pytest.raises(ValueError):
        slug_from_url("https://www.scarletink.com/")


# -- categories --------------------------------------------------------------

# spec: post-discovery / Categories from the categories page
def test_category_slugs_from_categories_page():
    slugs = category_slugs(fixture_text("categories.html"), BASE)
    assert len(slugs) == 9
    assert "career-growth" in slugs and "newsletter" not in slugs
    assert slugs.count("career-growth") == 1  # relative duplicate link ignored


def add_category(t, slug, title, pages):
    """pages: list of slug lists; adds /t/ redirect and /tag/ pages with rel=next."""
    t.add(f"{BASE}/t/{slug}", ("redirect", f"/tag/{slug}/"))
    for i, posts in enumerate(pages, start=1):
        url = f"{BASE}/tag/{slug}/" if i == 1 else f"{BASE}/tag/{slug}/page/{i}/"
        nxt = f"{BASE}/tag/{slug}/page/{i + 1}/" if i < len(pages) else None
        t.add(url, (200, tag_page(title, posts, nxt)))


# spec: post-discovery / Post in two categories; Pagination ends; Post without category
def test_crawl_categories_with_pagination(cfg, clock):
    t = FakeTransport(clock=clock).add(f"{BASE}/categories/", (200, categories_page(BASE, ["career-growth", "managing-up"])))
    add_category(t, "career-growth", "Career Growth", [["p1", "p2"]])
    add_category(t, "managing-up", "Managing Up", [["p3"], ["p1"]])
    crawl = crawl_categories(make_client(cfg, t, clock), BASE)
    assert crawl.names == ["Career Growth", "Managing Up"]
    assert crawl.post_categories[f"{BASE}/p1"] == ["Career Growth", "Managing Up"]
    assert crawl.post_categories[f"{BASE}/p3"] == ["Managing Up"]
    assert f"{BASE}/p9" not in crawl.post_categories
    assert f"{BASE}/tag/managing-up/page/2/" in t.urls()


def test_real_tag_fixture_pages(cfg, clock):
    t = (
        FakeTransport(clock=clock)
        .add(f"{BASE}/categories/", (200, categories_page(BASE, ["career-growth"])))
        .add(f"{BASE}/t/career-growth", ("redirect", "/tag/career-growth/"))
        .add(f"{BASE}/tag/career-growth/", (200, fixture_text("tag-career-growth-p1.html")))
        .add(f"{BASE}/tag/career-growth/page/2/", (200, fixture_text("tag-career-growth-p2.html")))
    )
    crawl = crawl_categories(make_client(cfg, t, clock), BASE)
    assert crawl.names == ["Career Growth"]
    assert len(crawl.post_categories) == 14  # 12 on page 1, 2 on page 2


def test_pagination_stops_when_page_adds_nothing(cfg, clock):
    t = FakeTransport(clock=clock).add(f"{BASE}/categories/", (200, categories_page(BASE, ["c"])))
    add_category(t, "c", "C", [["p1"], ["p1"], ["p2"]])
    crawl_categories(make_client(cfg, t, clock), BASE)
    assert f"{BASE}/tag/c/page/3/" not in t.urls()


def test_failed_category_is_skipped(cfg, clock):
    t = FakeTransport(clock=clock).add(f"{BASE}/categories/", (200, categories_page(BASE, ["bad", "good"])))
    t.add(f"{BASE}/t/bad", 404)
    add_category(t, "good", "Good", [["p1"]])
    crawl = crawl_categories(make_client(cfg, t, clock), BASE)
    assert crawl.failed == ["bad"]
    assert crawl.post_categories == {f"{BASE}/p1": ["Good"]}


def test_category_name_falls_back_to_slug(cfg, clock):
    t = FakeTransport(clock=clock).add(f"{BASE}/categories/", (200, categories_page(BASE, ["x"])))
    t.add(f"{BASE}/t/x", (200, "<html><body><article class='gh-card'><a class='gh-card-link' href='/p1/'>p</a></article></body></html>"))
    assert crawl_categories(make_client(cfg, t, clock), BASE).names == ["x"]


# -- manifest ----------------------------------------------------------------

def test_merge_adds_new_and_keeps_progress():
    m = Manifest()
    m.entries["a"] = ManifestEntry(url=f"{BASE}/a", slug="a", categories=["Old"], status="ok", content_hash="h")
    m.entries["gone"] = ManifestEntry(url=f"{BASE}/gone", slug="gone", status="ok")
    stats = merge_manifest(m, [f"{BASE}/b", f"{BASE}/a"], {f"{BASE}/a": ["New"]})
    assert [e.slug for e in m.ordered()] == ["b", "a", "gone"]
    assert m.entries["a"].status == "ok" and m.entries["a"].content_hash == "h"
    assert m.entries["a"].categories == ["New"]
    assert m.entries["b"].status == "discovered" and m.entries["b"].categories == []
    assert (stats.added, stats.updated, stats.stale) == (1, 1, 1)


def test_merge_limit_caps_new_posts_only():
    m = Manifest()
    m.entries["a"] = ManifestEntry(url=f"{BASE}/a", slug="a")
    stats = merge_manifest(m, [f"{BASE}/a", f"{BASE}/b", f"{BASE}/c"], {}, limit=1)
    assert list(m.entries) == ["a", "b"] and stats.skipped == 1


def test_merge_slug_collision_gets_suffix():
    m = Manifest()
    merge_manifest(m, [f"{BASE}/x", f"{BASE}/x?a=1"], {})
    assert list(m.entries) == ["x", "x-2"]


def discover_site(t):
    t.add(f"{BASE}/sitemap.xml", (200, sitemap_index(BASE, ["sitemap-pages.xml", "sitemap-posts.xml"])))
    t.add(f"{BASE}/sitemap-posts.xml", (200, sitemap_urlset(BASE, ["p1", "p2", "p3"])))
    t.add(f"{BASE}/categories/", (200, categories_page(BASE, ["career-growth"])))
    add_category(t, "career-growth", "Career Growth", [["p1", "p3"]])
    return t


# spec: post-discovery / First discovery
def test_run_discover_writes_manifest(cfg, clock):
    t = discover_site(FakeTransport(clock=clock))
    result = run_discover(make_client(cfg, t, clock), cfg, Manifest())
    data = json.loads(cfg.manifest_path.read_text())
    assert [p["slug"] for p in data["posts"]] == ["p1", "p2", "p3"]
    assert data["posts"][0] == {
        "url": f"{BASE}/p1", "slug": "p1", "categories": ["Career Growth"], "status": "discovered",
        "last_attempt": None, "error": None, "content_hash": None,
    }
    assert result.total == 3 and result.categories == 1 and result.failed_categories == []
    assert f"{BASE}/sitemap-pages.xml" not in t.urls()


# spec: post-discovery / Re-discovery keeps progress
def test_rediscovery_keeps_status(cfg, clock):
    m = Manifest()
    m.entries["p1"] = ManifestEntry(url=f"{BASE}/p1", slug="p1", status="ok", content_hash="abc")
    save_manifest(m, cfg.manifest_path)
    t = discover_site(FakeTransport(clock=clock))
    run_discover(make_client(cfg, t, clock), cfg, load_manifest(cfg.manifest_path))
    again = load_manifest(cfg.manifest_path)
    assert again.entries["p1"].status == "ok" and again.entries["p1"].content_hash == "abc"
    assert again.entries["p1"].categories == ["Career Growth"]
    assert again.entries["p2"].status == "discovered"
    assert len(again) == 3


def test_discover_dry_run_writes_nothing(cfg, clock):
    t = discover_site(FakeTransport(clock=clock))
    m = Manifest()
    result = run_discover(make_client(cfg, t, clock), cfg, m, dry_run=True)
    assert result.stats.added == 3 and len(m) == 0
    assert not cfg.manifest_path.exists()


def test_discover_does_not_need_cookie(tmp_path, clock):
    from scarlet_archive.config import Secret

    from .conftest import make_config

    cfg = make_config(tmp_path, cookie=Secret(""))
    t = discover_site(FakeTransport(clock=clock))
    run_discover(make_client(cfg, t, clock), cfg, Manifest())
    assert all("Cookie" not in c.headers for c in t.calls)
