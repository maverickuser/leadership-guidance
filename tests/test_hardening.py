"""Tests for the safety and robustness fixes from the best-practice review."""

import json
import socket

import pytest
import requests

from newsletter_archive import http as http_mod
from newsletter_archive.config import BACKOFF_SECONDS, load_config
from newsletter_archive.config import MAX_RETRIES as CONFIG_MAX_RETRIES
from newsletter_archive.discover import collect_sitemap_urls
from newsletter_archive.errors import ArchiveError, ConfigError, FetchError
from newsletter_archive.http import (
    Client,
    RawResponse,
    RequestsTransport,
    TransportConnectionError,
    TransportTimeout,
    to_base_host,
)
from newsletter_archive.models import ManifestEntry, is_valid_slug
from newsletter_archive.parsing import parse_xml
from newsletter_archive.store import load_manifest, run_lock, write_text_atomic

from .cli_helpers import CliEnv
from .conftest import TEST_COOKIE, make_config
from .fakes import FakeClock, FakeTransport
from .sitebuilder import sitemap_urlset

BASE = "https://www.example.com"


def index_of(*locs: str) -> str:
    items = "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
    return f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</sitemapindex>'


# -- run lock --------------------------------------------------------------


def test_second_run_is_refused_while_lock_is_held(tmp_path):
    lock = tmp_path / "data" / ".lock"
    with run_lock(lock), pytest.raises(ArchiveError, match="another archive run"), run_lock(lock):
        pass
    with run_lock(lock):  # released after the first run ends
        pass


def test_cli_exits_1_when_another_run_holds_the_lock(tmp_path, monkeypatch):
    env = CliEnv(tmp_path, monkeypatch)
    with run_lock(env.data / ".lock"):
        result = env.run("extract")
    assert result.exit_code == 1
    assert "another archive run" in result.output


# -- atomic writes ---------------------------------------------------------


def test_atomic_write_leaves_no_temp_files_and_normal_permissions(tmp_path):
    target = tmp_path / "a.json"
    write_text_atomic(target, "x")
    assert [p.name for p in tmp_path.iterdir()] == ["a.json"]
    assert target.stat().st_mode & 0o777 == 0o644


def test_atomic_write_removes_its_temp_file_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("os.replace", lambda s, d: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        write_text_atomic(tmp_path / "a.json", "x")
    assert list(tmp_path.iterdir()) == []


# -- manifest validation ---------------------------------------------------


@pytest.mark.parametrize(
    "data, message",
    [
        ([], "expected an object"),
        ({"posts": {}}, "expected an object"),
        ({"posts": ["x"]}, "must be an object"),
        ({"posts": [{"slug": "a"}]}, "no valid url"),
        ({"posts": [{"url": "file:///etc/passwd", "slug": "a"}]}, "no valid url"),
        ({"posts": [{"url": f"{BASE}/a"}]}, "unsafe slug"),
        ({"posts": [{"url": f"{BASE}/a", "slug": "../escape"}]}, "unsafe slug"),
        ({"posts": [{"url": f"{BASE}/a", "slug": "a/b"}]}, "unsafe slug"),
    ],
)
def test_malformed_manifest_is_a_clear_error(tmp_path, data, message):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ArchiveError, match=message):
        load_manifest(path)


@pytest.mark.parametrize(
    "slug, ok", [("a", True), ("post-1", True), ("", False), ("-a", False), ("..", False), ("A", False)]
)
def test_slug_rule(slug, ok):
    assert is_valid_slug(slug) is ok


def test_from_dict_keeps_good_entries():
    e = ManifestEntry.from_dict({"url": f"{BASE}/a", "slug": "a", "status": "ok"})
    assert (e.slug, e.status) == ("a", "ok")


# -- cookie validation -----------------------------------------------------


@pytest.mark.parametrize("bad", [TEST_COOKIE + "\nX-Evil: 1", TEST_COOKIE + "\rX", TEST_COOKIE + "é"])
def test_cookie_with_line_break_or_non_ascii_is_rejected(tmp_path, monkeypatch, bad):
    monkeypatch.setenv("NEWSLETTER_COOKIE", bad)
    with pytest.raises(ConfigError, match="line break or non-ASCII"):
        load_config(tmp_path / "missing.yaml", tmp_path / ".env", overrides={"base_url": BASE})


def test_retry_limit_matches_backoff_table():
    assert len(BACKOFF_SECONDS) == CONFIG_MAX_RETRIES


# -- config file errors ----------------------------------------------------


def test_unparseable_yaml_is_a_config_error(tmp_path):
    (tmp_path / "config.yaml").write_text("delay_seconds: [unclosed\n")
    with pytest.raises(ConfigError, match="could not parse"):
        load_config(tmp_path / "config.yaml", tmp_path / ".env")


def test_yaml_that_is_not_a_mapping_is_a_config_error(tmp_path):
    (tmp_path / "config.yaml").write_text("- just\n- a list\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(tmp_path / "config.yaml", tmp_path / ".env")


# -- sitemaps on other sites -----------------------------------------------


def test_child_sitemaps_on_other_sites_are_skipped(tmp_path, caplog):
    clock = FakeClock()
    t = (
        FakeTransport(clock=clock)
        .add(
            f"{BASE}/sitemap.xml",
            (200, index_of(f"{BASE}/sitemap-posts.xml", "https://evil.example/sitemap-posts.xml")),
        )
        .add(f"{BASE}/sitemap-posts.xml", (200, sitemap_urlset(BASE, ["a"])))
    )
    client = Client(make_config(tmp_path), transport=t, sleep=clock.sleep, clock=clock.clock)
    urls = collect_sitemap_urls(client, f"{BASE}/sitemap.xml", BASE)
    assert urls == [f"{BASE}/a/"]
    assert t.urls() == [f"{BASE}/sitemap.xml", f"{BASE}/sitemap-posts.xml"]
    assert "evil.example" in caplog.text


def test_non_www_child_sitemap_is_fetched_through_the_base_host(tmp_path):
    clock = FakeClock()
    t = (
        FakeTransport(clock=clock)
        .add(f"{BASE}/sitemap.xml", (200, index_of("https://example.com/sitemap-posts.xml")))
        .add(f"{BASE}/sitemap-posts.xml", (200, sitemap_urlset(BASE, ["a"])))
    )
    client = Client(make_config(tmp_path), transport=t, sleep=clock.sleep, clock=clock.clock)
    assert collect_sitemap_urls(client, f"{BASE}/sitemap.xml", BASE) == [f"{BASE}/a/"]
    assert t.calls[1].headers.get("Cookie") == TEST_COOKIE


def test_to_base_host():
    assert to_base_host("https://example.com/x/?a=1#f", BASE) == f"{BASE}/x/?a=1"


# -- XML parsing -----------------------------------------------------------


def test_parse_xml_does_not_expand_entities():
    xml = '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY e "EXPANDED">]><r>&e;</r>'
    root = parse_xml(xml)
    assert "EXPANDED" not in (root.text or "")


# -- real transport (no network: requests is stubbed) ----------------------


class _FakeResp:
    def __init__(self, chunks, headers=None, status=200):
        self._chunks, self.headers, self.status_code = chunks, headers or {}, status
        self.encoding = "iso-8859-1"

    def iter_content(self, chunk_size):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize(
    "exc, expected",
    [(requests.Timeout(), TransportTimeout), (requests.ConnectionError(), TransportConnectionError)],
)
def test_requests_transport_maps_errors(monkeypatch, exc, expected):
    t = RequestsTransport()
    monkeypatch.setattr(t.session, "get", lambda *a, **k: (_ for _ in ()).throw(exc))
    with pytest.raises(expected):
        t(f"{BASE}/a/", {}, 1)


def test_requests_transport_defaults_to_utf8_and_keeps_location(monkeypatch):
    t = RequestsTransport()
    resp = _FakeResp(["café".encode()], headers={"Content-Type": "text/html", "Location": "/b/"}, status=301)
    monkeypatch.setattr(t.session, "get", lambda *a, **k: resp)
    raw = t(f"{BASE}/a/", {}, 1)
    assert (raw.status, raw.text, raw.location) == (301, "café", "/b/")


def test_oversized_response_fails_without_retrying(tmp_path, monkeypatch):
    monkeypatch.setattr(http_mod, "MAX_RESPONSE_BYTES", 10)
    t = RequestsTransport()
    monkeypatch.setattr(t.session, "get", lambda *a, **k: _FakeResp([b"x" * 8, b"x" * 8]))
    clock = FakeClock()
    client = Client(make_config(tmp_path), transport=t, sleep=clock.sleep, clock=clock.clock)
    with pytest.raises(FetchError, match="too large"):
        client.get(f"{BASE}/a/")
    assert client.request_count == 1


def test_transport_stub_shape_matches_raw_response():
    assert RawResponse(status=200, url="u", text="t").location is None


# -- offline guard ---------------------------------------------------------


def test_dns_lookups_are_blocked_in_tests():
    with pytest.raises(RuntimeError, match="DNS lookup blocked"):
        socket.getaddrinfo("example.com", 443)
